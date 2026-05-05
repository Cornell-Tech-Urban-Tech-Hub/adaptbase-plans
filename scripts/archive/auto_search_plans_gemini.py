#!/usr/bin/env python3
# /// script
# dependencies = [
#   "openai",
#   "python-dotenv",
#   "httpx",
# ]
# ///

import csv
import datetime
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


class ClimateDocSearchAgent:
    """LLM-orchestrated agent for finding climate plans."""

    def __init__(self):
        self.llm = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_API_BASE"),
        )
        self.model = os.getenv("LLM_MODEL", "google.gemini-3.1-flash-lite-preview")
        self.serper_key = os.getenv("SERPER_API_KEY")
        self.base_path = Path.cwd()
        self.review_dir = self.base_path / "plans" / "_incoming_for_review"
        self.progress_file = self.base_path / "scripts" / "search_progress.json"
        self.progress = self.load_progress()

    def load_progress(self) -> dict:
        """Load progress from file or create new."""
        if self.progress_file.exists():
            return json.loads(self.progress_file.read_text())
        return {
            "last_updated": None,
            "total_cities": 0,
            "completed": 0,
            "failed": 0,
            "cities_processed": {},
        }

    def save_progress(self):
        """Atomically save progress to file."""
        self.progress["last_updated"] = datetime.datetime.now(datetime.UTC).isoformat()
        # Atomic write: write to temp file, then rename
        temp_file = self.progress_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(self.progress, indent=2))
        temp_file.replace(self.progress_file)

    def is_processed(self, loc_id: int) -> bool:
        """Check if city already processed."""
        return str(loc_id) in self.progress["cities_processed"]

    def file_exists_anywhere(self, city: dict, filename: str) -> tuple[bool, str]:
        """Check if file already exists in plans/ or _incoming_for_review/ directories.

        Returns (exists, location) where location is 'plans', 'incoming', or empty string.
        """
        base_name = Path(filename).stem.lower()

        # Check in plans/{ISO3}/{loc_id}/
        plans_folder = (
            self.base_path / "plans" / city["country_code_iso3"] / str(city["loc_id"])
        )
        if plans_folder.exists():
            # Check for exact filename match
            if (plans_folder / filename).exists():
                return True, "plans"

            # Check for similar filenames (in case of slight variations)
            # This catches cases like "plan.pdf" vs "plan_new_1.pdf"
            for existing_file in plans_folder.glob("*.pdf"):
                if existing_file.stem.lower() == base_name:
                    return True, "plans"

        # Check in _incoming_for_review/{ISO3}/{loc_id}/
        review_folder = (
            self.review_dir / city["country_code_iso3"] / str(city["loc_id"])
        )
        if review_folder.exists():
            # Check for exact filename match
            if (review_folder / filename).exists():
                return True, "incoming"

            # Check for similar filenames
            for existing_file in review_folder.glob("*.pdf"):
                if existing_file.stem.lower() == base_name:
                    return True, "incoming"

        return False, ""

    def plan_search(self, city: dict) -> dict:
        """Stage 1: Use LLM to plan search queries."""
        prompt = f"""You are a research assistant helping to locate official climate resilience and adaptation planning documents for cities worldwide.

CITY CONTEXT:
- City: {city["name"]}
- Country: {city["country_code_iso3"]}
- Database ID: {city["loc_id"]}

TASK:
Generate 3-5 highly targeted web search queries to find this city's official climate/resilience planning documents.

REQUIREMENTS:
1. Prioritize official government sources (city websites, municipal planning departments)
2. Look for recent documents (2015-present preferred)
3. Target these document types (in priority order):
   - Climate Resilience Strategies
   - Climate Adaptation Plans
   - Heat Action Plans
   - Climate Action Plans (ONLY if they include adaptation/resilience content, NOT mitigation-only)
   - Sustainability Plans (if they include climate/resilience sections)
   - Hazard Mitigation Plans (if they include climate adaptation)

SEARCH STRATEGY GUIDANCE:
- Keep queries concise (under 200 characters) to avoid API errors
- Use city name variations (e.g., "NYC" vs "New York City", "México" vs "Mexico")
- Include country name if city name is common (e.g., "Paris France" vs "Paris Texas")
- Try both English and local language terms if applicable
- Use filetype:pdf when looking for downloadable documents
- Include year ranges for recent documents (e.g., 2020..2024)
- Consider regional government levels (city, metropolitan area, county/province)

AVOID:
- Mitigation-only climate action plans (GHG reduction, renewable energy, emissions targets without adaptation/resilience)
- Academic papers about the city (unless no official plans exist)
- News articles or blog posts
- Consultant reports (unless officially adopted by city)
- Draft or proposed plans (final/adopted plans preferred)

OUTPUT FORMAT (JSON):
{{
  "search_queries": [
    {{
      "query": "exact search string",
      "rationale": "why this query is likely to find relevant documents",
      "expected_source_type": "city website | national government | regional planning body | etc."
    }}
  ],
  "search_notes": "Any special considerations for this city (e.g., name variations, known challenges, language)"
}}

Generate search queries for {city["name"]}, {city["country_code_iso3"]}."""

        try:
            response = self.llm.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
            )
            response_text = response.choices[0].message.content.strip()

            # Extract JSON from markdown code blocks if present
            if "```json" in response_text:
                response_text = (
                    response_text.split("```json")[1].split("```")[0].strip()
                )
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0].strip()

            result = json.loads(response_text)
            return result
        except Exception as e:
            print(f"  LLM error in plan_search: {e}")
            # Fallback to basic queries
            return {
                "search_queries": [
                    {
                        "query": f'"{city["name"]}" climate resilience plan filetype:pdf',
                        "rationale": "Basic search fallback",
                        "expected_source_type": "city website",
                    }
                ],
                "search_notes": "Fallback due to LLM error",
            }

    def execute_search(self, query: str) -> list[dict]:
        """Stage 2: Execute search via Serper API."""
        # Sanitize and truncate query if too long
        query = query.strip()
        if len(query) > 400:
            query = query[:400]

        url = "https://google.serper.dev/search"
        payload = {"q": query, "num": 10}
        headers = {"X-API-KEY": self.serper_key, "Content-Type": "application/json"}

        response = httpx.post(url, json=payload, headers=headers)

        # Check for quota/credit exhaustion
        if response.status_code == 429:
            print("\n" + "=" * 60)
            print("⚠️  SERPER API RATE LIMIT HIT")
            print("=" * 60)
            print("The script has been paused due to rate limiting.")
            print("Progress saved. Resume later by running the script again.")
            print("=" * 60)
            raise Exception("Serper API rate limit exceeded")

        if response.status_code in [402, 403]:
            print("\n" + "=" * 60)
            print("⚠️  SERPER API QUOTA EXHAUSTED")
            print("=" * 60)
            print("Your Serper API credit has run out.")
            print(f"Cities processed so far: {self.progress['completed']}")
            print(
                f"Cities remaining: {self.progress['total_cities'] - self.progress['completed']}"
            )
            print("\nOptions:")
            print("1. Add more credit to your Serper account")
            print("2. Resume later by running the script again")
            print("\nProgress has been saved to scripts/search_progress.json")
            print("=" * 60)
            raise Exception("Serper API quota exhausted")

        if response.status_code == 400:
            error_msg = f"Bad request for query: {query}"
            try:
                error_detail = response.json()
                error_msg += f"\nSerper response: {json.dumps(error_detail, indent=2)}"
            except Exception:
                error_msg += f"\nResponse text: {response.text}"
            print(f"\n{'=' * 60}")
            print("⚠️  SERPER API ERROR (400)")
            print(f"{'=' * 60}")
            print(error_msg)
            print(f"{'=' * 60}")
            raise Exception(f"Serper API 400 error: {error_msg}")

        response.raise_for_status()

        results = response.json().get("organic", [])
        return results

    def evaluate_results(self, city: dict, results: list[dict]) -> dict:
        """Stage 3: Use LLM to evaluate search results."""
        results_json = json.dumps(results[:20], indent=2)  # Top 20 results

        prompt = f"""You are evaluating web search results to identify official climate resilience/adaptation planning documents for {city["name"]}, {city["country_code_iso3"]}.

SEARCH RESULTS:
{results_json}

EVALUATION CRITERIA:
1. Authority (40 points): Official city/government source > Regional body > Consultant/NGO
2. Document Type (30 points):
   - Resilience Strategy (30pts)
   - Adaptation Plan (28pts)
   - Heat Action Plan (26pts)
   - Climate Action Plan with adaptation content (24pts)
   - Sustainability Plan with resilience sections (20pts)
   - Hazard Mitigation Plan with climate adaptation (18pts)
   - Mitigation-only Climate Action Plan (0pts - EXCLUDE)
3. Recency (15 points): 2020+ (15pts) | 2015-2019 (10pts) | 2010-2014 (5pts) | Older (0pts)
4. Relevance (15 points): Directly matches city > Metropolitan area > Regional > Tangentially related

CRITICAL: Exclude any document that is purely focused on emissions reduction/mitigation without adaptation or resilience content.

SCORING:
- 80-100: Highly likely to be the official plan - download immediately
- 60-79: Strong candidate - worth downloading for review
- 40-59: Possible match - download if no better options
- Below 40: Skip

OUTPUT FORMAT (JSON):
{{
  "candidates": [
    {{
      "url": "full URL",
      "title": "document title from search result",
      "score": 85,
      "score_breakdown": {{
        "authority": 35,
        "document_type": 30,
        "recency": 15,
        "relevance": 15
      }},
      "reasoning": "Why this score was assigned",
      "download_priority": 1,
      "filename_suggestion": "descriptive-filename.pdf"
    }}
  ],
  "evaluation_notes": "Overall assessment of search results quality",
  "recommendation": "download_all | download_top_3 | download_top_1 | skip_all"
}}

Only include candidates with score >= 40.
Rank by score (highest first).
If multiple documents for same city, prefer most recent and most comprehensive."""

        try:
            response = self.llm.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
            )
            response_text = response.choices[0].message.content.strip()

            # Extract JSON from markdown code blocks if present
            if "```json" in response_text:
                response_text = (
                    response_text.split("```json")[1].split("```")[0].strip()
                )
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0].strip()

            result = json.loads(response_text)
            return result
        except Exception as e:
            print(f"  LLM error in evaluate_results: {e}")
            return {
                "candidates": [],
                "evaluation_notes": "LLM error",
                "recommendation": "skip_all",
            }

    def download_pdf(self, url: str, dest_path: Path) -> bool:
        """Stage 4: Download PDF and save metadata."""
        try:
            response = httpx.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if (
                "application/pdf" not in content_type
                and "application/octet-stream" not in content_type
            ):
                print(f"      Not a PDF: {content_type}")
                return False

            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # Handle duplicates by appending _new_N
            if dest_path.exists():
                base = dest_path.stem
                ext = dest_path.suffix
                counter = 1
                while dest_path.exists():
                    dest_path = dest_path.parent / f"{base}_new_{counter}{ext}"
                    counter += 1

            dest_path.write_bytes(response.content)
            return True
        except Exception as e:
            print(f"      Download failed: {e}")
            return False

    def process_city(self, city: dict):
        """Full pipeline for one city."""
        loc_id = city["loc_id"]

        # Skip if already processed
        if self.is_processed(loc_id):
            print(f"Skipping {city['name']} (already processed)")
            return

        print(f"\n{'=' * 60}")
        print(f"Processing: {city['name']}, {city['country_code_iso3']}")

        try:
            # Stage 1: Plan
            search_plan = self.plan_search(city)
            queries = search_plan.get("search_queries", [])
            print(f"  Planned {len(queries)} queries")

            # Stage 2: Search
            all_results = []
            for sq in queries:
                print(f"  Searching: {sq['query'][:70]}...")
                results = self.execute_search(sq["query"])
                all_results.extend(results)
                time.sleep(1)  # Rate limit

            print(f"  Retrieved {len(all_results)} total search results")

            # Stage 3: Evaluate
            evaluation = self.evaluate_results(city, all_results)
            candidates = evaluation.get("candidates", [])
            print(f"  Found {len(candidates)} candidates (score >= 40)")

            # Stage 4: Download
            downloads = 0
            dest_folder = (
                self.review_dir / city["country_code_iso3"] / str(city["loc_id"])
            )
            for i, candidate in enumerate(candidates[:3], 1):  # Top 3
                filename = candidate.get("filename_suggestion", f"plan_{i}.pdf")
                # Sanitize filename
                filename = "".join(c for c in filename if c.isalnum() or c in "._- ")
                if not filename.endswith(".pdf"):
                    filename += ".pdf"

                # Check if file already exists anywhere
                exists, location = self.file_exists_anywhere(city, filename)
                if exists:
                    location_msg = (
                        "plans/" if location == "plans" else "_incoming_for_review/"
                    )
                    print(
                        f"  [{i}] Score: {candidate.get('score', 0)} - Already in {location_msg} (skipping)"
                    )
                    continue

                dest_path = dest_folder / filename

                print(
                    f"  [{i}] Score: {candidate.get('score', 0)} - {candidate['url'][:50]}..."
                )
                if self.download_pdf(candidate["url"], dest_path):
                    # Save metadata
                    meta_path = dest_path.with_suffix(".json")
                    meta_path.write_text(
                        json.dumps(candidate, indent=2, ensure_ascii=False)
                    )
                    print(f"      ✓ Saved to: {dest_path.relative_to(self.base_path)}")
                    downloads += 1

            # Mark as completed
            self.progress["cities_processed"][str(loc_id)] = {
                "status": "completed",
                "downloads": downloads,
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            }
            self.progress["completed"] += 1
            self.save_progress()

        except Exception as e:
            if "Serper API" in str(e):
                # Re-raise to stop execution
                raise

            # Log detailed error for debugging
            import traceback

            print(f"\n{'=' * 60}")
            print(f"  ✗ ERROR processing {city['name']}")
            print(f"{'=' * 60}")
            print(f"Error: {e}")
            print("\nFull traceback:")
            traceback.print_exc()
            print(f"{'=' * 60}\n")

            # Mark as failed
            self.progress["cities_processed"][str(loc_id)] = {
                "status": "failed",
                "reason": str(e)[:200],
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            }
            self.progress["failed"] += 1
            self.save_progress()


