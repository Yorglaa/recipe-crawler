"""
Gestion SQLite des metadonnees de recettes.
Thread-safe : toutes les ecritures sont serialisees via un Lock.
"""

import json
import sqlite3
import threading
from pathlib import Path

import config

_lock = threading.Lock()
_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        db_path = Path(config.SQLITE_DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def init_db() -> None:
    with _lock:
        _conn().executescript("""
            CREATE TABLE IF NOT EXISTS recipes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                title            TEXT    NOT NULL,
                site             TEXT    NOT NULL,
                pdf_path         TEXT    UNIQUE NOT NULL,
                ingredients      TEXT,
                duration_minutes INTEGER,
                category         TEXT,
                description      TEXT,
                full_text        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_recipes_category
                ON recipes (category);
            CREATE INDEX IF NOT EXISTS idx_recipes_duration
                ON recipes (duration_minutes)
                WHERE duration_minutes IS NOT NULL;
        """)
        _conn().commit()
        # Migration silencieuse pour les DBs existantes sans full_text
        try:
            _conn().execute("ALTER TABLE recipes ADD COLUMN full_text TEXT")
            _conn().commit()
        except Exception:
            pass


def insert_recipe(data: dict) -> int | None:
    ingredients = data.get("ingredients")
    if isinstance(ingredients, list):
        ingredients = json.dumps(ingredients, ensure_ascii=False)

    with _lock:
        cur = _conn().execute(
            """
            INSERT OR IGNORE INTO recipes
                (title, site, pdf_path, ingredients, duration_minutes, category, description, full_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["title"],
                data["site"],
                str(data["pdf_path"]),
                ingredients,
                data.get("duration_minutes"),
                data.get("category"),
                data.get("description"),
                data.get("full_text"),
            ),
        )
        _conn().commit()
        return cur.lastrowid if cur.rowcount else None


def update_full_text(recipe_id: int, full_text: str) -> None:
    with _lock:
        _conn().execute(
            "UPDATE recipes SET full_text = ? WHERE id = ?",
            (full_text, recipe_id),
        )
        _conn().commit()


def get_recipes_missing_full_text() -> list[dict]:
    rows = _conn().execute(
        "SELECT id, pdf_path, site FROM recipes WHERE full_text IS NULL OR full_text = ''"
    ).fetchall()
    return [dict(r) for r in rows]


def recipe_exists(pdf_path: str) -> bool:
    row = _conn().execute(
        "SELECT 1 FROM recipes WHERE pdf_path = ?", (str(pdf_path),)
    ).fetchone()
    return row is not None


def count_recipes() -> int:
    row = _conn().execute("SELECT COUNT(*) FROM recipes").fetchone()
    return row[0] if row else 0


def get_recipe_by_title(title: str) -> dict | None:
    row = _conn().execute(
        "SELECT * FROM recipes WHERE title = ?", (title,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def get_recipe_by_id(recipe_id: int) -> dict | None:
    row = _conn().execute(
        "SELECT * FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def get_recipes_by_ids(recipe_ids: list[int]) -> list[dict]:
    if not recipe_ids:
        return []
    placeholders = ",".join("?" * len(recipe_ids))
    rows = _conn().execute(
        f"SELECT * FROM recipes WHERE id IN ({placeholders})", recipe_ids
    ).fetchall()
    by_id = {r["id"]: _row_to_dict(r) for r in rows}
    return [by_id[rid] for rid in recipe_ids if rid in by_id]


def _ligature_variants(term: str) -> list[str]:
    """Génère les variantes oe↔œ et ae↔æ pour un terme normalisé."""
    variants = {term}
    for a, b in (("oe", "œ"), ("ae", "æ")):
        expanded = set()
        for v in variants:
            expanded.add(v.replace(a, b))
            expanded.add(v.replace(b, a))
        variants |= expanded
    return list(variants)


def search_by_ingredient(ingredient: str) -> list[dict]:
    terms = _ligature_variants(ingredient)
    where = " OR ".join("LOWER(ingredients) LIKE LOWER(?)" for _ in terms)
    params = [f"%{t}%" for t in terms]
    rows = _conn().execute(
        f"SELECT * FROM recipes WHERE {where}", params
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_by_duration(max_minutes: int) -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM recipes WHERE duration_minutes IS NOT NULL AND duration_minutes <= ?",
        (max_minutes,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_by_title_keywords(keywords: list[str]) -> list[dict]:
    """Recettes dont le titre contient TOUS les mots-clés donnés."""
    if not keywords:
        return []
    conditions = " AND ".join("LOWER(title) LIKE LOWER(?)" for _ in keywords)
    params = [f"%{kw}%" for kw in keywords]
    rows = _conn().execute(
        f"SELECT * FROM recipes WHERE {conditions}", params
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_by_category(category: str) -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM recipes WHERE LOWER(category) LIKE LOWER(?)",
        (f"%{category}%",),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_recipes() -> list[dict]:
    rows = _conn().execute("SELECT * FROM recipes").fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_recipes(recipe_ids: list[int]) -> int:
    if not recipe_ids:
        return 0
    placeholders = ",".join("?" * len(recipe_ids))
    with _lock:
        cur = _conn().execute(
            f"DELETE FROM recipes WHERE id IN ({placeholders})", recipe_ids
        )
        _conn().commit()
    return cur.rowcount


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("ingredients"):
        try:
            d["ingredients"] = json.loads(d["ingredients"])
        except json.JSONDecodeError:
            pass
    return d


def get_sites_summary() -> list[dict]:
    rows = _conn().execute(
        "SELECT site, COUNT(*) as count FROM recipes GROUP BY site ORDER BY site"
    ).fetchall()
    return [dict(r) for r in rows]
