# Document Organization Scripts

Scripts for collecting and organizing climate resilience planning documents.

## Setup

Required `.env` keys (varies by script):

```bash
# Supabase (fetch_reference_data.py)
PUBLIC_SUPABASE_URL=https://...
SUPABASE_SERVICE_ROLE_KEY=...

# LiteLLM proxy / Gemini (search + reorganize)
OPENAI_API_BASE=https://api.ai.it.cornell.edu
OPENAI_API_KEY=sk-...
LLM_MODEL=google.gemini-3.1-flash-lite-preview

# Serper (auto_search_plans_gemini_v2.py, retry_403_downloads.py)
SERPER_API_KEY=...
```

Run from the repository root, not from `scripts/`:

```bash
uv run --env-file .env scripts/<script>.py
```

All scripts use PEP 723 inline metadata for dependencies — `uv run` resolves them automatically.

## Workflow

1. **`fetch_reference_data.py`** — Pulls `cities.csv`, `countries.csv`, and `loc_id_mapping.json` from Supabase into `reference/`. Run first; refresh periodically.

2. **`reorganize_pdfs_improved.py`** — Moves loose PDFs into `plans/{ISO3}/{loc_id}/`. Two-stage matching: filename normalization first (free), Gemini extraction as fallback.

3. **`auto_search_plans_gemini_v2.py`** — For cities still missing plans, runs a four-stage agent: Gemini plans queries → Serper executes → Gemini scores results → top 3 PDFs land in `plans/_incoming_for_review/{ISO3}/{loc_id}/` with sidecar `.json` metadata. Resumable via `search_progress.json`; stops cleanly on Serper quota/rate-limit.

4. **`retry_403_downloads.py`** — Re-attempts URLs in `failed_downloads.json` whose error was a 403. Skips entries already downloaded successfully on a later pass.

5. **`add_language_to_existing.py`** — Backfills the `language` field on metadata sidecars for PDFs already in `plans/` and `plans/_incoming_for_review/`. Use `-v` for per-file diagnostics.

## Utilities

- **`move_unsorted.py`** — Sweeps any PDFs that aren't in `plans/{ISO3}/{loc_id}/` into a top-level `unsorted/` directory for manual triage.

## Generated artifacts

These files live alongside the scripts and are written/read at runtime; do not edit by hand:

- `search_progress.json` — per-city status for the search agent (resume marker)
- `failed_downloads.json` — append-only log of download failures (input to the retry script)
- `search_tasks.json` — output of an older script (kept for reference, not consumed)

## Archive

Older variants are in `scripts/archive/` with their own README. Not part of the current workflow.
