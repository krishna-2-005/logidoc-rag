"""
LogiDoc-RAG ingestion service

Adds uploaded PDFs to the document store through the existing safe
ingestion (ingestion_manager), and wraps it with what a production
workflow needs:

    validation       document_validation: a clear reason for every
                     rejected file, before anything is written
    status           every file has a status in the ingestion register
                     (queued, processing, indexed, partial, failed,
                     unsupported) from the moment it is accepted
    duplicates       by content hash (ingestion_manager), whatever the
                     file name; the upload is recorded as indexed with
                     code "duplicate" and nothing is written
    retries          a transient failure (the embedding service not
                     answering, a locked database) is retried a few
                     times with a growing pause. ingestion_manager rolls
                     back every failed attempt, and vector IDs come from
                     the content hash, so a retry can never index a
                     file twice
    diagnostics      the document type and shipment ID found, and
                     whether the document can be attributed to a
                     shipment at all
    register         failures keep their reason and corrective action;
                     they stay open errors until resolved

Rules this service keeps

    - An original PDF is never modified or deleted. Files are only ever
      added to the raw folder, by ingestion_manager, after the vectors
      are verified.
    - A file that could not be ingested is never silently dropped: it is
      in the register, and audit_problems() hands it to the audit, so no
      shipment is reported clean while a file that might belong to it is
      missing.
    - Evidence of different shipments is never mixed: the service
      indexes a document under the shipment ID found in the document, as
      the existing ingestion does; nothing is assigned by name.
    - Image-only PDFs are reported as needing OCR (unsupported). There is
      no OCR here.

This module must stay free of Streamlit calls (see ingestion_manager).
"""

import hashlib
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import authz
import document_validation as validation
import ingestion_manager
import ingestion_register as register_module
from authz import require
from config import COLLECTION_NAME
from shipment_identity import identifier_key


MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (0.5, 1.0, 2.0)

# Failures that are about the moment, not about the file
TRANSIENT = re.compile(
    r"connect|timed? ?out|timeout|temporar|unavailable|database is locked|busy|"
    r"reset by peer|broken pipe|connection (?:aborted|refused|reset)|"
    r"\b50[234]\b|too many requests|try again|disk i/o|\bEOF\b",
    re.IGNORECASE,
)

# Stages of ingestion_manager.ingest_file whose failure can be transient
TRANSIENT_STAGES = ("embed", "journal", "index", "store")

NAME_CONFLICT = "name_conflict"
INDEX_INCONSISTENT = "index_inconsistent"
PROCESSING_FAILED = "processing_failed"
RETRIES_EXHAUSTED = "retries_exhausted"
DUPLICATE = "duplicate"
NO_SHIPMENT_ID = "no_shipment_id"
UNKNOWN_DOCUMENT_TYPE = "unknown_document_type"
PARTIAL_TEXT = validation.PARTIAL_TEXT

ISSUE_TEXT = {
    NO_SHIPMENT_ID: (
        "No shipment ID was found in the document, so it is indexed but belongs to no shipment: "
        "audits ignore it."
    ),
    UNKNOWN_DOCUMENT_TYPE: "The document type could not be determined (not recognised as a BOL, invoice or POD).",
    PARTIAL_TEXT: validation.CODES[validation.PARTIAL_TEXT],
}

ISSUE_ACTION = {
    NO_SHIPMENT_ID: (
        "Check that the document shows 'Shipment Number: ...' as text. Replace it with a corrected copy "
        "(an administrator can use Replace)."
    ),
    UNKNOWN_DOCUMENT_TYPE: "Check the document title; a BOL, invoice or POD is recognised by its content.",
    PARTIAL_TEXT: validation.ACTIONS[validation.PARTIAL_TEXT],
}


@dataclass(frozen=True)
class Upload:
    """
    One file offered for ingestion: the name the client gave (never used
    as a path) and its bytes.
    """

    name: str
    data: bytes


def sha256_of(data):

    return hashlib.sha256(data).hexdigest()


def is_transient(outcome):
    """
    Whether a failed ingestion outcome is worth retrying: the failure
    happened while talking to a service or writing, and says so.
    """

    if outcome["status"] != ingestion_manager.STATUS_FAILED:
        return False

    if outcome.get("stage") not in TRANSIENT_STAGES:
        return False

    return bool(TRANSIENT.search(str(outcome.get("error") or "")))


