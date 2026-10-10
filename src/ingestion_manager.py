"""
LogiDoc-RAG Safe Incremental Ingestion

Adds newly uploaded PDFs to an explicitly supplied destination
(a raw PDF folder and a ChromaDB folder) without rebuilding the
index or deleting any existing shipment data.

For each input file:
    1. Copy it to a private staging folder and hash the copy (SHA-256).
    2. Skip it if the same content is already in the destination,
       whatever its filename.
    3. Report it as changed, and keep the existing file, if a file
       with the same name but different content is already there.
    4. Otherwise load, classify and chunk it with the existing
       document_loader / chunker APIs, embed each chunk with
       vector_store.create_embedding and upsert the vectors.
    5. Only after the vectors are verified in the index is the PDF
       copied into the raw folder. If any step fails, the vectors
       this run wrote for that file are removed; nothing else is.

Vector IDs are built from the content hash, so processing the same
content again can never create duplicate vectors. Full rebuilds are
still done by vector_store.build_vector_database().

This module does not depend on Streamlit.
"""

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import chromadb


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import COLLECTION_NAME
from chunker import create_document_chunks
from document_loader import load_document
from vector_store import create_embedding


# ============================================================
# CONSTANTS
# ============================================================

STATUS_ADDED = "added"
STATUS_DUPLICATE = "duplicate"
STATUS_CHANGED = "changed"
STATUS_FAILED = "failed"

# Metadata key recording which file content a vector came from.
# Vectors from a full rebuild do not have it.
HASH_METADATA_KEY = "content_sha256"


# ============================================================
# HASHING
# ============================================================

def calculate_sha256(path):
    """
    Return the SHA-256 hex digest of a file's content.
    """

    digest = hashlib.sha256()

    with open(path, "rb") as file:

        for block in iter(
            lambda: file.read(1024 * 1024),
            b""
        ):
            digest.update(block)

    return digest.hexdigest()


# ============================================================
# DESTINATION
# ============================================================

def get_destination_collection(
    chroma_dir,
    collection_name=COLLECTION_NAME
):
    """
    Open or create the ChromaDB collection in chroma_dir.

    Same call as vector_store.get_collection(), but for an
    explicitly supplied folder instead of config.CHROMA_DIR.
    """

    client = chromadb.PersistentClient(
        path=str(chroma_dir)
    )

    return client.get_or_create_collection(
        name=collection_name
    )


def scan_destination(raw_dir, collection):
    """
    Describe what the destination already holds.

    Filenames are compared case-insensitively, because Windows
    treats "bol.pdf" and "BOL.PDF" as the same file.
    """

    raw_hashes = {}
    raw_names = {}

    for pdf_file in sorted(raw_dir.glob("*.pdf")):

        content_hash = calculate_sha256(pdf_file)

        raw_names[pdf_file.name.casefold()] = pdf_file.name
        raw_hashes.setdefault(content_hash, pdf_file.name)

    indexed_sources = set()
    indexed_hashes = {}

    metadatas = collection.get(
        include=["metadatas"]
    ).get("metadatas") or []

    for metadata in metadatas:

        metadata = metadata or {}

        source = metadata.get("source")
        content_hash = metadata.get(HASH_METADATA_KEY)

        if source:
            indexed_sources.add(
                str(source).casefold()
            )

        if content_hash:
            indexed_hashes.setdefault(
                content_hash,
                source
            )

    return {
        "raw_hashes": raw_hashes,
        "raw_names": raw_names,
        "indexed_sources": indexed_sources,
        "indexed_hashes": indexed_hashes,
    }


def check_existing(filename, content_hash, state):
    """
    Compare one hashed input with the destination.

    Returns (status, message, duplicate_of); status is None
    when the file is new and should be processed.
    """

    if content_hash in state["raw_hashes"]:

        existing = state["raw_hashes"][content_hash]

        return (
            STATUS_DUPLICATE,
            f"Same content as existing file {existing}.",
            existing,
        )

    if content_hash in state["indexed_hashes"]:

        existing = state["indexed_hashes"][content_hash]

        return (
            STATUS_DUPLICATE,
            f"Same content is already indexed as {existing}.",
            existing,
        )

    if filename.casefold() in state["raw_names"]:

        return (
            STATUS_CHANGED,
            (
                f"{filename} already exists with different "
                "content. The existing file was kept and the "
                "new version was not indexed."
            ),
            None,
        )

    # A full rebuild indexes files without recording their hash,
    # so a name match there cannot be proven to be the same content
    if filename.casefold() in state["indexed_sources"]:

        return (
            STATUS_CHANGED,
            (
                f"The index already has vectors for {filename} "
                "from different or untracked content. Nothing "
                "was indexed, to avoid duplicate vectors."
            ),
            None,
        )

    return None, None, None


# ============================================================
# FILE STORAGE
# ============================================================

