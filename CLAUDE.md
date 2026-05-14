# adaptbase-plans

## Purpose

Local PDF storage for climate adaptation plans. Sibling repo to `adaptbase-core` (the production pipeline). Legacy prototype scripts have been archived to `_archive/`.

## Structure

```
adaptbase-plans/
├── plans/              # PDFs filed by ISO3/loc_id
├── reference/          # countries.csv, cities.csv
├── _archive/           # Legacy scripts and tools (deprecated)
├── _planning/          # Design docs
└── src/app/            # Next.js frontend (plan browser)
```

## Important Constraints

- **Do not use subagents (Agent tool).** This environment routes API calls through a LiteLLM proxy. Subagents cannot inherit the proxy configuration and will fail with 401 errors when they try to reach Anthropic's endpoint directly. Always do exploration, search, and research inline instead of delegating to Explore, general-purpose, or other agent types.
- **Always** use `uv` for Python package management
- **Always** run `uv run ruff check . --fix && uv run ruff format .` before committing
- **Never** commit `.env` files

## Related Repositories

- **adaptbase-core**: Monorepo with importers, schema, admin, researcher packages
- **adaptbase-frontend**: Next.js App Router on Vercel
- **adaptbase-ontology**: Domain model (submodule in adaptbase-core)
