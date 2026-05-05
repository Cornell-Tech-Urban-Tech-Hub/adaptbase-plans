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
Local PDF folder
       │
       │  tools/migrate.py  (idempotent upload)
       ▼
Supabase Storage (public bucket: plans/)
       │
       │  tools/index.py    (extract → chunk → embed)
       ▼
Supabase Postgres
   ├── documents (metadata + thumbnails)
   └── chunks (text + pgvector embeddings + tsvector)
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

### Indexing Documents

After uploading, generate searchable chunks:

```bash
uv run --env-file .env python tools/index.py
```

This will:
- Extract text per page
- Chunk with ~500 tokens, 50-token overlap
- Embed using OpenAI `text-embedding-3-small` @ 512 dims
- Bulk insert into `chunks` table with page tracking

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
│   ├── migrate.py              # Local → Supabase Storage
│   ├── index.py                # Chunking + embeddings
│   ├── manifest.py             # Shared helpers
│   └── requirements.txt
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

- [ ] Finalize Supabase schema migration
- [ ] Implement shared Python helpers
- [ ] Build migration and indexing tools
- [ ] Create embed-query Edge Function
- [ ] Build Astro gallery page
- [ ] Build Astro search page
- [ ] Set up GitHub Actions deployment
- [ ] Configure DNS for custom domain

## License

Part of the adaptbase project. See main project for license details.
