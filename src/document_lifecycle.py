"""
LogiDoc-RAG document lifecycle: reprocess, replace and remove

The safe ingestion (ingestion_manager) only ever adds. This module is the
explicit, administrator-only way to change what is already stored, with
the same guarantees:

    - An original PDF is never overwritten or deleted without a copy: a
      replaced or removed PDF is first copied into the archive folder
      (named with its content hash and the time) and the copy is
      verified. The archive is never emptied by this system.
    - Nothing destructive happens until the new vectors exist. A
      replacement embeds the new document first, then swaps; if any step
      fails, the old vectors are written back from the copy taken
      before, the new ones are removed and the old PDF is put back, so
      the store ends as it began.
    - The index and the raw folder stay consistent: after every
      operation the vectors of a file are exactly those of the PDF that
      is stored under its name (verify_document), and the journal has a
      begin and a commit record (replace) so an interrupted operation is
      found by the consistency check.
    - Each action is logged with who did it (ingestion_register actions).
    - corpus_signature() changes whenever a document is added, replaced
      or removed, or the index changes, so anything cached on it is
      recomputed: SignatureCache and the Streamlit caches are keyed by
      it.

Reprocessing indexes a PDF that is already stored but has no vectors
(for example after a failed rebuild or a transient failure). It never
indexes content that already has vectors, so repeating it cannot create
duplicates.

A hard crash can still leave a removal half done (vectors deleted, PDF
not yet archived): the PDF stays where it was and the consistency check
reports a PDF without vectors, which Reprocess repairs. Nothing is lost.

This module must stay free of Streamlit calls (see ingestion_manager).
"""

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import authz
import document_validation as validation
import ingestion_journal
import ingestion_manager
from audit_analytics import source_fingerprint
from authz import require
from chunker import create_document_chunks
from config import COLLECTION_NAME
from document_loader import load_document
from ingestion_manager import (
    HASH_METADATA_KEY,
    INDEX_DIFFERENT,
    INDEX_VERIFIED,
    calculate_sha256,
    describe_error,
    get_destination_collection,
    scan_destination,
)


class LifecycleError(ValueError):
    """
    An operation that is not allowed or cannot be done safely. The
    store was not changed (or was restored).
    """


# ============================================================
# HELPERS
# ============================================================

def stored_path(raw_dir, filename):
    """
    The PDF stored under a bare file name in the raw folder. Never
    follows a client-supplied path.
    """

    problem = validation.check_filename(filename)

    if problem:
        raise LifecycleError(f"'{filename}' is not a valid file name ({validation.CODES[problem]}).")

    raw_dir = Path(raw_dir).resolve()
    path = (raw_dir / filename).resolve()

    if path.parent != raw_dir:
        raise LifecycleError("The file is not in the document folder.")

    return path


def archive_copy(path, archive_dir, sha256):
    """
    Copies a PDF into the archive folder under a name made of its hash,
    the time and its own name, and verifies the copy. Returns the
    archived path.
    """

    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = archive_dir / f"{sha256[:12]}_{stamp}_{path.name}"

    shutil.copy2(path, target)

    if calculate_sha256(target) != sha256:
        target.unlink(missing_ok=True)
        raise IOError("The archived copy does not match the original; nothing was changed.")

    return target


def source_vectors(collection, filename):
    """
    Everything the index holds for a source file name (compared without
    regard to case): ids, embeddings, documents and metadatas, as plain
    lists, so they can be written back.
    """

    metadatas = collection.get(include=["metadatas"])
    wanted = [
        vector_id
        for vector_id, metadata in zip(metadatas["ids"], metadatas["metadatas"] or [])
        if metadata and str(metadata.get("source", "")).casefold() == filename.casefold()
    ]

    if not wanted:
        return {"ids": [], "embeddings": [], "documents": [], "metadatas": []}

    data = collection.get(ids=wanted, include=["embeddings", "documents", "metadatas"])

    return {
        "ids": list(data["ids"]),
        "embeddings": [list(map(float, vector)) for vector in data["embeddings"]],
        "documents": list(data["documents"]),
        "metadatas": list(data["metadatas"]),
    }


def build_vectors(path, content_hash, embed_text):
    """
    Loads, chunks and embeds a PDF exactly as ingestion_manager does:
    the same chunking, the same vector IDs (content hash, page, chunk)
    and the same metadata. Nothing is written.
    """

    document = load_document(path)
    chunks = create_document_chunks(document)

    if not chunks:
        raise ValueError("No extractable text was found in the PDF.")

    return {
        "document": document,
        "ids": [f"{content_hash}:{c['metadata']['page']}:{c['metadata']['chunk']}" for c in chunks],
        "embeddings": [embed_text(chunk["text"]) for chunk in chunks],
        "documents": [chunk["text"] for chunk in chunks],
        "metadatas": [
            {key: "" if value is None else value for key, value in {**c["metadata"], HASH_METADATA_KEY: content_hash}.items()}
            for c in chunks
        ],
    }


