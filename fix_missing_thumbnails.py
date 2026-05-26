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

Usage:
    uv run --env-file .env python fix_missing_thumbnails.py [--dry-run]
"""

import argparse
import os
import sys
from io import BytesIO
from pathlib import Path

import pymupdf
from dotenv import load_dotenv
from PIL import Image
from supabase import create_client

load_dotenv()

BUCKET = "plans"
THUMBNAIL_WIDTH = 400
THUMBNAIL_QUALITY = 80


def generate_thumbnail(pdf_bytes: bytes) -> bytes:
    """Render the first page of a PDF (in-memory) as a JPEG thumbnail."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    if len(doc) == 0:
        doc.close()
        raise ValueError("PDF has no pages")
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


def storage_key(path: str) -> str:
    """Strip leading 'plans/' bucket prefix if present."""
    return path[len("plans/") :] if path.startswith("plans/") else path


def find_missing(supabase) -> list[dict]:
    """Return documents whose thumbnail_path file is missing from storage."""
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

    missing = []
    for d in docs:
        thumb_path = d.get("thumbnail_path")
        if not thumb_path:
            missing.append(d)
            continue
        key = storage_key(thumb_path)
        parent = os.path.dirname(key)
        name = os.path.basename(key)
        try:
            files = supabase.storage.from_(BUCKET).list(
                path=parent, options={"limit": 1000, "search": name}
            )
            if not any(f["name"] == name for f in files):
                missing.append(d)
        except Exception:
            missing.append(d)
    return missing


def fix_one(supabase, doc: dict, dry_run: bool) -> str:
    """Returns 'ok' or 'error:<msg>'."""
    storage_path = doc["storage_path"]
    pdf_key = storage_key(storage_path)

    # Determine thumbnail destination
    thumb_path = doc.get("thumbnail_path")
    if not thumb_path:
        # Mirror PDF path under a thumbnails/ prefix, with .jpg
        rel = Path(pdf_key).with_suffix(".jpg")
        thumb_path = f"thumbnails/{rel}"
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

    missing = find_missing(supabase)
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
