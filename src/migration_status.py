"""
LogiDoc-RAG extraction store: the durable migration status record

A small JSON file next to the store, written only by the migration command, read by the server, so that the outcome
of a migration attempt is visible across process boundaries. See docs/MIGRATION_STATUS_DESIGN.md.

The database is authoritative. Nothing here changes what the schema says; a record can add a notice to a store that
is genuinely at an older schema, and every other case (missing, malformed, unreadable, another store's, out of date)
is ignored. The record holds file names, fixed codes and a fingerprint of the history, never a path, an exception
name or a message.

This module imports only the standard library, so the store can use it and the migration can too.
"""

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

FORMAT = 1
MAX_BYTES = 8192

STARTED, FAILED, SUCCEEDED, UNVERIFIED = "started", "failed", "succeeded", "committed_unverified"
OUTCOMES = (STARTED, FAILED, SUCCEEDED, UNVERIFIED)

CODES = ("history_verification_failed", "integrity_check_failed", "backup_failed", "store_changed", "rolled_back",
         "verification_failed", "post_verification_failed")

KEYS = ("format", "attempt_id", "store_name", "outcome", "code", "from_version", "to_version", "baseline", "at", "backup_name")

HISTORY_TABLES = ("extractions", "field_corrections", "correction_decisions")


def now_text():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status_path(store_path):
    return Path(str(store_path) + ".migration-status.json")


# ---- the history, as a comparable fingerprint --------------------------------------------------------

def table_names(connection):
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def column_names(connection, table):
    return [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]