def write_vectors(collection, vectors):

    if vectors["ids"]:
        collection.upsert(
            ids=vectors["ids"], embeddings=vectors["embeddings"],
            documents=vectors["documents"], metadatas=vectors["metadatas"],
        )


def verify_vectors_present(collection, ids):

    found = set(collection.get(ids=ids, include=[])["ids"]) if ids else set()

    if found != set(ids):
        raise RuntimeError(f"Only {len(found)} of {len(ids)} chunks were found in the index after writing.")


def restore(collection, old_vectors, new_ids, errors):
    """
    Puts the index back as it was: removes the new vectors and writes
    the old ones again. Failures are collected, not raised, so the other
    steps still run.
    """

    try:
        if new_ids:
            collection.delete(ids=new_ids)
    except Exception as error:
        errors.append(f"new vectors could not be removed: {describe_error(error)}")

    try:
        write_vectors(collection, old_vectors)
    except Exception as error:
        errors.append(f"old vectors could not be written back: {describe_error(error)}")


def verify_document(collection, raw_dir, filename):
    """
    Whether the index and the stored PDF agree for one file name:
    {"consistent", "problems", "pdf", "vectors"}. Consistent means: no
    PDF and no vectors, or a PDF whose every vector records its content
    hash (a vector without a hash comes from a full rebuild and is
    accepted as unverified).
    """

    path = Path(raw_dir) / filename
    vectors = source_vectors(collection, filename)
    hashes = {(metadata or {}).get(HASH_METADATA_KEY) or None for metadata in vectors["metadatas"]}
    problems = []

    if not path.is_file():

        if vectors["ids"]:
            problems.append("The index still holds vectors for a file that is not stored.")

    else:

        sha = calculate_sha256(path)

        if not vectors["ids"]:
            problems.append("The PDF is stored but has no vectors in the index.")
        elif hashes - {sha, None}:
            problems.append("The index holds vectors of different content than the stored PDF.")

    return {
        "consistent": not problems, "problems": problems,
        "pdf": path.is_file(), "vectors": len(vectors["ids"]),
    }


# ============================================================
# CORPUS SIGNATURE AND CACHE
# ============================================================

def index_signature(collection):
    """
    A hash of every vector ID in the index.
    """

    ids = sorted(collection.get(include=[])["ids"])

    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def corpus_signature(raw_dir, chroma_dir, collection_name=COLLECTION_NAME):
    """
    A string that changes whenever a PDF is added, replaced or removed
    (names, sizes and modification times of the raw folder) or the index
    changes (its vector IDs). Anything cached on it is stale as soon as
    it differs.
    """

    raw = hashlib.sha256(json.dumps(source_fingerprint(raw_dir)).encode("utf-8")).hexdigest()
    index = index_signature(get_destination_collection(chroma_dir, collection_name))

    return f"{raw[:16]}-{index[:16]}"


class SignatureCache:
    """
    Keeps computed results (analytics, scans) per corpus signature.
    get() recomputes when the signature changed, so a result never
    outlives the documents it was computed from. Not shared between
    processes.
    """

    def __init__(self, signature, max_entries=4):

        self.signature = signature
        self.max_entries = max_entries
        self.entries = {}

    def get(self, name, compute):

        current = self.signature()

        if current not in self.entries:
            self.entries = {key: value for key, value in list(self.entries.items())[-(self.max_entries - 1):]}
            self.entries[current] = {}

        results = self.entries[current]

        if name not in results:
            results[name] = compute()

        return results[name]


# ============================================================
# REPROCESS
# ============================================================

