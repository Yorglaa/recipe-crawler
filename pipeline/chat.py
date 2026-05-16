"""
Orchestration du chatbot de recettes.

Routing de recherche :
  - sql_duration  : durée seule
  - sql_category  : catégorie seule
  - hybrid        : ingredient + durée ou catégorie (intersection SQL)
  - rag           : tout le reste (recherche sémantique + SQL supplémentaire)

Affinement progressif (_active_filters) :
  - query : requête accumulée entre les tours (ingrédients, durée, mots-clés)
  - site  : filtre site appliqué en post-search
  Ex: "agneau" → "de chez qoqa" → "en moins de 45 min" conserve tous les filtres.
"""

import logging
import os
import platform
import re
import subprocess
import time
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

from google import genai
from google.genai import types

import config
from pipeline import database, embeddings
from pipeline.database import get_recipe_by_title as _get_by_title

_client: genai.Client | None = None
_last_recipe_ids: list[int] = []
_last_disambig_titles: list[str] = []
_last_site: str | None = None

# Filtres de recherche persistants entre les tours.
# query : requête textuelle accumulée ; site : filtre post-search.
_active_filters: dict = {"query": "", "site": None}

DISAMBIG_MARKER = "Plusieurs recettes correspondent"
_DISAMBIG_MARKER = DISAMBIG_MARKER  # alias interne


def reset_context() -> None:
    global _last_recipe_ids, _last_disambig_titles, _last_site, _active_filters
    _last_recipe_ids = []
    _last_disambig_titles = []
    _last_site = None
    _active_filters = {"query": "", "site": None}


_SYSTEM_PROMPT = """Tu es un assistant culinaire francophone specialise dans les recettes de cuisine.
Tu reponds uniquement en francais, de facon concise et chaleureuse.
Tu t'appuies exclusivement sur les recettes fournies dans le contexte pour repondre.
Quand plusieurs recettes correspondent a la demande, liste-les toutes avec leur titre et duree.
Pour chaque recette que tu mentionnes, indique TOUJOURS sa provenance entre parentheses apres le titre,
en utilisant exactement le nom du site tel qu'il apparait dans le contexte : viandesuisse, qoqa, migusto ou fooby.
Exemple : "Poulet roti aux herbes (viandesuisse) — 45 min".
Si aucune recette pertinente n'est disponible, dis-le honnetement et propose une piste generale.
Ne mentionne jamais les noms de fichiers PDF ni les identifiants techniques.
N'hesite pas a interagir avec l'utilisateur : pose des questions de precision si la demande est vague
(ingredients disponibles, nombre de personnes, contraintes alimentaires, temps disponible, etc.)
afin de proposer des recettes vraiment adaptees a sa situation."""

_DETAIL_PATTERNS = re.compile(
    # Préfixes : \b au début seulement — matchent toutes les conjugaisons/formes
    r"\b(?:detaill|etap|prepar|prechauff)"
    # Mots complets : \b des deux côtés
    r"|\b(?:details?|marche.?a.?suivre|instruction|comment faire|comment cuire|"
    r"comment cuisiner|comment la faire|comment le faire|"
    r"recette complete|explique|procedure|cuisson|"
    r"temperature|degre|combien de temps|combien d.heures|"
    r"fais.?la|fais.?le|je veux faire|je vais faire|je la fais)\b",
    re.IGNORECASE,
)

_DURATION_PATTERNS = re.compile(
    r"\b(?:minutes?|mins?|heures?|hrs?|rapide|vite|express|"
    r"moins de|plus de|maximum|max|en \d+|sous \d+)\b",
    re.IGNORECASE,
)
_DURATION_VALUE = re.compile(r"(\d+)\s*(?:minutes?|mins?|h)", re.IGNORECASE)

_PDF_OPEN_PATTERN = re.compile(r"\bpdf\b", re.IGNORECASE)


def _open_system_pdf(path: str) -> None:
    if platform.system() == "Windows":
        os.startfile(path)
    elif platform.system() == "Darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


