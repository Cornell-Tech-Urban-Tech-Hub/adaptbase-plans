#!/usr/bin/env python3
# /// script
# dependencies = [
#   "pymupdf",
#   "langdetect",
# ]
# ///
"""
Add language detection to already-processed documents.

Scans plans/{ISO3}/{loc_id}/ directories for PDFs and adds a language
field to their metadata JSON files. Uses PyMuPDF (fitz) for text
extraction — extracts cleanly from many PDFs where pypdf produces
nothing.

Flags:
  -v, --debug              Per-file diagnostics.
  --reprocess-unknown      Re-attempt detection on files where the recorded
                           language is "unknown" (otherwise they are skipped
                           because they already have a language field).
  --sequester-unknown      After detection, move PDFs that remain "unknown"
                           into plans/_incoming_for_review/_unknown_language/
                           preserving the {ISO3}/{loc_id}/ structure. The
                           original location is recorded in
                           metadata.sequestered_from.
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
from langdetect import DetectorFactory, LangDetectException, detect

# Make langdetect deterministic across runs.
DetectorFactory.seed = 0

PAGE_CAP = 20
MIN_CHARS = 50
SAMPLE_CHARS = 2000

SEQUESTER_DIR = Path("plans") / "_incoming_for_review" / "_unknown_language"


def detect_language(pdf_path: Path, debug: bool = False) -> str:
    """Detect language from PDF, scanning up to PAGE_CAP pages with PyMuPDF."""
    try:
        with pymupdf.open(pdf_path) as doc:
            if doc.page_count == 0:
                if debug:
                    print("    → No pages in PDF")
                return "unknown"

            chunks = []
            for i in range(min(PAGE_CAP, doc.page_count)):
                chunks.append(doc[i].get_text() or "")
            text = " ".join(chunks).strip()

        if len(text) < MIN_CHARS:
            if debug:
                print(f"    → Only {len(text)} chars extracted (likely scan)")
            return "unknown"

        try:
            lang = detect(text[:SAMPLE_CHARS])
        except LangDetectException as e:
            if debug:
                print(f"    ✗ langdetect failed: {e}")
            return "unknown"

        if debug:
            print(f"    → Detected '{lang}' from {len(text)} chars")
        return lang
    except Exception as e:
        if debug:
            print(f"    ✗ Error reading PDF: {e}")
        return "unknown"


def sequester(pdf_path: Path, base_path: Path) -> Path:
    """Move pdf and its sidecar into the unknown-language bucket.

    Preserves {ISO3}/{loc_id}/ structure. Records the original path in
    the sidecar's `sequestered_from` field. Returns the new pdf path.
    """
    loc_id = pdf_path.parent.name
    iso3 = pdf_path.parent.parent.name

    dest_dir = base_path / SEQUESTER_DIR / iso3 / loc_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_pdf = dest_dir / pdf_path.name
    counter = 1
    while dest_pdf.exists():
        dest_pdf = dest_dir / f"{pdf_path.stem}_{counter}{pdf_path.suffix}"
        counter += 1

    src_meta = pdf_path.with_suffix(pdf_path.suffix + ".json")
    dest_meta = dest_pdf.with_suffix(dest_pdf.suffix + ".json")

    # Update metadata first so sequestered_from is in the moved file.
    if src_meta.exists():
        try:
            metadata = json.loads(src_meta.read_text())
        except json.JSONDecodeError:
            metadata = {"pdf_filename": pdf_path.name}
    else:
        metadata = {"pdf_filename": pdf_path.name}

    metadata["sequestered_from"] = str(pdf_path.relative_to(base_path))
    metadata["sequestered_at"] = datetime.now(UTC).isoformat()
    src_meta.write_text(json.dumps(metadata, indent=2, ensure_ascii=False))

    pdf_path.rename(dest_pdf)
    if src_meta.exists():
        src_meta.rename(dest_meta)

    return dest_pdf


def process_pdf(
    pdf_path: Path, debug: bool = False, reprocess_unknown: bool = False
) -> tuple[bool, str]:
    """Process a single PDF: detect language and update metadata.

    Returns (updated, status_or_language).
    """
    metadata_path = pdf_path.with_suffix(pdf_path.suffix + ".json")

    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text())
        except json.JSONDecodeError:
            print(f"    ✗ Invalid JSON in {metadata_path.name}, skipping")
            return False, "invalid_json"
    else:
        metadata = {"pdf_filename": pdf_path.name}

    current = metadata.get("language")
    if current and current != "unknown":
        return False, "already_has_language"
    if current == "unknown" and not reprocess_unknown:
        return False, "already_has_language"

    language = detect_language(pdf_path, debug=debug)
    metadata["language"] = language
    metadata["language_detection_date"] = datetime.now(UTC).isoformat()

    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False))
    return True, language


def scan_tree(
    base_dir: Path,
    label: str,
    stats: dict,
    debug: bool,
    reprocess_unknown: bool,
    sequester_unknown: bool,
    base_path: Path,
) -> None:
    """Walk base_dir/{country}/{loc}/*.pdf and update language metadata."""
    print(f"\nScanning {label}/ directory for PDFs...\n")

    for country_dir in sorted(base_dir.iterdir()):
        if not country_dir.is_dir() or country_dir.name.startswith("_"):
            continue

        country_code = country_dir.name

        for loc_dir in sorted(country_dir.iterdir()):
            if not loc_dir.is_dir():
                continue

            loc_id = loc_dir.name

            for pdf_path in sorted(loc_dir.glob("*.pdf")):
                stats["total_pdfs"] += 1
                rel = f"{country_code}/{loc_id}/{pdf_path.name}"
                print(rel)

                updated, result = process_pdf(
                    pdf_path, debug=debug, reprocess_unknown=reprocess_unknown
                )

                if updated:
                    stats["updated"] += 1
                    stats["by_language"][result] = (
                        stats["by_language"].get(result, 0) + 1
                    )
                    print(f"  ✓ Language: {result}")
                    if result == "unknown" and sequester_unknown:
                        new_path = sequester(pdf_path, base_path)
                        stats["sequestered"] += 1
                        print(f"  → Sequestered to {new_path.relative_to(base_path)}")
                elif result == "already_has_language":
                    stats["already_had_language"] += 1
                    print("  ⊘ Already has language")
                else:
                    stats["errors"] += 1


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-v", "--debug", action="store_true", help="Show detailed diagnostics"
    )
    parser.add_argument(
        "--reprocess-unknown",
        action="store_true",
        help='Re-attempt detection on files where language is "unknown"',
    )
    parser.add_argument(
        "--sequester-unknown",
        action="store_true",
        help="Move residual unknowns into plans/_incoming_for_review/_unknown_language/",
    )
    args = parser.parse_args()

    base_path = Path.cwd()
    plans_dir = base_path / "plans"
    if not plans_dir.exists():
        print("No plans/ directory found")
        return

    stats = {
        "total_pdfs": 0,
        "updated": 0,
        "already_had_language": 0,
        "errors": 0,
        "sequestered": 0,
        "by_language": {},
    }

    if args.debug:
        print("Debug mode enabled")
    if args.reprocess_unknown:
        print("Reprocessing files with language='unknown'")
    if args.sequester_unknown:
        print(f"Will sequester residual unknowns to {SEQUESTER_DIR}/")

    scan_tree(
        plans_dir,
        "plans",
        stats,
        args.debug,
        args.reprocess_unknown,
        args.sequester_unknown,
        base_path,
    )

    incoming_dir = plans_dir / "_incoming_for_review"
    if incoming_dir.exists():
        scan_tree(
            incoming_dir,
            "plans/_incoming_for_review",
            stats,
            args.debug,
            args.reprocess_unknown,
            args.sequester_unknown,
            base_path,
        )

    print("\n" + "=" * 60)
    print("LANGUAGE DETECTION SUMMARY")
    print("=" * 60)
    print(f"Total PDFs found: {stats['total_pdfs']}")
    print(f"Updated with language: {stats['updated']}")
    print(f"Already had language: {stats['already_had_language']}")
    print(f"Errors: {stats['errors']}")
    if args.sequester_unknown:
        print(f"Sequestered: {stats['sequestered']}")

    if stats["by_language"]:
        print("\nLanguages detected:")
        for lang, count in sorted(
            stats["by_language"].items(), key=lambda x: x[1], reverse=True
        ):
            print(f"  {lang}: {count}")


if __name__ == "__main__":
    main()