def reprocess(
    principal, register, filename, raw_dir, chroma_dir, collection_name=COLLECTION_NAME, embed_text=None,
):
    """
    Indexes a PDF that is stored in the raw folder but has no vectors
    (or only vectors of other content: those are replaced). Returns
    {"filename", "action", "chunks", "signature"}. Content that already has
    vectors is reported as already_indexed and nothing is written.
    """

    require(principal, authz.INGESTION_WRITE)

    embed_text = embed_text or ingestion_manager.create_embedding
    path = stored_path(raw_dir, filename)

    if not path.is_file():
        raise LifecycleError(f"{filename} is not in the document folder.")

    data = path.read_bytes()
    checked = validation.validate_pdf(path.name, data)

    if not checked["ok"]:
        raise LifecycleError(f"{filename} cannot be indexed: {validation.describe(checked)} {checked['action']}")

    sha = hashlib.sha256(data).hexdigest()

    with ingestion_manager.INGESTION_LOCK:

        collection = get_destination_collection(chroma_dir, collection_name)
        state = scan_destination(Path(raw_dir), collection)
        status = ingestion_manager.index_status(path.name, sha, state)

        if status == INDEX_VERIFIED or status == ingestion_manager.INDEX_UNVERIFIED:
            return {"filename": path.name, "action": "already_indexed", "chunks": 0,
                    "signature": corpus_signature(raw_dir, chroma_dir, collection_name)}

        other = [name for name in state["indexed_hashes"].get(sha, []) if name.casefold() != path.name.casefold()]

        if other:
            raise LifecycleError(
                f"The same content is already indexed as {', '.join(other)}; indexing it again would duplicate it."
            )

        # Vectors of other content under this name are replaced; they are kept to write back
        old = source_vectors(collection, path.name) if status == INDEX_DIFFERENT else \
            {"ids": [], "embeddings": [], "documents": [], "metadatas": []}

        new = build_vectors(path, sha, embed_text)

        operation = ingestion_journal.new_operation_id()
        ingestion_journal.append_record(
            raw_dir, ingestion_journal.EVENT_BEGIN, operation, filename=path.name, sha256=sha,
            vector_ids=new["ids"], operation="reprocess",
        )

        errors = []

        try:

            if old["ids"]:
                collection.delete(ids=old["ids"])

            write_vectors(collection, new)
            verify_vectors_present(collection, new["ids"])

        except BaseException as error:

            restore(collection, old, new["ids"], errors)

            record_end(raw_dir, operation, path.name, sha, error, errors)

            raise

        ingestion_journal.append_record(
            raw_dir, ingestion_journal.EVENT_COMMIT, operation, filename=path.name, sha256=sha,
            stored_path=str(path), operation="reprocess",
        )

        register.log_action("reprocessed", path.name, sha, f"{len(new['ids'])} chunk(s) indexed",
                            principal.username, principal.authenticated)

        return {"filename": path.name, "action": "indexed", "chunks": len(new["ids"]),
                "signature": corpus_signature(raw_dir, chroma_dir, collection_name)}


def record_end(raw_dir, operation, filename, sha, error, errors):
    """
    Journal record for an operation that failed: rolled_back when the
    store was restored, rollback_failed when it was not.
    """

    try:

        if errors:
            ingestion_journal.append_record(
                raw_dir, ingestion_journal.EVENT_ROLLBACK_FAILED, operation, filename=filename, sha256=sha,
                reason=describe_error(error), error="; ".join(errors),
            )
        else:
            ingestion_journal.append_record(
                raw_dir, ingestion_journal.EVENT_ROLLED_BACK, operation, filename=filename, sha256=sha,
                reason=describe_error(error),
            )

    except Exception:
        pass


# ============================================================
# REPLACE
# ============================================================