_CATEGORY_KEYWORDS = {
    "soupe", "veloute", "potage", "dessert", "gateau", "tarte", "entree",
    "salade", "gratin", "plat principal", "apero", "aperitif", "risotto",
    "pates", "pizza", "sandwich", "burger", "brunch", "petit-dejeuner",
}


def _normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")  # apostrophes typographiques
    text = text.replace("œ", "oe").replace("Œ", "oe")
    text = text.replace("æ", "ae").replace("Æ", "ae")
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("ascii").lower()


_NON_INGREDIENTS = {"recettes", "recette", "plats", "plat", "idees", "idee", "chose", "autres"}

# Mots à exclure pour détecter du contenu sémantique dans une requête durée
_DURATION_CONTENT_STRIP = re.compile(
    r"\b(?:en\s+moins\s+de|moins\s+de|plus\s+de|en\s+\d+|sous|max(?:imum)?)\s*\d*"
    r"|\b(?:minutes?|mins?|heures?|hrs?|rapide|vite|express)\b"
    r"|\d+",
    re.IGNORECASE,
)
_FILLER = {"avec", "sans", "pour", "dans", "des", "les", "une", "un", "de", "du", "la", "le", "et", "ou"}


def _has_semantic_content(normalized_query: str) -> bool:
    """True si la requête contient du contenu au-delà des contraintes de durée."""
    stripped = _DURATION_CONTENT_STRIP.sub("", normalized_query)
    words = [w for w in re.split(r"[\s,]+", stripped) if len(w) >= 3 and w not in _FILLER | _NON_INGREDIENTS]
    return bool(words)


def _extract_ingredients(normalized_query: str) -> list[str]:
    """Extrait tous les ingrédients de la requête (premier + conjonctions 'et')."""
    first = re.search(
        r"(?:avec\s+(?:de\s+l[' ]?|du\s+|de\s+la\s+|des\s+)|"
        r"a\s+base\s+de\s+|contenant\s+|(?:des?\s+)?recettes?\s+(?:de\s+|d[' ]\s*|du\s+|au?\s+|aux\s+))"
        r"([a-z]{3,})",
        normalized_query,
    )
    ingredients = [first.group(1)] if first else []
    extras = re.findall(
        r"\bet\s+(?:avec\s+)?(?:de\s+l[' ]?|du\s+|de\s+la\s+|des\s+|au?\s+|aux\s+)([a-z]{3,})",
        normalized_query,
    )
    for ing in extras:
        if ing not in ingredients:
            ingredients.append(ing)

    # Cas "du X et des Y" sans contexte verbal : cherche article+ingrédient avant le premier "et"
    if not first and (extras or not ingredients):
        pre = normalized_query.split(" et ")[0]
        m = re.search(
            r"(?:du\s+|des\s+|de\s+la\s+|de\s+l[' ]?|au?\s+|aux\s+)([a-z]{3,})",
            pre,
        )
        if m and m.group(1) not in _NON_INGREDIENTS and m.group(1) not in ingredients:
            ingredients.insert(0, m.group(1))

    return ingredients


def classify_query(query: str) -> str:
    q = _normalize(query)
    has_duration = bool(_DURATION_PATTERNS.search(q))
    has_category = any(kw in q for kw in _CATEGORY_KEYWORDS)
    has_ingredient = bool(_extract_ingredients(q))

    if has_duration and (has_category or has_ingredient or _has_semantic_content(q)):
        return "hybrid"
    if has_duration:
        return "sql_duration"
    if has_category:
        return "sql_category"
    return "rag"


def _rag(query: str, n_results: int) -> list[dict]:
    hits = embeddings.semantic_search(query, n_results=n_results)
    ids = [int(h["recipe_id"]) for h in hits if h.get("recipe_id") is not None]
    return database.get_recipes_by_ids(ids)


def _extract_minutes(query: str) -> int | None:
    m = _DURATION_VALUE.search(query)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(0).lower()
    if "h" in unit and "min" not in unit:
        return value * 60
    return value


