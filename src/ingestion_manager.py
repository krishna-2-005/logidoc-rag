"""
LogiDoc-RAG Safe Incremental Ingestion

Adds newly uploaded PDFs to an explicitly supplied destination
(a raw PDF folder and a ChromaDB folder) without rebuilding the
index or deleting any existing shipment data.

For each input file:
    1. Copy it to a private staging folder and hash the copy (SHA-256).
    2. Compare it with the destination (check_existing):
         duplicate     the same content is already stored and indexed,
                       under exactly one filename; nothing is written
                       except a provenance record in the journal
         inconsistent  the content or filename is present on only one
                       side (PDF without vectors, vectors without PDF)
                       or under several filenames; nothing is written
                       and nothing is repaired
         changed       a file with the same name but different content
                       exists; it and its vectors are kept and the new
                       version is not indexed (replacing a document
                       needs an explicit reindex)
    3. Otherwise load, classify and chunk it with the existing
       document_loader / chunker APIs and embed every chunk with
       vector_store.create_embedding. Nothing is written yet.
    4. Write a "begin" journal record (ingestion_journal), upsert the
       vectors and verify them, then store the PDF (written under a
       temporary .part name, verified, renamed) and verify it. Only
       then is a "commit" record written.
    5. If a step after the "begin" record fails or is interrupted
       (any exception, KeyboardInterrupt, SystemExit, or a Streamlit
       stop/rerun), the vectors this operation wrote are removed and
       a "rolled_back" or "rollback_failed" record is written.
       Interruptions are re-raised after the rollback.

A hard crash or power loss can still stop an operation between the
vector write and the PDF storage. Nothing can roll that back; the
journal shows the operation as unfinished and
consistency_report.check_destination() reports it. Nothing is
repaired automatically.

Vector IDs are built from the content hash, so the same content can
never get a second vector set. Full rebuilds are still done by
vector_store.build_vector_database(); their vectors carry no content
hash, so they are treated as unverified (linked by filename only).

This module must stay free of Streamlit calls: a st.* call inside an
operation would let Streamlit stop the script half-way.
"""

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import threading
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

import ingestion_journal
from ingestion_journal import (
    EVENT_BEGIN,
    EVENT_COMMIT,
    EVENT_DUPLICATE,
    EVENT_ROLLBACK_FAILED,
    EVENT_ROLLED_BACK,
)


# ============================================================
# CONSTANTS
# ============================================================

STATUS_ADDED = "added"
STATUS_DUPLICATE = "duplicate"
STATUS_CHANGED = "changed"
STATUS_INCONSISTENT = "inconsistent"
STATUS_FAILED = "failed"

# Metadata key recording which file content a vector came from.
# Vectors from a full rebuild do not have it.
HASH_METADATA_KEY = "content_sha256"

# How the vectors stored under a filename relate to a PDF
INDEX_VERIFIED = "indexed"
INDEX_UNVERIFIED = "indexed_unverified"
INDEX_MISSING = "no_vectors"
INDEX_DIFFERENT = "vectors_from_different_content"
INDEX_PDF_MISSING = "pdf_missing"

INDEX_STATUS_TEXT = {
    INDEX_VERIFIED: "indexed",
    INDEX_UNVERIFIED: "indexed by a full rebuild, unverified",
    INDEX_MISSING: "no vectors",
    INDEX_DIFFERENT: "vectors from different content",
    INDEX_PDF_MISSING: "vectors, but no PDF",
}

HEALTHY_INDEX_STATUSES = {
    INDEX_VERIFIED,
    INDEX_UNVERIFIED,
}

ACTION_CHECK = (
    "Run the consistency check (Diagnostics tab, or "
    "python src/consistency_report.py) and resolve the reported "
    "issue. Nothing is repaired automatically."
)

# One ingestion at a time per process. Streamlit serves every browser
# session from the same process; two concurrent runs on the same
# destination could both index the same content (same vector IDs),
# and one run's rollback would then remove the other's vectors.
INGESTION_LOCK = threading.Lock()

