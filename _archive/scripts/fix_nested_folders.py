#!/usr/bin/env python3
# /// script
# dependencies = []
# ///
"""
Fix nested _incoming_for_review folders created by triage script bug.

Moves files from:
  plans/_rejected/_incoming_for_review/{ISO3}/{loc_id}/*
  plans/_human_review_needed/_incoming_for_review/{ISO3}/{loc_id}/*

To correct locations:
  plans/_rejected/{ISO3}/{loc_id}/*
  plans/_human_review_needed/{ISO3}/{loc_id}/*

Also moves PASS files from _incoming_for_review to final locations:
  plans/_incoming_for_review/{ISO3}/{loc_id}/*
  → plans/{ISO3}/{loc_id}/*

Usage:
  uv run scripts/fix_nested_folders.py [--dry-run]
"""

import argparse
import shutil
from pathlib import Path


PLANS_DIR = Path(__file__).parent.parent / "plans"
REJECTED_DIR = PLANS_DIR / "_rejected"
HUMAN_REVIEW_DIR = PLANS_DIR / "_human_review_needed"
INCOMING_DIR = PLANS_DIR / "_incoming_for_review"


def move_with_sidecar(src_pdf: Path, dest_pdf: Path, dry_run: bool) -> None:
    """Move PDF and its .json sidecar to destination."""
    src_json = src_pdf.with_suffix(src_pdf.suffix + ".json")
    dest_json = dest_pdf.with_suffix(dest_pdf.suffix + ".json")

    if dry_run:
        print(f"  [dry-run] {src_pdf.name}")
        print(f"            → {dest_pdf.relative_to(PLANS_DIR)}")
        return

    # Create parent directory
    dest_pdf.parent.mkdir(parents=True, exist_ok=True)

    # Move PDF
    shutil.move(str(src_pdf), str(dest_pdf))

    # Move sidecar if exists
    if src_json.exists():
        shutil.move(str(src_json), str(dest_json))


def flatten_nested_incoming(root_dir: Path, target_root: Path, dry_run: bool) -> int:
    """
    Flatten nested _incoming_for_review folders.

    Example:
      root_dir/_incoming_for_review/USA/107/file.pdf
      → target_root/USA/107/file.pdf
    """
    nested_incoming = root_dir / "_incoming_for_review"
    if not nested_incoming.exists():
        return 0

    moved = 0
    for pdf in nested_incoming.rglob("*.pdf"):
        # Get path relative to nested _incoming_for_review
        rel_path = pdf.relative_to(nested_incoming)
        dest = target_root / rel_path

        move_with_sidecar(pdf, dest, dry_run)
        moved += 1

    # Clean up empty nested folder
    if not dry_run and moved > 0:
        # Remove empty directories
        for dirpath in sorted(nested_incoming.rglob("*"), reverse=True):
            if dirpath.is_dir() and not any(dirpath.iterdir()):
                dirpath.rmdir()
        if nested_incoming.exists() and not any(nested_incoming.iterdir()):
            nested_incoming.rmdir()

    return moved


def move_pass_files(dry_run: bool) -> int:
    """
    Move PASS files from _incoming_for_review to final locations.

    Example:
      plans/_incoming_for_review/USA/107/file.pdf
      → plans/USA/107/file.pdf
    """
    moved = 0
    for pdf in INCOMING_DIR.rglob("*.pdf"):
        # Get path relative to _incoming_for_review
        rel_path = pdf.relative_to(INCOMING_DIR)
        dest = PLANS_DIR / rel_path

        move_with_sidecar(pdf, dest, dry_run)
        moved += 1

    # Clean up empty directories in _incoming_for_review
    if not dry_run and moved > 0:
        for dirpath in sorted(INCOMING_DIR.rglob("*"), reverse=True):
            if dirpath.is_dir() and not any(dirpath.iterdir()):
                dirpath.rmdir()
        # Remove _incoming_for_review itself if empty
        if INCOMING_DIR.exists() and not any(INCOMING_DIR.iterdir()):
            INCOMING_DIR.rmdir()
            print("   Removed empty _incoming_for_review folder")

    return moved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix nested _incoming_for_review folders and move PASS files"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be moved without actually moving files",
    )
    args = parser.parse_args()

    print("=== Fixing Nested Folders ===\n")

    # 1. Flatten _rejected/_incoming_for_review
    print(f"1. Flattening _rejected/_incoming_for_review...")
    rejected_moved = flatten_nested_incoming(REJECTED_DIR, REJECTED_DIR, args.dry_run)
    print(f"   Moved {rejected_moved} files\n")

    # 2. Flatten _human_review_needed/_incoming_for_review
    print(f"2. Flattening _human_review_needed/_incoming_for_review...")
    review_moved = flatten_nested_incoming(
        HUMAN_REVIEW_DIR, HUMAN_REVIEW_DIR, args.dry_run
    )
    print(f"   Moved {review_moved} files\n")

    # 3. Move PASS files to final locations
    print(f"3. Moving PASS files from _incoming_for_review to final locations...")
    pass_moved = move_pass_files(args.dry_run)
    print(f"   Moved {pass_moved} files\n")

    print("=== Summary ===")
    print(f"  Rejected files flattened:      {rejected_moved}")
    print(f"  Human review files flattened:  {review_moved}")
    print(f"  PASS files moved to final:     {pass_moved}")
    print(f"  Total files moved:             {rejected_moved + review_moved + pass_moved}")

    if args.dry_run:
        print("\n[DRY RUN] No files were actually moved. Run without --dry-run to apply changes.")
    else:
        print("\n✅ All files moved successfully!")
        print("\nNext steps:")
        print("  1. Review files in plans/_human_review_needed/{ISO3}/{loc_id}/")
        print("  2. Move valid ones to plans/{ISO3}/{loc_id}/")
        print("  3. Move invalid ones to plans/_rejected/{ISO3}/{loc_id}/")


if __name__ == "__main__":
    main()