def search_recipes(query: str, n_results: int = 6) -> tuple[list[dict], int]:
    """Retourne (results, total_found) où total_found est le nombre avant troncature."""
    global _last_recipe_ids
    route = classify_query(query)
    q = _normalize(query)
    results: list[dict] = []
    total_found: int = 0

    if route == "sql_duration":
        minutes = _extract_minutes(query)
        if minutes:
            rows = database.search_by_duration(minutes)
            if rows:
                total_found = len(rows)
                results = rows[:n_results]
        if not results:
            results = _rag(query, n_results)
            total_found = len(results)

    elif route == "sql_category":
        for kw in _CATEGORY_KEYWORDS:
            if kw in q:
                rows = database.search_by_category(kw)
                if rows:
                    total_found = len(rows)
                    results = rows[:n_results]
                    break
        if not results:
            results = _rag(query, n_results)
            total_found = len(results)

    elif route == "hybrid":
        minutes = _extract_minutes(query)
        ingredients = _extract_ingredients(q)

        if minutes and ingredients:
            # Intersection : recettes qui correspondent aux ingrédients ET à la durée
            ing_ids: set[int] = set()
            for i, ing in enumerate(ingredients):
                ids = {r["id"] for r in database.search_by_ingredient(ing)}
                ids |= {r["id"] for r in database.search_by_title_keywords([ing])}
                ing_ids = ids if i == 0 else ing_ids & ids
            duration_ids = {r["id"] for r in database.search_by_duration(minutes)}
            intersection = ing_ids & duration_ids
            if intersection:
                all_matching = database.get_recipes_by_ids(list(intersection))
                rag_ids = {r["id"] for r in _rag(query, n_results)}
                total_found = len(all_matching)
                results = sorted(
                    all_matching, key=lambda r: (0 if r["id"] in rag_ids else 1)
                )[:n_results]
            else:
                # Aucune intersection : union, priorité aux ingrédients
                all_matching = database.get_recipes_by_ids(list(ing_ids | duration_ids))
                total_found = len(all_matching)
                results = all_matching[:n_results]
        elif minutes:
            # Durée + contenu sémantique sans ingrédient structuré (ex: "curry en 30 min")
            # RAG sur la requête complète, puis filtrage par durée
            rag_rows = _rag(query, n_results * 5)
            duration_filtered = [
                r for r in rag_rows
                if r.get("duration_minutes") and r["duration_minutes"] <= minutes
            ]
            if duration_filtered:
                total_found = len(duration_filtered)
                results = duration_filtered[:n_results]
            else:
                # Rien de filtré : fallback durée seule
                sql_rows = database.search_by_duration(minutes)
                total_found = len(sql_rows)
                results = sql_rows[:n_results]
        else:
            # Durée + catégorie : union RAG + SQL durée
            sql_rows = database.search_by_duration(minutes) if minutes else []
            rag_rows = _rag(query, n_results)
            seen = {r["id"] for r in rag_rows}
            merged = list(rag_rows) + [r for r in sql_rows if r["id"] not in seen]
            total_found = len(merged)
            results = merged[:n_results]

    else:  # rag — complété avec recherche SQL par ingrédient
        rag_rows = _rag(query, n_results)
        ingredients = _extract_ingredients(q)
        if len(ingredients) >= 2:
            per_ing = []
            for ing in ingredients:
                ids = {r["id"] for r in database.search_by_ingredient(ing)}
                ids |= {r["id"] for r in database.search_by_title_keywords([ing])}
                per_ing.append(ids)
            intersection = set.intersection(*per_ing)
            if intersection:
                all_matching = database.get_recipes_by_ids(list(intersection))
                rag_ids = {r["id"] for r in rag_rows}
                total_found = len(all_matching)
                results = sorted(
                    all_matching, key=lambda r: (0 if r["id"] in rag_ids else 1)
                )[:n_results]
            else:
                results = rag_rows
                total_found = len(results)
        elif ingredients:
            ing_rows = database.search_by_ingredient(ingredients[0])
            title_rows = database.search_by_title_keywords([ingredients[0]])
            seen = {r["id"] for r in rag_rows}
            extra = [r for r in ing_rows + title_rows if r["id"] not in seen]
            all_candidates = rag_rows + extra
            total_found = len(all_candidates)
            results = all_candidates[:n_results]
        else:
            results = rag_rows
            total_found = len(results)

    # Réinjecter les recettes précédentes SEULEMENT si la recherche n'a rien trouvé
    if not results and _last_recipe_ids:
        results = database.get_recipes_by_ids(_last_recipe_ids[:6])
        total_found = len(results)

    _last_recipe_ids = [r["id"] for r in results]
    return results, total_found