ACTION_REINDEX = (
    "To replace the existing document, put the new version in the "
    "raw folder yourself and run a full rebuild "
    "(python src/vector_store.py), which reindexes every PDF. If it "
    "is a different document, upload it under a different name."
)


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
    Describe what the destination already holds:

        raw_files        casefolded name -> {"name", "sha256"}
        raw_by_hash      content hash -> every raw filename with it
        indexed_sources  casefolded source -> content hashes on its
                         vectors (None for full-rebuild vectors)
        indexed_hashes   content hash -> every source with vectors
                         recording it

    Filenames are compared case-insensitively, because Windows
    treats "bol.pdf" and "BOL.PDF" as the same file. Lists are
    sorted, so results do not depend on scan order.
    """

    raw_files = {}
    raw_by_hash = {}

    for pdf_file in sorted(
        raw_dir.glob("*.pdf"),
        key=lambda path: path.name.casefold()
    ):

        content_hash = calculate_sha256(pdf_file)

        raw_files[pdf_file.name.casefold()] = {
            "name": pdf_file.name,
            "sha256": content_hash,
        }

        raw_by_hash.setdefault(
            content_hash,
            []
        ).append(pdf_file.name)

    indexed_sources = {}
    indexed_hashes = {}

    metadatas = collection.get(
        include=["metadatas"]
    ).get("metadatas") or []

    for metadata in metadatas:

        metadata = metadata or {}

        source = metadata.get("source")
        content_hash = metadata.get(HASH_METADATA_KEY) or None

        if not source:
            continue

        indexed_sources.setdefault(
            str(source).casefold(),
            set()
        ).add(content_hash)

        if content_hash:

            sources = indexed_hashes.setdefault(
                content_hash,
                []
            )

            if source not in sources:
                sources.append(source)

    for sources in indexed_hashes.values():
        sources.sort(key=str.casefold)

    return {
        "raw_files": raw_files,
        "raw_by_hash": raw_by_hash,
        "indexed_sources": indexed_sources,
        "indexed_hashes": indexed_hashes,
    }


def index_status(filename, content_hash, state):
    """
    How the vectors stored under filename relate to content_hash.
    """

    hashes = state["indexed_sources"].get(
        filename.casefold(),
        set()
    )

    if not hashes:
        return INDEX_MISSING

    if content_hash in hashes:
        return INDEX_VERIFIED

    # Full-rebuild vectors record no hash: filename is the only link
    if hashes == {None}:
        return INDEX_UNVERIFIED

    return INDEX_DIFFERENT


def describe_files(matched_files):

    return ", ".join(
        f"{item['filename']} "
        f"({INDEX_STATUS_TEXT[item['index_status']]})"
        for item in matched_files
    )


def check_existing(filename, content_hash, state):
    """
    Compare one hashed input with the destination.

    Returns None when the file is new and should be processed,
    otherwise a dict with status, message, action, duplicate_of
    and matched_files (every related file and its index status).
    """

    # --------------------------------------------------------
    # Same content in the raw folder: a duplicate only if it is
    # stored under exactly one filename and that file is indexed
    # --------------------------------------------------------

    names = state["raw_by_hash"].get(content_hash, [])

    if names:

        matched = [
            {
                "filename": name,
                "index_status": index_status(name, content_hash, state),
            }
            for name in names
        ]

        if (
            len(matched) == 1
            and matched[0]["index_status"] in HEALTHY_INDEX_STATUSES
        ):

            note = (
                " Its vectors come from a full rebuild, so they are "
                "linked to it by filename only."
                if matched[0]["index_status"] == INDEX_UNVERIFIED
                else ""
            )

            return {
                "status": STATUS_DUPLICATE,
                "message": f"Same content as existing file {names[0]}.{note}",
                "action": None,
                "duplicate_of": names[0],
                "matched_files": matched,
            }

        if len(matched) == 1:

            reason = {
                INDEX_MISSING: "that file has no vectors in the index",
                INDEX_DIFFERENT: (
                    "the vectors indexed for that file come from "
                    "different content"
                ),
            }[matched[0]["index_status"]]

            message = (
                f"Same content as {names[0]} in the raw folder, "
                f"but {reason}. Nothing was changed."
            )

        else:

            message = (
                f"The same content is stored under {len(names)} "
                f"filenames in the raw folder: "
                f"{describe_files(matched)}. Nothing was changed."
            )

        return {
            "status": STATUS_INCONSISTENT,
            "message": message,
            "action": ACTION_CHECK,
            "duplicate_of": None,
            "matched_files": matched,
        }

    # --------------------------------------------------------
    # Same content in the index only: vectors without the PDF
    # --------------------------------------------------------

    sources = state["indexed_hashes"].get(content_hash, [])

    if sources:

        return {
            "status": STATUS_INCONSISTENT,
            "message": (
                f"The index already has vectors for this content "
                f"(as {', '.join(sources)}), but the PDF is missing "
                "from the raw folder. Nothing was changed."
            ),
            "action": ACTION_CHECK,
            "duplicate_of": None,
            "matched_files": [
                {"filename": source, "index_status": INDEX_PDF_MISSING}
                for source in sources
            ],
        }

    # --------------------------------------------------------
    # Same filename, different content
    # --------------------------------------------------------

    existing = state["raw_files"].get(filename.casefold())

    if existing:

        return {
            "status": STATUS_CHANGED,
            "message": (
                f"{existing['name']} already exists with different "
                "content. The existing file and its vectors were "
                "kept and the new version was not indexed."
            ),
            "action": ACTION_REINDEX,
            "duplicate_of": None,
            "matched_files": [
                {
                    "filename": existing["name"],
                    "index_status": index_status(
                        existing["name"],
                        existing["sha256"],
                        state
                    ),
                }
            ],
        }

    # Vectors under this filename, but no PDF
    hashes = state["indexed_sources"].get(filename.casefold())

    if hashes:

        detail = (
            "from a full rebuild, which records no content hash, so "
            "they cannot be compared with this upload"
            if hashes == {None}
            else "from different content"
        )

        return {
            "status": STATUS_INCONSISTENT,
            "message": (
                f"The index has vectors for {filename} {detail}, but "
                "the PDF is missing from the raw folder. Nothing was "
                "indexed."
            ),
            "action": ACTION_CHECK,
            "duplicate_of": None,
            "matched_files": [
                {"filename": filename, "index_status": INDEX_PDF_MISSING}
            ],
        }

    return None


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
    Remove vectors this operation wrote. Returns None on success,
    otherwise an error message.

    The IDs are derived from content that was not indexed before
    (check_existing guarantees this), so no pre-existing vector
    can be removed here.
    """

    try:

        collection.delete(ids=vector_ids)

        return None

    except Exception as error:

        return (
            f"Rollback failed, {len(vector_ids)} vector(s) "
            f"may remain: {describe_error(error)}"
        )


