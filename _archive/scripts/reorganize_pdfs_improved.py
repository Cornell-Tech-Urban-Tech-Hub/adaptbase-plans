#!/usr/bin/env python3
# /// script
# dependencies = [
#   "pypdf",
#   "openai",
#   "python-dotenv",
# ]
# ///

import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI
from pypdf import PdfReader

load_dotenv()


def load_reference_data():
    """Load cities and countries reference data from reference/ directory."""
    ref_dir = Path("reference")

    if not ref_dir.exists():
        print("Error: reference/ directory not found")
        print("Run fetch_reference_data.py first to create reference data")
        sys.exit(1)

    # Load cities CSV
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

    # Load countries CSV for better mapping
    countries = {}
    with open(ref_dir / "countries.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            iso3 = row["code_iso3"]
            countries[iso3] = {
                "name": row["name"],
                "iso2": row.get("code_iso2"),
            }

    return cities, countries


def normalize_name(name: str) -> str:
    """Normalize city/country name for matching."""
    import unicodedata

    # Convert to lowercase
    name = name.lower()
    # Remove accents/diacritics (Medellín -> medellin)
    name = "".join(
        c for c in unicodedata.normalize("NFD", name)
        if unicodedata.category(c) != "Mn"
    )
    # Replace hyphens with spaces
    name = name.replace("-", " ")
    # Remove extra whitespace
    name = " ".join(name.split())
    return name


def extract_city_from_filename(filename: str, cities_db: list) -> Optional[dict]:
    """Try to extract city name from filename by matching against database."""
    # Remove extension
    name_part = Path(filename).stem

    # Common patterns in filenames
    # e.g., "Athens-Resilience-Strategy", "Bangkok-climate-plan", "barcelona_nzc"

    # Try to find city names in the filename
    normalized_filename = normalize_name(name_part)

    # Sort cities by name length (longest first) to match "New York" before "York"
    sorted_cities = sorted(cities_db, key=lambda c: len(c["name"]), reverse=True)

    for city in sorted_cities:
        city_normalized = normalize_name(city["name"])

        # Check if normalized city name appears in filename
        if city_normalized in normalized_filename:
            return city

    return None


def extract_text_from_pdf(pdf_path: Path, max_pages: int = 5) -> str:
    """Extract text from first few pages of PDF."""
    try:
        reader = PdfReader(pdf_path)
        pages_to_read = min(max_pages, len(reader.pages))
        text = ""
        for i in range(pages_to_read):
            text += reader.pages[i].extract_text() + "\n"
        return text[:10000]
    except Exception as e:
        return ""


def identify_country_city_llm(text: str, filename: str, cities_db: list) -> Optional[dict]:
    """Use LLM via LiteLLM proxy to identify country and city from PDF text."""
    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_API_BASE"),
    )

    model = os.getenv("LLM_MODEL", "google.gemini-3.1-flash-lite-preview")

    cities_list_sample = "\n".join(
        [f"- {c['name']}, {c['country_code_iso3']}" for c in cities_db[:100]]
    )

    prompt = f"""Based on this document text and filename, identify the EXACT country and city name.

Filename: {filename}

Document text (first 5 pages):
{text}

The city MUST be one that exists in a climate resilience database. Here are some examples:
{cities_list_sample}
(... and 880 more cities)

Respond ONLY with a JSON object:
{{"country": "United States", "city": "Atlanta"}}

Use EXACT city name as in database (e.g., "New York" not "NYC", "Buenos Aires" not "Buenos-Aires").

If uncertain, respond:
{{"country": null, "city": null}}"""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        response_text = response.choices[0].message.content.strip()
        result = json.loads(response_text)
        return result
    except Exception as e:
        print(f"  LLM error: {e}")
        return None


def get_country_code_iso3(country_identifier: str, countries_db: dict, cities_db: list) -> Optional[str]:
    """Get ISO3 country code from country name/code."""
    if not country_identifier:
        return None

    country_lower = normalize_name(country_identifier)

    # Try exact ISO3 match
    if country_identifier.upper() in countries_db:
        return country_identifier.upper()

    # Try exact country name match
    for iso3, info in countries_db.items():
        if normalize_name(info["name"]) == country_lower:
            return iso3

    # Try partial match
    for iso3, info in countries_db.items():
        country_name_norm = normalize_name(info["name"])
        if country_lower in country_name_norm or country_name_norm in country_lower:
            return iso3

    return None


