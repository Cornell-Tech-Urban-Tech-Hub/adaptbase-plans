#!/usr/bin/env python3
# /// script
# dependencies = []
# ///
"""
Reset cities in search_progress.json so auto_search_plans_gemini_v2.py re-processes them.

By default resets only completed cities with 0 downloads.
Pass --all to reset every completed city (re-search all, skip docs already on disk).

After running, re-run auto_search_plans_gemini_v2.py. The main script's
file_exists_anywhere() check (filename + URL) prevents re-downloading docs
that were already saved.
"""

import argparse
import json
import sys
from pathlib import Path

PROGRESS_FILE = Path("scripts/search_progress.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Reset ALL completed cities, not just those with 0 downloads",
    )
    args = parser.parse_args()

    if not PROGRESS_FILE.exists():
        print("Error: scripts/search_progress.json not found")
        sys.exit(1)

    with open(PROGRESS_FILE) as f:
        progress = json.load(f)

    cities = progress["cities_processed"]

    if args.all:
        to_reset = [k for k, v in cities.items() if v.get("status") == "completed"]
        label = "ALL completed"
    else:
        to_reset = [
            k
            for k, v in cities.items()
            if v.get("status") == "completed" and v.get("downloads", 0) == 0
        ]
        label = "completed with 0 downloads"

    if not to_reset:
        print(f"No {label} cities found. Nothing to reset.")
        return

    print(f"Found {len(to_reset)} {label} cities.")
    if not args.all:
        print("(Use --all to reset all completed cities, including those with existing downloads.)")

    confirm = input(f"\nReset {len(to_reset)} cities? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    for loc_id in to_reset:
        del cities[loc_id]

    completed = sum(1 for d in cities.values() if d.get("status") == "completed")
    failed = sum(1 for d in cities.values() if d.get("status") == "failed")
    progress["completed"] = completed
    progress["failed"] = failed

    temp = PROGRESS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(progress, indent=2))
    temp.replace(PROGRESS_FILE)

    print(f"\nReset {len(to_reset)} cities.")
    print(f"Progress now: {completed} completed, {failed} failed.")
    print("Re-run auto_search_plans_gemini_v2.py to process them.")


if __name__ == "__main__":
    main()