def actor_fields(principal):

    return {"actor": principal.username, "authenticated": bool(principal.authenticated)}


# ============================================================
# QUARANTINE (our own copy of a file that failed for a transient reason)
# ============================================================

def quarantine_path(quarantine_dir, sha256):
    """
    Where a file is kept for a retry: named by its content hash, so no
    client-supplied name or path is ever used.
    """

    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise ValueError("Not a SHA-256.")

    return Path(quarantine_dir) / f"{sha256}.pdf"


def keep_for_retry(quarantine_dir, sha256, data):

    path = quarantine_path(quarantine_dir, sha256)
    path.parent.mkdir(parents=True, exist_ok=True)

    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(path)

    return path.name


def drop_quarantined(quarantine_dir, name):
    """
    Removes our own quarantine copy once its content is indexed and
    stored. It never touches the raw folder.
    """

    if not name or not re.fullmatch(r"[0-9a-f]{64}\.pdf", name):
        return

    try:
        (Path(quarantine_dir) / name).unlink()
    except OSError:
        pass


# ============================================================
# PROCESSING
# ============================================================

def diagnose(outcome, checked):
    """
    The problems of an indexed document worth knowing: pages without
    text, no shipment ID, an unknown document type. Returns the issue
    codes.
    """

    issues = []

    if checked["warnings"]:
        issues.extend(checked["warnings"])

    if not (outcome.get("shipment_id") or "").strip() or not identifier_key(outcome.get("shipment_id")):
        issues.append(NO_SHIPMENT_ID)

    if str(outcome.get("document_type") or "UNKNOWN").upper() in ("UNKNOWN", ""):
        issues.append(UNKNOWN_DOCUMENT_TYPE)

    return issues


def finish_from_outcome(register, file_id, outcome, checked, actor, quarantine_dir, data, sha256, attempts_made):
    """
    Records how ingestion_manager's outcome for one file ends in the
    register and returns the record.
    """

    status = outcome["status"]

    common = {"sha256": outcome.get("sha256") or sha256, "pages": checked["pages"], "text_pages": checked["text_pages"]}

    if status == ingestion_manager.STATUS_ADDED:

        issues = diagnose(outcome, checked)
        shipment_id = (outcome.get("shipment_id") or None)

        return register.update(
            file_id,
            register_module.PARTIAL if issues else register_module.INDEXED,
            actor=actor,
            code=issues[0] if issues else None,
            message=(
                " ".join(ISSUE_TEXT[issue] for issue in issues) if issues
                else f"Indexed {outcome['chunks_indexed']} chunk(s) and stored as {Path(outcome['stored_path']).name}."
            ),
            action=" ".join(ISSUE_ACTION[issue] for issue in issues) or None,
            document_type=outcome.get("document_type"),
            shipment_id=shipment_id,
            shipment_key=identifier_key(shipment_id),
            chunks_indexed=outcome["chunks_indexed"],
            **common,
        )

    if status == ingestion_manager.STATUS_DUPLICATE:

        return register.update(
            file_id, register_module.INDEXED, actor=actor, code=DUPLICATE,
            message=outcome["message"] + " Nothing was added.", duplicate_of=outcome["duplicate_of"],
            **common,
        )

    if status == ingestion_manager.STATUS_CHANGED:

        return register.update(
            file_id, register_module.FAILED, actor=actor, code=NAME_CONFLICT,
            message=outcome["message"],
            action=(
                "To replace the stored document with this version, an administrator uses Replace. "
                "If it is a different document, upload it under a different name."
            ),
            **common,
        )

    if status == ingestion_manager.STATUS_INCONSISTENT:

        return register.update(
            file_id, register_module.FAILED, actor=actor, code=INDEX_INCONSISTENT,
            message=outcome["message"],
            action=(
                "The document store and the search index disagree about this file. Run the consistency "
                "check (Diagnostics); an administrator can reprocess a stored file that has no vectors."
            ),
            **common,
        )

    # Failed
    transient = is_transient(outcome)
    kept = keep_for_retry(quarantine_dir, sha256, data) if transient else None

    return register.update(
        file_id, register_module.FAILED, actor=actor,
        code=RETRIES_EXHAUSTED if transient else PROCESSING_FAILED,
        message=(
            f"{outcome.get('error')} (stage: {outcome.get('stage')}"
            + (f"; tried {attempts_made} times)" if transient else ")")
        ),
        action=(
            "The service was not available. A copy is kept: use Retry once it is back."
            if transient else
            "Fix the problem named above. If the PDF itself is the cause, upload a corrected copy."
        ),
        quarantine_name=kept,
        **common,
    )


