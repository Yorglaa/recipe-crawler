"""
Gestion ChromaDB pour la recherche sémantique (RAG).
Utilise sentence-transformers (all-MiniLM-L6-v2) comme embedding function.
ChromaDB est thread-safe nativement.
"""

from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

import config

_client: chromadb.ClientAPI | None = None
_collection: chromadb.Collection | None = None


def _get_collection() -> chromadb.Collection:
    global _client, _collection
    if _collection is None:
        db_path = Path(config.CHROMA_DB_PATH)
        db_path.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(db_path))
        ef = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=config.EMBEDDING_MODEL
        )
        _collection = _client.get_or_create_collection(
            name="recipes",
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def init_chroma() -> chromadb.Collection:
    """Initialise et retourne la collection ChromaDB."""
    return _get_collection()


def add_recipe(
    recipe_id: int,
    title: str,
    description: str | None,
    ingredients: list[str] | None,
    metadata: dict | None = None,
) -> None:
    """
    Ajoute ou met à jour une recette dans ChromaDB.
    Le document embedé = titre + description + ingrédients concaténés.
    Idempotent : upsert par recipe_id.
    """
    parts = [title]
    if description:
        parts.append(description)
    if ingredients:
        parts.append("Ingrédients: " + ", ".join(ingredients))
    document = ". ".join(parts)

    meta = {"recipe_id": recipe_id, "title": title}
    if metadata:
        # ChromaDB n'accepte que str/int/float/bool comme valeurs de metadata
        for k, v in metadata.items():
            if isinstance(v, (str, int, float, bool)) and v is not None:
                meta[k] = v

    _get_collection().upsert(
        ids=[str(recipe_id)],
        documents=[document],
        metadatas=[meta],
    )


def semantic_search(query: str, n_results: int = 5) -> list[dict]:
    """
    Recherche sémantique. Retourne une liste de dicts avec :
        recipe_id, title, distance (0 = identique, 2 = opposé en cosine)
    """
    col = _get_collection()
    if col.count() == 0:
        return []

    n_results = min(n_results, col.count())
    results = col.query(query_texts=[query], n_results=n_results)

    output = []
    for i, doc_id in enumerate(results["ids"][0]):
        meta = results["metadatas"][0][i]
        output.append(
            {
                "recipe_id": meta.get("recipe_id"),
                "title": meta.get("title"),
                "distance": results["distances"][0][i],
                "document": results["documents"][0][i],
            }
        )
    return output


def recipe_exists(recipe_id: int) -> bool:
    """Retourne True si la recette est déjà indexée dans ChromaDB."""
    result = _get_collection().get(ids=[str(recipe_id)])
    return len(result["ids"]) > 0


def count() -> int:
    """Nombre de recettes dans ChromaDB."""
    return _get_collection().count()