# ============================================================
# OUTCOMES AND JOURNAL HELPERS
# ============================================================

def describe_error(error):

    text = str(error)

    if not isinstance(error, Exception):
        return f"Interrupted ({type(error).__name__})"

    return (
        f"{type(error).__name__}: {text}"
        if text
        else type(error).__name__
    )


def new_outcome(input_path, stage="validate"):

    return {
        "input_path": str(input_path),
        "filename": input_path.name,
        "sha256": None,
        "status": None,
        "stage": stage,
        "message": None,
        "action": None,
        "error": None,
        "rollback_error": None,
        "warnings": [],
        "operation_id": None,
        "duplicate_of": None,
        "matched_files": [],
        "stored_path": None,
        "document_type": None,
        "shipment_id": None,
        "chunks_indexed": 0,
        "vector_ids": [],
    }


def journal_event(raw_dir, outcome, event, operation_id, **fields):
    """
    Append a journal record after the fact. A failure becomes a
    warning on the outcome instead of an error, because the
    operation itself has already finished.
    """

    try:

        ingestion_journal.append_record(
            raw_dir,
            event,
            operation_id,
            filename=outcome["filename"],
            sha256=outcome["sha256"],
            **fields
        )

    except Exception as error:

        outcome["warnings"].append(
            f"The journal '{event}' record could not be written: "
            f"{describe_error(error)}"
        )


