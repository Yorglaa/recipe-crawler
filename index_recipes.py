"""
Indexe les PDFs de recettes dans SQLite et ChromaDB.
Operation independante du crawl.

Usage:
  python index_recipes.py
  python index_recipes.py --sites migusto qoqa
  python index_recipes.py --limit 100
"""

import argparse
import json
import logging
import re
from pathlib import Path

import pdfplumber

import config
from pipeline.database import (
    init_db, insert_recipe, recipe_exists, get_all_recipes,
    update_full_text, get_recipes_missing_full_text,
)
from pipeline.embeddings import init_chroma, add_recipe, recipe_exists as embed_exists

logger = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).resolve().parent
PDF_DIRS = {
    "viandesuisse": str(_SCRIPT_DIR / "pdfs" / "viandesuisse"),
    "migusto":      str(_SCRIPT_DIR / "pdfs" / "migusto"),
    "qoqa":         str(_SCRIPT_DIR / "pdfs" / "qoqa"),
    "fooby":        str(_SCRIPT_DIR / "pdfs" / "fooby"),
}


def _extract_text(pdf_path: Path) -> str:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        logger.warning("pdfplumber failed for %s: %s", pdf_path.name, exc)
        return ""


def _title_from_filename(pdf_path: Path, prefix: str) -> str:
    return pdf_path.stem.removeprefix(prefix).replace("-", " ").title()


def _iso_to_minutes(iso: str) -> int | None:
    if not iso:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso)
    if not m:
        return None
    total = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return total or None


def _parse_migusto(pdf_path: Path) -> dict:
    text = _extract_text(pdf_path)
    json_path = pdf_path.with_suffix(".json")
    if json_path.exists():
        data = json.loads(json_path.read_text("utf-8"))
        return {
            "title":            data.get("name", "") or _title_from_filename(pdf_path, "migusto_"),
            "site":             "migusto",
            "pdf_path":         str(pdf_path),
            "ingredients":      data.get("recipeIngredient") or [],
            "duration_minutes": _iso_to_minutes(data.get("totalTime", "")),
            "category":         data.get("recipeCategory") or None,
            "description":      (data.get("description") or "").strip() or None,
            "full_text":        text or None,
        }
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return {
        "title":            lines[0] if lines else _title_from_filename(pdf_path, "migusto_"),
        "site":             "migusto",
        "pdf_path":         str(pdf_path),
        "ingredients":      None,
        "duration_minutes": None,
        "category":         None,
        "description":      None,
        "full_text":        text or None,
    }


_VS_DURATION_RE = re.compile(
    r"Dur[eé]e totale\s*:\s*(?:(\d+)\s*h(?:eure)?s?\s*)?(\d+)?\s*min",
    re.IGNORECASE,
)
_VS_NUTR_SKIP = re.compile(
    r"kcal|prot[eé]ines?|glucides?|lipides?|sans gluten|sans lactose"
    r"|une portion|contient|valeurs nutritives|viandesuisse\.ch",
    re.IGNORECASE,
)


def _parse_viandesuisse(pdf_path: Path) -> dict:
    text = _extract_text(pdf_path)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    title = lines[0] if lines else _title_from_filename(pdf_path, "viandesuisse_")

    duration_minutes = None
    m = _VS_DURATION_RE.search(text)
    if m:
        h = int(m.group(1) or 0)
        mn = int(m.group(2) or 0)
        duration_minutes = h * 60 + mn or None

    in_ingredients = False
    ingredients = []
    for line in lines:
        if re.search(r"\bIngr[eé]dients?\b", line):
            in_ingredients = True
            continue
        if re.search(r"\bPr[eé]paration\b", line):
            break
        if not in_ingredients:
            continue
        if _VS_NUTR_SKIP.search(line):
            continue
        if re.match(r"pour\s+\d+\s+personnes", line, re.IGNORECASE):
            continue
        ingredients.append(line)

    return {
        "title":            title,
        "site":             "viandesuisse",
        "pdf_path":         str(pdf_path),
        "ingredients":      ingredients or None,
        "duration_minutes": duration_minutes,
        "category":         None,
        "description":      None,
        "full_text":        text or None,
    }


