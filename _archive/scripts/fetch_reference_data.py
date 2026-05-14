#!/usr/bin/env python3
# /// script
# dependencies = [
#   "python-dotenv",
#   "supabase",
# ]
# ///

import csv
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()


def main():
    # Create reference directory
    ref_dir = Path("reference")
    ref_dir.mkdir(exist_ok=True)

    # Connect to Supabase
    supabase_url = os.getenv("SUPABASE_URL") or os.getenv("PUBLIC_SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv(
        "SUPABASE_ANON_KEY"
    ) or os.getenv("PUBLIC_SUPABASE_ANON_KEY")

    if not supabase_url or not supabase_key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")
        return

    client = create_client(supabase_url, supabase_key)

    # Fetch all countries
    print("Fetching countries...")
    countries_response = (
        client.table("countries").select("code_iso3,name,code_iso2").execute()
    )
    countries = countries_response.data
    print(f"  Found {len(countries)} countries")

    # Save countries to CSV
    countries_csv = ref_dir / "countries.csv"
    with open(countries_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["code_iso3", "name", "code_iso2"])
        writer.writeheader()
        writer.writerows(countries)
    print(f"  Saved to {countries_csv}")

    # Fetch all cities
    print("Fetching cities...")
    cities_response = (
        client.table("cities")
        .select("loc_id,name,country_code_iso3")
        .order("country_code_iso3,name")
        .execute()
    )
    cities = cities_response.data
    print(f"  Found {len(cities)} cities")

    # Save cities to CSV
    cities_csv = ref_dir / "cities.csv"
    with open(cities_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["loc_id", "name", "country_code_iso3"]
        )
        writer.writeheader()
        writer.writerows(cities)
    print(f"  Saved to {cities_csv}")

    # Create a combined mapping: loc_id -> {name, country_code}
    mapping = {
        city["loc_id"]: {
            "name": city["name"],
            "country_code_iso3": city["country_code_iso3"],
        }
        for city in cities
    }

    mapping_json = ref_dir / "loc_id_mapping.json"
    with open(mapping_json, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)
    print(f"  Saved mapping to {mapping_json}")

    print("\nDone! Reference data saved to reference/ directory")


if __name__ == "__main__":
    main()
