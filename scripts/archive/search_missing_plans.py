#!/usr/bin/env python3
# /// script
# dependencies = [
#   "python-dotenv",
# ]
# ///

import csv
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def load_cities_data():
    """Load cities from reference CSV."""
    ref_dir = Path("reference")
    cities = []

    with open(ref_dir / "cities.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cities.append(
                {
                    "loc_id": int(row["loc_id"]),
                    "name": row["name"],
                    "country_code_iso3": row["country_code_iso3"],
                }
            )

    return cities


def get_empty_cities():
    """Find cities with no PDFs in their folders."""
    base_path = Path.cwd()
    cities = load_cities_data()
    empty_cities = []

    for city in cities:
        city_folder = base_path / "plans" / city["country_code_iso3"] / str(city["loc_id"])
        if not city_folder.exists():
            continue

        # Check if folder is empty (no PDFs)
        pdfs = list(city_folder.glob("*.pdf")) + list(city_folder.glob("*.PDF"))
        if not pdfs:
            empty_cities.append(city)

    return empty_cities


def generate_search_queries(city):
    """Generate search queries for a city's climate plans."""
    city_name = city["name"]
    country = city["country_code_iso3"]

    queries = [
        f'"{city_name}" climate action plan filetype:pdf',
        f'"{city_name}" climate resilience strategy filetype:pdf',
        f'"{city_name}" climate adaptation plan filetype:pdf',
        f'"{city_name}" resilience strategy filetype:pdf',
        f'"{city_name}" sustainability plan filetype:pdf',
    ]

    return queries


def main():
    print("Finding cities without documents...")
    empty_cities = get_empty_cities()
    print(f"Found {len(empty_cities)} cities without PDFs\n")

    if not empty_cities:
        print("All cities have documents!")
        return

    # Create search tasks file
    search_tasks = []

    for city in empty_cities[:50]:  # Limit to first 50 for now
        queries = generate_search_queries(city)
        search_tasks.append(
            {
                "loc_id": city["loc_id"],
                "city_name": city["name"],
                "country_code": city["country_code_iso3"],
                "queries": queries,
                "status": "pending",
            }
        )

    # Save to JSON
    output_file = Path("search_tasks.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(search_tasks, f, indent=2, ensure_ascii=False)

    print(f"Created {len(search_tasks)} search tasks")
    print(f"Saved to: {output_file}")
    print("\nSample search queries:")
    for i, task in enumerate(search_tasks[:3], 1):
        print(f"\n{i}. {task['city_name']}, {task['country_code']}:")
        for q in task["queries"][:2]:
            print(f"   - {q}")

    print(f"\n\nNext steps:")
    print(f"1. Review search_tasks.json")
    print(f"2. Use web search to find plans")
    print(f"3. Download PDFs to appropriate city folders")


if __name__ == "__main__":
    main()