_QOQA_STEPS_RE = re.compile(r"[ÉE]tapes?\s+de\s+pr[eé]paration", re.IGNORECASE)


def _parse_qoqa(pdf_path: Path) -> dict:
    text = _extract_text(pdf_path)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    title = lines[0] if lines else _title_from_filename(pdf_path, "qoqa_")

    category = None
    for line in lines[1:6]:
        if "/" in line and "Qooking" not in line and "Recette" not in line:
            category = line.split("/")[-1].strip()
            break

    duration_minutes = None
    m = re.search(r"Pr[eé]paration\s+Cuisson", text, re.IGNORECASE)
    if m:
        times = re.findall(r"(\d+)\s*min", text[m.end(): m.end() + 300])
        if times:
            duration_minutes = sum(int(t) for t in times[:2])

    in_ingredients = False
    ingredients = []
    for line in lines:
        if "Liste des ingr" in line:
            in_ingredients = True
            continue
        if in_ingredients and (_QOQA_STEPS_RE.search(line) or "Recette du" in line):
            break
        if in_ingredients and line:
            ingredients.append(line)

    return {
        "title":            title,
        "site":             "qoqa",
        "pdf_path":         str(pdf_path),
        "ingredients":      ingredients or None,
        "duration_minutes": duration_minutes,
        "category":         category,
        "description":      None,
        "full_text":        text or None,
    }


def _fooby_ingredients_from_pdf(pdf_path: Path) -> list[str]:
    """Extrait les ingrédients depuis la colonne gauche du PDF fooby (layout 2 colonnes)."""
    try:
        with pdfplumber.open(pdf_path) as p:
            page = p.pages[0]
            left = page.within_bbox((0, 0, page.width * 0.45, page.height))
            text = left.extract_text() or ""
    except Exception:
        return []

    ingredients = []
    in_section = False
    for line in (l.strip() for l in text.split("\n") if l.strip()):
        if re.search(r"IL VOUS FAUT", line, re.IGNORECASE):
            in_section = True
            continue
        if not in_section:
            continue
        if re.search(r"^Attention\b|dur[eé]es de pr[eé]paration|adapt[eé]es automatiquement", line, re.IGNORECASE):
            continue
        if re.search(r"^Ustensiles$", line, re.IGNORECASE):
            break
        ingredients.append(line)
    return ingredients


def _parse_fooby(pdf_path: Path) -> dict:
    text = _extract_text(pdf_path)
    ingredients = _fooby_ingredients_from_pdf(pdf_path)

    # Utiliser le JSON sidecar API si disponible (titre, durée, catégorie fiables)
    json_path = pdf_path.with_suffix(".json")
    if json_path.exists():
        api = json.loads(json_path.read_text("utf-8"))
        total = api.get("dauer_gesamt")
        duration_minutes = int(total) if total and str(total).isdigit() else None
        return {
            "title":            api.get("title") or _title_from_filename(pdf_path, "fooby_"),
            "site":             "fooby",
            "pdf_path":         str(pdf_path),
            "ingredients":      ingredients or None,
            "duration_minutes": duration_minutes,
            "category":         api.get("ernaehrungsweise") or None,
            "description":      None,
            "full_text":        text or None,
        }

    # Fallback sans sidecar
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    title = lines[0] if lines else _title_from_filename(pdf_path, "fooby_")
    duration_minutes = None
    m = re.search(r"(?:(\d+)\s*h(?:eure)?s?\s*)?(\d+)\s*min", text, re.IGNORECASE)
    if m:
        h = int(m.group(1) or 0)
        mn = int(m.group(2) or 0)
        duration_minutes = h * 60 + mn or None
    return {
        "title":            title,
        "site":             "fooby",
        "pdf_path":         str(pdf_path),
        "ingredients":      ingredients or None,
        "duration_minutes": duration_minutes,
        "category":         None,
        "description":      None,
        "full_text":        text or None,
    }


_PARSERS = {
    "viandesuisse": _parse_viandesuisse,
    "migusto":      _parse_migusto,
    "qoqa":         _parse_qoqa,
    "fooby":        _parse_fooby,
}



