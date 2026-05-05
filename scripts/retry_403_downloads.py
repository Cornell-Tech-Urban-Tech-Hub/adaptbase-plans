#!/usr/bin/env python3
"""
Retry downloads that failed with 403 Forbidden errors.

Loads failed_downloads.json, filters for 403 errors, and retries with
improved HTTP client. Skips URLs where the document was successfully
downloaded in a subsequent run.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, UTC

import httpx
from pypdf import PdfReader
from langdetect import detect


def setup_http_client() -> httpx.Client:
    """Create HTTP client with browser-like headers."""
    return httpx.Client(
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        },
        timeout=30.0,
        follow_redirects=True,
    )


def detect_language(pdf_path: Path) -> str:
    """Detect language from first page of PDF."""
    try:
        reader = PdfReader(pdf_path)
        if len(reader.pages) == 0:
            return "unknown"

        text = reader.pages[0].extract_text()
        if not text or len(text.strip()) < 50:
            return "unknown"

        sample = text[:1000] if len(text) > 1000 else text
        lang = detect(sample)
        return lang
    except Exception:
        return "unknown"


def is_already_downloaded(city: dict) -> bool:
    """Check if document exists in plans/ or _incoming_for_review/."""
    loc_id = city["loc_id"]
    country_code = city["country_code_iso3"]

    # Check plans folder
    plans_dir = Path("plans") / country_code / str(loc_id)
    if plans_dir.exists():
        pdf_files = list(plans_dir.glob("*.pdf"))
        if pdf_files:
            return True

    # Check incoming folder
    incoming_dir = Path("_incoming_for_review") / country_code / str(loc_id)
    if incoming_dir.exists():
        pdf_files = list(incoming_dir.glob("*.pdf"))
        if pdf_files:
            return True

    return False


def retry_download(client: httpx.Client, url: str, city: dict, output_dir: Path) -> tuple[bool, str, str]:
    """
    Retry downloading a PDF.

    Returns:
        (success, error_message, language_code)
    """
    try:
        response = client.get(url)
        response.raise_for_status()

        # Save PDF
        loc_id = city["loc_id"]
        city_name = city["name"]
        country_code = city["country_code_iso3"]

        # Sanitize city name for filename
        safe_city_name = "".join(c for c in city_name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_city_name = safe_city_name.replace(' ', '_')

        pdf_filename = f"{country_code}_{loc_id}_{safe_city_name}_climate_plan.pdf"
        pdf_path = output_dir / pdf_filename

        pdf_path.write_bytes(response.content)
        print(f"  ✓ Saved: {pdf_path}")

        # Detect language
        language = detect_language(pdf_path)
        print(f"  ✓ Language: {language}")

        # Save metadata
        metadata = {
            "loc_id": loc_id,
            "city_name": city_name,
            "country_code": country_code,
            "url": url,
            "download_date": datetime.now(UTC).isoformat(),
            "language": language,
            "retry_after_403": True,
        }

        metadata_path = output_dir / f"{pdf_filename}.json"
        metadata_path.write_text(json.dumps(metadata, indent=2))

        return True, "", language

    except httpx.HTTPStatusError as e:
        return False, f"{e.response.status_code} {e.response.reason_phrase}", "unknown"
    except Exception as e:
        return False, str(e), "unknown"


def main():
    # Load failed downloads
    failed_path = Path("scripts/failed_downloads.json")
    if not failed_path.exists():
        print("No failed_downloads.json found")
        return

    with open(failed_path) as f:
        failed_downloads = json.load(f)

    # Filter for 403 errors
    forbidden_failures = [
        item for item in failed_downloads
        if item.get("error_type") == "403_forbidden"
    ]

    print(f"Found {len(forbidden_failures)} downloads with 403 Forbidden errors")

    if not forbidden_failures:
        print("Nothing to retry")
        return

    # Setup
    client = setup_http_client()
    output_dir = Path("_incoming_for_review_retry403")
    output_dir.mkdir(exist_ok=True)

    # Track results
    results = {
        "skipped_already_downloaded": [],
        "success": [],
        "still_failed": [],
    }

    # Retry each
    for i, item in enumerate(forbidden_failures, 1):
        url = item["url"]
        city = item["city"]

        print(f"\n[{i}/{len(forbidden_failures)}] {city['name']}, {city['country_code_iso3']}")
        print(f"  URL: {url}")

        # Check if already downloaded
        if is_already_downloaded(city):
            print("  ⊘ Skipped: already downloaded")
            results["skipped_already_downloaded"].append({
                "url": url,
                "city": city,
            })
            continue

        # Create city output dir
        loc_id = city["loc_id"]
        country_code = city["country_code_iso3"]
        city_dir = output_dir / country_code / str(loc_id)
        city_dir.mkdir(parents=True, exist_ok=True)

        # Retry download
        success, error, language = retry_download(client, url, city, city_dir)

        if success:
            results["success"].append({
                "url": url,
                "city": city,
                "language": language,
            })
        else:
            print(f"  ✗ Still failed: {error}")
            results["still_failed"].append({
                "url": url,
                "city": city,
                "error": error,
                "original_score": item.get("score"),
                "original_reasoning": item.get("llm_reasoning"),
            })

    # Save results
    results_path = Path("scripts/retry_403_results.json")
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)

    # Summary
    print("\n" + "=" * 60)
    print("RETRY SUMMARY")
    print("=" * 60)
    print(f"Skipped (already downloaded): {len(results['skipped_already_downloaded'])}")
    print(f"Success: {len(results['success'])}")
    print(f"Still failed: {len(results['still_failed'])}")
    print(f"\nResults saved to: {results_path}")

    if results["success"]:
        print(f"\nSuccessful downloads saved to: {output_dir}/")


if __name__ == "__main__":
    main()
