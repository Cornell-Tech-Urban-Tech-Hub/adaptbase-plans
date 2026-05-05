# adaptbase-plans — Project Spec & Handoff

A searchable archive of planning PDFs for the adaptbase project. Static site hosted on GitHub Pages at `plans.adaptbase.us`, with PDFs and embeddings stored in Supabase.

---

## Implementation Progress

### Architecture Decision: Schema lives in adaptbase-schema

All database migrations live in `adaptbase-schema/supabase/migrations/`. The plans site is built on the shared `documents` / `chunks` tables with `source='plans'`. Plans-specific columns (storage_path, thumbnail_path, page_count, file_size, sha256, indexed_at, page_number on chunks), hybrid search RPCs, storage buckets, and public-read RLS all live in a single consolidation migration.

### Local Testing
- [x] Install npm dependencies (`cd site && npm install`)
- [x] Start dev server (`npm run dev` or press F5 in VS Code)
- [x] Verify gallery page loads (shows empty state, API key warning expected before migration)
- [x] Verify search page loads
- [x] Test navigation between pages (header nav functional)
- [x] Add Hero component with headline, stats, and search integration
- [x] Add About button and modal to header
- [x] Integrate Hero into index.astro and search.astro pages
- [x] Filter frontend queries by source='plans'
- [x] Wire graceful fallback: title-based search when chunks/embeddings unavailable

### Database Setup (in adaptbase-schema)
- [x] Write consolidation migration `20260505100000_add_plans_support.sql`
- [x] Delete duplicate migration from adaptbase-plans
- [x] Apply migration to Supabase (`cd adaptbase-schema && supabase db push`)
- [x] Verify plans columns added to documents/chunks
- [x] Verify storage buckets created (plans, thumbnails)
- [x] Verify indexes created (HNSW, GIN, sha256 unique)
- [x] Verify public-read RLS active for source='plans'
- [ ] Test `get_document_stats` RPC
- [ ] Regenerate shared types (`cd adaptbase-schema/packages/ts && npm run generate`)

### Data Pipeline (Phase 1: Upload only)
- [x] Rewrite `tools/migrate.py` to use shared schema (UUID PKs, IngestRun, source='plans')
- [x] PDFs are in `./plans/` directory (2,885 PDFs organized as `{ISO3}/{loc_id}/filename.pdf`)
- [ ] Test run with ~10 PDFs (`LOCAL_PDF_DIR=./plans uv run --env-file .env python tools/migrate.py`)
- [ ] Verify PDFs uploaded to `plans` storage bucket
- [ ] Verify thumbnails generated in `thumbnails` bucket
- [ ] Verify document rows in database with indexed_at=NULL
- [ ] Verify gallery page renders uploaded documents
- [ ] Verify title-based search returns matching documents
- [ ] Full run: upload all 2,885 PDFs

### Data Pipeline (Phase 2: Chunking + Embedding)
Design chunking/embedding strategy before building — this unlocks hybrid search and knowledge graph extraction.
- [ ] Design chunking strategy (granularity, page-anchoring, NER/triplet compatibility)
- [ ] Build `tools/index.py` (chunks → embeddings → upsert)
- [ ] Deploy `embed-query` Edge Function
- [ ] Switch search page from title fallback back to hybrid search (already structured in search.astro)

### Phase 3: Vector + Hybrid Search
- [ ] Implement hybrid search UI (replace title fallback with `match_hybrid` RPC)
- [ ] Add keyword/semantic/both badges to search results
- [ ] Add snippet highlighting
- [ ] Filters by jurisdiction/year/doc_type

### Phase 4: Knowledge Graph Extraction
→ Next major phase — design and implement NER/triplet extraction pipeline to populate adaptbase ontology entities from plan text. See adaptbase-schema ontology drift check work as prerequisite.

### Deployment
- [x] Configure GitHub repository secrets (PUBLIC_SUPABASE_URL, PUBLIC_SUPABASE_ANON_KEY)
- [x] Push to main branch
- [x] Verify GitHub Actions workflow runs
- [x] Configure custom domain DNS (plans.adaptbase.us)
- [x] Verify site accessible at plans.adaptbase.us

---

## Project Overview

**Goal:** Build a public website that lets users browse and search a corpus of ~1,000 planning PDFs (~15GB total) using both keyword and semantic (vector) search.

**Constraints:**
- Static frontend hosted on GitHub Pages
- Custom domain: `plans.adaptbase.us`
- Themed to match the existing adaptbase ontology site (reference needed)
- Supabase for storage, chunks, and embeddings (already provisioned for the broader adaptbase project)
- Repo name: `adaptbase-plans`

**Note:** Adaptbase / Resilience Scanner uses **Postgres (Supabase)**, NOT Neo4j. Do not introduce property-graph databases.

