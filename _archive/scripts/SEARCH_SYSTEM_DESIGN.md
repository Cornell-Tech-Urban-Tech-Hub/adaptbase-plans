# Automated Climate Plan Search System

## Overview

Multi-stage LLM-orchestrated system to find and download climate resilience plans for all 980 cities in the database. This includes cities that already have documents, to ensure comprehensive coverage (cities may have multiple relevant plans: resilience strategy + heat action plan + adaptation plan, etc.).

## System Architecture

```
┌─────────────────┐
│ City Queue      │ → All 980 cities from reference/cities.csv
│                 │   (includes cities with existing plans to ensure comprehensive coverage)
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│ Stage 1: Search Planning (Gemini)                  │
│                                                     │
│ Input:  City name, country, context about city     │
│ Output: 3-5 targeted search queries                │
│         + search strategy notes                     │
└────────┬────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│ Stage 2: Web Search (Serper API)                   │
│                                                     │
│ Execute each planned query                          │
│ Collect top 10 results per query                   │
└────────┬────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│ Stage 3: Result Evaluation (Gemini)                │
│                                                     │
│ Input:  Search results (titles, snippets, URLs)    │
│ Output: Ranked list of candidate PDFs              │
│         + confidence scores + reasoning             │
└────────┬────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│ Stage 4: Download & Organize                       │
│                                                     │
│ Download PDFs to:                                   │
│ plans/_incoming_for_review/{ISO3}/{loc_id}/        │
│                                                     │
│ Save metadata JSON with:                            │
│ - Source URL                                        │
│ - Search query that found it                        │
│ - Confidence score                                  │
│ - LLM reasoning                                     │
└─────────────────────────────────────────────────────┘
```

## Stage 1: Search Planning Prompt

This is the prompt given to Gemini for each city:

```
You are a research assistant helping to locate official climate resilience and adaptation planning documents for cities worldwide.

CITY CONTEXT:
- City: {city_name}
- Country: {country_name} ({iso3_code})
- Database ID: {loc_id}

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
{
  "search_queries": [
    {
      "query": "exact search string",
      "rationale": "why this query is likely to find relevant documents",
      "expected_source_type": "city website | national government | regional planning body | etc."
    }
  ],
  "search_notes": "Any special considerations for this city (e.g., name variations, known challenges, language)"
}

EXAMPLE for São Paulo, Brazil (BRA):
{
  "search_queries": [
    {
      "query": "São Paulo climate resilience adaptation plan filetype:pdf site:prefeitura.sp.gov.br",
      "rationale": "Official city government website, focusing on resilience/adaptation not just mitigation",
      "expected_source_type": "city website"
    },
    {
      "query": "\"São Paulo\" \"plano de resiliência climática\" OR \"adaptação climática\" 2020..2024 filetype:pdf",
      "rationale": "Portuguese language query for resilience/adaptation with recent date range",
      "expected_source_type": "city website"
    },
    {
      "query": "Sao Paulo Brazil heat action plan OR extreme heat filetype:pdf",
      "rationale": "Heat action plans are highly relevant for tropical cities; ASCII fallback for city name",
      "expected_source_type": "city website"
    },
    {
      "query": "\"São Paulo\" climate adaptation strategy 2020..2024 -mitigation filetype:pdf",
      "rationale": "Explicitly exclude mitigation-only plans",
      "expected_source_type": "city website"
    }
  ],
  "search_notes": "City name has accent mark (São Paulo), try both accented and ASCII versions. Primary language is Portuguese. Large tropical city likely facing heat challenges - prioritize heat action and adaptation plans over mitigation-only documents."
}

Generate search queries for {city_name}, {country_name}.
```

## Stage 3: Result Evaluation Prompt

After Serper returns results, this prompt evaluates them:

```
You are evaluating web search results to identify official climate resilience/adaptation planning documents for {city_name}, {country_name}.

SEARCH RESULTS:
{json_formatted_results}

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
{
  "candidates": [
    {
      "url": "full URL",
      "title": "document title from search result",
      "score": 85,
      "score_breakdown": {
        "authority": 35,
        "document_type": 30,
        "recency": 15,
        "relevance": 15
      },
      "reasoning": "Why this score was assigned",
      "download_priority": 1,
      "filename_suggestion": "descriptive-filename.pdf"
    }
  ],
  "evaluation_notes": "Overall assessment of search results quality",
  "recommendation": "download_all | download_top_3 | download_top_1 | skip_all"
}

Only include candidates with score >= 40.
Rank by score (highest first).
If multiple documents for same city, prefer most recent and most comprehensive.
```

