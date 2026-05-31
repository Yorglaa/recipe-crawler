"""
Orchestration du chatbot de recettes.

Routing de recherche :
  - sql_duration  : durée seule
  - sql_category  : catégorie seule
  - hybrid        : ingredient + durée ou catégorie (intersection SQL)
  - rag           : tout le reste (recherche sémantique + SQL supplémentaire)

Affinement progressif (_active_filters) :
  - query       : requête de base (pour le ranking RAG sémantique)
  - ingredients : liste d'ingrédients requis (filtre SQL dur — intersection)
  - max_minutes : durée max en minutes (filtre SQL dur)
  - site        : filtre site (filtre dur)
  Ex: "curry" → "avec du poulet" → "en moins de 45 min" → "de chez migusto"
  conserve tous les filtres. Chaque recette retournée respecte toutes les contraintes.
"""

import logging
import os
import platform
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from pipeline import database
from pipeline.database import get_recipe_by_title as _get_by_title
from pipeline.query_parser import (
    _normalize, _extract_ingredients, _extract_minutes, _extract_site,
    _REFINE_PATTERN, _SHOW_ALL_PATTERN, _COUNT_PATTERN, _LIST_REQUEST_PATTERN,
    _DETAIL_PATTERNS, _PDF_OPEN_PATTERN, _extract_list_ref,
)
from pipeline.search_router import (
    search_recipes, _search_with_filters, _find_in_context,
    _search_for_detail, _compute_exact_count,
)
from pipeline.llm_client import _call_llm

_last_recipe_ids: list[int] = []
_last_disambig_titles: list[str] = []
_last_site: str | None = None

# Filtres de recherche persistants entre les tours.
# query : requête de base (RAG) ; ingredients/max_minutes/site : filtres SQL durs.
_active_filters: dict = {"query": "", "ingredients": [], "site": None, "max_minutes": None}

DISAMBIG_MARKER = "Plusieurs recettes correspondent"
_DISAMBIG_MARKER = DISAMBIG_MARKER  # alias interne


def reset_context() -> None:
    global _last_recipe_ids, _last_disambig_titles, _last_site, _active_filters
    _last_recipe_ids = []
    _last_disambig_titles = []
    _last_site = None
    _active_filters = {"query": "", "ingredients": [], "site": None, "max_minutes": None}


def _open_system_pdf(path: str) -> None:
    if platform.system() == "Windows":
        os.startfile(path)
    elif platform.system() == "Darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


def _is_detail_request(query: str) -> bool:
    return bool(_DETAIL_PATTERNS.search(_normalize(query)))


def _numbered_site_list(recipes: list[dict]) -> str:
    """Construit une liste numérotée compacte (titre + durée) pour un listing par site."""
    lines = []
    for i, r in enumerate(recipes, 1):
        line = f"{i}. **{r['title']}** ({r['site']})"
        if r.get("duration_minutes"):
            line += f" — {r['duration_minutes']} min"
        lines.append(line)
    return "\n".join(lines)


def _build_site_listing_msg(site: str, all_site: list[dict], user_message: str) -> str:
    sites = database.get_sites_summary()
    sites_str = "Sources : " + ", ".join(
        f"{s['site']} ({s['count']} recettes)" for s in sites
    ) + ".\n\n"
    site_context = _numbered_site_list(all_site[:50])
    trunc = (
        f"\n[Note : {len(all_site)} recettes au total pour {site}, "
        f"50 premières affichées. L'utilisateur peut demander 'les suivantes'.]\n"
        if len(all_site) > 50 else ""
    )
    num_instruction = (
        "\n[INSTRUCTION : La liste ci-dessus est numérotée. "
        "Reproduis-la telle quelle avec les numéros. "
        "L'utilisateur peut référencer une recette par son numéro (ex: 'recette 3', 'la 15').]\n"
    )
    return (
        sites_str
        + "Recettes disponibles :\n\n"
        + site_context + "\n"
        + trunc
        + num_instruction
        + "Question de l'utilisateur : " + user_message
    )


