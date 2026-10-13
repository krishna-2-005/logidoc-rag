"""
LogiDoc-RAG Ingestion Journal

An append-only JSON Lines file, <raw_dir>/.ingestion_journal.jsonl,
that records every ingestion operation which writes to the vector
index, so that interrupted operations can be found afterwards.

The journal DETECTS operations that did not finish, for example
after a crash or power loss between writing vectors and saving the
PDF. It cannot prevent such crashes and never repairs anything.

The file name does not end in .pdf, so PDF scans ignore it, and a
full rebuild (which only removes *.pdf files) leaves it in place.

Schema (one JSON object per line):

    Every record:
        schema        1
        event         "begin" | "commit" | "rolled_back" |
                      "rollback_failed" | "duplicate"
        operation_id  unique id shared by the records of one operation
        time          UTC timestamp, ISO 8601
        filename      name of the file being ingested
        sha256        SHA-256 of the file content

    begin            vector_ids: the IDs about to be written.
                     Written and fsynced BEFORE the first vector write.
    commit           stored_path: where the PDF was stored.
                     Written only after the vectors and the stored
                     PDF were both verified.
    rolled_back      reason: why the operation was undone.
                     Written only after the rollback succeeded.
    rollback_failed  reason, error, vector_ids: vectors that may
                     remain in the index.
    duplicate        matched_files: existing files with the same
                     content. Provenance only; nothing was written.

An operation with a "begin" record but no later "commit",
"rolled_back" or "rollback_failed" record did not finish.

A missing journal record does not prove that an operation never
started: vectors from a full rebuild, or from before the journal
existed, have no records at all.

Single writer: two ingestions must not run on the same raw folder
at the same time.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# CONSTANTS
# ============================================================

JOURNAL_FILENAME = ".ingestion_journal.jsonl"
SCHEMA_VERSION = 1

EVENT_BEGIN = "begin"
EVENT_COMMIT = "commit"
EVENT_ROLLED_BACK = "rolled_back"
EVENT_ROLLBACK_FAILED = "rollback_failed"
EVENT_DUPLICATE = "duplicate"

KNOWN_EVENTS = {
    EVENT_BEGIN,
    EVENT_COMMIT,
    EVENT_ROLLED_BACK,
    EVENT_ROLLBACK_FAILED,
    EVENT_DUPLICATE,
}

# Events that end an operation started by "begin"
TERMINAL_EVENTS = {
    EVENT_COMMIT,
    EVENT_ROLLED_BACK,
    EVENT_ROLLBACK_FAILED,
}


# ============================================================
# WRITING
# ============================================================

def journal_path(raw_dir):

    return Path(raw_dir) / JOURNAL_FILENAME


def new_operation_id():

    return uuid.uuid4().hex


def append_record(raw_dir, event, operation_id, **fields):
    """
    Append one record and force it to disk (flush + fsync).

    If a crash left the last line unterminated, a newline is
    written first so the new record stays readable.
    """

    record = {
        "schema": SCHEMA_VERSION,
        "event": event,
        "operation_id": operation_id,
        "time": datetime.now(timezone.utc).isoformat(),
        **fields,
    }

    path = journal_path(raw_dir)

    prefix = ""

    if path.exists() and path.stat().st_size > 0:

        with open(path, "rb") as journal:

            journal.seek(-1, os.SEEK_END)

            if journal.read(1) != b"\n":
                prefix = "\n"

    line = prefix + json.dumps(record, sort_keys=True) + "\n"

    with open(path, "a", encoding="utf-8") as journal:

        journal.write(line)
        journal.flush()
        os.fsync(journal.fileno())

    return record


# ============================================================
# READING
# ============================================================

def read_journal(raw_dir):
    """
    Read every valid record. Never raises: unreadable, truncated
    or malformed lines are skipped and listed in "problems".
    """

    path = journal_path(raw_dir)

    result = {
        "path": str(path),
        "exists": path.exists(),
        "records": [],
        "problems": [],
    }

    if not result["exists"]:
        return result

    try:

        text = path.read_bytes().decode(
            "utf-8",
            errors="replace"
        )

    except Exception as error:

        result["problems"].append(
            {
                "line": None,
                "reason": f"Journal could not be read: {error}",
                "text": "",
            }
        )

        return result

    for number, line in enumerate(text.splitlines(), start=1):

        if not line.strip():
            continue

        try:
            record = json.loads(line)
        except ValueError:
            record = None

        if not isinstance(record, dict):

            reason = "Not valid JSON (truncated or corrupted)"

        elif record.get("event") not in KNOWN_EVENTS:

            reason = f"Unknown event {record.get('event')!r}"

        elif not isinstance(record.get("operation_id"), str):

            reason = "Missing operation_id"

        else:

            result["records"].append(record)

            continue

        result["problems"].append(
            {
                "line": number,
                "reason": reason,
                "text": line[:200],
            }
        )

    return result


def summarize_operations(records):
    """
    Group records by operation_id.

    Returns {operation_id: {"begin": record or None,
                            "end": last terminal record or None,
                            "events": [records in file order]}}
    """

    operations = {}

    for record in records:

        operation = operations.setdefault(
            record["operation_id"],
            {
                "begin": None,
                "end": None,
                "events": [],
            }
        )

        operation["events"].append(record)

        if record["event"] == EVENT_BEGIN:
            operation["begin"] = record

        if record["event"] in TERMINAL_EVENTS:
            operation["end"] = record

    return operations


def unfinished_operations(records):
    """
    Operations with a "begin" record and no terminal record.
    """

    return [
        operation
        for operation in summarize_operations(records).values()
        if operation["begin"] and not operation["end"]
    ]
