#!/usr/bin/env python3
"""
Find and regenerate missing thumbnails for plans shown on the website.

The website displays documents WHERE doc_type='adaptation_plan'. A thumbnail
is "missing" when the documents row points to a thumbnail_path but the file
doesn't exist in the storage bucket (orphaned reference).

For each missing thumbnail:
  1. Download the PDF from the `plans` bucket
  2. Render the first page as a JPEG
  3. Upload it to the original thumbnail_path

Rendering goes through PyMuPDF first and falls back to poppler's pdftoppm. A
class of otherwise-fine PDFs (~30 in the heatplans corpus) carries a page tree
MuPDF refuses — it reports 0 pages and will not rasterize anything — while
poppler parses them without complaint and renders a perfectly good cover page.
Without the fallback those documents show a placeholder tile forever.

Usage:
    uv run --env-file .env python fix_missing_thumbnails.py [--dry-run]
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import httpx
import pymupdf
from dotenv import load_dotenv
from PIL import Image

from supabase import create_client

load_dotenv()

BUCKET = "plans"
THUMBNAIL_WIDTH = 400
THUMBNAIL_QUALITY = 80
HEAD_CONCURRENCY = 24
POPPLER_TIMEOUT = 120


def downscale(img: Image.Image) -> bytes:
    """Resize to THUMBNAIL_WIDTH, preserving aspect ratio, and encode as JPEG."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    new_height = int(THUMBNAIL_WIDTH * (img.height / img.width))
    img = img.resize((THUMBNAIL_WIDTH, new_height), Image.Resampling.LANCZOS)
    output = BytesIO()
    img.save(output, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
    return output.getvalue()


def render_pymupdf(pdf_bytes: bytes) -> bytes:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        if len(doc) == 0:
            raise ValueError("PDF has no pages")
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
        return downscale(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
    finally:
        doc.close()


def render_poppler(pdf_bytes: bytes) -> bytes:
    """Render page 1 via pdftoppm — the fallback for PDFs MuPDF won't parse."""
    if not shutil.which("pdftoppm"):
        raise RuntimeError("pdftoppm not installed (brew install poppler)")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdf"
        src.write_bytes(pdf_bytes)
        subprocess.run(
            [
                "pdftoppm",
                "-jpeg",
                "-r",
                "150",
                "-f",
                "1",
                "-l",
                "1",
                str(src),
                str(Path(tmp) / "page"),
            ],
            check=True,
            capture_output=True,
            timeout=POPPLER_TIMEOUT,
        )
        pages = sorted(Path(tmp).glob("page*.jpg"))
        if not pages:
            raise ValueError("pdftoppm produced no output")
        with Image.open(pages[0]) as img:
            return downscale(img)


def generate_thumbnail(pdf_bytes: bytes) -> bytes:
    """Render the first page of a PDF (in-memory) as a JPEG thumbnail."""
    try:
        return render_pymupdf(pdf_bytes)
    except Exception as mupdf_error:
        try:
            return render_poppler(pdf_bytes)
        except Exception as poppler_error:
            raise ValueError(
                f"pymupdf: {mupdf_error}; poppler: {poppler_error}"
            ) from poppler_error


def storage_key(path: str) -> str:
    """Strip leading 'plans/' bucket prefix if present."""
    return path[len("plans/") :] if path.startswith("plans/") else path


def thumbnail_path_for(storage_path: str) -> str:
    """Derive the thumbnail path a PDF's thumbnail belongs at.

    The live convention — set by adaptbase-core's migrate_storage_paths.py and
    matched by every row that already has one — mirrors the PDF's path under a
    `thumbnails/` segment inside the *plans* bucket, with a .jpg suffix:

        plans/USA/Q1297/chicago-2025.pdf
          ->  plans/thumbnails/USA/Q1297/chicago-2025.jpg
    """
    rel = Path(storage_key(storage_path)).with_suffix(".jpg")
    return f"{BUCKET}/thumbnails/{rel}"


def public_url(supabase_url: str, path: str) -> str:
    return f"{supabase_url}/storage/v1/object/public/{BUCKET}/{storage_key(path)}"


def find_missing(supabase, supabase_url: str) -> list[dict]:
    """Return documents whose thumbnail is absent — unset, or a dead pointer.

    The bucket is public, so existence is one HEAD per thumbnail rather than a
    `storage.list()` round-trip per document; at this concurrency the whole
    corpus checks in seconds instead of minutes.
    """
    print("🔍 Fetching all adaptation plans...")
    docs = []
    page_size = 1000
    offset = 0
    while True:
        page = (
            supabase.table("documents")
            .select("id,title,storage_path,thumbnail_path")
            .eq("doc_type", "adaptation_plan")
            .not_.is_("storage_path", "null")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        docs.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    print(f"   Got {len(docs)} plans with storage_path")

    unset = [d for d in docs if not d.get("thumbnail_path")]
    pointed = [d for d in docs if d.get("thumbnail_path")]
    print(f"   {len(unset)} with no thumbnail_path; checking {len(pointed)} files...")

    orphaned: list[dict] = []
    with httpx.Client(timeout=30) as client:

        def check(doc: dict) -> dict | None:
            url = public_url(supabase_url, doc["thumbnail_path"])
            try:
                return None if client.head(url).status_code == 200 else doc
            except httpx.HTTPError:
                return doc

        with ThreadPoolExecutor(HEAD_CONCURRENCY) as pool:
            orphaned = [d for d in pool.map(check, pointed) if d is not None]

    print(f"   {len(orphaned)} with a thumbnail_path pointing at a missing file")
    return unset + orphaned


def fix_one(supabase, doc: dict, dry_run: bool) -> str:
    """Returns 'ok' or 'error:<msg>'."""
    storage_path = doc["storage_path"]
    pdf_key = storage_key(storage_path)

    # Determine thumbnail destination. An existing pointer is reused (the file
    # behind it is gone, not the path); an unset one is derived.
    thumb_path = doc.get("thumbnail_path") or thumbnail_path_for(storage_path)
    thumb_key = storage_key(thumb_path)

    try:
        pdf_bytes = supabase.storage.from_(BUCKET).download(pdf_key)
    except Exception as e:
        return f"error:download:{e}"

    try:
        thumb_bytes = generate_thumbnail(pdf_bytes)
    except Exception as e:
        return f"error:render:{e}"

    if dry_run:
        print(f"  [DRY RUN] would upload {len(thumb_bytes)} bytes → {thumb_key}")
        if not doc.get("thumbnail_path"):
            print(f"           and set thumbnail_path={thumb_path}")
        return "ok"

    try:
        supabase.storage.from_(BUCKET).upload(
            path=thumb_key,
            file=thumb_bytes,
            file_options={"content-type": "image/jpeg", "upsert": "true"},
        )
    except Exception as e:
        return f"error:upload:{e}"

    # Update row only if thumbnail_path wasn't already set
    if not doc.get("thumbnail_path"):
        try:
            supabase.table("documents").update({"thumbnail_path": thumb_path}).eq(
                "id", doc["id"]
            ).execute()
        except Exception as e:
            return f"error:db:{e}"

    return "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Don't upload/update")
    args = parser.parse_args()

    url = os.getenv("PUBLIC_SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("❌ Missing PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env")
        sys.exit(1)
    supabase = create_client(url, key)

    missing = find_missing(supabase, url)
    print(f"\n📊 Found {len(missing)} plans with missing thumbnail files\n")
    if not missing:
        return

    if args.dry_run:
        print("🏃 DRY RUN MODE — no uploads or db writes\n")

    ok = 0
    errors = []
    for i, doc in enumerate(missing, 1):
        print(f"[{i}/{len(missing)}] {doc['title'][:60]}")
        result = fix_one(supabase, doc, dry_run=args.dry_run)
        if result == "ok":
            ok += 1
            print(f"  ✅ {'would fix' if args.dry_run else 'fixed'}")
        else:
            errors.append((doc, result))
            print(f"  ❌ {result}")

    print("\n" + "=" * 60)
    print(f"✅ {ok} fixed, ❌ {len(errors)} errors")
    if errors:
        print("\nErrors:")
        for doc, err in errors:
            print(f"  {doc['title'][:50]} — {err}")


if __name__ == "__main__":
    main()