def _search_with_filters(filters: dict, n_results: int = 20) -> tuple[list[dict], int]:
    """search_recipes + filtre site post-search si actif.
    Augmente n_results en amont pour compenser la réduction du filtre site."""
    broad_n = 300 if filters.get("site") else n_results
    results, total = search_recipes(filters["query"], n_results=broad_n)
    if filters.get("site"):
        results = [r for r in results if r["site"] == filters["site"]]
        total = len(results)
    return results[:n_results], total


def _is_detail_request(query: str) -> bool:
    return bool(_DETAIL_PATTERNS.search(_normalize(query)))


def _find_in_context(query: str, semantic: bool = True) -> list[dict]:
    """Trouve la recette la plus pertinente parmi celles du dernier résultat."""
    if not _last_recipe_ids:
        return []

    context_recipes = database.get_recipes_by_ids(_last_recipe_ids)
    q_norm = _normalize(query)

    # 1) Correspondance directe du titre dans la requête (cas "détaille moi X")
    by_title = [r for r in context_recipes if _normalize(r["title"]) in q_norm]
    if by_title:
        return by_title

    # 2) Overlap de mots-clés (> 3 caractères, sans mots de requête parasites)
    q_words = {w for w in re.split(r"[\s']+", q_norm) if len(w) > 3 and w not in _DETAIL_STOPWORDS}

    def _overlap(r: dict) -> int:
        return len({w for w in re.split(r"[\s']+", _normalize(r["title"])) if len(w) > 3} & q_words)

    scored = sorted(context_recipes, key=_overlap, reverse=True)
    top_score = _overlap(scored[0]) if scored else 0
    if top_score > 0:
        return [r for r in scored if _overlap(r) == top_score]

    # 3) Recherche sémantique filtrée au contexte (désactivable pour les requêtes de navigation)
    if semantic:
        context_set = set(_last_recipe_ids)
        hits = embeddings.semantic_search(query, n_results=min(20, len(context_set)))
        matched = [
            int(h["recipe_id"]) for h in hits
            if h.get("recipe_id") is not None and int(h["recipe_id"]) in context_set
        ]
        if matched:
            return database.get_recipes_by_ids(matched[:2])

    return []


_DETAIL_STOPWORDS = {
    "detaille", "detailler", "detaillons", "marche", "suivre", "donne", "montre",
    "etape", "etapes", "preparer", "preparation", "recette", "recettes",
    "instruction", "instructions", "comment", "faire", "cuire", "cuisiner",
    "procedure", "cuisson", "affiche", "explique", "temperature",
}

_KNOWN_SITES = {"viandesuisse", "qoqa", "migusto", "fooby"}

# Affinement ingrédients : phrases qui prolongent la recherche précédente
# Affinement durée : "en moins de 30 min", "rapide", "sous 45 min", etc.
_REFINE_PATTERN = re.compile(
    r"^\s*(?:"
    r"avec\b|sans\b"
    r"|et\s+(?:du|de\s+la|des|aussi|avec)\b"
    r"|mais\s+(?:sans|avec)\b"
    r"|aussi\s+avec\b"
    r"|en\s+moins\b"
    r"|moins\s+de\b"
    r"|en\s+\d+"
    r"|(?:sous|max(?:imum)?)\s*\d+"
    r"|(?:rapide|vite|express)\b"
    r")",
    re.IGNORECASE,
)

