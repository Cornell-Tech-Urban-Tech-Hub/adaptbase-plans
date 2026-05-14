"""
Document enrichment tool for adaptbase-plans.

Reads first 3 pages of each PDF, sends text to an LLM, and writes back:
  - title (updated in the documents.title column)
  - year, document_type, language, enriched_at (merged into metadata JSONB)

Idempotent: skips docs where metadata.enriched_at is already set unless --force.

Usage:
    uv run --env-file .env python tools/enrich.py [--force] [--limit N] [-c N]
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
from dotenv import load_dotenv
from openai import AsyncOpenAI
from tqdm.asyncio import tqdm_asyncio

sys.path.append(str(Path(__file__).parent))

from manifest import get_supabase_client

load_dotenv()

_FALLBACK_MODEL = "google.gemini-3-flash-preview"
DEFAULT_CONCURRENCY = 20
MAX_TEXT_CHARS = 8000
DEFAULT_PAGES = 5

PROMPT_PREFIX = "\n".join(
    [
        "Analyze this climate/urban planning document text (first pages only)."
        " Return ONLY valid JSON with no markdown fences or extra text:",
        '{"title": "<clean descriptive title>", "year": <4-digit int or null>,'
        ' "document_type": "<type>", "language": "<ISO 639-1 code>"}',
        "",
        "document_type values (pick exactly one):",
        '- "plan"        → a forward-looking climate adaptation, resilience,'
        " or sustainability plan or strategy",
        '- "legislation" → a law, act, statute, or bill',
        '- "ordinance"   → a local government ordinance, resolution, or bylaw',
        '- "report"      → a retrospective report, assessment, inventory,'
        " or evaluation (not forward-looking)",
        '- "other"       → anything else (manual, handbook, guidance, etc.)',
        "",
        "language: ISO 639-1 two-letter code for the document's primary language"
        ' (e.g. "en", "fr", "es", "pt", "ar"). Use "unknown" only if the text'
        " is entirely unreadable (scanned image with no text).",
        "",
        "Title rules:",
        "- Write a clean, human-readable title"
        ' (e.g. "Cape Town Climate Action Plan 2022–2030")',
        "- Fix garbled PDF metadata that looks like filenames, CamelCase, or ALL CAPS",
        "- Include the city/region and year range if clearly stated",
        "- Keep it concise (under 100 characters)",
        "- If you cannot determine a clean title, use the original",
        "",
        "Text:",
        "",
    ]
)


def extract_text(
    pdf_path: Path,
    max_pages: int = DEFAULT_PAGES,
    max_chars: int = MAX_TEXT_CHARS,
) -> str:
    try:
        doc = pymupdf.open(pdf_path)
        pages_to_read = min(len(doc), max_pages)
        parts = []
        total = 0
        for i in range(pages_to_read):
            page_text = doc[i].get_text()
            remaining = max_chars - total
            if remaining <= 0:
                break
            parts.append(page_text[:remaining])
            total += len(page_text)
            if total >= max_chars:
                break
        doc.close()
        return "\n".join(parts).strip()
    except Exception as e:
        return f"[error reading pdf: {e}]"


def parse_llm_response(content: str) -> dict:
    content = content.strip()
    content = re.sub(r"^```[a-z]*\n?", "", content)
    content = re.sub(r"\n?```$", "", content)
    content = content.strip()

    data = json.loads(content)

    if "title" not in data or "document_type" not in data:
        msg = f"Missing required fields in LLM response: {data}"
        raise ValueError(msg)

    year = data.get("year")
    if year is not None:
        year = int(year)
        if year < 1900 or year > 2100:
            year = None

    valid_types = {"plan", "legislation", "ordinance", "report", "other"}
    doc_type = str(data.get("document_type", "other")).lower()
    if doc_type not in valid_types:
        doc_type = "other"

    language = str(data.get("language", "")).lower().strip()
    if not language or language == "unknown":
        language = None

    return {
        "title": str(data["title"]).strip(),
        "year": year,
        "document_type": doc_type,
        "language": language,
    }


async def enrich_document(
    doc: dict,
    client: AsyncOpenAI,
    supabase,
    sem: asyncio.Semaphore,
    model: str,
    force: bool,
    max_pages: int = DEFAULT_PAGES,
) -> str:
    """Returns 'ok', 'skipped', or 'error:<msg>'."""
    doc_id = doc["id"]
    existing_meta = doc.get("metadata") or {}

    if not force and existing_meta.get("enriched_at"):
        return "skipped"

    async with sem:
        storage_path = doc.get("storage_path", "")
        local_path = Path("./") / storage_path

        if local_path.exists():
            text = extract_text(local_path, max_pages=max_pages)
        else:
            text = ""

        if not text or text.startswith("[error"):
            return f"error:no_text:{storage_path}"

        prompt = PROMPT_PREFIX + text

        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=256,
                ),
                timeout=30,
            )
            raw = response.choices[0].message.content or ""
            result = parse_llm_response(raw)
        except Exception as e:
            return f"error:llm:{e}"

        new_meta = {
            **existing_meta,
            "year": result["year"],
            "document_type": result["document_type"],
            "enriched_at": datetime.now(UTC).isoformat(),
        }
        if result.get("language"):
            new_meta["language"] = result["language"]

        try:
            supabase.table("documents").update(
                {"title": result["title"], "metadata": new_meta}
            ).eq("id", doc_id).execute()
        except Exception as e:
            return f"error:db:{e}"

        return "ok"


async def run(
    force: bool,
    limit: int | None,
    concurrency: int,
    model: str,
    max_pages: int,
) -> None:
    print("Connecting to Supabase...")
    supabase = get_supabase_client()

    print("Fetching documents...")
    if limit:
        docs = (
            supabase.table("documents")
            .select("id, title, storage_path, metadata")
            .eq("source", "plan_crawler")
            .limit(limit)
            .execute()
            .data
        )
    else:
        docs = []
        page_size = 1000
        offset = 0
        while True:
            page = (
                supabase.table("documents")
                .select("id, title, storage_path, metadata")
                .eq("source", "plan_crawler")
                .range(offset, offset + page_size - 1)
                .execute()
                .data
            )
            docs.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
    print(f"  Fetched {len(docs)} documents")

    if not force:
        unenriched = [
            d for d in docs if not (d.get("metadata") or {}).get("enriched_at")
        ]
        already = len(docs) - len(unenriched)
        print(f"  Unenriched: {len(unenriched)} (skipping {already} already enriched)")
        docs = unenriched
    else:
        print("  --force: re-enriching all documents")

    if not docs:
        print("Nothing to enrich.")
        return

    base_url = os.getenv("OPENAI_API_BASE") or os.getenv("LITELLM_PROXY_URL")
    api_key = (
        os.getenv("OPENAI_API_KEY") or os.getenv("LITELLM_API_KEY") or "placeholder"
    )

    if not base_url:
        print("ERROR: OPENAI_API_BASE or LITELLM_PROXY_URL must be set in .env")
        sys.exit(1)

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    sem = asyncio.Semaphore(concurrency)

    print(f"\nEnriching {len(docs)} docs (model={model}, concurrency={concurrency})...")

    tasks = [
        enrich_document(doc, client, supabase, sem, model, force, max_pages)
        for doc in docs
    ]
    results = await tqdm_asyncio.gather(*tasks, desc="Enriching")

    ok = sum(1 for r in results if r == "ok")
    skipped = sum(1 for r in results if r == "skipped")
    errors = [r for r in results if r not in ("ok", "skipped")]

    print("\nDone.")
    print(f"  OK:      {ok}")
    print(f"  Skipped: {skipped}")
    print(f"  Errors:  {len(errors)}")
    if errors:
        print("\nError details (first 20):")
        for e in errors[:20]:
            print(f"  {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Enrich document titles and metadata via LLM"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-enrich already-enriched documents"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Process only N documents (for testing)"
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Max concurrent LLM calls (default {DEFAULT_CONCURRENCY})",
    )
    _default_model = os.getenv("LLM_MODEL", _FALLBACK_MODEL)
    parser.add_argument(
        "--model",
        default=_default_model,
        help=f"LLM model name (default: $LLM_MODEL or {_FALLBACK_MODEL})",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_PAGES,
        help=f"Pages to read per PDF (default {DEFAULT_PAGES})",
    )
    args = parser.parse_args()

    asyncio.run(
        run(
            force=args.force,
            limit=args.limit,
            concurrency=args.concurrency,
            model=args.model,
            max_pages=args.max_pages,
        )
    )
