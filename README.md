# adaptbase-plans

A searchable archive of planning PDFs for the adaptbase project. Static site hosted on GitHub Pages at `plans.adaptbase.us`, with PDFs and embeddings stored in Supabase.

## Quick Start

### Prerequisites

- Python 3.11+ with [uv](https://github.com/astral-sh/uv) installed
- Node.js 18+ and npm
- Supabase project (reuse existing adaptbase project)
- OpenAI API key (or Cornell LiteLLM proxy access)

### Setup

1. **Clone and install dependencies**
   ```bash
   # Python tools
   uv venv
   uv sync
   
   # Astro site
   cd site
   npm install
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your Supabase and OpenAI credentials
   ```

3. **Run database migrations**
   ```bash
   # TODO: Add Supabase CLI commands once migrations are ready
   ```

## Architecture

```
Local PDF folder  (./plans/)
       │
       │  tools/migrate.py  (idempotent upload + thumbnail generation)
       ▼
Supabase Storage (bucket: plans/, bucket: thumbnails/)
       │
       │  tools/enrich.py   (LLM: title / year / doc_type / language)
       ▼
documents table  (metadata enriched)
       │
       │  adaptbase-import plans index  (extract → clean → chunk → embed)
       ▼
Supabase Postgres
   ├── documents  (metadata, full_text, indexed_at)
   └── chunks     (text + pgvector embeddings + tsvector full-text index)
       │
       │  match_hybrid() RPC (vector + keyword + RRF)
       ▼
Static site (GitHub Pages)
   ├── Gallery (paginated thumbnails)
   └── Search (hybrid results with snippets)
```

## Usage

### Uploading PDFs

Place PDFs in the configured `LOCAL_PDF_DIR` (default: `./pdfs/`), then:

```bash
uv run --env-file .env python tools/migrate.py
```

This will:
- Compute SHA256 hashes (for idempotency)
- Upload to Supabase Storage under `plans/`
- Generate thumbnails (first page, 400px wide JPEG)
- Insert metadata into `documents` table

### Enriching Metadata

After upload, enrich each document with LLM-extracted title, year, document type, and language:

```bash
uv run --env-file .env python tools/enrich.py [--force] [--limit N] [-c N]
```

Reads the first 5 pages of each local PDF and sends the text to the configured LLM (`$LLM_MODEL`, default: Gemini flash-lite via Cornell LiteLLM proxy). Writes back to `documents.title` and `documents.metadata` (`year`, `document_type`, `language` as ISO 639-1). Idempotent — skips docs where `metadata.enriched_at` is already set unless `--force`.

### Indexing Documents

After enriching, generate searchable chunks and embeddings:

```bash
cd ../adaptbase-importers
uv run --env-file .env adaptbase-import plans index [--language en]
```

Or run both steps together via the convenience script:

```bash
bash tools/run_pipeline.sh
```

#### Extraction

Uses **pymupdf4llm** (`_extract_file`) to convert each PDF page to markdown locally — no network call needed when the PDF is present in `./plans/`. If the local file is missing, falls back to downloading from Supabase Storage.

`pymupdf4llm` uses PyMuPDF's native text layer first. For image-only pages (scanned documents with no embedded text) it automatically falls back to **Tesseract OCR**. This covers the majority of plans in the corpus, but OCR quality varies significantly for low-resolution scans and non-Latin scripts.

#### Chunking

Section-aware markdown chunker (`chunk.py`):

- Splits at H1–H4 headers to track `section_path` hierarchy
- Tables (HTML or markdown) are extracted as dedicated `chunk_type="table"` chunks
- Text sections are packed to ≤ **1 400 tokens** (cl100k_base) at paragraph boundaries
- Adjacent text chunks within a section overlap by ≈ **150 tokens**

#### Embedding

Calls the LiteLLM proxy (`$OPENAI_API_BASE`) with model `$EMBEDDING_MODEL` (default: `text-embedding-3-small`), batched at 100 texts per request. Vectors are stored at **512 dimensions** in the `chunks.embedding` pgvector column.

#### Storage

Each chunk row stores: `document_id`, `chunk_idx`, `section_path`, `content`, `content_tokens`, `chunk_type`, `embedding`. After all chunks are written, `documents.full_text` is set to the concatenated markdown and `documents.indexed_at` is stamped.

### OCR Contingency: Mistral OCR

If hybrid search quality is poor for image-heavy or scanned PDFs — detectable by empty or garbled `full_text`, low chunk counts relative to page count, or search recall failures on known content — the fallback plan is to re-extract using **Mistral OCR** (`mistral-ocr-latest`), which produces substantially higher-quality structured markdown from scanned pages.

Mistral OCR is not available on the Cornell LiteLLM proxy and must be provisioned separately:

1. **Provision**: Create a Mistral AI account at [console.mistral.ai](https://console.mistral.ai), generate an API key, and add `MISTRAL_API_KEY` to `.env`.

2. **Identify candidates**: Query for low-quality extractions:
   ```sql
   select id, title, page_count, char_length(full_text)
   from documents
   where source = 'plan_crawler'
     and indexed_at is not null
     and char_length(coalesce(full_text, '')) < page_count * 200
   order by page_count desc;
   ```

3. **Re-extract**: Modify `extract.py` to add a `_extract_file_mistral(path)` function that base64-encodes the PDF and calls the Mistral OCR API (returns structured markdown per page). Wire it into `ingest_document` as an opt-in path gated on an env var (e.g. `USE_MISTRAL_OCR=1`).

4. **Re-index**: Re-run `adaptbase-import plans index --force` (or a targeted list of document IDs) to replace chunks with the higher-quality extraction.

### Local Development

```bash
cd site
npm run dev
```

Open http://localhost:4321 to preview the site.

Use VS Code's launch config ("Astro: Dev Server") or press F5 to start debugging.

### Building for Production

```bash
cd site
npm run build
```

Output in `site/dist/` is deployed automatically via GitHub Actions when pushed to `main`.

## Project Structure

```
adaptbase-plans/
├── site/                       # Astro static site
│   ├── src/
│   │   ├── pages/
│   │   │   ├── index.astro     # Gallery view
│   │   │   └── search.astro    # Search UI
│   │   ├── components/
│   │   └── styles/
│   │       └── tokens.css      # Design system from ontology
│   ├── public/
│   │   └── CNAME               # plans.adaptbase.us
│   └── package.json
├── tools/
│   ├── migrate.py              # Local → Supabase Storage + thumbnails
│   ├── enrich.py               # LLM metadata enrichment (title/year/lang)
│   ├── run_pipeline.sh         # enrich → index (convenience wrapper)
│   └── manifest.py             # Shared helpers (Supabase client, SHA256, etc.)
├── supabase/
│   ├── migrations/
│   │   └── 0001_init.sql       # Schema + indexes + RPC
│   └── functions/
│       └── embed-query/        # Edge Function for search
├── .github/workflows/
│   └── deploy.yml              # GitHub Pages deployment
├── .env.example
└── README.md
```

## Features

- **Hybrid Search**: Combines vector (semantic) and keyword (PostgreSQL full-text) search using Reciprocal Rank Fusion
- **Page-Level Linking**: Search results link directly to specific pages in PDFs
- **Idempotent Upload**: SHA256-based deduplication prevents re-uploading unchanged files
- **Thumbnail Generation**: Automatic first-page previews for gallery view
- **Themed UI**: Matches adaptbase ontology design system

## Custom Domain Setup

Domain: `plans.adaptbase.us`

1. **GitHub Settings**: Pages → Custom domain → `plans.adaptbase.us`
2. **DNS**: Add CNAME record pointing `plans` to `<github-org>.github.io`
3. **Verify**: Enable "Enforce HTTPS" after cert provisions

## Tech Stack

- **Frontend**: Astro (static site generation)
- **Storage**: Supabase Storage (PDFs + thumbnails)
- **Database**: Supabase Postgres with pgvector + full-text search
- **Embeddings**: OpenAI `text-embedding-3-small` (512 dimensions)
- **Hosting**: GitHub Pages
- **Tools**: Python 3.11+ with uv, PyMuPDF, tiktoken

## Development

### Running Tests

```bash
# TODO: Add test commands
```

### Code Quality

```bash
# Format and lint Python
uv run ruff check . --fix
uv run ruff format .

# Format TypeScript/Astro
cd site
npm run format
```

## Deployment

Automatic via GitHub Actions on push to `main`:

1. Builds Astro site (`npm run build`)
2. Uploads `site/dist/` to GitHub Pages
3. Deploys to `plans.adaptbase.us`

## Outstanding Tasks

- [ ] Evaluate hybrid search quality after full index run — identify scanned/OCR-poor docs
- [ ] Provision Mistral OCR if re-extraction is needed (see contingency plan above)
- [ ] Add `--force` / targeted re-index path to `adaptbase-import plans index`
- [ ] Set up GitHub Actions deployment
- [ ] Configure DNS for custom domain (`plans.adaptbase.us`)

## License

Part of the adaptbase project. See main project for license details.
