"""
Parsing et classification des requêtes utilisateur.
Toutes les fonctions sont pures (pas d'état, pas d'accès DB).
"""
import re
import unicodedata

_NON_INGREDIENTS = {"recettes", "recette", "plats", "plat", "idees", "idee", "chose", "autres"}

_DURATION_PATTERNS = re.compile(
    r"\b(?:minutes?|mins?|heures?|hrs?|rapide|vite|express|"
    r"moins de|plus de|maximum|max|en \d+|sous \d+)\b",
    re.IGNORECASE,
)
_DURATION_VALUE = re.compile(r"(\d+)\s*(?:minutes?|mins?|h)", re.IGNORECASE)
_DURATION_CONTENT_STRIP = re.compile(
    r"\b(?:en\s+moins\s+de|moins\s+de|plus\s+de|en\s+\d+|sous|max(?:imum)?)\s*\d*"
    r"|\b(?:minutes?|mins?|heures?|hrs?|rapide|vite|express)\b"
    r"|\d+",
    re.IGNORECASE,
)
_FILLER = {"avec", "sans", "pour", "dans", "des", "les", "une", "un", "de", "du", "la", "le", "et", "ou"}

_CATEGORY_KEYWORDS = {
    "soupe", "veloute", "potage", "dessert", "gateau", "tarte", "entree",
    "salade", "gratin", "plat principal", "apero", "aperitif", "risotto",
    "pates", "pizza", "sandwich", "burger", "brunch", "petit-dejeuner",
}

_KNOWN_SITES = {"viandesuisse", "qoqa", "migusto", "fooby"}

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

_DETAIL_PATTERNS = re.compile(
    r"\b(?:detaill|etap|prepar|prechauff)"
    r"|\b(?:details?|marche.?a.?suivre|instruction|comment faire|comment cuire|"
    r"comment cuisiner|comment la faire|comment le faire|"
    r"recette complete|explique|procedure|cuisson|"
    r"temperature|degre|combien de temps|combien d.heures|"
    r"fais.?la|fais.?le|je veux faire|je vais faire|je la fais)\b",
    re.IGNORECASE,
)

_PDF_OPEN_PATTERN = re.compile(r"\bpdf\b", re.IGNORECASE)

_DETAIL_STOPWORDS = {
    "detaille", "detailler", "detaillons", "marche", "suivre", "donne", "montre",
    "etape", "etapes", "preparer", "preparation", "recette", "recettes",
    "instruction", "instructions", "comment", "faire", "cuire", "cuisiner",
    "procedure", "cuisson", "affiche", "explique", "temperature",
}


def _normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("œ", "oe").replace("Œ", "oe")
    text = text.replace("æ", "ae").replace("Æ", "ae")
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("ascii").lower()


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

    if not first and (extras or not ingredients):
        pre = normalized_query.split(" et ")[0]
        m = re.search(
            r"(?:du\s+|des\s+|de\s+la\s+|de\s+l[' ]?|au?\s+|aux\s+)([a-z]{3,})",
            pre,
        )
        if m and m.group(1) not in _NON_INGREDIENTS and m.group(1) not in ingredients:
            ingredients.insert(0, m.group(1))

    return ingredients


def _extract_minutes(query: str) -> int | None:
    m = _DURATION_VALUE.search(query)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(0).lower()
    if "h" in unit and "min" not in unit:
        return value * 60
    return value


def _extract_site(query: str) -> str | None:
    q = _normalize(query)
    for site in _KNOWN_SITES:
        if site in q:
            return site
    return None


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


def _extract_list_ref(message: str) -> int | None:
    """Extrait un numéro de référence à la liste précédente (1-based), ou None."""
    if re.match(r"^\s*(\d+)\s*$", message):
        return int(re.match(r"^\s*(\d+)\s*$", message).group(1))
    m = re.search(r"(?:recette|num[eé]ro|n[o°]\.?|pdf)\s*(\d+)", message, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None