def undo_operation(raw_dir, collection, outcome, vector_ids):
    """
    Remove the vectors of a failed or interrupted operation and
    record the result in the journal.
    """

    rollback_error = rollback_vectors(
        collection,
        vector_ids
    )

    if rollback_error is None:

        journal_event(
            raw_dir,
            outcome,
            EVENT_ROLLED_BACK,
            outcome["operation_id"],
            reason=outcome["error"]
        )

        return

    outcome["rollback_error"] = rollback_error

    journal_event(
        raw_dir,
        outcome,
        EVENT_ROLLBACK_FAILED,
        outcome["operation_id"],
        reason=outcome["error"],
        error=rollback_error,
        vector_ids=vector_ids
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

    Must not call Streamlit (st.*); see the module docstring.
    """

    outcome = new_outcome(input_path)

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
        # Duplicate / inconsistent / changed
        # ----------------------------------------------------

        outcome["stage"] = "check"

        existing = check_existing(
            input_path.name,
            content_hash,
            state
        )

        if existing:

            outcome.update(existing)

            if existing["status"] == STATUS_DUPLICATE:

                journal_event(
                    raw_dir,
                    outcome,
                    EVENT_DUPLICATE,
                    ingestion_journal.new_operation_id(),
                    matched_files=[
                        item["filename"]
                        for item in existing["matched_files"]
                    ]
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

        # ----------------------------------------------------
        # Journal "begin" before the first persistent write.
        # If it cannot be written, nothing is indexed.
        # ----------------------------------------------------

        outcome["stage"] = "journal"

        outcome["operation_id"] = (
            ingestion_journal.new_operation_id()
        )

        ingestion_journal.append_record(
            raw_dir,
            EVENT_BEGIN,
            outcome["operation_id"],
            filename=input_path.name,
            sha256=content_hash,
            vector_ids=vector_ids
        )

        # ----------------------------------------------------
        # Index and verify
        # ----------------------------------------------------

        outcome["stage"] = "index"

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
        # Store the PDF only after indexing succeeded, then
        # verify the stored copy
        # ----------------------------------------------------

        outcome["stage"] = "store"

        destination = raw_dir / input_path.name

        store_file(
            staged_path,
            destination,
            content_hash
        )

        if calculate_sha256(destination) != content_hash:
            raise IOError(
                f"The stored PDF {destination.name} does not match "
                "the indexed content. It was left in place for "
                "inspection."
            )

    except BaseException as error:

        outcome.update(
            status=STATUS_FAILED,
            error=describe_error(error),
        )

        if written_ids:

            undo_operation(
                raw_dir,
                collection,
                outcome,
                written_ids
            )

        # KeyboardInterrupt, SystemExit, Streamlit stop/rerun:
        # rolled back above, now let them continue
        if not isinstance(error, Exception):
            raise

        return outcome

    # --------------------------------------------------------
    # Commit: vectors and PDF are both verified
    # --------------------------------------------------------

    outcome.update(
        status=STATUS_ADDED,
        stage="done",
        stored_path=str(destination),
        chunks_indexed=len(vector_ids),
        vector_ids=vector_ids,
        message=(
            f"Indexed {len(vector_ids)} chunk(s) and "
            f"stored as {destination.name}."
        ),
    )

    journal_event(
        raw_dir,
        outcome,
        EVENT_COMMIT,
        outcome["operation_id"],
        stored_path=str(destination)
    )

    # Later files in this batch see this one as existing
    name = destination.name

    state["raw_files"][name.casefold()] = {
        "name": name,
        "sha256": content_hash,
    }

    state["raw_by_hash"].setdefault(content_hash, []).append(name)
    state["raw_by_hash"][content_hash].sort(key=str.casefold)

    state["indexed_sources"].setdefault(
        name.casefold(),
        set()
    ).add(content_hash)

    state["indexed_hashes"].setdefault(content_hash, []).append(name)
    state["indexed_hashes"][content_hash].sort(key=str.casefold)

    return outcome


# ============================================================
# RESULT
# ============================================================

def build_result(
    raw_dir,
    chroma_dir,
    collection_name,
    outcomes,
    destination_warnings=()
):
    """
    Summarise per-file outcomes.

    files_processed counts files that were indexed and stored.
    Changed files are reported but never replaced, so every
    processed file is currently also an added file.

    Inconsistent and changed files are not failures: nothing was
    attempted, but they need attention, so they are counted
    separately and do not affect "success".

    errors lists each failed file's original error; rollback
    failures are listed separately in rollback_errors.
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
        "inconsistent_detected": count(STATUS_INCONSISTENT),
        "files_processed": count(STATUS_ADDED),
        "files_failed": count(STATUS_FAILED),
        "success": count(STATUS_FAILED) == 0,
        "errors": [
            f"{outcome['filename']}: {outcome['error']}"
            for outcome in outcomes
            if outcome["status"] == STATUS_FAILED
        ],
        "rollback_errors": [
            f"{outcome['filename']}: {outcome['rollback_error']}"
            for outcome in outcomes
            if outcome["rollback_error"]
        ],
        "warnings": list(destination_warnings) + [
            f"{outcome['filename']}: {warning}"
            for outcome in outcomes
            for warning in outcome["warnings"]
        ],
        "files": outcomes,
    }


