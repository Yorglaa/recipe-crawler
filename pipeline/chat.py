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

_SYSTEM_PROMPT = """Tu es un assistant culinaire francophone specialise dans les recettes de cuisine.
Tu reponds uniquement en francais, de facon concise et chaleureuse.
Tu t'appuies exclusivement sur les recettes fournies dans le contexte pour repondre.
Quand plusieurs recettes correspondent a la demande, liste-les toutes avec leur titre et duree.
Si aucune recette pertinente n'est disponible, dis-le honnetement et propose une piste generale.
Ne mentionne jamais les noms de fichiers PDF ni les identifiants techniques.
N'hesite pas a interagir avec l'utilisateur : pose des questions de precision si la demande est vague
(ingredients disponibles, nombre de personnes, contraintes alimentaires, temps disponible, etc.)
afin de proposer des recettes vraiment adaptees a sa situation."""

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
        r"(?:avec\s+(?:de\s+l[' ]|du\s+|de\s+la\s+|des\s+)|"
        r"a\s+base\s+de\s+|contenant\s+|recette\s+(?:de\s+|au?\s+|aux\s+))"
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

    # Always include previously cited recipes (support follow-up questions)
    if _last_recipe_ids:
        seen_ids = {r["id"] for r in results}
        prev = database.get_recipes_by_ids(_last_recipe_ids)
        results = results + [r for r in prev if r["id"] not in seen_ids]

    _last_recipe_ids = [r["id"] for r in results[:n_results]]
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


def format_context(recipes: list[dict]) -> str:
    if not recipes:
        return "Aucune recette trouvee pour cette recherche."
    parts = []
    for r in recipes:
        lines = [f"**{r['title']}** ({r['site']})"]
        if r.get("duration_minutes"):
            lines.append(f"Duree : {r['duration_minutes']} min")
        if r.get("category"):
            lines.append(f"Categorie : {r['category']}")
        if r.get("description"):
            lines.append(r["description"])
        if r.get("ingredients"):
            ing = r["ingredients"]
            if isinstance(ing, list):
                lines.append("Ingredients : " + ", ".join(ing[:10]))
            else:
                lines.append(f"Ingredients : {ing}")
        parts.append("\n".join(lines))
    return "\n\n---\n\n".join(parts)


def chat(message: str, history: list[dict]) -> str:
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)

    recipes = search_recipes(message)
    context = format_context(recipes)

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

    response = _client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.7,
        ),
    )
    return response.text
