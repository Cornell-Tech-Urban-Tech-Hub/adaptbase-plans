#!/usr/bin/env python3
# /// script
# dependencies = [
#   "anthropic",
#   "python-dotenv",
# ]
# ///

import json
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()


def search_for_city_plans(city_name, country_code, client):
    """Use Claude with web search to find climate plans for a city."""

    prompt = f"""Find climate resilience, adaptation, or action plans for {city_name}, {country_code}.

Search for official city government PDFs with titles like:
- Climate Action Plan
- Climate Resilience Strategy
- Climate Adaptation Plan
- Resilience Strategy
- Sustainability Plan

Return ONLY a JSON array of found documents with this structure:
[
  {{
    "title": "document title",
    "url": "direct PDF URL",
    "source": "publishing organization",
    "year": "publication year if known"
  }}
]

If no official plans found, return empty array: []"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            tools=[
                {
                    "type": "web_search_tool_20250127",
                    "name": "web_search",
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract text response
        result_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                result_text += block.text

        # Parse JSON response
        try:
            results = json.loads(result_text)
            return results if isinstance(results, list) else []
        except json.JSONDecodeError:
            print(f"  Warning: Could not parse JSON response for {city_name}")
            return []

    except Exception as e:
        print(f"  Error searching for {city_name}: {e}")
        return []


def main():
    tasks_file = Path("search_tasks.json")
    results_file = Path("search_results.json")

    if not tasks_file.exists():
        print("Error: search_tasks.json not found")
        print("Run scripts/search_missing_plans.py first")
        return

    # Load search tasks
    with open(tasks_file, encoding="utf-8") as f:
        tasks = json.load(f)

    # Load existing results if any
    results = {}
    if results_file.exists():
        with open(results_file, encoding="utf-8") as f:
            results = json.load(f)

    # Initialize Claude client
    import os

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set in .env")
        return

    client = anthropic.Anthropic(api_key=api_key)

    print(f"Searching for plans for {len(tasks)} cities...\n")

    for i, task in enumerate(tasks, 1):
        city_name = task["city_name"]
        country_code = task["country_code"]
        loc_id = task["loc_id"]

        # Skip if already searched
        if str(loc_id) in results:
            print(f"[{i}/{len(tasks)}] {city_name}, {country_code} - already searched")
            continue

        print(f"[{i}/{len(tasks)}] Searching for {city_name}, {country_code}...")

        found_plans = search_for_city_plans(city_name, country_code, client)

        results[str(loc_id)] = {
            "city_name": city_name,
            "country_code": country_code,
            "found_plans": found_plans,
            "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        if found_plans:
            print(f"  ✓ Found {len(found_plans)} plan(s)")
            for plan in found_plans:
                print(f"    - {plan.get('title', 'Unknown')}")
        else:
            print(f"  ✗ No plans found")

        # Save after each search
        with open(results_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        # Rate limiting
        time.sleep(2)

    print(f"\n{'='*60}")
    print(f"Search complete!")
    print(f"Results saved to: {results_file}")
    print(f"Total cities searched: {len(results)}")
    print(f"Cities with plans found: {sum(1 for r in results.values() if r['found_plans'])}")


if __name__ == "__main__":
    main()
