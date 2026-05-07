#!/usr/bin/env bash
# Runs enrich → index sequentially. Called after migrate.py completes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/tmp/pipeline-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$ROOT/tmp"

echo "=== Pipeline started at $(date) ===" | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "--- Step 1: enrich (title + year + doc_type + language via LLM) ---" | tee -a "$LOG"
cd "$ROOT"
uv run --env-file .env python tools/enrich.py --concurrency 20 2>&1 | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "--- Step 2: chunk + embed (English docs) ---" | tee -a "$LOG"
cd /Users/anthonytownsend/code/_dev/adaptbase/adaptbase-importers
uv run --env-file .env adaptbase-import plans index --language en 2>&1 | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "=== Pipeline complete at $(date) ===" | tee -a "$LOG"
