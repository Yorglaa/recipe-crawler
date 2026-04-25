import logging
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://viandesuisse.ch"
LIST_URL = "https://viandesuisse.ch/recettes"
PDF_OUTPUT_DIR = "./pdfs/viandesuisse"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def get_recipe_links() -> list[str]:
    """Retourne toutes les URLs de recettes depuis la page liste, avec pagination."""
    links = []
    seen = set()
    page = 0

    while True:
        url = LIST_URL if page == 0 else f"{LIST_URL}?page={page}"
        logger.info("Fetching list page %d: %s", page, url)

        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        new_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.match(r"^/recettes/[^/?#]+$", href):
                full_url = BASE_URL + href
                if full_url not in seen:
                    seen.add(full_url)
                    new_links.append(full_url)

        if not new_links:
            logger.info("No new recipes on page %d, stopping pagination", page)
            break

        links.extend(new_links)
        logger.info("Found %d recipes on page %d", len(new_links), page)

        if not soup.find("a", rel="next"):
            break

        page += 1
        time.sleep(1)

    logger.info("Total recipes found: %d", len(links))
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


def crawl(output_dir: str = PDF_OUTPUT_DIR) -> None:
    """Orchestre le crawl complet : recupere les liens et telecharge tous les PDFs."""
    logger.info("Starting crawl of viandesuisse.ch")

    recipe_links = get_recipe_links()
    logger.info("Starting download of %d PDFs", len(recipe_links))

    for i, recipe_url in enumerate(recipe_links, 1):
        logger.info("Processing recipe %d/%d: %s", i, len(recipe_links), recipe_url)
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
