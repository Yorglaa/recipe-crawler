# recipe-crawler

Crawl recipe websites, generate PDFs, and query them with a local RAG chatbot.

## Stack

- **Crawlers**: GraphQL API · REST API · Playwright · BeautifulSoup
- **Indexer**: pdfplumber · SQLite (metadata) · ChromaDB + sentence-transformers (semantic search)
- **Chat**: Gemini 2.5 Flash · Gemini 2.5 Flash Lite (fallback) · Groq llama-3.3-70b (fallback) · Gradio

## Sites supported

| Site | Recipes | Method |
|---|---|---|
| [viandesuisse.ch](https://viandesuisse.ch/recettes) | ~620 | GraphQL API + native PDF download |
| [migusto.migros.ch](https://migusto.migros.ch/fr/apercu-des-recettes) | ~7 950 | REST API + schema.org JSON-LD + weasyprint PDF |
| [qoqa.ch](https://www.qoqa.ch/fr/posts?kind=recipe) | variable | Playwright (JS-rendered list) + native PDF download |
| [fooby.ch](https://fooby.ch/fr/recettes.html) | ~8 800 | REST API + JSON sidecar + native PDF download |

---

## Requirements

- Python 3.11+
- [weasyprint](https://doc.courtbouillon.org/weasyprint/) (+ GTK on Windows — see below)
- [Playwright](https://playwright.dev/python/) with Chromium (for QoQa)
- A Gemini API key
- A Groq API key (optional — used as final fallback if Gemini is unavailable)

### Windows: weasyprint dependencies

weasyprint requires GTK3 runtime on Windows.
Install [GTK3 for Windows](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases) and make sure it is on your PATH before running.

---

## Installation

```bash
git clone https://github.com/Yorglaa/recipe-crawler.git
cd recipe-crawler

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux / macOS

pip install -r requirements.txt
playwright install chromium
```

---

## Configuration

Create a `.env` file at the root of the project:

```
GEMINI_API_KEY=your-gemini-key-here
GROQ_API_KEY=your-groq-key-here      # optional — final fallback only
```

`config.py` loads this file automatically via `python-dotenv`. The `.env` file is gitignored — never commit it.

All other settings are defined in `config.py` with sensible defaults:

| Setting | Default | Description |
|---|---|---|
| `CHROMA_DB_PATH` | `./chromadb` | ChromaDB vector store directory |
| `SQLITE_DB_PATH` | `./recipes.db` | SQLite metadata database |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Primary LLM |
| `GEMINI_FALLBACK_MODEL` | `gemini-2.5-flash-lite` | Gemini fallback if primary fails |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Final fallback (requires `GROQ_API_KEY`) |

---

## Usage

Crawling and indexing are independent operations.

### 1. Crawl — download PDFs

```
python main.py [--sites SITE [SITE ...]] [--limit N] [--renew] [--index]
```

| Option | Description |
|---|---|
| `--sites` | `viandesuisse`, `migusto`, `qoqa`, `fooby`, or `all` (default: `all`) |
| `--limit N` | Download at most **N new** recipes per site |
| `--renew` | Ignore local link/slug cache and re-fetch from source |
| `--index` | Also run the indexer (SQLite + ChromaDB) after crawling |

**Examples:**

```bash
# Download 10 new migusto recipes
python main.py --sites migusto --limit 10

# Crawl all sites with no limit
python main.py

# Crawl fooby and immediately index
python main.py --sites fooby --limit 100 --index
```

### 2. Index — build SQLite + ChromaDB

```
python index_recipes.py [--sites SITE [SITE ...]] [--limit N] [--sync-embeddings] [--backfill-text]
```

Reads existing PDFs from disk, extracts metadata, and loads everything into SQLite and ChromaDB. Safe to re-run — already-indexed recipes are skipped.

| Option | Description |
|---|---|
| `--sites` | Sites to index (default: all) |
| `--limit N` | Index at most **N new** recipes per site |
| `--sync-embeddings` | Add to ChromaDB any recipe already in SQLite but missing an embedding |
| `--backfill-text` | Populate `full_text` for already-indexed recipes that are missing it |

```bash
# Index everything
python index_recipes.py

# Index only the next 200 fooby recipes
python index_recipes.py --sites fooby --limit 200

# Fix a SQLite / ChromaDB mismatch without re-crawling
python index_recipes.py --sync-embeddings
```

> **Note:** If SQLite and ChromaDB counts diverge (e.g. after an interrupted indexation), `--sync-embeddings` iterates over all SQLite entries and upserts the missing ones into ChromaDB without touching the PDFs or re-parsing anything.

### 3. Chat — launch the Gradio interface

**Via the desktop shortcut** (Windows): double-click **start_app.bat** — the browser opens automatically.

**Or from the terminal:**

```bash
venv\Scripts\python.exe ui/app.py    # Windows
# python ui/app.py                   # Linux / macOS
```

The interface has two tabs:

#### Chat tab

Ask questions in natural language. The query router selects between SQL (duration/category filters), ChromaDB semantic search, or a combination, depending on the question.

Examples:
- *Quelque chose de cremeux avec du poulet*
- *Toutes les recettes de moins de 30 minutes*
- *Une bonne soupe reconfortante*
- *Des recettes de bœuf*
- *Combien de recettes connais-tu avec du porc ?*

**Progressive refinement** — The bot maintains active filters across turns. Each refinement narrows the current context without losing previous constraints:

```
"curry"                          → results filtered by keyword
"avec du poulet"                 → curry + poulet (SQL intersection)
"en moins de 45 min"             → curry + poulet + ≤ 45 min
"de chez migusto"                → curry + poulet + ≤ 45 min + site migusto
```

Filters reset only when you start a **Nouvelle conversation** or ask a clearly unrelated question.

**Numbered list navigation** — Results are presented as a numbered list. Reference any recipe by its number:

- *recette 3* — view full details
- *pdf de la recette 5* — open the PDF directly
- *détaille la 2* — show full preparation steps

**Count queries** — Ask how many recipes match a given ingredient, category, duration, or site. The bot returns the exact count (SQL-based) and a few examples, so you can then ask *donne moi la liste* or refine the search.

**Detail mode** — Once a recipe is listed, ask for full preparation steps in natural language:

- *Détaille moi la salade russe*
- *Donne moi la marche à suivre pour les boulettes de poulet au curry*
- *Comment préparer le tartare à l'italienne ?*

Detail mode works even after a **Nouvelle conversation** reset or when the recipe was mentioned in an earlier turn — the bot searches the full database by title keywords if the recipe is not in the current context.

**LLM fallback chain** — The chat uses three models in order:
1. **Gemini 2.5 Flash** (primary) — fast, high quality
2. **Gemini 2.5 Flash Lite** (fallback) — if primary is unavailable
3. **Groq llama-3.3-70b** (final fallback) — if both Gemini models fail; requires `GROQ_API_KEY`

All three models apply the same system prompt and formatting rules (numbered lists, French, source attribution).

**Nouvelle conversation** resets the chat history and all active filters, starting fresh without restarting the server.

Both tabs have a **Fermer l'application** button that shuts down the server.

#### Admin tab

Manage crawling, indexing, and maintenance without touching the terminal. All operations stream live logs.

| Section | What it does |
|---|---|
| **Status bar** | PDF counts per site · SQLite recipe count · ChromaDB embedding count |
| **Crawling** | Launch a crawl batch for selected sites; configurable limit, `--renew`, optional post-crawl indexing |
| **Indexation** | Index PDFs in configurable batch sizes; loop mode runs until no new recipes are found |
| **Nettoyage** | Delete DB + ChromaDB entries whose PDF no longer exists on disk |
| **Synchronisation** | Add ChromaDB embeddings for recipes already in SQLite but not yet vectorised |

---

### Production batch crawl — `run_batch.py`

Loops `main.py` automatically until a site is exhausted (no new recipes found).

```
python run_batch.py [--sites SITE [SITE ...]] [--batch-size N] [--delay SEC] [--renew]
```

| Option | Default | Description |
|---|---|---|
| `--sites` | `all` | Same choices as `main.py` |
| `--batch-size N` | `50` | Recipes per batch |
| `--delay SEC` | `30` | Seconds to wait between batches |
| `--renew` | — | Re-fetch link/slug list on first batch |

```bash
# Crawl fooby in batches of 100
python run_batch.py --sites fooby --batch-size 100 --delay 30

# After crawling, index everything
python index_recipes.py
```

---

## Cache (viandesuisse, migusto, qoqa, fooby)

Collecting the full recipe list can be slow or expensive:
- **viandesuisse** — 1 GraphQL POST call (~620 recipes); cached for convenience
- **migusto** — ~330 paginated API calls to enumerate ~7 950 slugs
- **qoqa** — full Playwright session clicking "Voir plus" until exhausted
- **fooby** — ~45 paginated API calls (~55 seconds, ~8 800 recipes)

The list is cached locally as JSON after the first fetch.

| Situation | Behaviour |
|---|---|
| No cache file yet | List is fetched from source and saved automatically |
| Cache exists | Loaded instantly from disk (no network call) |
| `--renew` passed | Cache is ignored, list is re-fetched and overwritten |

Cache files are stored in `./cache/` (gitignored):

```
cache/
├── viandesuisse_links.json  # list of ~620 recipe URLs
├── migusto_slugs.json       # list of ~7 950 recipe slugs
├── qoqa_links.json          # list of recipe URLs
└── fooby_links.json         # list of ~8 800 recipe records (id, url, title, duration...)
```

---

## PDF compression (qoqa)

qoqa PDFs are native high-resolution documents (~2 MB each). A Ghostscript-based
utility reduces them by ~89% without affecting text extraction or PDF display.

**Prerequisite:** [Ghostscript](https://ghostscript.com/releases/gsdnld.html) installed (Windows: `gswin64c` in PATH).

```bash
# Dry-run: measure gain without modifying files
python compress_pdfs.py --site qoqa --dry-run --limit 10

# Full compression with text verification (no backup kept)
python compress_pdfs.py --site qoqa --no-backup --verify
```

| Option | Description |
|---|---|
| `--site` | `qoqa`, `migusto`, `viandesuisse`, `fooby` |
| `--quality` | `/screen` (72 dpi) · `/ebook` (150 dpi, default) · `/printer` (300 dpi) |
| `--dry-run` | Simulate only — no files modified |
| `--limit N` | Process only N files |
| `--no-backup` | Replace in place (no `.bak` kept) |
| `--verify` | Check pdfplumber can still extract text after compression |

Typical result on qoqa: **4 346 MB → 468 MB (−89%)**.

---

## Project structure

```
recipe-crawler/
├── .env                    # API key (gitignored — create manually)
├── config.example.py       # Template showing all available settings
├── config.py               # Active config, loads .env (gitignored)
├── main.py                 # Crawl PDFs (+ optional --index)
├── index_recipes.py        # Standalone indexer: PDFs -> SQLite + ChromaDB
├── run_batch.py            # Production runner: loops main.py until done
├── compress_pdfs.py        # PDF size reduction via Ghostscript (~89% on qoqa)
├── start_app.bat           # Windows launcher (uses venv automatically)
│
├── crawlers/
│   ├── viandesuisse.py     # viandesuisse.ch crawler
│   ├── migusto.py          # migusto.migros.ch crawler
│   ├── qoqa.py             # qoqa.ch crawler (Playwright)
│   └── fooby.py            # fooby.ch crawler (REST API, ~8 800 recettes)
│
├── pipeline/
│   ├── database.py         # SQLite metadata store (thread-safe)
│   ├── embeddings.py       # ChromaDB vector store (offline, HF_HUB_OFFLINE=1)
│   └── chat.py             # Query router + progressive filters + Gemini/Groq chat
│
├── ui/
│   └── app.py              # Gradio interface: Chat + Admin tabs
│
├── cache/                  # Auto-generated link/slug lists (gitignored)
│   ├── viandesuisse_links.json
│   ├── migusto_slugs.json
│   ├── qoqa_links.json
│   └── fooby_links.json
│
└── pdfs/                   # Generated PDFs (gitignored)
    ├── viandesuisse/
    ├── migusto/
    ├── qoqa/
    └── fooby/
```

---

## Crawler details

### viandesuisse.ch
Uses the site's internal GraphQL endpoint (`POST /graphql`, `index_id: "recipe_index"`) to retrieve all ~620 recipe URLs in a single request. For each recipe, fetches the HTML page to extract the native print-PDF link (`/print/pdf/node/...`) and downloads it directly. The full link list is cached in `cache/viandesuisse_links.json`.

### migusto.migros.ch
Uses the internal REST API (`POST /.rest/recipes/v1`) to paginate through all ~7 950 recipes and collect slugs. For each slug, fetches the HTML page and extracts the `schema.org/Recipe` JSON-LD block (name, ingredients, steps, nutrition). Generates a formatted PDF with weasyprint and saves a `.json` sidecar alongside it for fast indexing.

### qoqa.ch
Uses Playwright (headless Chromium) to load the JS-rendered recipe list and click "Voir plus" until all links are collected. PDFs are downloaded directly from `https://download.qoqa.ch/fr/posts/{id}.pdf`.

### fooby.ch
Uses the internal REST API (`GET /hawaii_search.sri`) to collect all ~8 800 recipes in ~55 seconds (paginated via `start`/`num`, cached in `cache/fooby_links.json`). The API response includes recipe metadata (title, total duration, dietary category) saved as a `.json` sidecar alongside each PDF. PDFs are downloaded directly from the predictable URL `https://fooby.ch/bin/coop/fooby/pdfs/recipe.id-{id}.lang-fr.qty-4.pdf` — no Playwright needed. Download rate: ~1–2 seconds per recipe.

---

## Embedding model

The semantic search uses `all-MiniLM-L6-v2` (sentence-transformers) running fully offline. The model is downloaded once on first run to the default HuggingFace cache (`~/.cache/huggingface/`). Afterwards, `HF_HUB_OFFLINE=1` is set automatically so the app never makes network calls to HuggingFace.

---

## French text normalisation

All text comparisons (query routing, ingredient search, detail-mode title matching) go through a shared `_normalize()` function that:

- Converts typographic apostrophes (U+2019) to ASCII `'`
- Expands ligatures before ASCII stripping: `œ` → `oe`, `æ` → `ae`
- Strips diacritics via NFD decomposition + ASCII encode
- Lowercases

This ensures that `bœuf` / `boeuf` and `œuf` / `oeuf` are treated as identical by both the router and the SQL `LIKE` ingredient search (which also tries both forms).

---

## Query routing and search architecture

The chatbot routes each query through one of four paths before calling the LLM:

| Route | Trigger | Strategy |
|---|---|---|
| `sql_duration` | Duration only (*en moins de 30 min*) | SQL filter by `duration_minutes` |
| `sql_category` | Category keyword (*soupe*, *dessert*…) | SQL `LIKE` on `category` |
| `hybrid` | Ingredient + duration/category | SQL intersection, ranked by RAG |
| `rag` | Everything else | Semantic search (ChromaDB) + SQL ingredient boost |

### Progressive refinement (`_active_filters`)

Active filters persist across conversation turns as a structured dict:

```python
{
    "query":       "curry",        # base query for RAG ranking
    "ingredients": ["poulet"],     # SQL hard filters — intersection
    "max_minutes": 45,             # SQL hard filter
    "site":        "migusto",      # SQL hard filter
}
```

Each refinement message (starting with *avec*, *et du*, *en moins de*, *de chez*…) updates only the relevant field without clearing the others. A new unrelated question resets all filters.

### Ingredient matching

Ingredient filters use title-first matching: `search_by_title_keywords` is preferred over `search_by_ingredient` to avoid false positives (e.g. "fond de boeuf" in a pork recipe). The ingredient list is used as fallback only when no title match is found.