---

## Architecture

```
Local PDF folder
       │
       │  tools/migrate.py  (one-time + incremental sync, sha256-based idempotency)
       ▼
Supabase Storage (public bucket: `plans/`)
       │
       │  tools/index.py    (extract → chunk → embed → upsert)
       ▼
Supabase Postgres
   ├── documents table (metadata + thumbnail path)
   └── chunks table   (text + pgvector embedding + tsvector for keyword)
       │
       │  match_hybrid() RPC (vector + keyword + RRF fusion)
       ▼
Static site (GitHub Pages, plans.adaptbase.us)
   ├── Gallery view (paginated thumbnail grid)
   └── Search view (hybrid results with snippets + page links)
```

### Why this shape

- **Supabase as single source of truth.** PDFs, chunks, and embeddings all live together. One set of credentials, one URL pattern, built-in image transforms for thumbnails.
- **Static frontend.** GitHub Pages, no servers to maintain. Frontend talks to Supabase via anon key + RLS (read-only public access).
- **Two scripts, manual trigger.** At ~1000 docs, a `repository_dispatch` / cron pipeline is overkill. Scripts are idempotent — run them when you have new PDFs.

---

## Repository Layout

```
adaptbase-plans/
├── site/                       # static frontend (Astro recommended)
│   ├── src/
│   │   ├── pages/
│   │   │   ├── index.astro     # gallery
│   │   │   └── search.astro    # search UI
│   │   ├── components/
│   │   └── styles/
│   ├── public/
│   │   └── CNAME               # contains: plans.adaptbase.us
│   └── astro.config.mjs
├── tools/
│   ├── migrate.py              # local → Supabase Storage
│   ├── index.py                # storage → chunks + embeddings
│   ├── manifest.py             # shared helpers (hashing, etc.)
│   └── requirements.txt
├── supabase/
│   ├── migrations/
│   │   └── 0001_init.sql       # schema + RPCs + indexes + RLS
│   └── seed.sql                # optional
├── .github/workflows/
│   └── deploy.yml              # builds site, deploys to Pages
├── .env.example
└── README.md
```

---

## Database Schema

```sql
-- Extensions
create extension if not exists vector;
create extension if not exists pg_trgm;

-- Documents (one row per PDF)
create table documents (
  id              bigserial primary key,
  filename        text not null,
  title           text,                       -- derived from PDF metadata or filename
  sha256          text not null unique,       -- for idempotent migration
  page_count      int,
  file_size       bigint,
  storage_path    text not null,              -- e.g. "plans/2024/miami-cra.pdf"
  thumbnail_path  text,                       -- e.g. "thumbnails/2024/miami-cra.jpg"
  uploaded_at     timestamptz default now(),
  indexed_at      timestamptz,                -- null until chunks generated
  metadata        jsonb default '{}'::jsonb   -- jurisdiction, year, doc_type, etc.
);

create index on documents (indexed_at);
create index on documents using gin (metadata);

-- Chunks (many per document)
create table chunks (
  id            bigserial primary key,
  document_id   bigint not null references documents(id) on delete cascade,
  chunk_index   int not null,
  page_number   int,
  content       text not null,
  embedding     vector(512),                  -- text-embedding-3-small @ 512 dims
  tsv           tsvector generated always as (to_tsvector('english', content)) stored,
  unique (document_id, chunk_index)
);

-- Indexes
create index chunks_embedding_hnsw on chunks
  using hnsw (embedding vector_cosine_ops)
  with (m = 16, ef_construction = 64);

create index chunks_tsv_gin on chunks using gin (tsv);
create index chunks_document_id on chunks (document_id);

-- Hybrid search RPC (vector + keyword + RRF fusion)
create or replace function match_hybrid(
  query_embedding vector(512),
  query_text text,
  match_count int default 20,
  rrf_k int default 60
)
returns table (
  chunk_id bigint,
  document_id bigint,
  page_number int,
  content text,
  score float,
  vector_rank int,
  keyword_rank int
)
language sql stable as $$
  with vector_search as (
    select id, row_number() over (order by embedding <=> query_embedding) as rank
    from chunks
    order by embedding <=> query_embedding
    limit match_count * 2
  ),
  keyword_search as (
    select id, row_number() over (order by ts_rank_cd(tsv, websearch_to_tsquery('english', query_text)) desc) as rank
    from chunks
    where tsv @@ websearch_to_tsquery('english', query_text)
    limit match_count * 2
  ),
  fused as (
    select
      coalesce(v.id, k.id) as chunk_id,
      coalesce(1.0 / (rrf_k + v.rank), 0) + coalesce(1.0 / (rrf_k + k.rank), 0) as score,
      v.rank::int as vector_rank,
      k.rank::int as keyword_rank
    from vector_search v
    full outer join keyword_search k on v.id = k.id
  )
  select
    f.chunk_id,
    c.document_id,
    c.page_number,
    c.content,
    f.score,
    f.vector_rank,
    f.keyword_rank
  from fused f
  join chunks c on c.id = f.chunk_id
  order by f.score desc
  limit match_count;
$$;

-- RLS: public read-only
alter table documents enable row level security;
alter table chunks enable row level security;

create policy "public read documents" on documents for select using (true);
create policy "public read chunks" on chunks for select using (true);
```