def match_city_to_database(city_name: str, country_code_iso3: str, cities_db: list) -> Optional[int]:
    """Match extracted city name to database and return loc_id."""
    if not city_name or not country_code_iso3:
        return None

    city_normalized = normalize_name(city_name)

    # Try exact match first
    for city in cities_db:
        if (
            normalize_name(city["name"]) == city_normalized
            and city["country_code_iso3"] == country_code_iso3
        ):
            return city["loc_id"]

    # Try partial match
    for city in cities_db:
        if city["country_code_iso3"] == country_code_iso3:
            db_name_norm = normalize_name(city["name"])
            if city_normalized in db_name_norm or db_name_norm in city_normalized:
                return city["loc_id"]

    return None


def find_all_pdfs(base_path: Path) -> list[Path]:
    """Recursively find all PDF files."""
    return list(base_path.rglob("*.pdf")) + list(base_path.rglob("*.PDF"))


def main():
    base_path = Path.cwd()
    print(f"Scanning for PDFs in: {base_path}")

    # Load reference data
    print("Loading reference data...")
    cities_db, countries_db = load_reference_data()
    print(f"  Loaded {len(cities_db)} cities, {len(countries_db)} countries")

    pdfs = find_all_pdfs(base_path)
    print(f"Found {len(pdfs)} PDF files\n")

    if not pdfs:
        print("No PDFs found!")
        return

    # Track results
    moved = 0
    skipped = 0
    failed = 0
    filename_matches = 0
    llm_matches = 0

    # Process each PDF
    for i, pdf_path in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf_path.name}")

        # Skip if already organized
        parent_name = pdf_path.parent.name
        grandparent_name = pdf_path.parent.parent.name if pdf_path.parent.parent else ""
        great_grandparent_name = (
            pdf_path.parent.parent.parent.name if pdf_path.parent.parent.parent else ""
        )

        if (
            great_grandparent_name == "plans"
            and len(grandparent_name) == 3
            and grandparent_name.isupper()
            and parent_name.isdigit()
        ):
            print(f"  ✓ Already organized")
            skipped += 1
            continue

        # Skip utility folders (but not _unsorted which we want to process)
        if any(
            part in ["scripts", "reference", ".git", ".claude"]
            for part in pdf_path.parts
        ):
            print(f"  ↷ Skipping (utility folder)")
            skipped += 1
            continue

        # Strategy 1: Try filename matching first (fast, no LLM cost)
        city_match = extract_city_from_filename(pdf_path.name, cities_db)

        if city_match:
            print(f"  → Filename match: {city_match['name']}, {city_match['country_code_iso3']}")
            iso3_code = city_match["country_code_iso3"]
            loc_id = city_match["loc_id"]
            filename_matches += 1
        else:
            # Strategy 2: Extract text and use LLM
            text = extract_text_from_pdf(pdf_path)
            if not text:
                print(f"  ✗ Could not extract text")
                failed += 1
                continue

            result = identify_country_city_llm(text, pdf_path.name, cities_db)
            if not result or not result.get("country") or not result.get("city"):
                print(f"  ✗ LLM could not identify city/country")
                failed += 1
                continue

            country_name = result["country"]
            city_name = result["city"]
            print(f"  → LLM identified: {city_name}, {country_name}")

            # Get ISO3 code
            iso3_code = get_country_code_iso3(country_name, countries_db, cities_db)
            if not iso3_code:
                print(f"  ✗ Could not map to ISO3 code")
                failed += 1
                continue

            print(f"  → Country code: {iso3_code}")

            # Match city to database
            loc_id = match_city_to_database(city_name, iso3_code, cities_db)
            if not loc_id:
                print(f"  ✗ City not in database")
                failed += 1
                continue

            print(f"  → Matched loc_id: {loc_id}")
            llm_matches += 1

        # Move file
        dest_folder = base_path / "plans" / iso3_code / str(loc_id)
        dest_folder.mkdir(parents=True, exist_ok=True)
        dest_path = dest_folder / pdf_path.name

        if dest_path.exists():
            print(f"  ⚠ File exists at destination")
            skipped += 1
            continue

        try:
            pdf_path.rename(dest_path)
            print(f"  ✓ Moved to: plans/{iso3_code}/{loc_id}/")
            moved += 1
        except Exception as e:
            print(f"  ✗ Error moving: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Moved:             {moved}")
    print(f"    - Filename match: {filename_matches}")
    print(f"    - LLM match:      {llm_matches}")
    print(f"  Skipped:           {skipped}")
    print(f"  Failed:            {failed}")
    print(f"  Total:             {len(pdfs)}")


if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("OPENAI_API_BASE"):
        print("Error: OPENAI_API_KEY and OPENAI_API_BASE not found")
        print("Please set them in .env file")
        sys.exit(1)

    main()
