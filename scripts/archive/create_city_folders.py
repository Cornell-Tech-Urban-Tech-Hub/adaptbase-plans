#!/usr/bin/env python3
# /// script
# dependencies = []
# ///

import csv
from pathlib import Path


def main():
    base_path = Path.cwd()
    ref_dir = base_path / "reference"

    if not (ref_dir / "cities.csv").exists():
        print("Error: reference/cities.csv not found")
        print("Run scripts/fetch_reference_data.py first")
        return

    # Load all cities
    cities = []
    with open(ref_dir / "cities.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cities.append(
                {
                    "loc_id": row["loc_id"],
                    "name": row["name"],
                    "country_code_iso3": row["country_code_iso3"],
                }
            )

    print(f"Creating folders for {len(cities)} cities...")

    created = 0
    existing = 0

    for city in cities:
        city_folder = base_path / "plans" / city["country_code_iso3"] / city["loc_id"]
        if city_folder.exists():
            existing += 1
        else:
            city_folder.mkdir(parents=True, exist_ok=True)
            created += 1

    print(f"\nResults:")
    print(f"  Created: {created}")
    print(f"  Existing: {existing}")
    print(f"  Total: {len(cities)}")


if __name__ == "__main__":
    main()
