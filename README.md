# recipe-crawler

Crawl recipe websites, generate PDFs, and query them with a local RAG chatbot.

## Stack

- **Crawlers**: BeautifulSoup · REST API · Playwright
- **Indexer**: pdfplumber · SQLite (metadata) · ChromaDB + sentence-transformers (semantic search)
- **Chat**: Gemini 2.5 Flash · Gradio

## Sites supported

| Site | Recipes | Method |
|---|---|---|
| [viandesuisse.ch](https://viandesuisse.ch/recettes) | ~17 | HTML scraping + native PDF download |
| [migusto.migros.ch](https://migusto.migros.ch/fr/apercu-des-recettes) | ~7 950 | REST API + schema.org JSON-LD + weasyprint PDF |
| [qoqa.ch](https://www.qoqa.ch/fr/posts?kind=recipe) | variable | Playwright (JS-rendered list) + native PDF download |

---

## Requirements

- Python 3.11+
- [weasyprint](https://doc.courtbouillon.org/weasyprint/) (+ GTK on Windows — see below)
- [Playwright](https://playwright.dev/python/) with Chromium (for QoQa)
- A Gemini API key

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

Copy `config.example.py` to `config.py` and fill in your Gemini API key:

```python
GEMINI_API_KEY = "your-key-here"
```

All other values have sensible defaults. `config.py` is gitignored — never commit it.

---

## Usage

Crawling and indexing are independent operations.

### 1. Crawl — download PDFs

```
python main.py [--sites SITE [SITE ...]] [--limit N] [--renew]
```

| Option | Description |
|---|---|
| `--sites` | `viandesuisse`, `migusto`, `qoqa`, or `all` (default: `all`) |
| `--limit N` | Download at most **N new** recipes per site |
| `--renew` | Ignore local link/slug cache and re-fetch from source |
| `--index` | Also run the indexer after crawling |

**Examples:**

```bash
# Download 10 new migusto recipes
python main.py --sites migusto --limit 10

# Crawl all sites with no limit
python main.py

# Crawl and immediately index
python main.py --sites migusto --limit 50 --index
```

### 2. Index — build SQLite + ChromaDB

```
python index_recipes.py [--sites SITE [SITE ...]] [--limit N]
```

Reads existing PDFs from disk, extracts metadata, and loads everything into SQLite and ChromaDB. Safe to re-run — already-indexed recipes are skipped.

```bash
# Index everything
python index_recipes.py

# Index only the next 200 qoqa recipes
python index_recipes.py --sites qoqa --limit 200
```

### 3. Chat — launch the Gradio interface

**Via the desktop shortcut** (Windows): double-click **Assistant Recettes** — the browser opens automatically.

**Or from the terminal:**

```bash
venv\Scripts\python.exe ui/app.py    # Windows
# python ui/app.py                   # Linux / macOS
```

The interface has two tabs:

- **Chat** — ask questions in natural language; hybrid search (SQL for duration/category, ChromaDB for everything else)
- **Admin** — manage crawling and indexing visually without touching the terminal:
  - Launch a crawl batch per site with configurable limit and `--renew`
  - Index PDFs in configurable batch sizes, with an optional loop until complete
  - Live log streaming and status display

Both tabs have a **Fermer l'application** button that shuts down the server and closes the console.

---

### Production batch crawl — `run_batch.py`

Loops `main.py` automatically until a site is exhausted (no new recipes found).

```
python run_batch.py [--sites SITE [SITE ...]] [--batch-size N] [--delay SEC] [--renew]
```

| Option | Default | Description |
|---|---|---|
| `--sites` | `all` | Same choices as main.py |
| `--batch-size N` | `50` | Recipes per batch |
| `--delay SEC` | `30` | Seconds to wait between batches |
| `--renew` | — | Re-fetch link/slug list on first batch |

```bash
# Crawl migusto in batches of 100
python run_batch.py --sites migusto --batch-size 100 --delay 60

# After crawling, index everything
python index_recipes.py
```

---

## Cache (migusto and qoqa)

Collecting the full recipe list is expensive:
- **migusto** requires ~330 paginated API calls to enumerate 7 950 slugs
- **qoqa** requires a full Playwright session to click through "Voir plus"

The list is cached locally as JSON after the first fetch.

| Situation | Behaviour |
|---|---|
| No cache file yet | List is fetched from source and saved automatically |
| Cache exists | Loaded instantly from disk (no network call) |
| `--renew` passed | Cache is ignored, list is re-fetched and overwritten |

Cache files are stored in `./cache/` (gitignored):

```
cache/
├── migusto_slugs.json   # list of ~7 950 recipe slugs
└── qoqa_links.json      # list of recipe URLs
```

---

## Project structure

```
recipe-crawler/
├── config.example.py       # Template — copy to config.py and fill in secrets
├── config.py               # Local config (gitignored)
├── main.py                 # Crawl PDFs (+ optional --index)
├── index_recipes.py        # Standalone indexer: PDFs → SQLite + ChromaDB
├── run_batch.py            # Production runner: loops main.py until done
├── start_app.bat           # Windows launcher (uses venv automatically)
│
├── crawlers/
│   ├── viandesuisse.py     # viandesuisse.ch crawler
│   ├── migusto.py          # migusto.migros.ch crawler
│   └── qoqa.py             # qoqa.ch crawler (Playwright)
│
├── pipeline/
│   ├── database.py         # SQLite metadata store
│   ├── embeddings.py       # ChromaDB vector store (offline, no HF network calls)
│   └── chat.py             # Query router + Gemini chat
│
├── ui/
│   └── app.py              # Gradio interface: Chat + Admin tabs
│
├── cache/                  # Auto-generated link/slug lists (gitignored)
│   ├── migusto_slugs.json
│   └── qoqa_links.json
│
└── pdfs/                   # Generated PDFs (gitignored)
    ├── viandesuisse/
    ├── migusto/
    └── qoqa/
```

---

## Crawler details

### viandesuisse.ch
Scrapes the paginated recipe list with BeautifulSoup, then fetches each recipe page to find the native print-PDF link (`/print/pdf/node/…`). Downloads the PDF directly.

### migusto.migros.ch
Uses the internal REST API (`POST /.rest/recipes/v1`) to paginate through all ~7 950 recipes and collect slugs. For each slug, fetches the HTML page and extracts the `schema.org/Recipe` JSON-LD block (name, ingredients, steps, nutrition). Generates a formatted PDF with weasyprint and saves a `.json` sidecar alongside it for fast indexing.

### qoqa.ch
Uses Playwright (headless Chromium) to load the JS-rendered recipe list and click "Voir plus" until all links are collected. PDFs are downloaded directly from `https://download.qoqa.ch/fr/posts/{id}.pdf`.