## Implementation: auto_search_plans_gemini.py

```python
#!/usr/bin/env python3
# /// script
# dependencies = [
#   "openai",
#   "python-dotenv",
#   "httpx",
# ]
# ///

import json
import os
import time
from pathlib import Path
from typing import Optional

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
            "cities_processed": {}
        }

    def save_progress(self):
        """Atomically save progress to file."""
        import datetime
        self.progress["last_updated"] = datetime.datetime.utcnow().isoformat() + "Z"
        # Atomic write: write to temp file, then rename
        temp_file = self.progress_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(self.progress, indent=2))
        temp_file.replace(self.progress_file)

    def is_processed(self, loc_id: int) -> bool:
        """Check if city already processed."""
        return str(loc_id) in self.progress["cities_processed"]

    def plan_search(self, city: dict) -> dict:
        """Stage 1: Use LLM to plan search queries."""
        # [PROMPT FROM ABOVE]
        pass

    def execute_search(self, query: str) -> list[dict]:
        """Stage 2: Execute search via Serper API."""
        url = "https://google.serper.dev/search"
        payload = {"q": query, "num": 10}
        headers = {"X-API-KEY": self.serper_key, "Content-Type": "application/json"}

        response = httpx.post(url, json=payload, headers=headers)
        
        # Check for quota/credit exhaustion
        if response.status_code == 429:
            print("\n" + "="*60)
            print("⚠️  SERPER API RATE LIMIT HIT")
            print("="*60)
            print("The script has been paused due to rate limiting.")
            print("Progress saved. Resume later by running the script again.")
            print("="*60)
            raise Exception("Serper API rate limit exceeded")
        
        if response.status_code in [402, 403]:
            print("\n" + "="*60)
            print("⚠️  SERPER API QUOTA EXHAUSTED")
            print("="*60)
            print("Your Serper API credit has run out.")
            print(f"Cities processed so far: {self.progress['completed']}")
            print(f"Cities remaining: {self.progress['total_cities'] - self.progress['completed']}")
            print("\nOptions:")
            print("1. Add more credit to your Serper account")
            print("2. Resume later by running the script again")
            print("\nProgress has been saved to scripts/search_progress.json")
            print("="*60)
            raise Exception("Serper API quota exhausted")
        
        response.raise_for_status()

        results = response.json().get("organic", [])
        return results

    def evaluate_results(self, city: dict, results: list[dict]) -> dict:
        """Stage 3: Use LLM to evaluate search results."""
        # [PROMPT FROM ABOVE]
        pass

    def download_pdf(self, url: str, dest_path: Path) -> bool:
        """Stage 4: Download PDF and save metadata."""
        try:
            response = httpx.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()

            if "application/pdf" not in response.headers.get("content-type", ""):
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
            print(f"    Download failed: {e}")
            return False

    def process_city(self, city: dict):
        """Full pipeline for one city."""
        import datetime
        
        loc_id = city["loc_id"]
        
        # Skip if already processed
        if self.is_processed(loc_id):
            print(f"Skipping {city['name']} (already processed)")
            return
        
        print(f"\n{'='*60}")
        print(f"Processing: {city['name']}, {city['country_code_iso3']}")

        try:
            # Stage 1: Plan
            search_plan = self.plan_search(city)
            print(f"  Planned {len(search_plan['search_queries'])} queries")

            # Stage 2: Search
            all_results = []
            for sq in search_plan["search_queries"]:
                print(f"  Searching: {sq['query'][:60]}...")
                results = self.execute_search(sq["query"])
                all_results.extend(results)
                time.sleep(1)  # Rate limit

            # Stage 3: Evaluate
            evaluation = self.evaluate_results(city, all_results)
            candidates = evaluation["candidates"]
            print(f"  Found {len(candidates)} candidates")

            # Stage 4: Download
            downloads = 0
            dest_folder = (
                self.review_dir / city["country_code_iso3"] / str(city["loc_id"])
            )
            for i, candidate in enumerate(candidates[:3], 1):  # Top 3
                filename = candidate.get("filename_suggestion", f"plan_{i}.pdf")
                dest_path = dest_folder / filename

                print(f"  [{i}] Downloading: {candidate['url'][:60]}...")
                if self.download_pdf(candidate["url"], dest_path):
                    # Save metadata
                    meta_path = dest_path.with_suffix(".json")
                    meta_path.write_text(json.dumps(candidate, indent=2))
                    print(f"      ✓ Saved to: {dest_path.relative_to(self.base_path)}")
                    downloads += 1
            
            # Mark as completed
            self.progress["cities_processed"][str(loc_id)] = {
                "status": "completed",
                "downloads": downloads,
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z"
            }
            self.progress["completed"] += 1
            self.save_progress()
            
        except Exception as e:
            print(f"  ✗ Error: {e}")
            # Mark as failed
            self.progress["cities_processed"][str(loc_id)] = {
                "status": "failed",
                "reason": str(e),
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z"
            }
            self.progress["failed"] += 1
            self.save_progress()


def main():
    """Main entry point."""
    import csv
    import sys
    
    agent = ClimateDocSearchAgent()
    
    # Load all cities from reference data
    cities = []
    with open("reference/cities.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cities.append({
                "loc_id": int(row["loc_id"]),
                "name": row["name"],
                "country_code_iso3": row["country_code_iso3"],
            })
    
    agent.progress["total_cities"] = len(cities)
    
    print(f"Starting search for {len(cities)} cities")
    print(f"Already processed: {agent.progress['completed']} completed, {agent.progress['failed']} failed")
    print(f"Remaining: {len(cities) - agent.progress['completed'] - agent.progress['failed']}")
    
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
    
    print(f"\n{'='*60}")
    print("SEARCH COMPLETE")
    print(f"  Total cities: {agent.progress['total_cities']}")
    print(f"  Completed: {agent.progress['completed']}")
    print(f"  Failed: {agent.progress['failed']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
```