---

## Tooling

### `tools/migrate.py` — Local → Supabase Storage

**Behavior:**
1. Walk a configured local folder for `*.pdf`
2. For each file:
   - Compute sha256
   - Check `documents` table — skip if sha256 already present
   - Upload to Supabase Storage under `plans/<relative_path>`
   - Generate thumbnail (first page → 400px-wide JPEG via PyMuPDF) → upload to `thumbnails/<relative_path>.jpg`
   - Extract title from PDF metadata, fall back to filename
   - Insert row into `documents`
3. Print summary: uploaded N, skipped M, failed K

**Dependencies:** `supabase`, `pymupdf`, `python-dotenv`, `tqdm`

### `tools/index.py` — Documents → Chunks + Embeddings

**Behavior:**
1. Query `documents where indexed_at is null` (or with `--reindex` flag, all docs)
2. For each:
   - Download PDF from Storage
   - Extract text per page with PyMuPDF
   - Chunk: ~500 tokens with 50-token overlap, preserving page boundaries (track `page_number` per chunk)
   - Embed in batches of 100 via OpenAI `text-embedding-3-small` with `dimensions=512`
   - Bulk insert chunks
   - Update `documents.indexed_at = now()`
3. Print summary: indexed N docs, M chunks created

**Dependencies:** `supabase`, `pymupdf`, `openai`, `tiktoken`, `python-dotenv`, `tqdm`

### `tools/manifest.py` — Shared helpers

- `sha256_file(path) -> str`
- `extract_title(pdf) -> str`
- `chunk_text(pages, max_tokens=500, overlap=50) -> list[Chunk]`
- Supabase client factory with service-role key (for tools only — frontend uses anon)

---

## Frontend (Astro, static)

### Pages

**`/` — Gallery**
- Paginated grid of thumbnails (e.g. 24 per page)
- Each card: thumbnail, title, page count, file size
- Click → opens PDF in new tab (Supabase Storage public URL with `#page=1`)
- Optional filters: jurisdiction, year, doc type (driven by `documents.metadata`)

**`/search` — Hybrid search**
- Search input (URL query param `?q=...`)
- On submit:
  1. Embed query via small Netlify/Supabase Edge Function (or Supabase Edge Function `embed-query`) — keeps OpenAI key server-side
  2. Call `match_hybrid` RPC with query text + embedding
  3. Render results: thumbnail + title + matched snippet (with highlighted keywords) + "Page N" link → opens PDF at that page
- Show small badges: `keyword match`, `semantic match`, or both
- Quoted queries (`"exact phrase"`) → switch to keyword-only mode (skip embedding, use plain `tsv @@ phraseto_tsquery`)

### Theming

**TODO:** Reference the adaptbase ontology site for visual language. Need:
- Color palette (CSS custom properties)
- Typography (font families, scale, weights)
- Component patterns (cards, buttons, inputs)
- Layout density (spacing scale)

Once the reference is available, port the design tokens into `site/src/styles/tokens.css` and match component styles.

### Why Astro

- Truly static output (great for GitHub Pages)
- Content-heavy sites are its sweet spot
- Easy to add islands of interactivity (search UI) without shipping a full SPA
- Zero JS by default on the gallery page

---

## Deployment: GitHub Pages + Custom Domain

### Custom domain: `plans.adaptbase.us`

**1. In GitHub repo:**
- Settings → Pages → source = GitHub Actions (or branch + folder)
- Custom domain: `plans.adaptbase.us` → Save
- Creates `CNAME` file (also include in `site/public/CNAME` so build output preserves it)
- Enable "Enforce HTTPS" once cert provisions

**2. DNS (provider TBD — Cloudflare / Route53 / Namecheap / etc.):**
```
Type:  CNAME
Name:  plans
Value: <github-username-or-org>.github.io
TTL:   3600
```

If using Cloudflare: set to "DNS only" (gray cloud) initially. Switch to proxied only after GitHub cert provisions.

**3. Domain verification (recommended):**
- GitHub profile → Settings → Pages → Add `adaptbase.us`
- Add the provided TXT record to DNS
- Verify — prevents subdomain takeover on `*.adaptbase.us`

**4. Verify:**
```bash
dig plans.adaptbase.us +short
curl -I https://plans.adaptbase.us
```