def process_one(
    register, upload, actor, raw_dir, chroma_dir, collection_name, embed_text, quarantine_dir,
    max_attempts, backoff, sleep, existing_id=None,
):
    """
    One file from queue to its final status. Returns the register record.
    """

    data = bytes(upload.data)
    sha256 = sha256_of(data)
    principal_fields = actor_fields(actor)

    file_id = existing_id or register.queue(upload.name, sha256, len(data), **principal_fields)

    checked = validation.validate_pdf(upload.name, data)

    if not checked["ok"]:

        return register.update(
            file_id, checked["status"], actor=actor.username, code=checked["code"],
            message=validation.describe(checked), action=checked["action"],
            sha256=sha256, size=len(data), pages=checked["pages"], text_pages=checked["text_pages"],
        )

    register.update(file_id, register_module.PROCESSING, actor=actor.username)

    work_dir = Path(tempfile.mkdtemp(prefix="logidoc_ingest_work_"))

    try:

        path = work_dir / Path(upload.name).name
        path.write_bytes(data)

        outcome, attempt = None, 0

        while True:

            attempt += 1

            outcome = ingestion_manager.run_ingestion(
                [path], Path(raw_dir), Path(chroma_dir), collection_name, embed_text
            )["files"][0]

            if not is_transient(outcome) or attempt >= max_attempts:
                break

            register.attempt_failed(file_id, outcome["error"], actor=actor.username)
            sleep(backoff[min(attempt - 1, len(backoff) - 1)])

        record = finish_from_outcome(
            register, file_id, outcome, checked, actor.username, quarantine_dir, data, sha256, attempt
        )

        # Which stored files an upload matched, with how each is indexed:
        # kept on the returned record only (the register stores the reason)
        record["matched_files"] = outcome.get("matched_files") or []

        return record

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def process_files(
    principal, uploads, raw_dir, chroma_dir, register, quarantine_dir,
    collection_name=COLLECTION_NAME, embed_text=None,
    max_attempts=MAX_ATTEMPTS, backoff=BACKOFF_SECONDS, sleep=time.sleep,
):
    """
    Ingests uploads (Upload objects) one by one and returns
    {"files": [register records], "indexed", "partial", "failed",
    "unsupported", "duplicates", "corpus_changed", "errors": [...]}.

    The whole batch holds ingestion_manager's lock, so no other ingestion
    runs at the same time, and anything left queued or processing by an
    earlier crash is marked failed (interrupted) first. If the process is
    interrupted (KeyboardInterrupt, SystemExit), the file in progress was
    already rolled back by ingestion_manager; it and the files not yet
    started are marked failed (interrupted) and the interruption is
    raised again.
    """

    require(principal, authz.INGESTION_WRITE)

    embed_text = embed_text or ingestion_manager.create_embedding

    uploads = list(uploads)
    records = []

    with ingestion_manager.INGESTION_LOCK:

        register.recover_interrupted()

        # Every file is in the register before the first one is processed,
        # so an interruption leaves each of them with a status
        file_ids = [
            register.queue(upload.name, sha256_of(bytes(upload.data)), len(bytes(upload.data)), **actor_fields(principal))
            for upload in uploads
        ]

        try:

            for upload, file_id in zip(uploads, file_ids):

                records.append(process_one(
                    register, upload, principal, raw_dir, chroma_dir, collection_name, embed_text,
                    quarantine_dir, max_attempts, backoff, sleep, existing_id=file_id,
                ))

        except BaseException:

            register.recover_interrupted()
            raise

    return summarize(records)