## Configuration

Add to `.env`:
```bash
SERPER_API_KEY=your_key_here
```

## Rate Limiting & Cost

- **Serper**: 2,500 free searches/month, then $50/5k searches
  - 980 cities × 4 queries avg = ~3,920 searches (~$42 if over free tier)
- **Gemini Flash Lite**: ~$0.04 per 1M tokens
  - 980 cities × 2 LLM calls × ~2k tokens = ~3.9M tokens (~$0.16)
- **Total estimated cost**: $0.16 - $42 depending on Serper usage

**Note**: For cities with existing plans, the script will still search and download candidates to `_incoming_for_review`. This ensures comprehensive coverage (cities may have multiple relevant documents). Manual review will determine which new documents to keep.

## Safeguards

1. **Review folder**: All downloads go to `_incoming_for_review` for manual validation
2. **Metadata tracking**: Every PDF gets a .json sidecar with source/reasoning
3. **Rate limiting**: 1 second delay between Serper API calls
4. **Max downloads**: Limit to top 3 candidates per city
5. **Progress tracking**: Save state after each city in `scripts/search_progress.json` (resume on failure)
6. **Duplicate handling**: If same filename exists in review folder, append `_new_1`, `_new_2`, etc. to avoid overwriting existing documents from prior runs or existing plans
7. **Quota monitoring**: Script halts immediately if Serper API quota is exhausted (HTTP 402/403) or rate limit hit (HTTP 429), displays summary of progress, and allows clean resume after adding credit

## Progress Tracking & Resume

The script maintains a progress file at `scripts/search_progress.json`:

```json
{
  "last_updated": "2026-05-03T14:23:45Z",
  "total_cities": 980,
  "completed": 45,
  "failed": 3,
  "cities_processed": {
    "1": {"status": "completed", "downloads": 2, "timestamp": "2026-05-03T14:20:12Z"},
    "5": {"status": "completed", "downloads": 1, "timestamp": "2026-05-03T14:21:34Z"},
    "78": {"status": "failed", "reason": "No results found", "timestamp": "2026-05-03T14:22:01Z"}
  }
}
```

**Resume Logic:**
1. On startup, load `search_progress.json` if it exists
2. Skip cities with `status: "completed"` or `status: "failed"`
3. Process remaining cities in order
4. Update progress file after each city (atomic write)
5. If interrupted, next run picks up from last completed city

**Manual Override:**
- Delete specific city from progress file to re-process
- Delete entire progress file to start fresh
- Use `--force-reprocess` flag to ignore progress file

## Next Steps

1. Review and approve this design
2. Refine the prompts if needed
3. Implement `auto_search_plans_gemini.py`
4. Test on 5-10 cities
5. Full run on all 928 cities
6. Manual review of `_incoming_for_review` folder
7. Move validated plans to final `plans/{ISO3}/{loc_id}/` locations