def replace(
    principal, register, filename, data, raw_dir, chroma_dir, archive_dir,
    collection_name=COLLECTION_NAME, embed_text=None,
):
    """
    Replaces the stored PDF called filename by a corrected version (data:
    the bytes of the new PDF) and re-indexes it.

    Order: validate and embed the new version (nothing changed yet);
    archive the old PDF; then swap the vectors and the file. On any
    failure the old vectors and the old PDF are put back. Returns
    {"filename", "action", "old_sha256", "new_sha256", "archived",
    "chunks", "signature"}; action is "unchanged" when the new content
    equals the stored one.
    """

    require(principal, authz.INGESTION_WRITE)

    embed_text = embed_text or ingestion_manager.create_embedding

    raw_dir = Path(raw_dir)
    path = stored_path(raw_dir, filename)

    if not path.is_file():
        raise LifecycleError(f"{filename} is not stored, so there is nothing to replace; upload it instead.")

    checked = validation.validate_pdf(path.name, data)

    if not checked["ok"]:
        raise LifecycleError(f"The new version cannot be used: {validation.describe(checked)} {checked['action']}")

    new_sha = hashlib.sha256(data).hexdigest()
    old_sha = calculate_sha256(path)

    if new_sha == old_sha:
        return {"filename": path.name, "action": "unchanged", "old_sha256": old_sha, "new_sha256": new_sha,
                "archived": None, "chunks": 0, "signature": corpus_signature(raw_dir, chroma_dir, collection_name)}

    with ingestion_manager.INGESTION_LOCK:

        collection = get_destination_collection(chroma_dir, collection_name)
        state = scan_destination(raw_dir, collection)

        elsewhere = [name for name in state["raw_by_hash"].get(new_sha, []) if name.casefold() != path.name.casefold()]

        if elsewhere:
            raise LifecycleError(
                f"The new version has the same content as {', '.join(elsewhere)}; storing it twice would duplicate it."
            )

        # Embed the new version first, loaded under the stored name (the
        # source recorded on every vector); nothing is changed yet
        work = Path(tempfile.mkdtemp(prefix="logidoc_replace_work_"))

        try:
            loadable = work / path.name
            loadable.write_bytes(data)
            new = build_vectors(loadable, new_sha, embed_text)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        # The file that will be swapped in, on the same volume as the raw folder
        staging = raw_dir / f".{path.name}.{new_sha[:12]}.new"
        staging.write_bytes(data)

        if calculate_sha256(staging) != new_sha:
            staging.unlink(missing_ok=True)
            raise IOError("The staged copy does not match the new version; nothing was changed.")

        old = source_vectors(collection, path.name)
        archived = archive_copy(path, archive_dir, old_sha)

        operation = ingestion_journal.new_operation_id()
        ingestion_journal.append_record(
            raw_dir, ingestion_journal.EVENT_BEGIN, operation, filename=path.name, sha256=new_sha,
            vector_ids=new["ids"], operation="replace", replaces_sha256=old_sha,
        )

        errors = []
        swapped_file = False

        try:

            if old["ids"]:
                collection.delete(ids=old["ids"])

            write_vectors(collection, new)
            verify_vectors_present(collection, new["ids"])

            os.replace(staging, path)
            swapped_file = True

            if calculate_sha256(path) != new_sha:
                raise IOError("The stored PDF does not match the new version.")

        except BaseException as error:

            restore(collection, old, new["ids"], errors)

            try:
                if swapped_file or staging.exists():
                    shutil.copy2(archived, path)
            except Exception as put_back:
                errors.append(f"the old PDF could not be put back: {describe_error(put_back)}")

            staging.unlink(missing_ok=True)

            record_end(raw_dir, operation, path.name, new_sha, error, errors)

            if errors:
                raise LifecycleError(
                    f"Replacing {path.name} failed ({describe_error(error)}) and the store could not be fully "
                    f"restored: {'; '.join(errors)}. The old PDF is archived as {archived.name}."
                ) from error

            raise

        ingestion_journal.append_record(
            raw_dir, ingestion_journal.EVENT_COMMIT, operation, filename=path.name, sha256=new_sha,
            stored_path=str(path), operation="replace",
        )

    register.log_action(
        "replaced", path.name, new_sha,
        f"replaced content {old_sha[:12]}; the old PDF is archived as {archived.name}",
        principal.username, principal.authenticated,
    )

    return {"filename": path.name, "action": "replaced", "old_sha256": old_sha, "new_sha256": new_sha,
            "archived": str(archived), "chunks": len(new["ids"]),
            "signature": corpus_signature(raw_dir, chroma_dir, collection_name)}


# ============================================================
# REMOVE
# ============================================================

def remove(
    principal, register, filename, raw_dir, chroma_dir, archive_dir, collection_name=COLLECTION_NAME,
):
    """
    Removes a document from the store and the index: its PDF is copied
    into the archive folder (verified), its vectors are deleted, and only
    then is the PDF taken out of the raw folder. If a step fails the
    vectors are written back and the PDF stays. Returns {"filename",
    "archived", "vectors_removed", "signature"}.
    """

    require(principal, authz.INGESTION_WRITE)

    raw_dir = Path(raw_dir)
    path = stored_path(raw_dir, filename)

    if not path.is_file():
        raise LifecycleError(f"{filename} is not stored.")

    sha = calculate_sha256(path)

    with ingestion_manager.INGESTION_LOCK:

        collection = get_destination_collection(chroma_dir, collection_name)
        old = source_vectors(collection, path.name)
        archived = archive_copy(path, archive_dir, sha)

        errors = []

        try:

            if old["ids"]:
                collection.delete(ids=old["ids"])

            remaining = source_vectors(collection, path.name)["ids"]

            if remaining:
                raise RuntimeError(f"{len(remaining)} vector(s) are still in the index after deleting them.")

            path.unlink()

        except BaseException as error:

            restore(collection, old, [], errors)

            if errors:
                raise LifecycleError(
                    f"Removing {path.name} failed ({describe_error(error)}) and the index could not be fully "
                    f"restored: {'; '.join(errors)}. The PDF is still stored and archived as {archived.name}."
                ) from error

            raise

    register.log_action(
        "removed", path.name, sha,
        f"{len(old['ids'])} vector(s) removed; the PDF is archived as {archived.name}",
        principal.username, principal.authenticated,
    )

    return {"filename": path.name, "archived": str(archived), "vectors_removed": len(old["ids"]),
            "signature": corpus_signature(raw_dir, chroma_dir, collection_name)}
