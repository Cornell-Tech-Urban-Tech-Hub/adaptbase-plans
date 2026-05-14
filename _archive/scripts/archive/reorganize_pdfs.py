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

    # Load cities CSV into a searchable structure
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

    # Load mapping JSON
    with open(ref_dir / "loc_id_mapping.json", encoding="utf-8") as f:
        loc_id_mapping = json.load(f)

    return cities, loc_id_mapping


def extract_text_from_pdf(pdf_path: Path, max_pages: int = 5) -> str:
    """Extract text from first few pages of PDF."""
    try:
        reader = PdfReader(pdf_path)
        pages_to_read = min(max_pages, len(reader.pages))
        text = ""
        for i in range(pages_to_read):
            text += reader.pages[i].extract_text() + "\n"
        return text[:10000]  # Limit to 10k chars
    except Exception as e:
        print(f"  Error reading PDF: {e}")
        return ""


def identify_country_city(text: str, filename: str, cities_db: list) -> Optional[dict]:
    """Use LLM via LiteLLM proxy to identify country and city from PDF text."""
    # Create OpenAI client pointing to LiteLLM proxy
    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_API_BASE"),
    )

    # Get model from env or use default
    model = os.getenv("LLM_MODEL", "google.gemini-3.1-flash")

    # Create a reference list of cities for LLM to choose from
    cities_list_sample = "\n".join(
        [f"- {c['name']}, {c['country_code_iso3']}" for c in cities_db[:100]]
    )

    prompt = f"""Based on this document text and filename, identify the EXACT country and city name.

Filename: {filename}

Document text (first 5 pages):
{text}

The city MUST be one that exists in a climate resilience database. Here are some examples of cities in the database:
{cities_list_sample}
(... and 880 more cities)

Respond ONLY with a JSON object in this exact format:
{{"country": "United States", "city": "Atlanta"}}

Use the EXACT city name as it would appear in a database (e.g., "New York" not "NYC", "Los Angeles" not "LA").

If you cannot determine the country or city with high confidence, respond with:
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
        print(f"  Error calling LLM API: {e}")
        return None


def match_city_to_database(city_name: str, country_code_iso3: str, cities_db: list) -> Optional[int]:
    """Match extracted city name to database and return loc_id."""
    if not city_name or not country_code_iso3:
        return None

    # Try exact match first
    for city in cities_db:
        if (
            city["name"].lower() == city_name.lower()
            and city["country_code_iso3"] == country_code_iso3
        ):
            return city["loc_id"]

    # Try partial match (city name contains or is contained in db name)
    for city in cities_db:
        if city["country_code_iso3"] == country_code_iso3:
            db_name = city["name"].lower()
            search_name = city_name.lower()
            if db_name in search_name or search_name in db_name:
                return city["loc_id"]

    return None


def get_country_code_iso3(country_name: str, cities_db: list) -> Optional[str]:
    """Get ISO3 country code from country name using the database."""
    if not country_name:
        return None

    # Try to find a city from that country to get the ISO code
    country_lower = country_name.lower()

    # Common mappings
    country_mappings = {
        "united states": "USA",
        "usa": "USA",
        "us": "USA",
        "united kingdom": "GBR",
        "uk": "GBR",
        "canada": "CAN",
        "china": "CHN",
        "india": "IND",
        "brazil": "BRA",
        "south africa": "ZAF",
    }

    if country_lower in country_mappings:
        return country_mappings[country_lower]

    # Search in database
    for city in cities_db:
        if country_lower in city.get("country_code_iso3", "").lower():
            return city["country_code_iso3"]

    return None


def find_all_pdfs(base_path: Path) -> list[Path]:
    """Recursively find all PDF files."""
    return list(base_path.rglob("*.pdf")) + list(base_path.rglob("*.PDF"))


def main():
    base_path = Path.cwd()
    print(f"Scanning for PDFs in: {base_path}")

    # Load reference data
    print("Loading reference data...")
    cities_db, loc_id_mapping = load_reference_data()
    print(f"  Loaded {len(cities_db)} cities from database")

    pdfs = find_all_pdfs(base_path)
    print(f"Found {len(pdfs)} PDF files\n")

    if not pdfs:
        print("No PDFs found!")
        return

    # Track results
    moved = 0
    skipped = 0
    failed = 0

    # Process each PDF
    for i, pdf_path in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf_path.name}")

        # Skip if already in plans/ISO3/{loc_id}/ structure
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
            print(f"  ✓ Already organized (skipping)")
            skipped += 1
            continue

        # Extract text
        text = extract_text_from_pdf(pdf_path)
        if not text:
            print(f"  ✗ Could not extract text")
            failed += 1
            continue

        # Identify country and city
        result = identify_country_city(text, pdf_path.name, cities_db)
        if not result or not result.get("country") or not result.get("city"):
            print(f"  ✗ Could not identify country/city")
            failed += 1
            continue

        country_name = result["country"]
        city_name = result["city"]
        print(f"  → Identified: {city_name}, {country_name}")

        # Get ISO3 code
        iso3_code = get_country_code_iso3(country_name, cities_db)
        if not iso3_code:
            print(f"  ✗ Could not map to ISO3 code")
            failed += 1
            continue

        print(f"  → Country code: {iso3_code}")

        # Match city to database to get loc_id
        loc_id = match_city_to_database(city_name, iso3_code, cities_db)
        if not loc_id:
            print(f"  ✗ City not found in database")
            failed += 1
            continue

        print(f"  → Matched to loc_id: {loc_id}")

        # Create destination folder
        dest_folder = base_path / "plans" / iso3_code / str(loc_id)
        dest_folder.mkdir(parents=True, exist_ok=True)

        # Move file
        dest_path = dest_folder / pdf_path.name
        if dest_path.exists():
            print(f"  ⚠ File already exists at destination")
            skipped += 1
            continue

        try:
            pdf_path.rename(dest_path)
            print(f"  ✓ Moved to: plans/{iso3_code}/{loc_id}/")
            moved += 1
        except Exception as e:
            print(f"  ✗ Error moving file: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Moved:   {moved}")
    print(f"  Skipped: {skipped}")
    print(f"  Failed:  {failed}")
    print(f"  Total:   {len(pdfs)}")


if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("OPENAI_API_BASE"):
        print("Error: OPENAI_API_KEY and OPENAI_API_BASE not found in environment")
        print("Please set them in .env file for LiteLLM proxy access")
        sys.exit(1)

    main()
