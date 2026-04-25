# Configuration centrale du projet recipe-crawler
from pathlib import Path

_ROOT = Path(__file__).parent.resolve()

# URLs de départ des sites à crawler
VIANDESUISSE_URL = "https://www.viandesuisse.ch/recettes"
MIGUSTO_URL = "https://www.migusto.ch/fr/recettes"
QOQA_URL = "https://www.qoqa.ch/fr/recettes"

# Dossier de sortie des PDFs
PDF_OUTPUT_DIR = str(_ROOT / "pdfs")

# Stack RAG locale (SQLite + ChromaDB + Gemini)
GEMINI_API_KEY = "REDACTED"
CHROMA_DB_PATH = str(_ROOT / "chromadb")
SQLITE_DB_PATH = str(_ROOT / "recipes.db")
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
GEMINI_MODEL = "gemini-2.5-flash"