_SHOW_ALL_PATTERN = re.compile(
    r"(toutes?|tout|liste\s+compl[eè]te?|compl[eè]tement|"
    r"montre[- ]les[- ]toutes?|affiche[- ]tout|toutes?\s+les\s+recettes?)",
    re.IGNORECASE,
)

_COUNT_PATTERN = re.compile(
    r"\bcombien\b.*\brecettes?\b|\bcombien\b.*\bplats?\b"
    r"|\btu\s+en\s+as\s+combien\b|\bcombien\s+en\s+as[- ]tu\b"
    r"|\bcombien\s+as[- ]tu\s+de\b",
    re.IGNORECASE,
)

_LIST_REQUEST_PATTERN = re.compile(
    r"\b(?:la\s+liste|liste[- ]les|donne[- ]moi\s+la\s+liste"
    r"|montre[- ](?:moi\s+)?la\s+liste|affiche[- ](?:la\s+)?liste)\b",
    re.IGNORECASE,
)


def _compute_exact_count(query: str) -> int:
    q = _normalize(query)

    site = _extract_site(query)
    if site:
        sites = database.get_sites_summary()
        for s in sites:
            if s["site"] == site:
                return s["count"]
        return 0

    ingredients = _extract_ingredients(q)
    if len(ingredients) >= 2:
        per_ing = []
        for ing in ingredients:
            ids = {r["id"] for r in database.search_by_ingredient(ing)}
            ids |= {r["id"] for r in database.search_by_title_keywords([ing])}
            per_ing.append(ids)
        return len(set.intersection(*per_ing))
    if ingredients:
        ing = ingredients[0]
        ids = {r["id"] for r in database.search_by_ingredient(ing)}
        ids |= {r["id"] for r in database.search_by_title_keywords([ing])}
        return len(ids)

    for kw in _CATEGORY_KEYWORDS:
        if kw in q:
            return len(database.search_by_category(kw))

    minutes = _extract_minutes(query)
    if minutes:
        return len(database.search_by_duration(minutes))

    return database.count_recipes()


def _extract_site(query: str) -> str | None:
    q = _normalize(query)
    for site in _KNOWN_SITES:
        if site in q:
            return site
    return None


def _search_for_detail(query: str) -> list[dict]:
    """Recherche une recette par mots-clés du titre, pour les demandes de détail hors contexte."""
    q_norm = _normalize(query)
    words = [w for w in re.split(r"[\s']+", q_norm) if len(w) > 4 and w not in _DETAIL_STOPWORDS]
    if len(words) >= 2:
        results = database.search_by_title_keywords(words[:3])
        if results:
            return results[:2]
    results, _ = search_recipes(query)
    return results


