import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://viandesuisse.ch"
_GRAPHQL_URL = "https://viandesuisse.ch/graphql"
_ROOT = Path(__file__).resolve().parent.parent
PDF_OUTPUT_DIR = str(_ROOT / "pdfs" / "viandesuisse")
_CACHE_FILE = str(_ROOT / "cache" / "viandesuisse_links.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

_GQL = """
{
  searchAPISearch(
    index_id: "recipe_index",
    language: "fr",
    range: {offset: 0, limit: 2000},
    sort: {field: "created", value: "desc"},
    fulltext: {keys: "", fields: ["title"]},
    facets: []
    conditions: [{operator: "=", name: "bundle", value: "recipe"}]
  ) {
    result_count
    documents {
      ... on RecipeIndexDoc {
        entity {
          ... on NodeRecipe {
            entityUrl { path }
          }
        }
      }
    }
  }
}
"""


def _fetch_recipe_links() -> list[str]:
    """Récupère tous les liens de recettes via l'API GraphQL du site."""
    logger.info("Fetching recipe list via GraphQL (%s)", _GRAPHQL_URL)
    resp = requests.post(
        _GRAPHQL_URL,
        json={"query": _GQL},
        headers={**HEADERS, "Content-Type": "application/json", "Referer": f"{BASE_URL}/recettes"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data["data"]["searchAPISearch"]
    logger.info("GraphQL result_count: %d", result["result_count"])

    links = []
    for doc in result.get("documents", []):
        entities = doc.get("entity") or []
        for entity in entities:
            path = (entity.get("entityUrl") or {}).get("path")
            if path and re.match(r"^/recettes/[^/?#]+$", path):
                links.append(BASE_URL + path)
    logger.info("Total recipe links: %d", len(links))
    return links


def get_recipe_links(renew: bool = False) -> list[str]:
    """Retourne tous les liens de recettes. Cache JSON dans cache/viandesuisse_links.json."""
    cache = Path(_CACHE_FILE)
    if not renew and cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        logger.info("Cache: %d viandesuisse links loaded", len(data))
        return data

    links = _fetch_recipe_links()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Cache saved: %s", cache)
    return links


def get_pdf_url(recipe_url: str) -> str | None:
    """Extrait l'URL du PDF natif depuis la page recette via le lien Imprimer."""
    logger.info("Fetching recipe page: %s", recipe_url)

    response = requests.get(recipe_url, headers=HEADERS, timeout=10)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/print/pdf/node/" in href:
            if href.startswith("http"):
                return href
            return BASE_URL + href

    logger.warning("No PDF link found on %s", recipe_url)
    return None


def download_pdf(pdf_url: str, output_dir: str, recipe_url: str) -> str | None:
    """Telecharge le PDF et le sauvegarde dans output_dir."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    slug = recipe_url.rstrip("/").split("/")[-1]
    filename = f"viandesuisse_{slug}.pdf"
    output_path = os.path.join(output_dir, filename)

    if os.path.exists(output_path):
        logger.info("PDF already exists, skipping: %s", output_path)
        return output_path

    logger.info("Downloading PDF: %s", pdf_url)
    response = requests.get(pdf_url, headers=HEADERS, timeout=30, stream=True)
    response.raise_for_status()

    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    logger.info("Saved PDF: %s", output_path)
    return output_path


def crawl(output_dir: str = PDF_OUTPUT_DIR, limit: int | None = None, renew: bool = False) -> None:
    """Orchestre le crawl : recupere les liens, skip les PDFs existants, telecharge jusqu'a `limit` nouveaux."""
    logger.info("Starting crawl of viandesuisse.ch (limit=%s)", limit)

    recipe_links = get_recipe_links(renew=renew)

    pending = []
    for recipe_url in recipe_links:
        slug = recipe_url.rstrip("/").split("/")[-1]
        if not (Path(output_dir) / f"viandesuisse_{slug}.pdf").exists():
            pending.append(recipe_url)

    logger.info("%d recipes pending (out of %d total)", len(pending), len(recipe_links))

    if limit is not None:
        pending = pending[:limit]

    logger.info("Starting download of %d PDFs", len(pending))
    for i, recipe_url in enumerate(pending, 1):
        logger.info("Processing recipe %d/%d: %s", i, len(pending), recipe_url)
        try:
            time.sleep(1)
            pdf_url = get_pdf_url(recipe_url)
            if pdf_url:
                time.sleep(1)
                download_pdf(pdf_url, output_dir, recipe_url)
            else:
                logger.warning("Skipping recipe (no PDF link): %s", recipe_url)
        except requests.RequestException as e:
            logger.error("Error processing %s: %s", recipe_url, e)

    logger.info("Crawl complete")
