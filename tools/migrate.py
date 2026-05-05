"""
PDF Migration Tool for adaptbase-plans.

Uploads PDFs from a local directory into Supabase Storage and inserts rows
into the shared `documents` table with `source='plans'`. Chunking and
embedding happen in a separate pipeline — this tool leaves `indexed_at` NULL
so the indexing step can pick up unprocessed documents later.

Idempotency: uses the SHA256 hash of the PDF as `source_id`. Re-running
skips files whose hash already exists under `source='plans'`.

Usage:
    uv run --env-file .env python tools/migrate.py [--reupload]
"""

import os
import sys
from io import BytesIO
from pathlib import Path

import pymupdf
from dotenv import load_dotenv
from PIL import Image
from tqdm import tqdm

sys.path.append(str(Path(__file__).parent))

from manifest import (
    build_location_metadata,
    extract_title,
    get_supabase_client,
    load_cities,
    load_countries,
    sha256_file,
)

load_dotenv()

LOCAL_PDF_DIR = Path(os.getenv("LOCAL_PDF_DIR", "./pdfs"))
PDF_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "plans")
THUMBNAIL_BUCKET = "thumbnails"
THUMBNAIL_WIDTH = 400
THUMBNAIL_QUALITY = 80
SOURCE = "plans"


def generate_thumbnail(pdf_path: Path) -> bytes:
    """Render the first page of a PDF as a JPEG thumbnail."""
    doc = pymupdf.open(pdf_path)
    if len(doc) == 0:
        doc.close()
        raise ValueError(f"PDF has no pages: {pdf_path.name}")
    try:
        page = doc[0]
        mat = pymupdf.Matrix(2, 2)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        aspect_ratio = img.height / img.width
        new_height = int(THUMBNAIL_WIDTH * aspect_ratio)
        img = img.resize((THUMBNAIL_WIDTH, new_height), Image.Resampling.LANCZOS)

        output = BytesIO()
        img.save(output, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
        return output.getvalue()
    finally:
        doc.close()


def create_ingest_run(supabase, run_type: str = "incremental") -> str:
    """Create an ingest_runs row and return its UUID."""
    result = (
        supabase.table("ingest_runs")
        .insert(
            {
                "source": SOURCE,
                "run_type": run_type,
                "status": "running",
            }
        )
        .execute()
    )
    return result.data[0]["id"]


def complete_ingest_run(supabase, run_id: str, *, uploaded: int, skipped: int, failed: int) -> None:
    """Mark ingest run complete with summary metadata."""
    supabase.table("ingest_runs").update(
        {
            "status": "completed" if failed == 0 else "failed",
            "completed_at": "now()",
            "metadata": {
                "uploaded": uploaded,
                "skipped": skipped,
                "failed": failed,
            },
        }
    ).eq("id", run_id).execute()


def upload_document(
    supabase,
    ingest_run_id: str,
    pdf_path: Path,
    relative_path: Path,
    reupload: bool,
    countries: dict[str, dict[str, str]],
    cities: dict[str, dict[str, str]],
) -> dict | None:
    """
    Upload a single PDF + thumbnail and insert a row into `documents`.

    Returns the inserted document row, or None if skipped (already present).
    """
    file_hash = sha256_file(pdf_path)

    if not reupload:
        existing = (
            supabase.table("documents")
            .select("id")
            .eq("source", SOURCE)
            .eq("sha256", file_hash)
            .execute()
        )
        if existing.data:
            return None

    title = extract_title(pdf_path)
    file_size = pdf_path.stat().st_size

    doc = pymupdf.open(pdf_path)
    try:
        page_count = len(doc)
    finally:
        doc.close()

    storage_path = f"{PDF_BUCKET}/{relative_path}"
    thumbnail_path = f"{THUMBNAIL_BUCKET}/{relative_path.with_suffix('.jpg')}"

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    supabase.storage.from_(PDF_BUCKET).upload(
        path=str(relative_path),
        file=pdf_bytes,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )

    thumbnail_bytes = generate_thumbnail(pdf_path)
    supabase.storage.from_(THUMBNAIL_BUCKET).upload(
        path=str(relative_path.with_suffix(".jpg")),
        file=thumbnail_bytes,
        file_options={"content-type": "image/jpeg", "upsert": "true"},
    )

    metadata = build_location_metadata(relative_path, countries, cities)

    doc_data = {
        "ingest_run_id": ingest_run_id,
        "source": SOURCE,
        "source_id": file_hash,
        "title": title,
        "content_type": "pdf",
        "sha256": file_hash,
        "page_count": page_count,
        "file_size": file_size,
        "storage_path": storage_path,
        "thumbnail_path": thumbnail_path,
        "metadata": metadata,
        # indexed_at intentionally NULL — filled in by the (separate) chunking pipeline.
    }

    # Upsert on (source, source_id) which is the natural key on documents.
    result = (
        supabase.table("documents")
        .upsert(doc_data, on_conflict="source,source_id")
        .execute()
    )
    return result.data[0] if result.data else None


def main(reupload: bool = False) -> None:
    print(f"🔍 Scanning {LOCAL_PDF_DIR} for PDFs...")

    if not LOCAL_PDF_DIR.exists():
        print(f"❌ Directory not found: {LOCAL_PDF_DIR}")
        print("   Create the directory or set LOCAL_PDF_DIR in .env")
        return

    pdf_files = list(LOCAL_PDF_DIR.rglob("*.pdf"))
    if not pdf_files:
        print(f"   No PDFs found in {LOCAL_PDF_DIR}")
        return
    print(f"   Found {len(pdf_files)} PDFs")

    print("🔗 Connecting to Supabase...")
    try:
        supabase = get_supabase_client()
    except ValueError as e:
        print(f"❌ {e}")
        return

    print("📚 Loading reference data...")
    countries = load_countries()
    cities = load_cities()
    print(f"   Loaded {len(countries)} countries, {len(cities)} cities")

    print("📝 Creating ingest run...")
    run_type = "full" if reupload else "incremental"
    ingest_run_id = create_ingest_run(supabase, run_type=run_type)
    print(f"   Run ID: {ingest_run_id}")

    print(f"\n📤 Uploading PDFs{' (force reupload)' if reupload else ''}...")
    uploaded = skipped = failed = 0

    for pdf_path in tqdm(pdf_files, desc="Processing", unit="file"):
        try:
            relative_path = pdf_path.relative_to(LOCAL_PDF_DIR)
            result = upload_document(
                supabase,
                ingest_run_id,
                pdf_path,
                relative_path,
                reupload,
                countries,
                cities,
            )
            if result:
                uploaded += 1
            else:
                skipped += 1
        except Exception as e:  # noqa: BLE001 - log and continue
            failed += 1
            tqdm.write(f"❌ Failed: {pdf_path.name} - {e}")

    complete_ingest_run(
        supabase, ingest_run_id, uploaded=uploaded, skipped=skipped, failed=failed
    )

    print("\n✅ Migration complete!")
    print(f"   Uploaded: {uploaded}")
    print(f"   Skipped:  {skipped} (already exist)")
    print(f"   Failed:   {failed}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Upload PDFs to Supabase Storage")
    parser.add_argument(
        "--reupload",
        action="store_true",
        help="Force reupload even if the SHA256 already exists for source='plans'",
    )
    args = parser.parse_args()
    main(reupload=args.reupload)