def _extract_list_ref(message: str) -> int | None:
    """Extrait un numéro de référence à la liste précédente (1-based), ou None."""
    if re.match(r"^\s*(\d+)\s*$", message):
        return int(re.match(r"^\s*(\d+)\s*$", message).group(1))
    m = re.search(r"(?:recette|num[eé]ro|n[o°]\.?)\s*(\d+)", message, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


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


def format_context(recipes: list[dict], detailed: bool = False) -> str:
    if not recipes:
        return "Aucune recette trouvee pour cette recherche."
    parts = []
    for r in recipes:
        lines = [f"**{r['title']}** ({r['site']})"]
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
    global _client, _last_disambig_titles, _last_recipe_ids, _last_site, _active_filters
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)

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
        recipes, _ = search_recipes(message, n_results=20)
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
            candidates = _find_in_context(message, semantic=False)
            if not candidates and last_bot:
                ctx_recipes = database.get_recipes_by_ids(_last_recipe_ids)
                q_last = _normalize(last_bot)
                candidates = [r for r in ctx_recipes if _normalize(r["title"]) in q_last]
            if len(candidates) == 1:
                recipe = candidates[0]
            elif 1 < len(candidates) <= 5:
                lines = [
                    f"{i + 1}. **{r['title']}** ({r['site']})" for i, r in enumerate(candidates)
                ]
                return "Plusieurs recettes correspondent — tapez le numéro :\n\n" + "\n".join(lines)
            elif len(candidates) > 5:
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
        recipes = _find_in_context(message, semantic=False) if _last_recipe_ids else []
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
        _active_filters["query"] += " " + message.strip()
    else:
        _active_filters = {"query": message, "site": None}

    recipes, total_found = _search_with_filters(_active_filters)
    _last_recipe_ids = [r["id"] for r in recipes]

    context = format_context(recipes)
    truncation_note = (
        f"\n[Note : {total_found} recettes correspondent au total, seules les {len(recipes)} plus pertinentes "
        f"sont affichées. Informez l'utilisateur et invitez-le à affiner sa recherche "
        f"(ingrédient, durée, catégorie, site...).]\n"
        if total_found > len(recipes) else ""
    )
    duration_minutes = _extract_minutes(message)
    duration_note = (
        f"\n[Note : Toutes les recettes ci-dessous ont une durée de préparation "
        f"de {duration_minutes} minutes ou moins — elles sont toutes filtrées par durée.]\n"
        if duration_minutes and recipes else ""
    )
    return _call_llm(
        _build_sources_str()
        + f"Recettes disponibles :\n\n{context}\n\n"
        + duration_note
        + truncation_note
        + f"Question de l'utilisateur : {message}",
        history,
    )


def _call_groq(user_message_with_context: str, history: list[dict]) -> str:
    from groq import Groq
    client = Groq(api_key=config.GROQ_API_KEY)
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for turn in history[-4:]:
        role = "user" if turn["role"] == "user" else "assistant"
        turn_content = turn["content"]
        if isinstance(turn_content, list):
            turn_content = " ".join(p.get("text", "") for p in turn_content if isinstance(p, dict))
        messages.append({"role": role, "content": turn_content})
    groq_prefix = (
        "INSTRUCTION ABSOLUE : base-toi UNIQUEMENT sur les recettes listées "
        "ci-dessous pour répondre. Si des recettes sont présentes dans le contexte, "
        "utilise-les — ne dis jamais que les informations sont manquantes ou imprécises "
        "si elles figurent dans le contexte.\n\n"
    )
    msg = (groq_prefix + user_message_with_context)[:25000]
    messages.append({"role": "user", "content": msg})
    response = client.chat.completions.create(
        model=config.GROQ_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
    )
    return f"[fallback: {config.GROQ_MODEL}]\n\n{response.choices[0].message.content}"


def _call_llm(user_message_with_context: str, history: list[dict]) -> str:
    contents: list[types.Content] = []
    for turn in history:
        role = "user" if turn["role"] == "user" else "model"
        content = turn["content"]
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        contents.append(
            types.Content(role=role, parts=[types.Part(text=content)])
        )
    contents.append(
        types.Content(role="user", parts=[types.Part(text=user_message_with_context)])
    )

    gen_config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        temperature=0.7,
        max_output_tokens=4096,
    )
    fallback = getattr(config, "GEMINI_FALLBACK_MODEL", None)
    for model_name in ([config.GEMINI_MODEL] + ([fallback] if fallback else [])):
        try:
            response = _client.models.generate_content(
                model=model_name,
                contents=contents,
                config=gen_config,
            )
            prefix = "" if model_name == config.GEMINI_MODEL else f"[fallback: {model_name}]\n\n"
            return prefix + response.text
        except Exception as e:
            print(f"[LLM] {model_name} failed: {e}")
            if model_name != config.GEMINI_MODEL:
                break
            time.sleep(2)

    groq_key = getattr(config, "GROQ_API_KEY", "")
    groq_model = getattr(config, "GROQ_MODEL", "")
    if groq_key and groq_model:
        try:
            return _call_groq(user_message_with_context, history)
        except Exception as e:
            print(f"[LLM] Groq failed: {e}")

    return "Tous les services IA sont indisponibles. Reessaie dans quelques minutes."