def baseline(connection, columns=None):
    """
    What the history holds, in a form that can be compared after a schema change. columns ({table: [names]}) fixes which
    columns are digested: the migration passes the columns of the schema it started from, so a column added later does
    not count as a change. Returns tables (columns, count, max id, digest), the ids of every correction and of the
    approved ones, and the chain tips.
    """

    connection.row_factory = sqlite3.Row
    present = table_names(connection)
    result = {"tables": {}, "correction_ids": [], "approved_ids": [], "tips": {"corrections": None, "decisions": None}}

    for table in HISTORY_TABLES:
        if table not in present:
            continue

        names = list(columns[table]) if columns and table in columns else column_names(connection, table)
        digest = sha256()
        count, newest = 0, 0

        for row in connection.execute(f"SELECT {', '.join(names)} FROM {table} ORDER BY id"):
            digest.update(repr(tuple(row)).encode("utf-8"))
            count += 1
            newest = row["id"] if "id" in row.keys() else newest

        result["tables"][table] = {"columns": names, "count": count, "max_id": newest, "digest": digest.hexdigest()}

    if "field_corrections" in present:
        result["correction_ids"] = [r[0] for r in connection.execute("SELECT id FROM field_corrections ORDER BY id")]

        if "hash" in column_names(connection, "field_corrections"):
            tip = connection.execute("SELECT hash FROM field_corrections WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            result["tips"]["corrections"] = tip[0] if tip else None

    if "correction_decisions" in present:
        result["approved_ids"] = [r[0] for r in connection.execute(
            "SELECT correction_id FROM correction_decisions WHERE decision = 'approved' ORDER BY correction_id")]
        tip = connection.execute("SELECT hash FROM correction_decisions ORDER BY id DESC LIMIT 1").fetchone()
        result["tips"]["decisions"] = tip[0] if tip else None

    return result


def columns_of(base):
    return {table: info["columns"] for table, info in base["tables"].items()}


def summary(base):
    """The baseline without the long lists, for a report."""

    return {"counts": {t: i["count"] for t, i in base["tables"].items()}, "corrections": len(base["correction_ids"]),
            "approved_corrections": len(base["approved_ids"]), "correction_chain_tip": base["tips"]["corrections"],
            "decision_chain_tip": base["tips"]["decisions"]}


def differences(before, after):
    """What differs between two baselines (empty when the history is exactly as it was)."""

    found = []

    for table, info in before["tables"].items():
        other = after["tables"].get(table)

        if other is None or other["count"] != info["count"] or other["digest"] != info["digest"]:
            found.append(f"{table} changed")

    for key, label in (("correction_ids", "correction ids"), ("approved_ids", "approved correction ids")):
        if before[key] != after[key]:
            found.append(f"{label} changed")

    if before["tips"] != after["tips"]:
        found.append("a chain tip changed")

    return found


def fingerprint(base):
    """One hex digest of a baseline: which history a status record is about."""

    return sha256(json.dumps(base, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---- the record --------------------------------------------------------------------------------------

def new_record(store_path, outcome, code, from_version, to_version, base_fingerprint, attempt_id=None, backup_name=None):
    return {"format": FORMAT, "attempt_id": attempt_id or uuid.uuid4().hex, "store_name": Path(store_path).name, "outcome": outcome,
            "code": code, "from_version": from_version, "to_version": to_version, "baseline": base_fingerprint, "at": now_text(),
            "backup_name": backup_name}


def valid(record):
    """True only for a record that is exactly what write() produces: every key, every type, every enum."""

    if not isinstance(record, dict) or set(record) != set(KEYS) or record["format"] != FORMAT:
        return False

    text = lambda value, limit: isinstance(value, str) and 0 < len(value) <= limit  # noqa: E731
    optional = lambda value, limit: value is None or text(value, limit)  # noqa: E731
    number = lambda value: isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1000  # noqa: E731

    if not (text(record["attempt_id"], 64) and text(record["store_name"], 300) and text(record["baseline"], 64) and optional(record["backup_name"], 300)):
        return False

    if record["outcome"] not in OUTCOMES or not (record["code"] is None or record["code"] in CODES):
        return False

    if not (number(record["from_version"]) and number(record["to_version"])) or not text(record["at"], 40):
        return False

    try:
        datetime.fromisoformat(record["at"])
    except ValueError:
        return False

    return True


def write(store_path, record):
    """
    Atomically replaces the record: a temporary file in the same directory, flushed and fsynced, then os.replace. A
    reader sees the old record or the new one. Raises OSError when it cannot (the caller decides what that means).
    """

    target = status_path(store_path)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(record, sort_keys=True, indent=2).encode("utf-8")

    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())

    # On Windows a reader that has the record open for a moment makes the replace fail with PermissionError: try again briefly
    for attempt in range(5):
        try:
            os.replace(temporary, target)

            return
        except PermissionError:
            if attempt == 4:
                raise

            time.sleep(0.05)


def read(store_path):
    """("missing" | "unreadable" | "ok", record or None). Never raises: a record that cannot be trusted is "unreadable"."""

    target = status_path(store_path)

    try:
        with open(target, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unreadable", None

    if len(data) > MAX_BYTES:
        return "unreadable", None

    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "unreadable", None

    return ("ok", record) if valid(record) else ("unreadable", None)


def reconcile(store_path, version, target_version):
    """
    What the record may say about a store at `version` (the schema decides; see the design table). Returns
    {"attempt": none|failed|unfinished|succeeded|unverified, "code", "at", "record": ok|missing|ignored|unreadable}.
    "failed" and "unfinished" are returned only for a store still below the target whose history is the one the record
    was written for. "unverified" asks the caller to run the integrity and history checks on a current store.
    """

    kind, record = read(store_path)
    info = {"attempt": "none", "code": None, "at": None, "record": kind}

    if kind != "ok":
        return info

    def ignored():
        return {"attempt": "none", "code": None, "at": None, "record": "ignored"}

    if record["store_name"] != Path(store_path).name:
        return ignored()

    if version >= target_version:
        if record["to_version"] != version:
            return ignored()

        if record["outcome"] == SUCCEEDED:
            return {**info, "attempt": "succeeded", "at": record["at"]}

        if record["outcome"] == UNVERIFIED:
            return {**info, "attempt": "unverified", "code": record["code"], "at": record["at"]}

        return ignored()                                  # failed / started next to a current store: another attempt won

    if record["outcome"] not in (STARTED, FAILED) or record["from_version"] != version:
        return ignored()                                  # a success next to an older store: it was restored or replaced

    try:
        connection = sqlite3.connect(f"{Path(store_path).resolve().as_uri()}?mode=ro", uri=True, timeout=10)

        try:
            now = fingerprint(baseline(connection))
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return {**info, "record": "unreadable"}

    if now != record["baseline"]:
        return ignored()                                  # the history is not the one the record was written for

    if record["outcome"] == FAILED:
        return {**info, "attempt": "failed", "code": record["code"], "at": record["at"]}

    return {**info, "attempt": "unfinished", "at": record["at"]}
