# recipe-crawler

Crawl recipe websites, generate PDFs, and query them with a local RAG chatbot.

## Stack

- **Crawlers**: BeautifulSoup · REST API · Playwright
- **Indexer**: pdfplumber · SQLite (metadata) · ChromaDB + sentence-transformers (semantic search)
- **Chat**: Gemini 2.5 Flash · Groq (llama-3.3-70b, fallback) · Gradio

## Sites supported

| Site | Recipes | Method |
|---|---|---|
| [viandesuisse.ch](https://viandesuisse.ch/recettes) | ~17 | HTML scraping + native PDF download |
| [migusto.migros.ch](https://migusto.migros.ch/fr/apercu-des-recettes) | ~7 950 | REST API + schema.org JSON-LD + weasyprint PDF |
| [qoqa.ch](https://www.qoqa.ch/fr/posts?kind=recipe) | variable | Playwright (JS-rendered list) + native PDF download |
| [fooby.ch](https://fooby.ch/fr/recettes.html) | ~8 000 | Playwright (infinite scroll) + native PDF download |

---

## Requirements

- Python 3.11+
- [weasyprint](https://doc.courtbouillon.org/weasyprint/) (+ GTK on Windows — see below)
- [Playwright](https://playwright.dev/python/) with Chromium (for QoQa)
- A Gemini API key
- A Groq API key (optional — used as fallback if Gemini is unavailable)

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
GROQ_API_KEY=your-groq-key-here      # optional — fallback only
```

`config.py` loads this file automatically via `python-dotenv`. The `.env` file is gitignored — never commit it.

All other settings (`CHROMA_DB_PATH`, `SQLITE_DB_PATH`, `EMBEDDING_MODEL`, `GEMINI_MODEL`, `GROQ_MODEL`) are defined in `config.py` with sensible defaults.

---

## Usage

Crawling and indexing are independent operations.

### 1. Crawl — download PDFs

```
python main.py [--sites SITE [SITE ...]] [--limit N] [--renew] [--index]
```

| Option | Description |
|---|---|
| `--sites` | `viandesuisse`, `migusto`, `qoqa`, or `all` (default: `all`) |
| `--limit N` | Download at most **N new** recipes per site |
| `--renew` | Ignore local link/slug cache and re-fetch from source |
| `--index` | Also run the indexer (SQLite + ChromaDB) after crawling |

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
python index_recipes.py [--sites SITE [SITE ...]] [--limit N] [--sync-embeddings]
```

Reads existing PDFs from disk, extracts metadata, and loads everything into SQLite and ChromaDB. Safe to re-run — already-indexed recipes are skipped.

| Option | Description |
|---|---|
| `--sites` | Sites to index (default: all) |
| `--limit N` | Index at most **N new** recipes per site |
| `--sync-embeddings` | Add to ChromaDB any recipe already in SQLite but missing an embedding |

```bash
# Index everything
python index_recipes.py

# Index only the next 200 qoqa recipes
python index_recipes.py --sites qoqa --limit 200

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

**Count queries** — Ask how many recipes match a given ingredient, category, duration, or site. The bot returns the exact count (SQL-based) and a few examples, so you can then ask *donne moi la liste* or refine the search.

**Detail mode** — Once a recipe is listed, ask for full preparation steps in natural language:

- *Détaille moi la salade russe*
- *Donne moi la marche à suivre pour les boulettes de poulet au curry*
- *Comment préparer le tartare à l'italienne ?*

Detail mode works even after a **Nouvelle conversation** reset or when the recipe was mentioned in an earlier turn — the bot searches the full database by title keywords if the recipe is not in the current context.

**Nouvelle conversation** resets the chat history and the recipe context, starting fresh without restarting the server.

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
# Crawl migusto in batches of 100
python run_batch.py --sites migusto --batch-size 100 --delay 60

# After crawling, index everything
python index_recipes.py
```

---

## Cache (migusto and qoqa)

Collecting the full recipe list is expensive:
- **migusto** requires ~330 paginated API calls to enumerate ~7 950 slugs
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
├── qoqa_links.json      # list of recipe URLs
└── fooby_links.json     # list of ~8 000 recipe URLs
```

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
├── start_app.bat           # Windows launcher (uses venv automatically)
│
├── crawlers/
│   ├── viandesuisse.py     # viandesuisse.ch crawler
│   ├── migusto.py          # migusto.migros.ch crawler
│   ├── qoqa.py             # qoqa.ch crawler (Playwright)
│   └── fooby.py            # fooby.ch crawler (Playwright, ~8 000 recettes)
│
├── pipeline/
│   ├── database.py         # SQLite metadata store (thread-safe)
│   ├── embeddings.py       # ChromaDB vector store (offline, HF_HUB_OFFLINE=1)
│   └── chat.py             # Query router + Gemini chat (Groq fallback)
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
    ├── qoqa/
    └── fooby/
```

---

## Crawler details

### viandesuisse.ch
Scrapes the paginated recipe list with BeautifulSoup, then fetches each recipe page to find the native print-PDF link (`/print/pdf/node/...`). Downloads the PDF directly.

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