def journal_warnings(raw_dir):
    """
    Warn about earlier problems recorded in the journal.
    """

    journal = ingestion_journal.read_journal(raw_dir)

    warnings = []

    unfinished = ingestion_journal.unfinished_operations(
        journal["records"]
    )

    if unfinished:

        warnings.append(
            f"The journal lists {len(unfinished)} unfinished "
            "operation(s) from an earlier run. " + ACTION_CHECK
        )

    if journal["problems"]:

        warnings.append(
            f"{len(journal['problems'])} journal line(s) could not "
            "be read and were ignored. " + ACTION_CHECK
        )

    return warnings


# ============================================================
# MAIN INGESTION FUNCTION
# ============================================================

def ingest_files(
    input_files,
    raw_dir,
    chroma_dir,
    collection_name=COLLECTION_NAME,
    embed_text=None
):
    """
    Safely add input PDFs to the destination.

    raw_dir and chroma_dir must be supplied explicitly; there is
    no default, so tests can never write to production by accident.
    Existing files and vectors are never deleted or overwritten.

    embed_text defaults to vector_store.create_embedding (Ollama),
    looked up when called so tests can replace it.

    An interruption (KeyboardInterrupt, SystemExit, Streamlit
    stop/rerun) is re-raised after the current file is rolled
    back; files finished before it are recorded in the journal.

    Runs in the same process wait for each other (INGESTION_LOCK).
    Separate processes are not coordinated.
    """

    with INGESTION_LOCK:

        return run_ingestion(
            [Path(input_file) for input_file in input_files],
            Path(raw_dir),
            Path(chroma_dir),
            collection_name,
            embed_text or create_embedding
        )


def run_ingestion(
    input_paths,
    raw_dir,
    chroma_dir,
    collection_name,
    embed_text
):
    """
    Body of ingest_files; call it only while holding
    INGESTION_LOCK.
    """

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

        destination_warnings = journal_warnings(raw_dir)

    except Exception as error:

        # Nothing was written; report every file as failed
        outcomes = []

        for input_path in input_paths:

            outcome = new_outcome(
                input_path,
                stage="destination"
            )

            outcome.update(
                status=STATUS_FAILED,
                error=(
                    "Destination could not be opened: "
                    f"{describe_error(error)}"
                ),
            )

            outcomes.append(outcome)

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
        outcomes,
        destination_warnings
    )


def ingest_uploads(
    uploaded_files,
    raw_dir,
    chroma_dir,
    collection_name=COLLECTION_NAME,
    embed_text=None
):
    """
    Ingest files uploaded through a UI: objects with .name and
    .getbuffer(), such as Streamlit's UploadedFile.

    Each upload is written to its own temporary folder under its
    original file name (any directory part is dropped), so two
    uploads with the same name never overwrite each other. The
    temporary copies are always removed.
    """

    upload_dir = Path(
        tempfile.mkdtemp(prefix="logidoc_upload_")
    )

    try:

        paths = []

        for index, uploaded_file in enumerate(uploaded_files):

            folder = upload_dir / str(index)
            folder.mkdir()

            path = folder / Path(uploaded_file.name).name

            path.write_bytes(
                bytes(uploaded_file.getbuffer())
            )

            paths.append(path)

        return ingest_files(
            paths,
            raw_dir,
            chroma_dir,
            collection_name,
            embed_text
        )

    finally:

        shutil.rmtree(
            upload_dir,
            ignore_errors=True
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
    print(f"Inconsistent      : {result['inconsistent_detected']}")
    print(f"Files processed   : {result['files_processed']}")
    print(f"Files failed      : {result['files_failed']}")

    for outcome in result["files"]:

        print(
            f"- {outcome['filename']}: "
            f"{outcome['status'].upper()} "
            f"({outcome['error'] or outcome['message']})"
        )

    for error in result["rollback_errors"]:
        print(f"! Rollback error: {error}")

    for warning in result["warnings"]:
        print(f"! Warning: {warning}")

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
