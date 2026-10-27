import os
from pathlib import Path

# ============================================================
# LogiDoc-RAG Configuration
# ============================================================

# Project directory
BASE_DIR = Path(__file__).resolve().parent

# Data directories: documents (raw/), the databases, the archive and the
# quarantine. Set LOGIDOC_DATA_DIR to keep them elsewhere (default: data/ in
# the project).
DATA_DIR = Path(os.environ.get("LOGIDOC_DATA_DIR") or BASE_DIR / "data")
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"

# Vector database. Set LOGIDOC_CHROMA_DIR to keep it elsewhere (default:
# chroma_db/ in the project).
CHROMA_DIR = Path(os.environ.get("LOGIDOC_CHROMA_DIR") or BASE_DIR / "chroma_db")

# Application logs
LOG_DIR = BASE_DIR / "logs"

# Exception review metadata (status, reviewer, notes; never document
# text). Set LOGIDOC_REVIEW_DB to keep it elsewhere.
REVIEW_DB_PATH = Path(
    os.environ.get("LOGIDOC_REVIEW_DB") or DATA_DIR / "audit_reviews.sqlite3"
)

# Audit history (snapshots of audit results: statuses, amounts and
# finding summaries; never document text). Set LOGIDOC_HISTORY_DB to
# keep it elsewhere.
HISTORY_DB_PATH = Path(
    os.environ.get("LOGIDOC_HISTORY_DB") or DATA_DIR / "audit_history.sqlite3"
)

# User accounts and sessions (password hashes, never passwords; see
# src/auth.py). Set LOGIDOC_AUTH_DB to keep it elsewhere. The sign-in
# mode itself is LOGIDOC_AUTH_MODE (local | required).
AUTH_DB_PATH = Path(
    os.environ.get("LOGIDOC_AUTH_DB") or DATA_DIR / "auth.sqlite3"
)

# Per-file ingestion status and the processing error register (metadata
# about files, never their contents): data/ingestion_register.sqlite3 for
# the default raw folder (see ingestion_locations). Set
# LOGIDOC_INGESTION_DB to keep it elsewhere.
INGESTION_DB_PATH = Path(
    os.environ.get("LOGIDOC_INGESTION_DB") or DATA_DIR / "ingestion_register.sqlite3"
)

# Exception workflow policy (see src/review_store.Policy). Set
# LOGIDOC_REQUIRE_APPROVAL=1 so that a resolution or dismissal waits for an
# administrator's approval; LOGIDOC_MIN_NOTE_LENGTH sets the shortest
# resolution or dismissal note. Server-side only: nothing in the web
# interface can change them.
REQUIRE_APPROVAL = os.environ.get("LOGIDOC_REQUIRE_APPROVAL", "").strip().lower() in ("1", "true", "yes")
MIN_NOTE_LENGTH = max(int(os.environ.get("LOGIDOC_MIN_NOTE_LENGTH") or 10), 1)

# Upload limit for one PDF (MB)
MAX_UPLOAD_MB = max(int(os.environ.get("LOGIDOC_MAX_UPLOAD_MB") or 25), 1)


def ingestion_locations(raw_dir=RAW_DATA_DIR):
    """
    Where the ingestion register, the retry copies and the archive of
    replaced or removed originals live for a raw folder: next to it, so a
    test or temporary workspace keeps its own. LOGIDOC_INGESTION_DB
    overrides the register file only. Originals that an administrator
    replaced or removed are moved to the archive, never deleted.
    """

    base = Path(raw_dir).parent

    return {
        "extractions": Path(os.environ.get("LOGIDOC_EXTRACTION_DB") or base / "extractions.sqlite3"),
        "register": Path(os.environ.get("LOGIDOC_INGESTION_DB") or base / "ingestion_register.sqlite3"),
        "quarantine": base / "quarantine",
        "archive": base / "archive",
    }


# Ollama models
LLM_MODEL = "nemotron-3-nano:4b"
EMBEDDING_MODEL = "nomic-embed-text"

# How long one call to Ollama may take (seconds; see src/model_client.py).
# Without a limit a stopped or stuck Ollama holds the request, and an
# upload's ingestion lock, forever. Measured on this project: an embedding
# with its search at most 3.9 s, a generated answer at most 23.8 s, so the
# defaults leave a wide margin. Set LOGIDOC_OLLAMA_CONNECT_TIMEOUT,
# LOGIDOC_EMBED_TIMEOUT or LOGIDOC_LLM_TIMEOUT to change them; where Ollama
# runs is the ollama library's OLLAMA_HOST.
OLLAMA_CONNECT_TIMEOUT = max(float(os.environ.get("LOGIDOC_OLLAMA_CONNECT_TIMEOUT") or 5), 1.0)
EMBED_TIMEOUT = max(float(os.environ.get("LOGIDOC_EMBED_TIMEOUT") or 60), 1.0)
LLM_TIMEOUT = max(float(os.environ.get("LOGIDOC_LLM_TIMEOUT") or 120), 1.0)

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