#!/usr/bin/env python3
# /// script
# dependencies = []
# ///

from pathlib import Path


def main():
    base_path = Path.cwd()
    unsorted_dir = base_path / "unsorted"

    # Find all PDFs not in ISO3 country code folders
    all_pdfs = list(base_path.rglob("*.pdf")) + list(base_path.rglob("*.PDF"))

    # Filter to only PDFs not in ISO3/{loc_id}/ structure
    unorganized = []
    for pdf in all_pdfs:
        # Skip if in scripts, reference, or unsorted folders
        if any(
            part in ["scripts", "reference", "unsorted", ".git", ".claude"]
            for part in pdf.parts
        ):
            continue

        # Check if in plans/ISO3/{loc_id}/ structure
        parent = pdf.parent.name
        grandparent = pdf.parent.parent.name if pdf.parent.parent else ""
        great_grandparent = (
            pdf.parent.parent.parent.name if pdf.parent.parent.parent else ""
        )

        # If in plans/ISO3/{loc_id}/ structure, it's organized
        if (
            great_grandparent == "plans"
            and len(grandparent) == 3
            and grandparent.isupper()
            and parent.isdigit()
        ):
            continue

        unorganized.append(pdf)

    if not unorganized:
        print("No unorganized PDFs found!")
        return

    print(f"Found {len(unorganized)} unorganized PDFs")
    print(f"Moving to {unsorted_dir}/\n")

    unsorted_dir.mkdir(exist_ok=True)
    moved = 0

    for pdf in unorganized:
        dest_path = unsorted_dir / pdf.name
        if dest_path.exists():
            # Handle duplicates
            base = pdf.stem
            ext = pdf.suffix
            counter = 1
            while dest_path.exists():
                dest_path = unsorted_dir / f"{base}_{counter}{ext}"
                counter += 1

        try:
            pdf.rename(dest_path)
            print(f"  ✓ {pdf.name}")
            moved += 1
        except Exception as e:
            print(f"  ✗ {pdf.name}: {e}")

    print(f"\nMoved {moved}/{len(unorganized)} PDFs to unsorted/")


if __name__ == "__main__":
    main()