def store_file(staged_path, destination, content_hash):
    """
    Copy the staged PDF into the raw folder without ever
    replacing an existing file. The copy is written under a
    temporary name and renamed once it is complete.
    """

    if destination.exists():

        raise FileExistsError(
            f"{destination.name} appeared in the destination "
            "during ingestion."
        )

    partial = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.part"
    )

    try:

        shutil.copy2(staged_path, partial)

        if calculate_sha256(partial) != content_hash:

            raise IOError(
                "The copied file does not match the "
                "staged content."
            )

        os.replace(partial, destination)

    finally:

        if partial.exists():
            partial.unlink()


def rollback_vectors(collection, vector_ids):
    """
    Remove vectors this run wrote for a failed file.

    The IDs are derived from content that was not indexed
    before, so no pre-existing vector can be removed here.
    """

    try:

        collection.delete(ids=vector_ids)

        return None

    except Exception as error:

        return (
            f"Rollback failed, {len(vector_ids)} vector(s) "
            f"may remain: {error}"
        )


# ============================================================
# SINGLE FILE
# ============================================================

def ingest_file(
    input_path,
    staging_dir,
    raw_dir,
    collection,
    state,
    embed_text
):
    """
    Ingest one file and return its outcome record.
    """

    outcome = {
        "input_path": str(input_path),
        "filename": input_path.name,
        "sha256": None,
        "status": None,
        "stage": "validate",
        "message": None,
        "error": None,
        "duplicate_of": None,
        "stored_path": None,
        "document_type": None,
        "shipment_id": None,
        "chunks_indexed": 0,
    }

    written_ids = []

    try:

        if not input_path.is_file():
            raise FileNotFoundError(
                f"Input file not found: {input_path}"
            )

        if input_path.suffix.lower() != ".pdf":
            raise ValueError(
                "Only PDF files can be ingested."
            )

        # ----------------------------------------------------
        # Stage a private copy, so the hashed content is
        # exactly what gets indexed and stored
        # ----------------------------------------------------

        outcome["stage"] = "stage"

        file_staging_dir = Path(
            tempfile.mkdtemp(dir=staging_dir)
        )

        staged_path = file_staging_dir / input_path.name

        shutil.copy2(input_path, staged_path)

        content_hash = calculate_sha256(staged_path)

        outcome["sha256"] = content_hash

        # ----------------------------------------------------
        # Duplicate / changed file
        # ----------------------------------------------------

        outcome["stage"] = "check"

        status, message, duplicate_of = check_existing(
            input_path.name,
            content_hash,
            state
        )

        if status:

            outcome.update(
                status=status,
                message=message,
                duplicate_of=duplicate_of,
            )

            return outcome

        # ----------------------------------------------------
        # Existing load, classification and chunking
        # ----------------------------------------------------

        outcome["stage"] = "load"

        document = load_document(staged_path)

        outcome["document_type"] = document.get(
            "document_type"
        )

        outcome["shipment_id"] = (
            document.get("identifiers") or {}
        ).get("shipment_id")

        chunks = create_document_chunks(document)

        if not chunks:
            raise ValueError(
                "No extractable text was found in the PDF."
            )

        # ----------------------------------------------------
        # Embed every chunk before writing anything
        # ----------------------------------------------------

        outcome["stage"] = "embed"

        embeddings = [
            embed_text(chunk["text"])
            for chunk in chunks
        ]

        # ----------------------------------------------------
        # Index
        # ----------------------------------------------------

        outcome["stage"] = "index"

        vector_ids = [
            f"{content_hash}:"
            f"{chunk['metadata']['page']}:"
            f"{chunk['metadata']['chunk']}"
            for chunk in chunks
        ]

        # ChromaDB does not accept None values; convert them
        # to empty strings, as build_vector_database() does
        metadatas = [
            {
                key: "" if value is None else value
                for key, value in {
                    **chunk["metadata"],
                    HASH_METADATA_KEY: content_hash,
                }.items()
            }
            for chunk in chunks
        ]

        # Recorded first, so a partially applied write
        # is still rolled back
        written_ids = vector_ids

        collection.upsert(
            ids=vector_ids,
            embeddings=embeddings,
            documents=[
                chunk["text"]
                for chunk in chunks
            ],
            metadatas=metadatas,
        )

        stored_ids = set(
            collection.get(
                ids=vector_ids,
                include=[]
            )["ids"]
        )

        if stored_ids != set(vector_ids):
            raise RuntimeError(
                f"Only {len(stored_ids)} of {len(vector_ids)} "
                "chunks were found in the index after writing."
            )

        # ----------------------------------------------------
        # Store the PDF only after indexing succeeded
        # ----------------------------------------------------

        outcome["stage"] = "store"

        destination = raw_dir / input_path.name

        store_file(
            staged_path,
            destination,
            content_hash
        )

    except Exception as error:

        outcome.update(
            status=STATUS_FAILED,
            error=f"{type(error).__name__}: {error}",
        )

        if written_ids:

            rollback_error = rollback_vectors(
                collection,
                written_ids
            )

            if rollback_error:
                outcome["error"] += f" | {rollback_error}"

        return outcome

    outcome.update(
        status=STATUS_ADDED,
        stage="done",
        stored_path=str(destination),
        chunks_indexed=len(vector_ids),
        message=(
            f"Indexed {len(vector_ids)} chunk(s) and "
            f"stored as {destination.name}."
        ),
    )

    # Later files in this batch see this one as existing
    state["raw_hashes"].setdefault(
        content_hash,
        destination.name
    )
    state["raw_names"][destination.name.casefold()] = (
        destination.name
    )
    state["indexed_sources"].add(
        destination.name.casefold()
    )
    state["indexed_hashes"].setdefault(
        content_hash,
        destination.name
    )

    return outcome