def backfill_full_text() -> int:
    """Met à jour full_text pour les recettes déjà indexées qui n'en ont pas."""
    missing = get_recipes_missing_full_text()
    updated = 0
    for row in missing:
        pdf_path = Path(row["pdf_path"])
        if not pdf_path.exists():
            logger.warning("PDF introuvable : %s", pdf_path)
            continue
        text = _extract_text(pdf_path)
        if text:
            update_full_text(row["id"], text)
            updated += 1
            if updated % 50 == 0:
                logger.info("  backfill : %d recettes mises à jour...", updated)
    return updated


def sync_embeddings() -> int:
    """Upserts into ChromaDB all recipes present in SQLite but missing in ChromaDB."""
    recipes = get_all_recipes()
    synced = 0
    for r in recipes:
        rid = r["id"]
        if embed_exists(rid):
            continue
        ingredients = r.get("ingredients") or []
        if isinstance(ingredients, str):
            try:
                ingredients = json.loads(ingredients)
            except Exception:
                ingredients = []
        add_recipe(
            recipe_id=rid,
            title=r["title"],
            description=r.get("description"),
            ingredients=ingredients,
            metadata={
                "site":             r["site"],
                "duration_minutes": r.get("duration_minutes") or 0,
                "category":         r.get("category") or "",
            },
        )
        synced += 1
        if synced % 100 == 0:
            logger.info("  synced %d so far...", synced)
    return synced


def index_site(site: str, limit: int | None = None) -> int:
    pdf_dir = Path(PDF_DIRS[site])
    if not pdf_dir.exists():
        logger.warning("Directory not found: %s", pdf_dir)
        return 0

    parser = _PARSERS[site]
    indexed = 0

    for pdf_path in sorted(pdf_dir.glob("*.pdf")):
        pdf_path = pdf_path.resolve()
        if limit is not None and indexed >= limit:
            break
        if recipe_exists(str(pdf_path)):
            logger.debug("Already in SQLite: %s", pdf_path.name)
            continue
        try:
            data = parser(pdf_path)
            recipe_id = insert_recipe(data)
            if recipe_id is None:
                continue
            add_recipe(
                recipe_id=recipe_id,
                title=data["title"],
                description=data.get("description"),
                ingredients=data.get("ingredients") or [],
                metadata={
                    "site":             data["site"],
                    "duration_minutes": data.get("duration_minutes") or 0,
                    "category":         data.get("category") or "",
                },
            )
            indexed += 1
            logger.info("[%s] %s", site, data["title"][:70])
        except Exception as exc:
            logger.error("Failed %s: %s", pdf_path.name, exc, exc_info=True)

    return indexed


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="Index recipe PDFs into SQLite and ChromaDB. Independent of crawling."
    )
    parser.add_argument(
        "--sites", nargs="+",
        choices=list(PDF_DIRS) + ["all"],
        default=["all"], metavar="SITE",
    )
    parser.add_argument("--limit", type=int, default=None, metavar="N")
    parser.add_argument(
        "--sync-embeddings", action="store_true",
        help="Sync SQLite recipes missing from ChromaDB without re-crawling",
    )
    parser.add_argument(
        "--backfill-text", action="store_true",
        help="Remplir full_text pour les recettes déjà indexées sans texte complet",
    )
    args = parser.parse_args()

    init_db()
    init_chroma()

    if args.backfill_text:
        logger.info("=== Backfill full_text ===")
        n = backfill_full_text()
        logger.info("Done — %d recettes mises à jour", n)
        return

    if args.sync_embeddings:
        logger.info("=== Syncing embeddings (SQLite -> ChromaDB) ===")
        n = sync_embeddings()
        logger.info("Done - %d embeddings added", n)
        return

    sites = list(PDF_DIRS) if "all" in args.sites else args.sites

    total = 0
    for site in sites:
        logger.info("=== Indexing %s ===", site)
        n = index_site(site, args.limit)
        logger.info("%s: %d new recipes indexed", site, n)
        total += n

    logger.info("Done - total indexed: %d", total)


if __name__ == "__main__":
    main()

