"""
Routage des recherches de recettes : SQL, RAG, hybride.
Les fonctions sont sans état — la gestion de _last_recipe_ids reste dans chat.py.
"""
import logging
import re

from pipeline import database, embeddings
from pipeline.query_parser import (
    _normalize, _extract_ingredients, _extract_minutes, _extract_site,
    classify_query,
    _CATEGORY_KEYWORDS, _FILLER, _NON_INGREDIENTS, _DETAIL_STOPWORDS,
)

logger = logging.getLogger(__name__)


def _rag(query: str, n_results: int) -> list[dict]:
    hits = embeddings.semantic_search(query, n_results=n_results)
    ids = [int(h["recipe_id"]) for h in hits if h.get("recipe_id") is not None]
    return database.get_recipes_by_ids(ids)


def search_recipes(
    query: str,
    n_results: int = 6,
    fallback_ids: list[int] | None = None,
) -> tuple[list[dict], int]:
    """Retourne (results, total_found) où total_found est le nombre avant troncature.

    fallback_ids : IDs à retourner si la recherche ne trouve rien (état géré par l'appelant).
    """
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
            ing_ids: set[int] | None = None
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
                all_matching = database.get_recipes_by_ids(list(ing_ids | duration_ids))
                total_found = len(all_matching)
                results = all_matching[:n_results]
        elif minutes:
            rag_rows = _rag(query, n_results * 5)
            duration_filtered = [
                r for r in rag_rows
                if r.get("duration_minutes") and r["duration_minutes"] <= minutes
            ]
            if duration_filtered:
                total_found = len(duration_filtered)
                results = duration_filtered[:n_results]
            else:
                sql_rows = database.search_by_duration(minutes)
                total_found = len(sql_rows)
                results = sql_rows[:n_results]
        else:
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
            ing_ids = {r["id"] for r in ing_rows}
            all_sql = ing_rows + [r for r in title_rows if r["id"] not in ing_ids]
            if all_sql:
                rag_ids = {r["id"] for r in rag_rows}
                total_found = len(all_sql)
                results = sorted(all_sql, key=lambda r: (0 if r["id"] in rag_ids else 1))[:n_results]
            else:
                results = rag_rows
                total_found = len(results)
        else:
            kws = [
                w for w in re.split(r"[\s']+", q)
                if len(w) >= 4 and w not in _FILLER | _NON_INGREDIENTS
            ]
            if kws:
                title_rows = database.search_by_title_keywords(kws[:2])
                if title_rows:
                    rag_ids = {r["id"] for r in rag_rows}
                    total_found = len(title_rows)
                    results = sorted(
                        title_rows, key=lambda r: (0 if r["id"] in rag_ids else 1)
                    )[:n_results]
                else:
                    results = rag_rows
                    total_found = len(results)
            else:
                results = rag_rows
                total_found = len(results)

    if not results and fallback_ids:
        results = database.get_recipes_by_ids(fallback_ids[:6])
        total_found = len(results)

    return results, total_found


def _search_with_filters(
    filters: dict,
    n_results: int = 20,
    fallback_ids: list[int] | None = None,
) -> tuple[list[dict], int]:
    """Applique les filtres durs (ingrédients, durée, site) puis rank par RAG.

    Garantit que chaque résultat respecte TOUTES les contraintes SQL actives.
    Le RAG est utilisé uniquement pour le classement, jamais comme filtre.
    """
    query = filters["query"]
    ingredients: list[str] = filters.get("ingredients") or []
    site: str | None = filters.get("site")
    max_minutes: int | None = filters.get("max_minutes")

    constrained_ids: set[int] | None = None

    if ingredients:
        ing_ids: set[int] | None = None
        for ing in ingredients:
            title_ids = {r["id"] for r in database.search_by_title_keywords([ing])}
            ids = title_ids if title_ids else {r["id"] for r in database.search_by_ingredient(ing)}
            ing_ids = ids if ing_ids is None else ing_ids & ids
        constrained_ids = ing_ids if ing_ids is not None else set()

    if max_minutes:
        dur_ids = {r["id"] for r in database.search_by_duration(max_minutes)}
        constrained_ids = constrained_ids & dur_ids if constrained_ids is not None else dur_ids

    if constrained_ids is not None:
        if not constrained_ids:
            return [], 0
        all_matching = database.get_recipes_by_ids(list(constrained_ids))
        if site:
            all_matching = [r for r in all_matching if r["site"] == site]
        if not all_matching:
            return [], 0
        rag_rows = _rag(query, n_results)
        rag_ids = {r["id"] for r in rag_rows}
        total_found = len(all_matching)
        results = sorted(all_matching, key=lambda r: (0 if r["id"] in rag_ids else 1))[:n_results]
        return results, total_found

    broad_n = 300 if site else n_results
    results, total = search_recipes(query, n_results=broad_n, fallback_ids=fallback_ids)
    if site:
        results = [r for r in results if r["site"] == site]
        total = len(results)
    return results[:n_results], total


def _find_in_context(query: str, last_recipe_ids: list[int], semantic: bool = True) -> list[dict]:
    """Trouve la recette la plus pertinente parmi celles du dernier résultat."""
    if not last_recipe_ids:
        return []

    context_recipes = database.get_recipes_by_ids(last_recipe_ids)
    q_norm = _normalize(query)

    by_title = [r for r in context_recipes if _normalize(r["title"]) in q_norm]
    if by_title:
        return by_title

    q_words = {w for w in re.split(r"[\s']+", q_norm) if len(w) > 3 and w not in _DETAIL_STOPWORDS}

    def _overlap(r: dict) -> int:
        return len({w for w in re.split(r"[\s']+", _normalize(r["title"])) if len(w) > 3} & q_words)

    scored = sorted(context_recipes, key=_overlap, reverse=True)
    top_score = _overlap(scored[0]) if scored else 0
    if top_score > 0:
        return [r for r in scored if _overlap(r) == top_score]

    if semantic:
        context_set = set(last_recipe_ids)
        hits = embeddings.semantic_search(query, n_results=min(20, len(context_set)))
        matched = [
            int(h["recipe_id"]) for h in hits
            if h.get("recipe_id") is not None and int(h["recipe_id"]) in context_set
        ]
        if matched:
            return database.get_recipes_by_ids(matched[:2])

    return []


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
