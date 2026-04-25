import json
import logging
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from weasyprint import HTML

logger = logging.getLogger(__name__)

BASE_URL = "https://migusto.migros.ch"
API_URL = "https://migusto.migros.ch/.rest/recipes/v1"
PDF_OUTPUT_DIR = "./pdfs/migusto"
_FILTER_UUID = "69656a62-ce86-42d3-a821-f3faeccb631c"
_PAGE_SIZE = 24

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://migusto.migros.ch/fr/apercu-des-recettes/",
}


def _parse_iso_duration(iso: str) -> str:
    """PT30M -> '30 min.'  PT1H30M -> '1h30'  PT2H -> '2h'"""
    if not iso:
        return ""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso)
    if not m:
        return iso
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    if hours and minutes:
        return f"{hours}h{minutes:02d}"
    if hours:
        return f"{hours}h"
    return f"{minutes} min."


def get_recipe_slugs(limit: int | None = None) -> list[str]:
    """POST pagination on /.rest/recipes/v1, returns list of recipe slugs."""
    slugs: list[str] = []
    offset = 0

    while True:
        logger.info("Fetching recipe list offset=%d (collected=%d)", offset, len(slugs))
        response = requests.post(
            API_URL,
            json={
                "language": "fr",
                "recipeFilterUuid": _FILTER_UUID,
                "searchTerm": "",
                "limit": _PAGE_SIZE,
                "offset": offset,
                "uuids": [],
                "ingredients": [],
                "order": "relevance:DESC",
            },
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        recipes = data.get("recipes", [])
        if not recipes:
            logger.info("No more recipes at offset=%d", offset)
            break

        for recipe in recipes:
            slugs.append(recipe["slug"])
            if limit is not None and len(slugs) >= limit:
                logger.info("Limit=%d reached, stopping slug fetch", limit)
                return slugs

        total = data.get("total", 0)
        logger.info("Slugs collected: %d / %d", len(slugs), total)

        if offset + _PAGE_SIZE >= total:
            break

        offset += _PAGE_SIZE
        time.sleep(1)

    return slugs


def get_recipe_data(slug: str) -> dict | None:
    """GET /fr/recettes/{slug}, extract and return schema.org/Recipe JSON-LD dict."""
    url = f"{BASE_URL}/fr/recettes/{slug}"
    logger.info("Fetching recipe page: %s", url)
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                data = data[0]
            if data.get("@type") == "Recipe":
                return data
        except (json.JSONDecodeError, AttributeError):
            continue

    logger.warning("No Recipe JSON-LD found for: %s", slug)
    return None


def generate_pdf(recipe_data: dict, output_dir: str) -> str | None:
    """Render recipe_data as a formatted PDF using weasyprint."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    slug = recipe_data.get("slug") or re.sub(r"[^a-z0-9]+", "-", recipe_data["name"].lower()).strip("-")
    filename = f"migusto_{slug}.pdf"
    output_path = Path(output_dir) / filename

    if output_path.exists():
        logger.info("PDF already exists, skipping: %s", filename)
        return str(output_path)

    title = recipe_data.get("name", "")
    description = recipe_data.get("description", "").strip()
    duration = _parse_iso_duration(recipe_data.get("totalTime", ""))
    servings = recipe_data.get("recipeYield", "")
    category = recipe_data.get("recipeCategory", "")
    ingredients = recipe_data.get("recipeIngredient", [])
    instructions = recipe_data.get("recipeInstructions", [])
    nutrition = recipe_data.get("nutrition") or {}

    meta_parts = []
    if duration:
        meta_parts.append(duration)
    if servings:
        meta_parts.append(servings)
    if category:
        meta_parts.append(category)

    ingredients_html = "\n".join(f"    <li>{ing}</li>" for ing in ingredients)

    steps_html = ""
    for step in instructions:
        text = step.get("text", "") if isinstance(step, dict) else str(step)
        steps_html += f"    <li>{text}</li>\n"

    # Migusto encodes glucides (carbs) in fiberContent
    nutr_rows = []
    for field, label in [
        ("calories", "Calories"),
        ("proteinContent", "Prot&eacute;ines"),
        ("fatContent", "Lipides"),
        ("fiberContent", "Glucides"),
    ]:
        val = nutrition.get(field)
        if val:
            nutr_rows.append(f"<tr><td>{label}</td><td>{val}</td></tr>")

    nutrition_block = ""
    if nutr_rows:
        rows = "\n".join(nutr_rows)
        nutrition_block = (
            "\n  <h2>Valeurs nutritives</h2>\n"
            "  <table class=\"nutrition\">\n"
            "    <tbody>\n"
            + rows + "\n"
            "    </tbody>\n"
            "  </table>"
        )

    description_block = f'<p class="description">{description}</p>' if description else ""
    meta_html = " &middot; ".join(meta_parts)

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<style>
  @page {{ margin: 20mm 18mm; }}
  body {{
    font-family: Georgia, "Times New Roman", serif;
    color: #1a1a1a;
    font-size: 11pt;
    line-height: 1.55;
  }}
  h1 {{
    font-size: 20pt;
    margin: 0 0 4px;
    color: #111;
  }}
  h2 {{
    font-size: 12pt;
    margin: 18px 0 5px;
    color: #333;
    border-bottom: 1px solid #ccc;
    padding-bottom: 2px;
  }}
  .meta {{
    color: #666;
    font-size: 9.5pt;
    margin-bottom: 14px;
    font-family: Arial, sans-serif;
  }}
  .description {{
    font-style: italic;
    color: #444;
    margin: 0 0 14px;
    padding-left: 10px;
    border-left: 3px solid #e8a020;
  }}
  ul, ol {{
    margin: 0;
    padding-left: 22px;
  }}
  li {{ margin-bottom: 4px; }}
  table.nutrition {{
    border-collapse: collapse;
    font-size: 9.5pt;
    font-family: Arial, sans-serif;
    margin-top: 4px;
  }}
  table.nutrition td {{
    padding: 2px 14px 2px 0;
    color: #444;
  }}
  table.nutrition td:first-child {{
    font-weight: bold;
    min-width: 90px;
  }}
  .source {{
    font-size: 8pt;
    color: #bbb;
    margin-top: 24px;
    font-family: Arial, sans-serif;
  }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="meta">{meta_html}</div>
  {description_block}
  <h2>Ingr&eacute;dients</h2>
  <ul>
{ingredients_html}
  </ul>
  <h2>Pr&eacute;paration</h2>
  <ol>
{steps_html}
  </ol>
  {nutrition_block}
  <div class="source">migusto.migros.ch/fr/recettes/{slug}</div>
</body>
</html>"""

    logger.info("Generating PDF: %s", filename)
    HTML(string=html, base_url=BASE_URL).write_pdf(str(output_path))
    logger.info("Saved: %s (%d bytes)", filename, output_path.stat().st_size)
    return str(output_path)


def crawl(output_dir: str = PDF_OUTPUT_DIR, limit: int | None = None) -> None:
    """Fetch all slugs, retrieve each recipe JSON-LD, generate PDFs."""
    logger.info("Starting Migusto crawl (limit=%s, output=%s)", limit, output_dir)

    slugs = get_recipe_slugs(limit=limit)
    logger.info("Total slugs to process: %d", len(slugs))

    for i, slug in enumerate(slugs, 1):
        logger.info("Processing %d/%d: %s", i, len(slugs), slug)
        try:
            data = get_recipe_data(slug)
            if not data:
                logger.warning("No data returned for slug: %s", slug)
                time.sleep(1)
                continue
            data["slug"] = slug
            generate_pdf(data, output_dir)
        except requests.RequestException as e:
            logger.error("Network error for %s: %s", slug, e)
        except Exception as e:
            logger.error("Unexpected error for %s: %s", slug, e)
        time.sleep(1)

    logger.info("Crawl complete: %d recipes processed", len(slugs))
