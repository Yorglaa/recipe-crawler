"""
Crawler pour fooby.ch.
- Liste  : API REST hawaii_search.sri (pagination via start/num, ~8 800 recettes, cache local)
- PDFs   : lien natif depuis page recette (Playwright)
- Sidecar: JSON de métadonnées API sauvegardé avec chaque PDF
"""
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright

import config

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
PDF_OUTPUT_DIR = str(_ROOT / "pdfs" / "fooby")
_CACHE_FILE = str(_ROOT / "cache" / "fooby_links.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

_API_URL = "https://fooby.ch/hawaii_search.sri"
_API_NUM = 200  # résultats par appel (le site en affiche 12, mais l'API accepte plus)

# MutationObserver injecté avant tout script : supprime #usercentrics-root dès qu'il apparaît
_CONSENT_INIT_SCRIPT = """
    new MutationObserver(mutations => {
        for (const m of mutations)
            for (const n of m.addedNodes)
                if (n.id === 'usercentrics-root') n.remove();
    }).observe(document.documentElement, { childList: true, subtree: true });
"""


# ── Collecte via API ───────────────────────────────────────────────────────────

def _fetch_recipe_data() -> list[dict]:
    """Collecte toutes les recettes via l'API REST (sans Playwright)."""
    results: list[dict] = []
    params = {
        "query": "", "lang": "fr", "treffertyp": "rezepte",
        "start": 0, "num": _API_NUM, "sort": "",
        "interface": "hawaii", "userquery": "true",
    }
    total = None

    while True:
        r = requests.get(_API_URL, params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()

        if total is None:
            total = int(data["resultcounts"]["all"])
            logger.info("Total fooby.ch : %d recettes", total)

        batch = data.get("results", [])
        results.extend(batch)
        logger.info("Collecté %d / %d...", len(results), total)

        next_start = data["resultcounts"].get("next_start")
        if not next_start or int(next_start) >= total:
            break
        params["start"] = int(next_start)
        time.sleep(0.3)

    logger.info("Collecte terminée : %d recettes fooby", len(results))
    return results


def get_recipe_data(renew: bool = False) -> list[dict]:
    """Retourne les données depuis le cache JSON, ou appelle l'API si absent."""
    cache = Path(_CACHE_FILE)
    if not renew and cache.exists():
        data = json.loads(cache.read_text("utf-8"))
        logger.info("Cache : %d recettes fooby chargées depuis %s", len(data), _CACHE_FILE)
        return data
    logger.info("Collecte des recettes fooby via API%s...", " (--renew)" if renew else " (pas de cache)")
    data = _fetch_recipe_data()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Cache sauvegardé : %d recettes -> %s", len(data), _CACHE_FILE)
    return data


def get_recipe_links(renew: bool = False) -> list[str]:
    """Interface standard : retourne les URLs des recettes."""
    return [r["url"] for r in get_recipe_data(renew=renew)]


# ── URL PDF directe ───────────────────────────────────────────────────────────

def _pdf_url_from_id(recipe_id: str, lang: str = "fr") -> str:
    """Construit l'URL PDF directe depuis le recipe_id (pas de Playwright nécessaire)."""
    return f"https://fooby.ch/bin/coop/fooby/pdfs/recipe.id-{recipe_id}.lang-{lang}.qty-4.pdf"


# ── Playwright (fallback PDF si recipe_id absent) ──────────────────────────────

def _new_page(browser):
    """Page Playwright avec blocage Usercentrics."""
    page = browser.new_page()
    page.route(re.compile(r".*usercentrics.*"), lambda route: route.abort())
    page.add_init_script(_CONSENT_INIT_SCRIPT)
    return page


def _slug_from_url(recipe_url: str) -> str:
    parts = recipe_url.rstrip("/").split("/")
    # URL : .../fr/recettes/{id}/{slug}
    return f"{parts[-2]}-{parts[-1]}" if len(parts) >= 2 else parts[-1]


def _pdf_url_from_html(html: str, base_url: str) -> str | None:
    m = re.search(r'href="([^"]+\.pdf)"', html)
    return urljoin(base_url, m.group(1)) if m else None


def _find_pdf_url_playwright(recipe_url: str) -> str | None:
    """Fallback Playwright pour récupérer l'URL PDF (utilisé uniquement si recipe_id absent)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = _new_page(browser)
        url = _find_pdf_url_page(page, recipe_url)
        browser.close()
    return url


def _find_pdf_url_page(page, recipe_url: str) -> str | None:
    """
    Cherche l'URL du PDF depuis la page recette.
    Stratégie 1 : requests direct (pas de Playwright — rapide si lien dans le HTML serveur).
    Stratégie 2 : Playwright domcontentloaded (si JS nécessaire).
    Stratégie 3 : clic sur 'Imprimer' puis recherche du lien PDF.
    """
    # Stratégie 1 : requête HTTP simple, pas de browser
    try:
        r = requests.get(recipe_url, headers=HEADERS, timeout=10)
        url = _pdf_url_from_html(r.text, recipe_url)
        if url:
            logger.debug("PDF via requests : %s", url)
            return url
    except requests.RequestException:
        pass

    # Stratégie 2 : Playwright avec domcontentloaded (plus rapide que networkidle)
    page.goto(recipe_url, wait_until="domcontentloaded", timeout=30000)
    url = _pdf_url_from_html(page.content(), recipe_url)
    if url:
        logger.debug("PDF via DOM : %s", url)
        return url

    # Stratégie 3 : clic sur le bouton Imprimer
    btn = page.query_selector(
        'button:has-text("Imprimer"), a:has-text("Imprimer"), '
        '[data-testid*="print"], [aria-label*="imprimer"], [aria-label*="print"]'
    )
    if not btn:
        logger.warning("Pas de bouton Imprimer sur %s", recipe_url)
        return None

    try:
        with page.expect_popup(timeout=5000) as popup_info:
            btn.dispatch_event("click")
        print_page = popup_info.value
        print_page.route(re.compile(r".*usercentrics.*"), lambda route: route.abort())
        print_page.wait_for_load_state("domcontentloaded", timeout=15000)
        pdf_link = print_page.query_selector(
            'a[href$=".pdf"], a:has-text("Sauvegarder"), a:has-text("sauvegarder")'
        )
        href = pdf_link.get_attribute("href") if pdf_link else None
        print_page.close()
        url = urljoin(recipe_url, href) if href else None
    except Exception:
        btn.dispatch_event("click")
        page.wait_for_timeout(1500)
        pdf_link = page.query_selector(
            'a[href$=".pdf"], a:has-text("Sauvegarder"), a:has-text("sauvegarder")'
        )
        href = pdf_link.get_attribute("href") if pdf_link else None
        url = urljoin(recipe_url, href) if href else None

    return url


def _download_pdf(pdf_url: str, pdf_path: Path) -> bool:
    try:
        r = requests.get(pdf_url, headers=HEADERS, timeout=30, stream=True)
        r.raise_for_status()
        pdf_path.write_bytes(r.content)
        logger.info("Sauvegardé : %s (%d octets)", pdf_path.name, pdf_path.stat().st_size)
        return True
    except requests.RequestException as e:
        logger.error("Erreur téléchargement %s : %s", pdf_url, e)
        return False


# ── Point d'entrée ─────────────────────────────────────────────────────────────

def crawl(output_dir: str = PDF_OUTPUT_DIR, limit: int | None = None, renew: bool = False) -> None:
    """Télécharge les PDFs fooby.ch et sauvegarde un JSON sidecar par recette."""
    logger.info("=== Fooby crawl (limit=%s, renew=%s) ===", limit, renew)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    recipe_data = get_recipe_data(renew=renew)

    pending = [
        (item, out / f"fooby_{_slug_from_url(item['url'])}.pdf")
        for item in recipe_data
        if not (out / f"fooby_{_slug_from_url(item['url'])}.pdf").exists()
    ]
    logger.info("%d recettes en attente (sur %d total)", len(pending), len(recipe_data))

    if limit is not None:
        pending = pending[:limit]

    if not pending:
        logger.info("Aucune nouvelle recette à télécharger.")
        return

    downloaded = 0
    for item, pdf_path in pending:
        try:
            recipe_id = item.get("recipe_id")
            if recipe_id:
                pdf_url = _pdf_url_from_id(recipe_id)
            else:
                # Fallback Playwright pour les rares cas sans recipe_id
                logger.warning("recipe_id absent pour %s — fallback Playwright", item["url"])
                pdf_url = _find_pdf_url_playwright(item["url"])
            if not pdf_url:
                logger.warning("URL PDF non trouvée : %s", item["url"])
                continue
            if _download_pdf(pdf_url, pdf_path):
                pdf_path.with_suffix(".json").write_text(
                    json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                downloaded += 1
            time.sleep(0.5)
        except Exception as e:
            logger.error("Erreur %s : %s", item["url"], e)

    logger.info("Fooby : %d nouveaux PDFs téléchargés", downloaded)