# ============================================================
# RESULT
# ============================================================

def build_result(
    raw_dir,
    chroma_dir,
    collection_name,
    outcomes
):
    """
    Summarise per-file outcomes.

    files_processed counts files that were indexed and stored.
    Changed files are reported but never replaced, so every
    processed file is currently also an added file.
    """

    def count(status):

        return sum(
            1
            for outcome in outcomes
            if outcome["status"] == status
        )

    return {
        "destination": {
            "raw_dir": str(raw_dir),
            "chroma_dir": str(chroma_dir),
            "collection": collection_name,
        },
        "files_discovered": len(outcomes),
        "files_added": count(STATUS_ADDED),
        "duplicates_skipped": count(STATUS_DUPLICATE),
        "changed_detected": count(STATUS_CHANGED),
        "files_processed": count(STATUS_ADDED),
        "files_failed": count(STATUS_FAILED),
        "success": count(STATUS_FAILED) == 0,
        "errors": [
            f"{outcome['filename']}: {outcome['error']}"
            for outcome in outcomes
            if outcome["status"] == STATUS_FAILED
        ],
        "files": outcomes,
    }


# ============================================================
# MAIN INGESTION FUNCTION
# ============================================================

def ingest_files(
    input_files,
    raw_dir,
    chroma_dir,
    collection_name=COLLECTION_NAME,
    embed_text=create_embedding
):
    """
    Safely add input PDFs to the destination.

    raw_dir and chroma_dir must be supplied explicitly; there is
    no default, so tests can never write to production by accident.
    Existing files and vectors are never deleted or overwritten.

    embed_text defaults to vector_store.create_embedding (Ollama).
    """

    input_paths = [
        Path(input_file)
        for input_file in input_files
    ]

    raw_dir = Path(raw_dir)
    chroma_dir = Path(chroma_dir)

    try:

        raw_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        collection = get_destination_collection(
            chroma_dir,
            collection_name
        )

        state = scan_destination(
            raw_dir,
            collection
        )

    except Exception as error:

        # Nothing was written; report every file as failed
        outcomes = [
            {
                "input_path": str(input_path),
                "filename": input_path.name,
                "sha256": None,
                "status": STATUS_FAILED,
                "stage": "destination",
                "message": None,
                "error": (
                    "Destination could not be opened: "
                    f"{type(error).__name__}: {error}"
                ),
                "duplicate_of": None,
                "stored_path": None,
                "document_type": None,
                "shipment_id": None,
                "chunks_indexed": 0,
            }
            for input_path in input_paths
        ]

        return build_result(
            raw_dir,
            chroma_dir,
            collection_name,
            outcomes
        )

    staging_dir = Path(
        tempfile.mkdtemp(prefix="logidoc_staging_")
    )

    outcomes = []

    try:

        for input_path in input_paths:

            outcomes.append(
                ingest_file(
                    input_path,
                    staging_dir,
                    raw_dir,
                    collection,
                    state,
                    embed_text
                )
            )

    finally:

        shutil.rmtree(
            staging_dir,
            ignore_errors=True
        )

    return build_result(
        raw_dir,
        chroma_dir,
        collection_name,
        outcomes
    )


# ============================================================
# CONSOLE REPORT
# ============================================================

def print_ingestion_result(result):

    print("=" * 70)
    print("INGESTION RESULT")
    print("=" * 70)

    print(f"Raw folder        : {result['destination']['raw_dir']}")
    print(f"Index folder      : {result['destination']['chroma_dir']}")
    print(f"Files discovered  : {result['files_discovered']}")
    print(f"Files added       : {result['files_added']}")
    print(f"Duplicates skipped: {result['duplicates_skipped']}")
    print(f"Changed detected  : {result['changed_detected']}")
    print(f"Files processed   : {result['files_processed']}")
    print(f"Files failed      : {result['files_failed']}")

    for outcome in result["files"]:

        print(
            f"- {outcome['filename']}: "
            f"{outcome['status'].upper()} "
            f"({outcome['error'] or outcome['message']})"
        )

    print("=" * 70)


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description=(
            "Safely add PDFs to a LogiDoc-RAG destination "
            "without rebuilding the index."
        )
    )

    parser.add_argument("files", nargs="+")
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument("--chroma-dir", required=True)

    arguments = parser.parse_args()

    ingestion_result = ingest_files(
        arguments.files,
        arguments.raw_dir,
        arguments.chroma_dir
    )

    print_ingestion_result(ingestion_result)

    sys.exit(0 if ingestion_result["success"] else 1)
