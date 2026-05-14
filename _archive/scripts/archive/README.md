# Archived Scripts

These scripts have been superseded by newer ones in `scripts/`. They are kept
here for historical reference and are not part of the current workflow.

| Script | Status | Replacement |
|---|---|---|
| `auto_search_plans.py` | Earliest search prototype, uses Anthropic SDK with built-in web search | `auto_search_plans_gemini_v2.py` |
| `auto_search_plans_gemini.py` | v1 of the Serper + Gemini search agent | `auto_search_plans_gemini_v2.py` |
| `reorganize_pdfs.py` | Original reorganizer (filename matching only) | `reorganize_pdfs_improved.py` |
| `search_missing_plans.py` | Wrote `search_tasks.json` for manual follow-up — no longer consumed by anything | The v2 search agent reads `reference/cities.csv` directly |
| `create_city_folders.py` | Pre-created an empty folder per city | Folders are now created on demand by the search agent and reorganizer |

If you need any of these again, copy them back up one directory before running.
