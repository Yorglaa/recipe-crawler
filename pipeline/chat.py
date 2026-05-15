"""
Orchestration du chatbot de recettes.

Routing :
  - sql_duration  : questions sur le temps de preparation
  - sql_category  : questions sur la categorie de plat
  - hybrid        : ingredient + contrainte structuree
  - rag           : tout le reste (recherche semantique)
"""

import os
import platform
import re
import subprocess
import time
import unicodedata
from pathlib import Path

from google import genai
from google.genai import types

import config
from pipeline import database, embeddings
from pipeline.database import get_recipe_by_title as _get_by_title

_client: genai.Client | None = None
_last_recipe_ids: list[int] = []
_last_disambig_titles: list[str] = []
_last_site: str | None = None

DISAMBIG_MARKER = "Plusieurs recettes correspondent"
_DISAMBIG_MARKER = DISAMBIG_MARKER  # alias interne


def reset_context() -> None:
    global _last_recipe_ids, _last_disambig_titles, _last_site
    _last_recipe_ids = []
    _last_disambig_titles = []
    _last_site = None



_SYSTEM_PROMPT = """Tu es un assistant culinaire francophone specialise dans les recettes de cuisine.
Tu reponds uniquement en francais, de facon concise et chaleureuse.
Tu t'appuies exclusivement sur les recettes fournies dans le contexte pour repondre.
Quand plusieurs recettes correspondent a la demande, liste-les toutes avec leur titre et duree.
Pour chaque recette que tu mentionnes, indique TOUJOURS sa provenance entre parentheses apres le titre,
en utilisant exactement le nom du site tel qu'il apparait dans le contexte : viandesuisse, qoqa ou migusto.
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
    text = text.replace("’", "’").replace("‘", "’")  # apostrophes typographiques
    text = text.replace("œ", "oe").replace("Œ", "oe")
    text = text.replace("æ", "ae").replace("Æ", "ae")
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("ascii").lower()


def classify_query(query: str) -> str:
    q = _normalize(query)
    has_duration = bool(_DURATION_PATTERNS.search(q))
    has_category = any(kw in q for kw in _CATEGORY_KEYWORDS)

    if has_duration and has_category:
        return "hybrid"
    if has_duration:
        return "sql_duration"
    if has_category:
        return "sql_category"
    return "rag"


_NON_INGREDIENTS = {"recettes", "recette", "plats", "plat", "idees", "idee", "chose", "autres"}


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
        r"\bet\s+(?:de\s+l[' ]?|du\s+|de\s+la\s+|des\s+|au?\s+|aux\s+)([a-z]{3,})",
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
        sql_rows = database.search_by_duration(minutes) if minutes else []
        rag_rows = _rag(query, n_results)
        seen = {r["id"] for r in rag_rows}
        merged = list(rag_rows)
        for r in sql_rows:
            if r["id"] not in seen:
                merged.append(r)
                seen.add(r["id"])
        total_found = len(merged)
        results = merged[:n_results]

    else:  # rag — supplement with SQL ingredient + title search
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
                results = sorted(all_matching, key=lambda r: (0 if r["id"] in rag_ids else 1))[:n_results]
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

    # Stocker TOUS les IDs pour le mode détail
    _last_recipe_ids = [r["id"] for r in results]
    return results, total_found


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

    # Rien trouvé dans le contexte → l'appelant fera une recherche fraîche
    return []


_DETAIL_STOPWORDS = {
    "detaille", "detailler", "detaillons", "marche", "suivre", "donne", "montre",
    "etape", "etapes", "preparer", "preparation", "recette", "recettes",
    "instruction", "instructions", "comment", "faire", "cuire", "cuisiner",
    "procedure", "cuisson", "affiche", "explique", "temperature",
}

_KNOWN_SITES = {'viandesuisse', 'qoqa', 'migusto'}

_SHOW_ALL_PATTERN = re.compile(
    r"(toutes?|tout|liste\s+compl[eè]te?|compl[eè]tement|"
    r"montre[- ]les[- ]toutes?|affiche[- ]tout|toutes?\s+les\s+recettes?)",
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
    """Extrait un numero de reference a la liste precedente (1-based), ou None."""
    if re.match(r"^\s*(\d+)\s*$", message):
        return int(re.match(r"^\s*(\d+)\s*$", message).group(1))
    m = re.search(r"(?:recette|num[eé]ro|n[o°]\.?)\s*(\d+)", message, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


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


def chat(message: str, history: list[dict]) -> str:
    global _client, _last_disambig_titles, _last_recipe_ids, _last_site
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)

    # Désambiguïsation : si le dernier message du bot était une liste numérotée,
    # lire le choix de l'utilisateur directement depuis l'historique
    last_bot_raw = next(
        (t["content"] for t in reversed(history) if t.get("role") == "assistant"),
        None,
    )
    if isinstance(last_bot_raw, list):
        last_bot = " ".join(p.get("text", "") for p in last_bot_raw if isinstance(p, dict))
    else:
        last_bot = last_bot_raw
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
                    detail_instruction = (
                        "\nINSTRUCTION : Reproduis la recette complète telle qu'elle apparaît dans "
                        "la section '--- Recette complète ---' : ingrédients, quantités, durées, "
                        "et toutes les étapes de préparation. Ne résume pas, ne saute rien.\n"
                    )
                    return _call_llm(
                        f"Recettes disponibles :\n\n{context}\n\n{detail_instruction}"
                        f"Question de l'utilisateur : {message}",
                        history,
                    )

    # Reference numerique a la liste precedente : "recette 12", "numero 3", etc.
    if _last_recipe_ids:
        ref = _extract_list_ref(message)
        if ref is not None:
            idx = ref - 1
            if 0 <= idx < len(_last_recipe_ids):
                recipe = database.get_recipe_by_id(_last_recipe_ids[idx])
                if recipe:
                    context = format_context([recipe], detailed=True)
                    detail_instruction = (
                        chr(10) + 'INSTRUCTION : Reproduis la recette complete telle qu' + chr(39) + 'elle apparait dans '
                        'la section ' + chr(39) + '--- Recette complete ---' + chr(39) + ' : ingredients, quantites, durees, '
                        'et toutes les etapes de preparation. Ne resume pas, ne saute rien.' + chr(10)
                    )
                    return _call_llm(
                        'Recettes disponibles :' + chr(10) * 2 + context + chr(10) * 2 + detail_instruction
                        + 'Question de l' + chr(39) + 'utilisateur : ' + message,
                        history,
                    )

    # Suivi de contexte site : "montre les toutes", "affiche la liste complète", etc.
    if _last_site and _SHOW_ALL_PATTERN.search(message):
        site_recipes = database.search_by_site(_last_site, limit=200)
        _last_recipe_ids = [r['id'] for r in site_recipes]
        site_context = format_context(site_recipes[:50])
        sites = database.get_sites_summary()
        sites_str = 'Sources : ' + ', '.join(
            f"{s['site']} ({s['count']} recettes)" for s in sites
        ) + '.' + chr(10) * 2
        msg = (
            sites_str
            + 'Recettes disponibles :' + chr(10) * 2
            + site_context + chr(10) * 2
            + "Question de l'utilisateur : " + message
        )
        return _call_llm(msg, history)

    site = _extract_site(message)

    if site:
        _last_site = site
        site_recipes = database.search_by_site(site, limit=200)
        _last_recipe_ids = [r['id'] for r in site_recipes]
        site_context = format_context(site_recipes[:50])
        sites = database.get_sites_summary()
        sites_str = 'Sources : ' + ', '.join(
            f"{s['site']} ({s['count']} recettes)" for s in sites
        ) + '.' + chr(10) * 2
        msg = (
            sites_str
            + 'Recettes disponibles :' + chr(10) * 2
            + site_context + chr(10) * 2
            + "Question de l'utilisateur : " + message
        )
        return _call_llm(msg, history)

    if _COUNT_PATTERN.search(message):
        exact_count = _compute_exact_count(message)
        recipes, _ = search_recipes(message, n_results=20)
        context = format_context(recipes)
        sites_data = database.get_sites_summary()
        sites_str = "Sources : " + ", ".join(
            f"{s['site']} ({s['count']} recettes)" for s in sites_data
        ) + "." + chr(10) * 2
        count_note = (
            f"\n[INSTRUCTION : L'utilisateur demande combien de recettes correspondent. "
            f"Le nombre exact est {exact_count}. "
            f"Commence ta réponse en mentionnant ce chiffre précis. "
            f"Les recettes ci-dessous sont des exemples parmi ces {exact_count}.]\n"
        )
        msg = (
            sites_str
            + "Recettes disponibles :" + chr(10) * 2
            + context + chr(10) * 2
            + count_note
            + "Question de l'utilisateur : " + message
        )
        return _call_llm(msg, history)

    if _last_recipe_ids and _LIST_REQUEST_PATTERN.search(message) and not _last_site:
        list_recipes = database.get_recipes_by_ids(_last_recipe_ids)
        list_context = format_context(list_recipes)
        sites_data = database.get_sites_summary()
        sites_str = "Sources : " + ", ".join(
            f"{s['site']} ({s['count']} recettes)" for s in sites_data
        ) + "." + chr(10) * 2
        msg = (
            sites_str
            + "Recettes disponibles :" + chr(10) * 2
            + list_context + chr(10) * 2
            + "Question de l'utilisateur : " + message
        )
        return _call_llm(msg, history)

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

    is_detail = _is_detail_request(message)
    if is_detail:
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
        total_found = len(recipes)
    else:
        recipes, total_found = search_recipes(message, n_results=20)
    context = format_context(recipes, detailed=is_detail)

    detail_instruction = (
        "\nINSTRUCTION : Reproduis la recette complète telle qu'elle apparaît dans "
        "la section '--- Recette complète ---' : ingrédients, quantités, durées, "
        "et toutes les étapes de préparation. Ne résume pas, ne saute rien.\n"
        if is_detail else ""
    )
    sites = database.get_sites_summary()
    sites_str = "Sources : " + ", ".join(
        f"{s['site']} ({s['count']} recettes)" for s in sites
    ) + "." + chr(10) * 2
    truncation_note = (
        f"\n[Note : {total_found} recettes correspondent au total, seules les {len(recipes)} plus pertinentes "
        f"sont affichées. Informez l'utilisateur et invitez-le à affiner sa recherche "
        f"(ingrédient, durée, catégorie, site...).]\n"
        if total_found > len(recipes) else ""
    )
    context_block = (
        "Recettes disponibles :" + chr(10) * 2
        + context + chr(10) * 2
        + truncation_note
    )
    user_message_with_context = (
        sites_str
        + context_block
        + detail_instruction
        + "Question de l'utilisateur : " + message
    )

    return _call_llm(user_message_with_context, history)


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
    msg = (groq_prefix + user_message_with_context)[:8000]
    messages.append({"role": "user", "content": msg})
    response = client.chat.completions.create(
        model=config.GROQ_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
    )
    return "[fallback: " + config.GROQ_MODEL + "]" + chr(10) * 2 + response.choices[0].message.content


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
            prefix = "" if model_name == config.GEMINI_MODEL else ("[fallback: " + model_name + "]" + chr(10) * 2)
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

    return "⚠️ Tous les services IA sont indisponibles. Réessaie dans quelques minutes."
