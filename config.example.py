# Configuration centrale du projet recipe-crawler
# Copier ce fichier en config.py et renseigner les valeurs sensibles
from pathlib import Path

_ROOT = Path(__file__).parent.resolve()

# URLs de départ des sites à crawler
VIANDESUISSE_URL = "https://www.viandesuisse.ch/recettes"
MIGUSTO_URL = "https://www.migusto.ch/fr/recettes"
QOQA_URL = "https://www.qoqa.ch/fr/recettes"

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
# Modèles Groq recommandés (du meilleur au plus léger) :
#   moonshotai/kimi-k2-instruct  — excellent suivi de contexte, recommandé
#   mistral-saba-24b             — léger, très bon en français
#   llama-3.3-70b-versatile      — disponible partout mais suit mal le contexte
GROQ_MODEL = "moonshotai/kimi-k2-instruct"
