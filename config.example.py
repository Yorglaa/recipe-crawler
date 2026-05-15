# Configuration centrale du projet recipe-crawler
# Copier ce fichier en config.py et renseigner les valeurs sensibles
from pathlib import Path

_ROOT = Path(__file__).parent.resolve()

# URLs de départ des sites à crawler
VIANDESUISSE_URL = "https://www.viandesuisse.ch/recettes"
MIGUSTO_URL = "https://www.migusto.ch/fr/recettes"
QOQA_URL = "https://www.qoqa.ch/fr/recettes"
FOOBY_URL = "https://fooby.ch/fr/recettes.html?query=&start=0&sort=&filters[treffertyp]=rezepte&y=0&x=0"

# Dossier de sortie des PDFs
PDF_OUTPUT_DIR = str(_ROOT / "pdfs")

# Stack RAG locale (SQLite + ChromaDB + Gemini)
GEMINI_API_KEY = ""          # à renseigner
CHROMA_DB_PATH = str(_ROOT / "chromadb")
SQLITE_DB_PATH = str(_ROOT / "recipes.db")
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash-lite"


GROQ_API_KEY = ""          # à renseigner
GROQ_MODEL = "llama-3.3-70b-versatile"
