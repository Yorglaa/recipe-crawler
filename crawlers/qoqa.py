import logging
import re
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

LIST_URL = "https://www.qoqa.ch/fr/posts?kind=recipe"
PDF_OUTPUT_DIR = "./pdfs/qoqa"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

_POST_RE = re.compile(r"^/fr/posts/(\d+)$")


def get_recipe_links() -> list[str]:
    """Charge la page de liste, clique 'voir plus' jusqu'a epuisement, retourne les URLs recettes."""
    links: list[str] = []
    seen: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        logger.info("Loading list page: %s", LIST_URL)
        page.goto(LIST_URL, wait_until="networkidle", timeout=30000)

        while True:
            html = page.content()
            for href in re.findall(r'href="(/fr/posts/\d+)"', html):
                if href not in seen:
                    seen.add(href)
                    links.append("https://www.qoqa.ch" + href)

            # Chercher le bouton "voir plus" / "load more"
            btn = page.query_selector(
                '[data-testid="load-more-button"], '
                'button:has-text("voir plus"), '
                'button:has-text("Voir plus"), '
                'button:has-text("Load more")'
            )
            if btn is None:
                logger.info("No more 'voir plus' button — done")
                break

            logger.info("Clicking 'voir plus' (%d links so far)...", len(links))
            btn.click()
            page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(1)

        browser.close()

    logger.info("Total recipe links found: %d", len(links))
    return links


def get_pdf_url(recipe_url: str) -> str | None:
    """Derive l'URL du PDF directement depuis l'ID numerique de la recette."""
    m = _POST_RE.search(recipe_url.replace("https://www.qoqa.ch", ""))
    if not m:
        return None
    post_id = m.group(1)
    return f"https://download.qoqa.ch/fr/posts/{post_id}.pdf"


def download_pdf(pdf_url: str, output_dir: str, recipe_url: str) -> str | None:
    """Telecharge le PDF et le sauvegarde sous qoqa_{id}.pdf."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    m = _POST_RE.search(recipe_url.replace("https://www.qoqa.ch", ""))
    post_id = m.group(1) if m else pdf_url.rstrip("/").split("/")[-1].replace(".pdf", "")
    filename = f"qoqa_{post_id}.pdf"
    output_path = Path(output_dir) / filename

    if output_path.exists():
        logger.info("Already downloaded: %s", filename)
        return str(output_path)

    logger.info("Downloading %s", pdf_url)
    response = requests.get(pdf_url, headers=HEADERS, timeout=30, stream=True)
    response.raise_for_status()

    with output_path.open("wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    logger.info("Saved: %s (%d bytes)", filename, output_path.stat().st_size)
    return str(output_path)


def crawl(output_dir: str = PDF_OUTPUT_DIR, limit: int | None = None) -> None:
    recipe_links = get_recipe_links()
    if limit is not None:
        recipe_links = recipe_links[:limit]
    for recipe_url in recipe_links:
        try:
            pdf_url = get_pdf_url(recipe_url)
            if not pdf_url:
                logger.warning("No PDF URL for %s", recipe_url)
                continue
            download_pdf(pdf_url, output_dir, recipe_url)
            time.sleep(1)
        except requests.RequestException as e:
            logger.error("Error processing %s: %s", recipe_url, e)
