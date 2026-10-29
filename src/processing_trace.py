"""
How one stored document was processed, step by step (Document Intelligence > Processing).

Read-only. The deterministic steps (validation, text extraction, classification
and chunking) are run again on the stored PDF exactly as ingestion runs them, so
their output is what ingestion produced; what ingestion stored is read back: the
register record and the chunk vectors in ChromaDB. Nothing is written and no
model is called. (ChromaDB itself re-saves its index files whenever a client
reads them, as it does for every question asked; the stored records stay
identical, but the files' bytes and times change.) Text is cut to TEXT_LIMIT
characters per page and per chunk, and each embedding is shown by its first
PREVIEW numbers.
"""

import hashlib
import math
import time
from datetime import datetime
from pathlib import Path

import chromadb

import config
import document_validation as validation
from chunker import create_chunks, create_document_chunks
from document_loader import load_document
from ingestion_manager import HASH_METADATA_KEY

TEXT_LIMIT = 1500
PREVIEW = 32

# The sizes chunking really uses (create_chunks' own defaults, which create_document_chunks relies on)
CHUNK_SIZE, CHUNK_OVERLAP = create_chunks.__defaults__


def timed(step):
    """A step's result and how long it took, in milliseconds."""

    started = time.perf_counter()
    value = step()
    return value, round((time.perf_counter() - started) * 1000, 1)


def cut(text):
    return {"text": text[:TEXT_LIMIT], "characters": len(text), "truncated": len(text) > TEXT_LIMIT}


def register_record(register, filename, sha256):
    """The register's latest record of this file: the same content if there is one, else the same name."""

    records = [record for record in register.files(search=filename) if record.get("filename") == filename]
    return next((record for record in records if record.get("sha256") == sha256), records[0] if records else None)


def ingestion_summary(record):
    if not record:
        return None

    seconds = None
    try:
        seconds = (datetime.fromisoformat(record["updated_at"]) - datetime.fromisoformat(record["created_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        pass

    return {
        "status": record.get("status"),
        "message": record.get("message"),
        "chunks_indexed": record.get("chunks_indexed"),
        "attempts": record.get("attempts"),
        "received_at": record.get("created_at"),
        "finished_at": record.get("updated_at"),
        "seconds": seconds,
    }


def stored_vectors(chroma_dir, collection_name, sha256, filename):
    """The chunks this file has in the vector index, with their embeddings shown in short."""

    try:
        collection = chromadb.PersistentClient(path=str(chroma_dir)).get_collection(name=collection_name)
    except Exception:
        return {"collection": collection_name, "available": False, "vectors": []}

    include = ["embeddings", "metadatas", "documents"]
    found = collection.get(where={HASH_METADATA_KEY: sha256}, include=include)

    # Vectors from a full rebuild carry no content hash, only the file name
    if not found["ids"]:
        found = collection.get(where={"source": filename}, include=include)

    vectors = []
    for vector_id, embedding, metadata in zip(found["ids"], found["embeddings"], found["metadatas"]):
        values = [float(value) for value in embedding]
        vectors.append({
            "id": vector_id,
            "dimensions": len(values),
            "preview": [round(value, 4) for value in values[:PREVIEW]],
            "norm": round(math.sqrt(sum(value * value for value in values)), 4),
            "metadata": {key: value for key, value in (metadata or {}).items() if value not in ("", None)},
        })

    vectors.sort(key=lambda item: (item["metadata"].get("page", 0), item["metadata"].get("chunk", 0)))

    return {"collection": collection_name, "available": True, "vectors": vectors}


def trace(path, chroma_dir, collection_name=config.COLLECTION_NAME, record=None):
    """Every step of one stored PDF, from the received file to the vectors that answer questions."""

    path = Path(path)
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()

    checked, validate_ms = timed(lambda: validation.validate_pdf(path.name, data))

    result = {
        "filename": path.name,
        "size": len(data),
        "sha256": sha256,
        "ingestion": ingestion_summary(record),
        "validation": {
            "ok": checked["ok"],
            "code": checked.get("code"),
            "pages": checked.get("pages"),
            "text_pages": checked.get("text_pages"),
            "warnings": checked.get("warnings") or [],
            "ms": validate_ms,
        },
        "extraction": None,
        "classification": None,
        "chunking": None,
        "embedding": {"model": config.EMBEDDING_MODEL},
        "index": None,
    }

    if not checked["ok"]:
        return result

    document, load_ms = timed(lambda: load_document(path))
    pages = document.get("pages") or []

    result["extraction"] = {
        "engine": "PyMuPDF",
        "ms": load_ms,
        "characters": sum(len(page.get("text") or "") for page in pages),
        "pages": [{"page": page.get("page"), **cut(page.get("text") or "")} for page in pages],
    }

    identifiers = document.get("identifiers") or {}
    result["classification"] = {
        "document_type": document.get("document_type"),
        "shipment_id": identifiers.get("shipment_id"),
        "identifiers": {key: value for key, value in identifiers.items() if value},
    }

    chunks, chunk_ms = timed(lambda: create_document_chunks(document))
    result["chunking"] = {
        "chunk_size": CHUNK_SIZE,
        "overlap": CHUNK_OVERLAP,
        "ms": chunk_ms,
        "chunks": [{"page": chunk["metadata"]["page"], "chunk": chunk["metadata"]["chunk"], **cut(chunk["text"])} for chunk in chunks],
    }

    result["index"] = stored_vectors(chroma_dir, collection_name, sha256, path.name)

    return result