def _build_sources_str() -> str:
    sites = database.get_sites_summary()
    return "Sources : " + ", ".join(
        f"{s['site']} ({s['count']} recettes)" for s in sites
    ) + ".\n\n"


def format_context(recipes: list[dict], detailed: bool = False, numbered: bool = False) -> str:
    if not recipes:
        return "Aucune recette trouvee pour cette recherche."
    parts = []
    for i, r in enumerate(recipes, 1):
        prefix = f"{i}. " if numbered else ""
        lines = [f"{prefix}**{r['title']}** ({r['site']})"]
        if r.get("duration_minutes"):
            lines.append(f"Duree : {r['duration_minutes']} min")
        if r.get("category"):
            lines.append(f"Categorie : {r['category']}")
        if detailed and r.get("full_text"):
            lines.append("\n--- Recette complète ---")
            lines.append(r["full_text"][:4000])
        else:
            if r.get("description"):
                lines.append(r["description"])
            if r.get("ingredients"):
                ing = r["ingredients"]
                if isinstance(ing, list):
                    lines.append("Ingredients : " + ", ".join(ing[:10]))
                else:
                    lines.append(f"Ingredients : {ing}")
            if detailed and not r.get("full_text"):
                lines.append("[Texte complet non disponible pour cette recette]")
        parts.append("\n".join(lines))
    return "\n\n---\n\n".join(parts)


_DETAIL_INSTRUCTION = (
    "\nINSTRUCTION : Reproduis la recette complète telle qu'elle apparaît dans "
    "la section '--- Recette complète ---' : ingrédients, quantités, durées, "
    "et toutes les étapes de préparation. Ne résume pas, ne saute rien.\n"
)


