from pathlib import Path

# ============================================================
# LogiDoc-RAG Configuration
# ============================================================

# Project directory
BASE_DIR = Path(__file__).resolve().parent

# Data directories
DATA_DIR = BASE_DIR / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"

# Vector database
CHROMA_DIR = BASE_DIR / "chroma_db"

# Application logs
LOG_DIR = BASE_DIR / "logs"

# Ollama models
LLM_MODEL = "nemotron-3-nano:4b"
EMBEDDING_MODEL = "nomic-embed-text"

# ChromaDB collection
COLLECTION_NAME = "logistics_documents"

# RAG settings
TOP_K = 5
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

# Supported logistics document types
DOCUMENT_TYPES = [
    "BOL",
    "POD",
    "INVOICE",
    "RATE_CONFIRMATION",
    "UNKNOWN",
]