def summarize(records):

    def count(status, duplicate=None):
        return sum(
            1 for item in records
            if item["status"] == status and (duplicate is None or (item["code"] == DUPLICATE) == duplicate)
        )

    return {
        "files": records,
        "indexed": count(register_module.INDEXED, duplicate=False),
        "partial": count(register_module.PARTIAL),
        "duplicates": count(register_module.INDEXED, duplicate=True),
        "failed": count(register_module.FAILED),
        "unsupported": count(register_module.UNSUPPORTED),
        # Something was added to the store and the index
        "corpus_changed": any(
            item["status"] in (register_module.INDEXED, register_module.PARTIAL) and item["code"] != DUPLICATE
            for item in records
        ),
        "errors": [
            f"{item['filename']}: {item['message']}"
            for item in records if item["status"] in register_module.ERROR_STATUSES
        ],
    }


def retry(
    principal, register, file_id, raw_dir, chroma_dir, quarantine_dir,
    collection_name=COLLECTION_NAME, embed_text=None,
    max_attempts=MAX_ATTEMPTS, backoff=BACKOFF_SECONDS, sleep=time.sleep,
):
    """
    Processes a failed file again from the copy kept for a retry (only
    files that failed for a transient reason have one). Returns the
    summary of process_files. A file without a kept copy cannot be
    retried: its corrected version has to be uploaded again.
    """

    require(principal, authz.INGESTION_WRITE)

    record = register.get(file_id)

    if record is None:
        raise register_module.RegisterError(f"There is no ingestion record {file_id}.")

    if record["status"] not in register_module.ERROR_STATUSES or not record["quarantine_name"]:
        raise register_module.RegisterError(
            "Only a file that failed because a service was unavailable can be retried; "
            "upload the corrected file otherwise."
        )

    path = quarantine_path(quarantine_dir, record["sha256"])

    if not path.is_file() or path.name != record["quarantine_name"]:
        raise register_module.RegisterError(
            "The copy kept for this retry is no longer there; upload the file again."
        )

    data = path.read_bytes()

    if sha256_of(data) != record["sha256"]:
        raise register_module.RegisterError("The kept copy no longer matches the file; upload it again.")

    embed_text = embed_text or ingestion_manager.create_embedding

    with ingestion_manager.INGESTION_LOCK:

        register.recover_interrupted()
        register.requeue(file_id, actor=principal.username)

        try:
            result = process_one(
                register, Upload(record["filename"], data), principal, raw_dir, chroma_dir, collection_name,
                embed_text, quarantine_dir, max_attempts, backoff, sleep, existing_id=file_id,
            )
        except BaseException:
            register.recover_interrupted()
            raise

    if result["status"] in (register_module.INDEXED, register_module.PARTIAL):
        drop_quarantined(quarantine_dir, record["quarantine_name"])

    return summarize([result])


def resolve_error(principal, register, file_id, note):
    """
    An administrator closes an open ingestion error and says what was
    done (the file was corrected elsewhere, or does not matter).
    """

    require(principal, authz.INGESTION_WRITE)

    return register.resolve(file_id, principal.username, note)


# ============================================================
# WHAT THE AUDIT MUST KNOW
# ============================================================

def merge_problems(raw_dir_problems, offered_problems):
    """
    The unreadable files of the raw folder (chunker.read_chunks) plus the
    unresolved files of the ingestion register, as dicts with filename,
    status and error. One entry per file name: the raw folder's own
    reading wins, since a stored file is read, or reported, there.
    """

    merged = {item["filename"]: item for item in raw_dir_problems}

    for item in offered_problems:
        merged.setdefault(item["filename"], item)

    return [merged[name] for name in sorted(merged)]


def audit_problems(register, raw_dir_problems=()):
    """
    The files the audit has to hold against every shipment: the
    unreadable files of the raw folder and the unresolved files of the
    ingestion register that were not stored (merge_problems).
    """

    return merge_problems(raw_dir_problems, register.audit_problems())


def open_problems(register_path):
    """
    audit_problems() of the register at register_path, read without
    creating it: no register file means nothing was ever offered through
    the register, so there is nothing open.
    """

    path = Path(register_path)

    if not path.is_file():
        return []

    return register_module.IngestionRegister(path).audit_problems()


def problems_token(problems):
    """
    A hashable summary of audit_problems(), for a cache key.
    """

    return tuple((item["filename"], item["status"]) for item in problems)
