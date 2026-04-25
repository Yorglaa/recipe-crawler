# Configuration centrale du projet recipe-crawler
# Copier ce fichier en config.py et renseigner les valeurs sensibles

# URLs de départ des sites à crawler
VIANDESUISSE_URL = "https://www.viandesuisse.ch/recettes"
MIGUSTO_URL = "https://www.migusto.ch/fr/recettes"
QOQA_URL = "https://www.qoqa.ch/fr/recettes"

# Dossier de sortie des PDFs
PDF_OUTPUT_DIR = "./pdfs"

# Stack RAG locale (SQLite + ChromaDB + Gemini)
GEMINI_API_KEY = ""          # à renseigner
CHROMA_DB_PATH = "./chromadb"
SQLITE_DB_PATH = "./recipes.db"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
GEMINI_MODEL = "gemini-2.5-flash"
