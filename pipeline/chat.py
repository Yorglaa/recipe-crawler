"""
Orchestration du chatbot de recettes.

Routing :
  - sql_duration  : questions sur le temps de preparation
  - sql_category  : questions sur la categorie de plat
  - hybrid        : ingredient + contrainte structuree
  - rag           : tout le reste (recherche semantique)
"""

import re
import unicodedata

from google import genai
from google.genai import types

import config
from pipeline import database, embeddings

_client: genai.Client | None = None
_last_recipe_ids: list[int] = []


def reset_context() -> None:
    global _last_recipe_ids
    _last_recipe_ids = []

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
    r"comment cuisiner|comment la faire|comment le faire|donne.?moi|montre.?moi|"
    r"recette complete|explique|procedure|cuisson|affiche|"
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


def _extract_main_ingredient(normalized_query: str) -> str | None:
    m = re.search(
        r"(?:avec\s+(?:de\s+l[' ]?|du\s+|de\s+la\s+|des\s+)|"
        r"a\s+base\s+de\s+|contenant\s+|(?:des?\s+)?recettes?\s+(?:de\s+|du\s+|au?\s+|aux\s+))"
        r"([a-z]{3,})",
        normalized_query,
    )
    return m.group(1) if m else None


def search_recipes(query: str, n_results: int = 6) -> list[dict]:
    global _last_recipe_ids
    route = classify_query(query)
    q = _normalize(query)
    results: list[dict] = []

    if route == "sql_duration":
        minutes = _extract_minutes(query)
        if minutes:
            rows = database.search_by_duration(minutes)
            if rows:
                results = rows[:n_results]
        if not results:
            results = _rag(query, n_results)

    elif route == "sql_category":
        for kw in _CATEGORY_KEYWORDS:
            if kw in q:
                rows = database.search_by_category(kw)
                if rows:
                    results = rows[:n_results]
                    break
        if not results:
            results = _rag(query, n_results)

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
        results = merged[:n_results]

    else:  # rag — supplement with SQL ingredient search
        rag_rows = _rag(query, n_results)
        ingredient = _extract_main_ingredient(q)
        if ingredient:
            ing_rows = database.search_by_ingredient(ingredient)
            seen = {r["id"] for r in rag_rows}
            extra = [r for r in ing_rows if r["id"] not in seen]
            results = rag_rows + extra
        else:
            results = rag_rows

    # Réinjecter les recettes précédentes SEULEMENT si la recherche n'a rien trouvé
    if not results and _last_recipe_ids:
        results = database.get_recipes_by_ids(_last_recipe_ids[:6])

    # Stocker TOUS les IDs pour le mode détail
    _last_recipe_ids = [r["id"] for r in results]
    return results


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


def _find_in_context(query: str) -> list[dict]:
    """Trouve la recette la plus pertinente parmi celles du dernier résultat."""
    if not _last_recipe_ids:
        return []

    context_recipes = database.get_recipes_by_ids(_last_recipe_ids)
    q_norm = _normalize(query)

    # 1) Correspondance directe du titre dans la requête (cas "détaille moi X")
    by_title = [r for r in context_recipes if _normalize(r["title"]) in q_norm]
    if by_title:
        return by_title[:2]

    # 2) Overlap de mots-clés (> 3 caractères)
    q_words = {w for w in q_norm.split() if len(w) > 3}
    def _overlap(r: dict) -> int:
        return len({w for w in _normalize(r["title"]).split() if len(w) > 3} & q_words)
    scored = sorted(context_recipes, key=_overlap, reverse=True)
    if scored and _overlap(scored[0]) > 0:
        return scored[:1]

    # 3) Recherche sémantique filtrée au contexte
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
    "etape", "etapes", "prepar", "preparer", "preparation", "recette", "recettes",
    "instruction", "instructions", "comment", "faire", "cuire", "cuisiner",
    "procedure", "cuisson", "affiche", "explique", "temperature", "pour",
    "moi", "les", "une", "des", "tout", "bien",
}


def _search_for_detail(query: str) -> list[dict]:
    """Recherche une recette par mots-clés du titre, pour les demandes de détail hors contexte."""
    q_norm = _normalize(query)
    words = [w for w in q_norm.split() if len(w) > 4 and w not in _DETAIL_STOPWORDS]
    if len(words) >= 2:
        results = database.search_by_title_keywords(words[:3])
        if results:
            return results[:2]
    return search_recipes(query)


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
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)

    is_detail = _is_detail_request(message)
    if is_detail and _last_recipe_ids:
        # Cherche dans le contexte courant d'abord
        recipes = _find_in_context(message)
        if not recipes:
            # Pas dans le contexte → recherche par titre dans toute la DB
            recipes = _search_for_detail(message)
    elif is_detail:
        # Pas de contexte (après reset ou première question) → recherche directe
        recipes = _search_for_detail(message)
    else:
        recipes = search_recipes(message)
    context = format_context(recipes, detailed=is_detail)

    user_message_with_context = (
        f"Recettes disponibles :\n\n{context}\n\n"
        f"Question de l'utilisateur : {message}"
    )

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

    try:
        response = _client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                temperature=0.7,
            ),
        )
        return response.text
    except Exception as e:
        code = getattr(e, "status_code", None) or getattr(e, "code", None)
        if code in (502, 503):
            return "⚠️ Le service Gemini est temporairement indisponible (502/503). Réessaie dans quelques secondes."
        if code == 429:
            return "⚠️ Quota Gemini dépassé (429). Attends un moment avant de réessayer."
        return f"⚠️ Erreur inattendue ({type(e).__name__}). Réessaie ou redémarre l'application."