def chat(message: str, history: list[dict]) -> str:
    global _last_disambig_titles, _last_recipe_ids, _last_site, _active_filters

    # ── Dernier message du bot (pour désambiguïsation) ──────────────────────
    last_bot_raw = next(
        (t["content"] for t in reversed(history) if t.get("role") == "assistant"),
        None,
    )
    if isinstance(last_bot_raw, list):
        last_bot = " ".join(p.get("text", "") for p in last_bot_raw if isinstance(p, dict))
    else:
        last_bot = last_bot_raw

    # ── Désambiguïsation ─────────────────────────────────────────────────────
    if last_bot and _DISAMBIG_MARKER in last_bot:
        num_val = None
        bare = re.match(r"^\s*(\d+)\s*$", message.strip())
        if bare:
            num_val = int(bare.group(1))
        elif _last_disambig_titles:
            ref = _extract_list_ref(message)
            if ref is not None:
                num_val = ref
        if num_val is not None and _last_disambig_titles:
            idx = num_val - 1
            if 0 <= idx < len(_last_disambig_titles):
                recipe = _get_by_title(_last_disambig_titles[idx])
                recipes = [recipe] if recipe else []
                if recipes:
                    _last_recipe_ids = [recipe["id"]]
                    context = format_context(recipes, detailed=True)
                    return _call_llm(
                        f"Recettes disponibles :\n\n{context}\n\n{_DETAIL_INSTRUCTION}"
                        f"Question de l'utilisateur : {message}",
                        history,
                    )

    # ── Référence numérique à la liste précédente ────────────────────────────
    if _last_recipe_ids:
        ref = _extract_list_ref(message)
        if ref is not None:
            idx = ref - 1
            if 0 <= idx < len(_last_recipe_ids):
                recipe = database.get_recipe_by_id(_last_recipe_ids[idx])
                if recipe:
                    _last_recipe_ids = [recipe["id"]]
                    context = format_context([recipe], detailed=True)
                    return _call_llm(
                        f"Recettes disponibles :\n\n{context}\n\n{_DETAIL_INSTRUCTION}"
                        f"Question de l'utilisateur : {message}",
                        history,
                    )

    # ── Listing complet d'un site ("montre les toutes") ──────────────────────
    if _last_site and _SHOW_ALL_PATTERN.search(message):
        all_site = database.search_by_site(_last_site)
        _last_recipe_ids = [r["id"] for r in all_site]
        return _call_llm(_build_site_listing_msg(_last_site, all_site, message), history)

    # ── Site détecté dans le message ─────────────────────────────────────────
    site_in_msg = _extract_site(message)
    # Affinement par site : message court (≤ 6 mots) avec contexte actif
    is_site_refinement = bool(site_in_msg and len(message.split()) <= 6 and _active_filters["query"])

    if site_in_msg and not is_site_refinement:
        # Listing complet du site
        _last_site = site_in_msg
        all_site = database.search_by_site(site_in_msg)
        _last_recipe_ids = [r["id"] for r in all_site]
        return _call_llm(_build_site_listing_msg(site_in_msg, all_site, message), history)

    # ── Comptage ──────────────────────────────────────────────────────────────
    if _COUNT_PATTERN.search(message):
        exact_count = _compute_exact_count(message)
        recipes, _ = search_recipes(message, n_results=20, fallback_ids=_last_recipe_ids)
        context = format_context(recipes)
        count_note = (
            f"\n[INSTRUCTION : L'utilisateur demande combien de recettes correspondent. "
            f"Le nombre exact est {exact_count}. "
            f"Commence ta réponse en mentionnant ce chiffre précis. "
            f"Les recettes ci-dessous sont des exemples parmi ces {exact_count}.]\n"
        )
        return _call_llm(
            _build_sources_str()
            + f"Recettes disponibles :\n\n{context}\n\n{count_note}"
            + f"Question de l'utilisateur : {message}",
            history,
        )

    # ── Listage du contexte courant ───────────────────────────────────────────
    if _last_recipe_ids and _LIST_REQUEST_PATTERN.search(message) and not _last_site:
        list_context = format_context(database.get_recipes_by_ids(_last_recipe_ids))
        return _call_llm(
            _build_sources_str()
            + f"Recettes disponibles :\n\n{list_context}\n\n"
            + f"Question de l'utilisateur : {message}",
            history,
        )

    # ── Ouverture PDF ─────────────────────────────────────────────────────────
    if _PDF_OPEN_PATTERN.search(message) and _last_recipe_ids:
        recipe = None
        ref = _extract_list_ref(message)
        if ref is not None:
            idx = ref - 1
            if 0 <= idx < len(_last_recipe_ids):
                recipe = database.get_recipe_by_id(_last_recipe_ids[idx])
        elif len(_last_recipe_ids) == 1:
            recipe = database.get_recipe_by_id(_last_recipe_ids[0])
        else:
            candidates = _find_in_context(message, _last_recipe_ids, semantic=False)
            if not candidates and last_bot:
                ctx_recipes = database.get_recipes_by_ids(_last_recipe_ids)
                q_last = _normalize(last_bot)
                candidates = [r for r in ctx_recipes if _normalize(r["title"]) in q_last]
            if len(candidates) == 1:
                recipe = candidates[0]
            elif 1 < len(candidates) <= 10:
                lines = [
                    f"{i + 1}. **{r['title']}** ({r['site']})" for i, r in enumerate(candidates)
                ]
                return "Plusieurs recettes correspondent — tapez le numéro :\n\n" + "\n".join(lines)
            elif len(candidates) > 10:
                return "Le contexte est encore trop large. Affinez d'abord votre recherche, puis redemandez le PDF."
        if recipe:
            pdf = recipe["pdf_path"]
            if Path(pdf).exists():
                _open_system_pdf(pdf)
                return f"PDF ouvert : **{recipe['title']}** ({recipe['site']})"
            return f"Fichier introuvable : {pdf}"
        return "Je n'ai pas pu identifier la recette. Précisez le nom ou tapez son numéro."

    # ── Détail d'une recette ──────────────────────────────────────────────────
    if _is_detail_request(message):
        recipes = _find_in_context(message, _last_recipe_ids, semantic=False) if _last_recipe_ids else []
        if not recipes and last_bot and _last_recipe_ids:
            ctx = database.get_recipes_by_ids(_last_recipe_ids)
            q_last = _normalize(last_bot)
            recipes = [r for r in ctx if _normalize(r["title"]) in q_last]
        if not recipes:
            recipes = _search_for_detail(message)
        if len(recipes) > 1:
            seen_titles: set[str] = set()
            unique: list[dict] = []
            for r in recipes:
                if r["title"] not in seen_titles:
                    seen_titles.add(r["title"])
                    unique.append(r)
            if len(unique) > 1:
                _last_disambig_titles = [r["title"] for r in unique]
                lines = [
                    f"{i + 1}. **{r['title']}** ({r['site']})"
                    + (f" — {r['duration_minutes']} min" if r.get("duration_minutes") else "")
                    for i, r in enumerate(unique)
                ]
                return (
                    f"{_DISAMBIG_MARKER} — tapez le numéro de votre choix "
                    f"ou précisez votre demande :\n\n"
                    + "\n".join(lines)
                )
            recipes = unique
        context = format_context(recipes, detailed=True)
        return _call_llm(
            _build_sources_str()
            + f"Recettes disponibles :\n\n{context}\n\n{_DETAIL_INSTRUCTION}"
            + f"Question de l'utilisateur : {message}",
            history,
        )

    # ── Recherche (nouvelle ou raffinement) ───────────────────────────────────
    if is_site_refinement:
        _active_filters["site"] = site_in_msg
    elif _REFINE_PATTERN.match(message) and _active_filters["query"]:
        # Raffinement : parse les nouveaux ingrédients et/ou la durée du message
        new_ings = _extract_ingredients(_normalize(message))
        new_minutes = _extract_minutes(message)
        for ing in new_ings:
            if ing not in _active_filters["ingredients"]:
                _active_filters["ingredients"].append(ing)
        if new_minutes:
            _active_filters["max_minutes"] = new_minutes
    else:
        # Nouvelle recherche : extraire d'emblée ingrédients et durée comme filtres durs
        q_norm = _normalize(message)
        _active_filters = {
            "query": message,
            "ingredients": _extract_ingredients(q_norm),
            "site": None,
            "max_minutes": _extract_minutes(message),
        }

    recipes, total_found = _search_with_filters(_active_filters, fallback_ids=_last_recipe_ids)
    _last_recipe_ids = [r["id"] for r in recipes]

    context = _numbered_site_list(recipes)
    num_instruction = (
        f"\n[INSTRUCTION ABSOLUE : La liste ci-dessus contient exactement {len(recipes)} recettes, "
        f"numérotées de 1 à {len(recipes)}. "
        f"Tu DOIS les reproduire TOUTES les {len(recipes)}, dans l'ordre, sans en omettre aucune. "
        "La numérotation commence obligatoirement à 1 — jamais à 2 ou autre. "
        "N'utilise PAS de puces (*). Ne réordonne PAS. Ne reformate PAS.]\n"
    )
    truncation_note = (
        f"\n[Note : {total_found} recettes correspondent au total. "
        f"La liste ci-dessus en contient exactement {len(recipes)} — affiche-les toutes. "
        f"Indique à l'utilisateur qu'il y a {total_found} recettes au total "
        f"et invite-le à affiner sa recherche (ingrédient, durée, catégorie, site...).]\n"
        if total_found > len(recipes) else ""
    )
    duration_minutes = _active_filters.get("max_minutes")
    duration_note = (
        f"\n[Note : Toutes les recettes ci-dessous ont une durée de préparation "
        f"de {duration_minutes} minutes ou moins — elles sont toutes filtrées par durée.]\n"
        if duration_minutes and recipes else ""
    )
    return _call_llm(
        _build_sources_str()
        + f"Recettes disponibles :\n\n{context}\n\n"
        + num_instruction
        + duration_note
        + truncation_note
        + f"Question de l'utilisateur : {message}",
        history,
    )