### GitHub Actions deploy

`.github/workflows/deploy.yml` should:
- Trigger on push to `main` (paths: `site/**`)
- Set up Node, install deps in `site/`
- Run `npm run build`
- Upload `site/dist/` as Pages artifact
- Deploy to Pages

Standard Astro + GitHub Pages workflow — Astro docs have a copy-paste version.

---

## Embedding Query (Server-side)

To avoid exposing the OpenAI API key in the browser:

**Option A: Supabase Edge Function** (preferred — keeps everything in Supabase)

`supabase/functions/embed-query/index.ts`:
- Accepts `{ query: string }`
- Calls OpenAI embeddings API with `text-embedding-3-small`, `dimensions=512`
- Returns `{ embedding: number[] }`
- Frontend calls via `supabase.functions.invoke('embed-query', { body: { query } })`

**Option B: Netlify Function** (if Netlify ends up in the stack)

Identical behavior, Node runtime.

Pick A unless there's a reason not to — keeps the stack tighter.

---

## Environment Variables

`.env.example`:
```
# Tools (server-side, service role)
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
OPENAI_API_KEY=sk-...

# Local
LOCAL_PDF_DIR=./pdfs
SUPABASE_STORAGE_BUCKET=plans

# Frontend (committed; anon key is safe with RLS)
PUBLIC_SUPABASE_URL=https://xxx.supabase.co
PUBLIC_SUPABASE_ANON_KEY=eyJ...
```

---

## Outstanding Decisions

These need answers before / during implementation:

1. **Local PDF folder path** — default to `./pdfs/` unless specified
2. **Supabase project** — reuse the existing adaptbase project, or new project for plans? (Recommend: new schema in same project, e.g. `plans` schema)
3. **Embedding model** — confirm OpenAI `text-embedding-3-small` @ 512 dims (cheap, good enough). Alternatives: `text-embedding-3-large` @ 1536 (better, more expensive) or self-hosted `gte-small`
4. **Thumbnail size** — default 400px wide JPEG @ 80% quality
5. **Ontology site reference** — URL or screenshots needed to drive theming
6. **PDF viewer** — link out to PDF (browser handles) vs. embedded PDF.js (nicer but more work). Recommend link-out for v1.
7. **Metadata schema** — what fields belong in `documents.metadata`? (jurisdiction, year, doc_type, source agency, etc.)
8. **DNS provider** for `adaptbase.us` — affects exact UI steps for the CNAME

---

## Implementation Order

Suggested sequence for Claude Code:

1. **Repo scaffold** — directory structure, `.env.example`, `README.md`, `.gitignore`
2. **Supabase migration** — `0001_init.sql` with schema, indexes, RPCs, RLS
3. **`tools/manifest.py`** — shared helpers (hashing, chunking, Supabase client)
4. **`tools/migrate.py`** — local → Storage with thumbnail generation
5. **`tools/index.py`** — chunk + embed + upsert
6. **Edge function** — `embed-query`
7. **Astro site scaffold** — gallery page first (no search dependency)
8. **Search page** — hybrid query UI + RPC integration
9. **Theming pass** — once ontology site reference is provided
10. **GitHub Pages deploy workflow** + DNS setup

---

## Earlier Discussion / Rejected Alternatives

For context — these were considered and ruled out, don't re-litigate:

- **Static JSON + transformers.js in browser** — works for <50k chunks; this corpus may exceed that, and Supabase is already in the stack
- **PDFs in GitHub (LFS or Releases)** — 15GB hits LFS bandwidth costs; Releases works but inconvenient. S3/Supabase Storage is cleaner.
- **S3 instead of Supabase Storage** — viable, but Supabase Storage means one platform, built-in image transforms, and aligns with chunks/embeddings already going there
- **GitHub Actions auto-sync** — overkill for ~1000 docs and infrequent additions. Manual script run is fine.
- **Pure vector search (no keyword)** — keyword wins for exact-phrase queries common in planning docs ("Section 8", street names, ordinance numbers). Hybrid via RRF is strictly better.
- **Neo4j / property graph** — explicitly out of scope for adaptbase

---

## Appendix: Hybrid Search Notes

**Reciprocal Rank Fusion (RRF):**
```
score(doc) = Σ 1 / (k + rank_in_each_list)
```
- `k = 60` is the conventional default
- Doesn't require score normalization between vector and keyword (which is the actually hard part of hybrid)
- Implemented server-side in `match_hybrid` RPC above

**UX touches that matter:**
- Highlight matched keywords in snippets
- Badge results as `keyword`, `semantic`, or both
- Quoted queries → keyword-only
- Snippets centered on matched span (~100 chars each side)
- Filters (jurisdiction, year) often beat ranking tweaks

---

*End of spec. Hand this to Claude Code to scaffold and implement.*