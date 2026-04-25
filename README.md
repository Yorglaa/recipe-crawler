# recipe-crawler

Crawl recipe websites, generate PDFs, and upload them to an [AnythingLLM](https://anythingllm.com) workspace for RAG-based querying.

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
- A running [AnythingLLM](https://anythingllm.com) instance with an API key

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

Copy `config.example.py` to `config.py` and fill in the values:

```python
# URLs (pre-filled, change only if sites move)
VIANDESUISSE_URL = "https://www.viandesuisse.ch/recettes"
MIGUSTO_URL      = "https://www.migusto.ch/fr/recettes"
QOQA_URL         = "https://www.qoqa.ch/fr/recettes"

# AnythingLLM instance
ANYTHINGLLM_API_URL   = "http://localhost:3001"
ANYTHINGLLM_WORKSPACE = "Recettes"   # workspace name (case-insensitive slug)
ANYTHINGLLM_API_KEY   = "your-key-here"

# PDF output root
PDF_OUTPUT_DIR = "./pdfs"
```

`config.py` is gitignored — never commit it.

---

## Usage

### One-shot run — `main.py`

```
python main.py [--sites SITE [SITE ...]] [--limit N] [--skip-upload]
```

| Option | Description |
|---|---|
| `--sites` | `viandesuisse`, `migusto`, `qoqa`, or `all` (default: `all`) |
| `--limit N` | Download at most **N new** recipes per site (skips already-downloaded) |
| `--skip-upload` | Generate PDFs only, skip AnythingLLM upload |

**Examples:**

```bash
# Download 10 new viandesuisse recipes and upload them
python main.py --sites viandesuisse --limit 10

# Download next 10 (incremental — skips the first 10 already on disk)
python main.py --sites viandesuisse --limit 10

# Dry-run all sites, 5 recipes each, no upload
python main.py --sites all --limit 5 --skip-upload

# Full run, no limit
python main.py --sites migusto
```

### Production batch run — `run_batch.py`

Loops `main.py` automatically until a site is exhausted (no new recipes found). Uploads each batch as it goes.

```
python run_batch.py [--sites SITE [SITE ...]] [--batch-size N] [--delay SEC] [--skip-upload]
```

| Option | Default | Description |
|---|---|---|
| `--sites` | `all` | Same choices as main.py |
| `--batch-size N` | `50` | Recipes per batch |
| `--delay SEC` | `30` | Seconds to wait between batches |
| `--skip-upload` | — | Download only, no AnythingLLM upload |

**Examples:**

```bash
# Crawl all sites, 50 recipes per batch, upload each batch
python run_batch.py

# Crawl migusto only, bigger batches
python run_batch.py --sites migusto --batch-size 100 --delay 60

# Crawl viandesuisse and qoqa, no upload
python run_batch.py --sites viandesuisse qoqa --skip-upload
```

#### Incremental / resumable

Both scripts are fully incremental: a recipe whose PDF already exists on disk is skipped and **does not count toward `--limit`**.  
Running the same command twice will download the *next* N recipes, not re-download the same ones.

---

## Project structure

```
recipe-crawler/
├── config.example.py       # Template — copy to config.py and fill in secrets
├── config.py               # Local config (gitignored)
├── main.py                 # CLI pipeline: crawl + upload one run
├── run_batch.py            # Production runner: loops main.py until done
│
├── crawlers/
│   ├── viandesuisse.py     # viandesuisse.ch crawler
│   ├── migusto.py          # migusto.migros.ch crawler
│   └── qoqa.py             # qoqa.ch crawler (Playwright)
│
├── pipeline/
│   └── anythingllm.py      # AnythingLLM API client (upload + embed)
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
Uses the internal REST API (`POST /.rest/recipes/v1`) to paginate through all ~7 950 recipes and collect slugs. For each slug, fetches the HTML page and extracts the `schema.org/Recipe` JSON-LD block (name, ingredients, steps, nutrition). Generates a formatted PDF with weasyprint. No authentication required.

### qoqa.ch
Uses Playwright (headless Chromium) to load the JS-rendered recipe list and click "Voir plus" until all links are collected. PDFs are downloaded directly from `https://download.qoqa.ch/fr/posts/{id}.pdf`.

---

## AnythingLLM integration

Each new PDF is:
1. **Uploaded** via `POST /api/v1/document/upload`
2. **Embedded** into the workspace via `POST /api/v1/workspace/{slug}/update-embeddings`

Only PDFs downloaded in the current run are uploaded — existing PDFs already in the workspace are not re-uploaded.