def main():
    """Main entry point."""
    agent = ClimateDocSearchAgent()

    # Load all cities from reference data
    cities = []
    with open("reference/cities.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cities.append(
                {
                    "loc_id": int(row["loc_id"]),
                    "name": row["name"],
                    "country_code_iso3": row["country_code_iso3"],
                }
            )

    agent.progress["total_cities"] = len(cities)

    print(f"Starting search for {len(cities)} cities")
    print(
        f"Already processed: {agent.progress['completed']} completed, {agent.progress['failed']} failed"
    )
    print(
        f"Remaining: {len(cities) - agent.progress['completed'] - agent.progress['failed']}"
    )

    try:
        for city in cities:
            agent.process_city(city)
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user (Ctrl+C)")
        print(f"Progress saved. Processed {agent.progress['completed']} cities.")
        sys.exit(0)
    except Exception as e:
        if "Serper API" in str(e):
            # Already printed detailed message in execute_search
            sys.exit(1)
        else:
            raise

    print(f"\n{'=' * 60}")
    print("SEARCH COMPLETE")
    print(f"  Total cities: {agent.progress['total_cities']}")
    print(f"  Completed: {agent.progress['completed']}")
    print(f"  Failed: {agent.progress['failed']}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    if not os.getenv("SERPER_API_KEY"):
        print("Error: SERPER_API_KEY not found in .env")
        sys.exit(1)
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("OPENAI_API_BASE"):
        print("Error: OPENAI_API_KEY and OPENAI_API_BASE not found in .env")
        sys.exit(1)

    main()
