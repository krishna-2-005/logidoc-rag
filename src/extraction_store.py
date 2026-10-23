"""
LogiDoc-RAG extraction store

A small SQLite database (separate from the audit, review, history, user and
ingestion databases) holding what document_extraction read from each PDF and
what reviewers corrected afterwards.

    extractions         one row per (file content hash, extractor version):
                        the typed result as JSON, with the SHA-256 of that JSON.
                        Reprocessing the same file with the same extractor
                        changes nothing; a new extractor version (any change to
                        the extraction code, see
                        document_extraction.EXTRACTOR_VERSION) adds a new row,
                        and the old one is kept. Rows are never updated or
                        deleted (triggers). The JSON holds typed values,
                        states, pages, reason codes and labels, never quoted
                        document text: the raw snippets are dropped and
                        warnings and review reasons do not quote the document
                        (the PDF is the source).
    field_corrections   append-only (triggers) and hash-chained: each row
                        stores the hash of the row before it, and
                        verify_history() recomputes the chain and the
                        extraction payload hashes. A reviewer's corrected value
                        for one field of one extraction, with the previous
                        effective value, the correction it was based on, the
                        reason, who and when. The extracted value is never
                        overwritten.

A correction is a PROPOSAL (propose). Its status is derived, never stored:

    legacy      written before approvals existed (schema < 4); it never has an
                effect and cannot be decided: propose it again
    pending     proposed, no decision yet: no effect
    approved    approved by a different, signed-in administrator
                (correction_decisions): the only status that can reach an audit
    rejected    rejected with a reason: no effect

correction_decisions is append-only (triggers), hash-chained, and allows one
decision per correction (UNIQUE), so two concurrent decisions cannot both
succeed. The proposer cannot decide their own correction. effective() keeps the
original extracted value, the pending proposal and the approved correction
apart. approved_corrections(shipment_key) is what the audit may read
(audit_inputs); audit_token() changes when an approval is added.

A proposal names the latest correction of that field it was based on (based_on;
None when there was none). If someone else proposed or decided in the meantime
it is refused (StaleCorrection, HTTP 409), so a later request never silently
replaces a change its author has not seen; while a proposal awaits its
decision, no other can be made for the field. Shipment IDs cannot be corrected
(document_extraction.coerce_correction).

Limits of the protection: the triggers and the hash chain stop and detect
changes made through ordinary SQL. They are not tamper-proof against someone
who can write the database file: such a person can drop the triggers, rewrite
rows and recompute every hash, delete the newest rows (which leaves no gap in
the chain), or replace the file. Keep the file where only the service can
write it and back it up.

Every change is one transaction (BEGIN IMMEDIATE). A new database is created at
the current schema from MIGRATIONS in one transaction (the version is read again
under the lock, so two processes starting at once create it once). An existing
database is NEVER upgraded by opening it: a request reads it (open_for_reading) or
writes it (open_for_write, only when current), and an older schema answers
MigrationPending until an operator runs the explicit migration
(extraction_migration, scripts/migrate_extraction_store.py: verified backup,
history verification, one transaction). A database from a newer version is
refused, never downgraded.

extraction_aliases (schema 5) keeps every file name a content was seen under,
apart from the extraction row, whose UNIQUE (sha256, extractor_version) is
unchanged: append-only, one original per extraction, a relation (original,
same_type, type_conflict) the database itself checks. A type conflict is recorded
and then refused; aliases are information only and never reach the audit.
"""

import hashlib
import json
import sqlite3
from collections import namedtuple
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import authz
import document_extraction as de
import migration_status as status
from shipment_identity import identifier_key


SCHEMA_VERSION = 5

# What the file on disk is, as far as the code that wants to use it is concerned (ExtractionStore.inspect)
ABSENT = "absent"                        # no file: nothing was ever stored
OLDER = "older"                          # older than the first schema that holds approvals: none can exist
MIGRATION_PENDING = "migration_pending"  # an older schema that holds approvals, valid and readable, awaiting migration
CURRENT = "current"
UNAVAILABLE = "unavailable"              # unreadable, from a newer version, or structurally invalid: never an empty store
MIGRATION_FAILED = "migration_failed"    # an upgrade was attempted by this process and rolled back; the file is unchanged

# Schema 4 introduced approvals (correction_decisions). Older files hold none, and their corrections are legacy
MIN_READABLE_VERSION = 4
REQUIRED_TABLES = ("extractions", "field_corrections", "correction_decisions")
REQUIRED_TABLES_V5 = REQUIRED_TABLES + ("extraction_aliases",)
REQUIRED_TRIGGERS_V5 = ("extraction_aliases_no_update", "extraction_aliases_no_delete", "extraction_aliases_consistent", "extraction_aliases_relation")

# migration is the reconciled status record (migration_status.reconcile) for a store that has one to report, else None
StoreInspection = namedtuple("StoreInspection", "state version reason migration", defaults=(None,))

# Derived correction statuses
LEGACY, PENDING, APPROVED, REJECTED = "legacy", "pending", "approved", "rejected"
MIN_REASON = 10
MAX_REASON = 1000

MIGRATIONS = {
    1: """
    CREATE TABLE IF NOT EXISTS extractions (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        filename          TEXT NOT NULL,
        sha256            TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        document_type     TEXT NOT NULL,
        status            TEXT NOT NULL,
        shipment_id       TEXT,
        shipment_key      TEXT,
        payload           TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        UNIQUE (sha256, extractor_version)
    );
    CREATE INDEX IF NOT EXISTS extractions_by_shipment ON extractions (shipment_key);
    """,
    2: """
    CREATE TABLE IF NOT EXISTS field_corrections (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        extraction_id INTEGER NOT NULL REFERENCES extractions (id),
        field_name    TEXT NOT NULL,
        previous      TEXT NOT NULL,
        corrected     TEXT NOT NULL,
        reason        TEXT NOT NULL,
        actor         TEXT NOT NULL,
        authenticated INTEGER NOT NULL,
        at            TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS corrections_by_extraction ON field_corrections (extraction_id, field_name, id);
    CREATE TRIGGER IF NOT EXISTS field_corrections_no_update
        BEFORE UPDATE ON field_corrections
        BEGIN SELECT RAISE(ABORT, 'field corrections are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS field_corrections_no_delete
        BEFORE DELETE ON field_corrections
        BEGIN SELECT RAISE(ABORT, 'field corrections are append-only'); END;
    """,
    3: """
    ALTER TABLE extractions ADD COLUMN payload_sha256 TEXT;
    ALTER TABLE field_corrections ADD COLUMN based_on INTEGER;
    ALTER TABLE field_corrections ADD COLUMN prev_hash TEXT;
    ALTER TABLE field_corrections ADD COLUMN hash TEXT;
    CREATE TRIGGER IF NOT EXISTS extractions_no_update
        BEFORE UPDATE ON extractions
        BEGIN SELECT RAISE(ABORT, 'stored extractions are never changed: extract again under a new version'); END;
    CREATE TRIGGER IF NOT EXISTS extractions_no_delete
        BEFORE DELETE ON extractions
        BEGIN SELECT RAISE(ABORT, 'stored extractions are never deleted'); END;
    """,
    # Existing corrections get legacy = 1 through the column default (no row is
    # rewritten, the append-only triggers are not involved): they are never
    # promoted to approved. New corrections insert legacy = 0 explicitly.
    4: """
    ALTER TABLE field_corrections ADD COLUMN legacy INTEGER NOT NULL DEFAULT 1;
    CREATE TABLE IF NOT EXISTS correction_decisions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        correction_id INTEGER NOT NULL UNIQUE REFERENCES field_corrections (id),
        decision      TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
        note          TEXT,
        actor         TEXT NOT NULL,
        authenticated INTEGER NOT NULL,
        at            TEXT NOT NULL,
        prev_hash     TEXT NOT NULL,
        hash          TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS correction_decisions_no_update
        BEFORE UPDATE ON correction_decisions
        BEGIN SELECT RAISE(ABORT, 'correction decisions are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS correction_decisions_no_delete
        BEFORE DELETE ON correction_decisions
        BEGIN SELECT RAISE(ABORT, 'correction decisions are append-only'); END;
    """,
    # Aliases: the file names a content was seen under, kept apart from the extraction row, which keeps its
    # UNIQUE (sha256, extractor_version). Append-only, checked on insert, backfilled with one original per row.
    5: """
    CREATE TABLE IF NOT EXISTS extraction_aliases (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        extraction_id     INTEGER NOT NULL REFERENCES extractions (id),
        sha256            TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        filename          TEXT NOT NULL,
        inferred_type     TEXT NOT NULL,
        relation          TEXT NOT NULL CHECK (relation IN ('original', 'same_type', 'type_conflict')),
        first_seen_at     TEXT NOT NULL,
        seen_by           TEXT,
        UNIQUE (sha256, extractor_version, filename)
    );
    CREATE INDEX IF NOT EXISTS aliases_by_extraction ON extraction_aliases (extraction_id);
    CREATE INDEX IF NOT EXISTS aliases_by_name ON extraction_aliases (filename);
    CREATE UNIQUE INDEX IF NOT EXISTS aliases_one_original ON extraction_aliases (extraction_id) WHERE relation = 'original';
    CREATE TRIGGER IF NOT EXISTS extraction_aliases_no_update
        BEFORE UPDATE ON extraction_aliases
        BEGIN SELECT RAISE(ABORT, 'extraction aliases are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS extraction_aliases_no_delete
        BEFORE DELETE ON extraction_aliases
        BEGIN SELECT RAISE(ABORT, 'extraction aliases are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS extraction_aliases_consistent
        BEFORE INSERT ON extraction_aliases
        WHEN NOT EXISTS (SELECT 1 FROM extractions e WHERE e.id = NEW.extraction_id AND e.sha256 = NEW.sha256 AND e.extractor_version = NEW.extractor_version)
        BEGIN SELECT RAISE(ABORT, 'an alias must carry the content hash and extractor version of its extraction'); END;
    CREATE TRIGGER IF NOT EXISTS extraction_aliases_relation
        BEFORE INSERT ON extraction_aliases
        WHEN (NEW.relation = 'type_conflict') = (NEW.inferred_type = (SELECT document_type FROM extractions WHERE id = NEW.extraction_id))
        BEGIN SELECT RAISE(ABORT, 'an alias relation must agree with its document type'); END;
    INSERT OR IGNORE INTO extraction_aliases (extraction_id, sha256, extractor_version, filename, inferred_type, relation, first_seen_at, seen_by)
        SELECT id, sha256, extractor_version, filename, document_type, 'original', created_at, NULL FROM extractions ORDER BY id;
    """,
}


class StoreError(ValueError):
    """
    A change that is not allowed (unknown extraction or field, bad value, short
    reason) or a database of a newer schema version.
    """


class StoreUnavailable(StoreError):
    """
    The store file exists but cannot be used for the audit: it is damaged, or
    from a newer version. The audit must say so instead of behaving as if no
    correction had ever been approved.
    """


class InvalidTransition(StoreError):
    """
    A decision that is not allowed from the correction's state: already
    decided, or a legacy correction.
    """


class MigrationPending(StoreError):
    """
    The store needs a schema upgrade before it can be written. It can still be read.
    No request other than an explicit upgrade may change the schema.
    """


class MigrationFailed(StoreError):
    """An upgrade was attempted and rolled back; the file is as it was and the reason is shown."""


# The errors that mean "the store cannot be used right now" (HTTP 503), not "this request was invalid"
STORE_STATE_ERRORS = (StoreUnavailable, MigrationPending, MigrationFailed)


class AliasTypeConflict(StoreError):
    """
    The same file content is already stored (same extractor version) as another
    document type, because the file name suggested a type the content did not.
    The store keeps one row per content and version, so the second name cannot
    be stored with its own type; it is refused instead of reusing the first
    name's type.
    """

    def __init__(self, existing_id, existing_filename, existing_type, new_filename, new_type):
        super().__init__(
            f"This file content is already stored as {existing_type} (extraction {existing_id}, file {existing_filename}); "
            f"the name {new_filename} suggests {new_type}. Identical content under two names is not stored twice with two types."
        )
        self.existing_id, self.existing_type, self.new_type = existing_id, existing_type, new_type


class StaleCorrection(StoreError):
    """
    The field was corrected after the version the request was based on.
    """


def now_text():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def correction_hash(prev_hash, extraction_id, field_name, previous, corrected, reason, actor, authenticated, at, based_on):
    """The chain hash of one correction; previous and corrected are the stored JSON text."""

    body = json.dumps([prev_hash, extraction_id, field_name, previous, corrected, reason, actor, int(authenticated), at, based_on],
                      ensure_ascii=False, separators=(",", ":"))

    return sha256_text(body)


def decision_hash(prev_hash, correction_id, decision, note, actor, authenticated, at):
    """The chain hash of one decision."""

    body = json.dumps([prev_hash, "decision", correction_id, decision, note, actor, int(authenticated), at],
                      ensure_ascii=False, separators=(",", ":"))

    return sha256_text(body)


def persistable(extraction):
    """The extraction as JSON-ready data without the raw text snippets."""

    data = asdict(extraction)

    for item in data["fields"].values():
        item["raw_text"] = None
        item["alternatives"] = [{**alt, "raw_text": None} for alt in item["alternatives"]]

    for item in data["line_items"]:
        item["raw_text"] = None

    return data


def statements(script):
    return [s.strip() for s in script.split(";\n") if s.strip()]


class ExtractionStore:

    # A store opened by open_for_reading cannot write: its connections are read-only at the SQLite level
    readonly = False

    def __init__(self, path):
        """
        Opens a store for writing. A file that does not exist (or is empty) is created at the current schema. A
        current file is opened as it is. A file at an older schema is never upgraded here: it raises
        MigrationPending, and only the explicit migration (extraction_migration.migrate) changes a schema.
        """

        self.path = Path(path)
        found = self.inspect(self.path)

        if found.state == ABSENT:
            self.initialize()
        elif found.state != CURRENT:
            raise self.refusal(found)

    @staticmethod
    def refusal(found):
        """The error that says why a store in this state cannot be written."""

        if found.state == UNAVAILABLE:
            return StoreUnavailable(found.reason)

        if found.state == MIGRATION_FAILED:
            return MigrationFailed(found.reason)

        return MigrationPending(found.reason)

    def connect(self):
        if self.readonly:
            connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        else:
            connection = sqlite3.connect(self.path, timeout=10)

        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    @contextmanager
    def write(self):
        if self.readonly:
            raise StoreError("This extraction store was opened for reading only.")

        connection = self.connect()

        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def current_version(connection):
        try:
            row = connection.execute("SELECT version FROM schema_info").fetchone()
        except sqlite3.OperationalError:
            return 0

        return row["version"] if row else 0

    @staticmethod
    def apply_migrations(connection, expected_from=None):
        """
        Applies every missing migration step to an open connection, inside the caller's transaction (BEGIN
        IMMEDIATE already issued). The version is read under that lock: when another process already brought the
        file to the current schema nothing is applied and False is returned. expected_from, when given, is the
        version the file must still have under the lock; any other older version raises MigrationPending (the file
        changed after it was inspected). The version row changes in the same transaction as the schema, so a
        failure leaves the original schema. Returns True when steps were applied.
        """

        connection.execute("CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL)")
        current = ExtractionStore.current_version(connection)

        if current > SCHEMA_VERSION:
            raise StoreUnavailable("The extraction store is from a newer version.")

        if current == SCHEMA_VERSION:
            return False

        if expected_from is not None and current != expected_from:
            raise MigrationPending("The extraction store changed after it was inspected; inspect it again.")

        for target in range(current + 1, SCHEMA_VERSION + 1):
            for statement in statements(MIGRATIONS[target]):
                connection.execute(statement)

        connection.execute("DELETE FROM schema_info")
        connection.execute("INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,))

        return True

    def initialize(self):
        """
        Creates a new database at the current schema (only for a file that does not exist yet). One transaction;
        a second process creating it at the same time finds it done under the lock and applies nothing.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self.apply_migrations(connection, expected_from=0)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    # ---- extractions ----------------------------------------------------------------------

    def version(self):
        with closing(self.connect()) as connection:
            return self.current_version(connection)

    def save(self, extraction):
        """
        Stores an extraction. Returns (id, created): the same file content read
        by the same extractor version is stored once and the existing row is
        returned. An unreadable file with no hash is not stored. The same content
        read as a different document type (another file name) is refused with
        AliasTypeConflict, never reused under the first type; the refusal is
        recorded first (see save_with_alias).
        """

        result = self.save_with_alias(extraction)

        return result["id"], result["created"]

    def save_with_alias(self, extraction, actor=None):
        """
        Stores an extraction and records the file name it was read under (schema 5).
        Returns {"id", "created", "alias": {"filename", "relation", "created"}}.

        The row is keyed by (content hash, extractor version) as before; names are kept in
        extraction_aliases. relation: "original" (the name the row was made from), "same_type"
        (another name, same document type: the row is reused) or "type_conflict" (another name
        that classifies as another type: recorded, the stored row is not touched, and
        AliasTypeConflict is raised AFTER the record is committed). A name already recorded for
        this content and version is not recorded again.
        """

        if not extraction.sha256:
            raise StoreError("An extraction without a file hash cannot be stored.")

        key = identifier_key(extraction.shipment_id) if extraction.shipment_id else None
        payload = json.dumps(persistable(extraction))
        name = extraction.filename[:300]
        conflict = None

        with self.write() as connection:
            row = connection.execute(
                "SELECT id, filename, document_type FROM extractions WHERE sha256 = ? AND extractor_version = ?",
                (extraction.sha256, extraction.extractor_version),
            ).fetchone()

            if row is None:
                cursor = connection.execute(
                    "INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, shipment_id, "
                    "shipment_key, payload, payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, extraction.sha256, extraction.extractor_version, extraction.document_type,
                     extraction.status, extraction.shipment_id, key, payload, sha256_text(payload), now_text()),
                )
                result = {"id": cursor.lastrowid, "created": True,
                          "alias": {"filename": name, "relation": "original", "created": True}}
                self.record_alias(connection, cursor.lastrowid, extraction, name, "original", actor)

                return result

            known = connection.execute(
                "SELECT relation FROM extraction_aliases WHERE sha256 = ? AND extractor_version = ? AND filename = ?",
                (extraction.sha256, extraction.extractor_version, name),
            ).fetchone()

            if known is not None:
                relation, created = known["relation"], False
            else:
                relation = "same_type" if row["document_type"] == extraction.document_type else "type_conflict"
                created = self.record_alias(connection, row["id"], extraction, name, relation, actor)

            if relation == "type_conflict":
                conflict = AliasTypeConflict(row["id"], row["filename"], row["document_type"], name, extraction.document_type)

            result = {"id": row["id"], "created": False, "alias": {"filename": name, "relation": relation, "created": created}}

        if conflict is not None:                        # the record above is committed; now the refusal
            raise conflict

        return result

    @staticmethod
    def record_alias(connection, extraction_id, extraction, name, relation, actor):
        """Inserts one alias row; False when the (content, version, name) is already there (a concurrent twin)."""

        cursor = connection.execute(
            "INSERT OR IGNORE INTO extraction_aliases (extraction_id, sha256, extractor_version, filename, inferred_type, relation, "
            "first_seen_at, seen_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (extraction_id, extraction.sha256, extraction.extractor_version, name, extraction.document_type, relation, now_text(), actor),
        )

        return cursor.rowcount == 1

    def has_aliases(self):
        """True when this store's schema has the alias table (schema 5); False for a valid older file awaiting its upgrade."""

        with closing(self.connect()) as connection:
            return connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'extraction_aliases'").fetchone() is not None

    def aliases(self, extraction_id):
        """
        The names recorded for an extraction's content and version, oldest first, or None when the schema has no
        alias table yet. Aliases are information only: they never feed the audit.
        """

        if not self.has_aliases():
            return None

        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT id, filename, inferred_type, relation, first_seen_at, seen_by FROM extraction_aliases "
                "WHERE extraction_id = ? ORDER BY id", (extraction_id,)).fetchall()

        return [dict(r) for r in rows]

    def row(self, extraction_id):
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM extractions WHERE id = ?", (extraction_id,)).fetchone()

        return self.shaped(row) if row else None

    @staticmethod
    def shaped(row):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])

        return item

    def list(self, shipment_key=None):
        """Stored extractions (newest first), optionally those of one shipment; payloads omitted."""

        query = ("SELECT id, filename, sha256, extractor_version, document_type, status, shipment_id, shipment_key, created_at "
                 "FROM extractions")
        args = ()

        if shipment_key:
            query += " WHERE shipment_key = ?"
            args = (shipment_key,)

        with closing(self.connect()) as connection:
            return [dict(r) for r in connection.execute(query + " ORDER BY id DESC", args)]

    # ---- corrections: proposals, decisions, effective values ----------------------------------

    CORRECTION_SELECT = (
        "SELECT c.*, d.id AS decision_id, d.decision, d.note AS decision_note, d.actor AS decided_by, "
        "d.at AS decided_at, e.filename, e.sha256, e.document_type, e.shipment_key, e.extractor_version "
        "FROM field_corrections c JOIN extractions e ON e.id = c.extraction_id "
        "LEFT JOIN correction_decisions d ON d.correction_id = c.id "
    )

    @staticmethod
    def shaped_correction(row):
        item = dict(row)
        item["previous"] = json.loads(item["previous"])
        item["corrected"] = json.loads(item["corrected"])
        item["authenticated"] = bool(item["authenticated"])
        item["legacy"] = bool(item["legacy"])
        # The state is derived: a correction from before the workflow is never approved
        item["status"] = LEGACY if item["legacy"] else (item["decision"] or PENDING)

        for key in ("prev_hash", "hash"):
            item.pop(key, None)

        return item

    def corrections(self, extraction_id):
        """The corrections of an extraction, oldest first, each with its derived status."""

        with closing(self.connect()) as connection:
            rows = connection.execute(self.CORRECTION_SELECT + "WHERE c.extraction_id = ? ORDER BY c.id", (extraction_id,)).fetchall()

        return [self.shaped_correction(r) for r in rows]

    def correction(self, correction_id):
        with closing(self.connect()) as connection:
            row = connection.execute(self.CORRECTION_SELECT + "WHERE c.id = ?", (correction_id,)).fetchone()

        return self.shaped_correction(row) if row else None

    def pending(self):
        """Proposals waiting for a decision, oldest first."""

        with closing(self.connect()) as connection:
            rows = connection.execute(
                self.CORRECTION_SELECT + "WHERE c.legacy = 0 AND d.id IS NULL ORDER BY c.id"
            ).fetchall()

        return [self.shaped_correction(r) for r in rows]

    def effective(self, extraction_id):
        """
        {field: {...}} for every field of the extraction, keeping the values apart:

            extracted      the original value, state and page (never changed)
            proposed       the pending proposal, or None
            approved       the latest approved correction, or None
            value, state, source, page, correction_id
                           what an audit may consume: the approved correction
                           (source "approved_correction", page unknown) or else
                           the extracted value (source "extracted")

        A pending, rejected or legacy correction has no effect on value.
        latest_correction_id is what a further proposal must name as based_on.
        """

        record = self.row(extraction_id)

        if record is None:
            raise StoreError(f"Unknown extraction {extraction_id}.")

        result = {}

        for name, item in record["payload"]["fields"].items():
            original = {"value": item["value"], "state": item["state"], "page": item["page"]}
            result[name] = {"extracted": original, "proposed": None, "approved": None, "latest_correction_id": None,
                            "value": item["value"], "state": item["state"], "source": "extracted", "page": item["page"],
                            "correction_id": None}

        for correction in self.corrections(extraction_id):
            entry = result.get(correction["field_name"])

            if entry is None:
                continue

            entry["latest_correction_id"] = correction["id"]

            if correction["status"] == PENDING:
                entry["proposed"] = correction
            elif correction["status"] == APPROVED:
                entry["approved"] = correction

        # The audit reads an approval by the file's content, not by the extraction it was made on, so
        # an extraction made later (a new extractor version) shows the same approved value: the latest
        # approval of this file and field, from whichever extraction, as audit_inputs applies it
        for name, entry in result.items():
            elsewhere = self.approved_for_content(record["sha256"], record["document_type"], name)

            if elsewhere is not None and (entry["approved"] is None or elsewhere["decision_id"] > entry["approved"]["decision_id"]):
                entry["approved"] = elsewhere

            if entry["approved"] is not None:
                entry.update(value=entry["approved"]["corrected"], state=de.EXTRACTED, source="approved_correction", page=None,
                             correction_id=entry["approved"]["id"])

        return result

    def approved_for_content(self, sha256, document_type, field_name):
        """The latest approved correction of a field of any extraction of this file content, or None."""

        with closing(self.connect()) as connection:
            row = connection.execute(
                self.CORRECTION_SELECT + "WHERE e.sha256 = ? AND e.document_type = ? AND c.field_name = ? AND c.legacy = 0 "
                "AND d.decision = 'approved' ORDER BY d.id DESC LIMIT 1",
                (sha256, document_type, field_name),
            ).fetchone()

        return self.shaped_correction(row) if row else None

    def propose(self, principal, extraction_id, field_name, text, reason, *, based_on):
        """
        Records a reviewer's proposed correction of one field. It is a
        proposal: it has no effect until a different, authorized person
        approves it (decide). Needs EXTRACTION_CORRECT. based_on is the id of the
        latest correction of the field the reviewer saw (None when they saw
        none); if the field has been corrected since, StaleCorrection is raised
        and nothing is recorded. While a proposal for the field awaits its
        decision, no other can be made. The text is parsed by the extractor's
        own parsers (an invalid value is refused); the value being replaced
        (the approved correction, else the extracted value) is kept in the
        record; the extracted value is not changed. Returns the proposal.
        """

        authz.require(principal, authz.EXTRACTION_CORRECT)

        reason = " ".join((reason or "").split())

        if len(reason) < MIN_REASON:
            raise StoreError(f"Write why the value is being corrected (at least {MIN_REASON} characters).")

        if len(reason) > MAX_REASON:
            raise StoreError(f"The reason is longer than {MAX_REASON} characters.")

        if based_on is not None and (not isinstance(based_on, int) or isinstance(based_on, bool)):
            raise StoreError("based_on must be a correction id or None.")

        with self.write() as connection:
            row = connection.execute("SELECT * FROM extractions WHERE id = ?", (extraction_id,)).fetchone()

            if row is None:
                raise StoreError(f"Unknown extraction {extraction_id}.")

            try:
                corrected = de.coerce_correction(row["document_type"], field_name, text)
            except ValueError as error:
                raise StoreError(str(error)) from error

            fields = json.loads(row["payload"])["fields"]
            latest = connection.execute(
                "SELECT c.id, d.id AS decision_id FROM field_corrections c LEFT JOIN correction_decisions d ON d.correction_id = c.id "
                "WHERE c.extraction_id = ? AND c.field_name = ? ORDER BY c.id DESC LIMIT 1",
                (extraction_id, field_name),
            ).fetchone()

            latest_id = latest["id"] if latest else None

            if latest_id != based_on:
                raise StaleCorrection(
                    f"{field_name} was corrected (correction {latest_id}) after the version this change is based on "
                    f"({'none' if based_on is None else based_on}). Reload it and decide again."
                )

            if latest and latest["decision_id"] is None and not self.is_legacy(connection, latest_id):
                raise StoreError(f"A correction of {field_name} (correction {latest_id}) is awaiting a decision.")

            # The value being replaced is the one an audit reads: the latest approval of this file's
            # content and field, whichever extraction it was made on
            approved = connection.execute(
                "SELECT c.corrected FROM field_corrections c JOIN correction_decisions d ON d.correction_id = c.id "
                "JOIN extractions e ON e.id = c.extraction_id WHERE e.sha256 = ? AND e.document_type = ? AND c.field_name = ? "
                "AND c.legacy = 0 AND d.decision = 'approved' ORDER BY d.id DESC LIMIT 1",
                (row["sha256"], row["document_type"], field_name),
            ).fetchone()

            if approved:
                previous = {"value": json.loads(approved["corrected"]), "state": de.EXTRACTED, "source": "approved_correction"}
            else:
                item = fields.get(field_name)
                previous = {"value": item["value"] if item else None, "state": item["state"] if item else de.MISSING,
                            "source": "extracted"}

            if previous["value"] == corrected and previous["state"] == de.EXTRACTED:
                raise StoreError("The corrected value is the same as the current value.")

            at = now_text()
            previous_text, corrected_text = json.dumps(previous), json.dumps(corrected)
            tip = connection.execute("SELECT hash FROM field_corrections WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = tip["hash"] if tip else ""
            digest = correction_hash(prev_hash, extraction_id, field_name, previous_text, corrected_text, reason,
                                     principal.username, principal.authenticated, at, based_on)

            cursor = connection.execute(
                "INSERT INTO field_corrections (extraction_id, field_name, previous, corrected, reason, actor, "
                "authenticated, at, based_on, prev_hash, hash, legacy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (extraction_id, field_name, previous_text, corrected_text, reason, principal.username,
                 int(principal.authenticated), at, based_on, prev_hash, digest),
            )

            return {"id": cursor.lastrowid, "extraction_id": extraction_id, "field_name": field_name, "previous": previous,
                    "corrected": corrected, "reason": reason, "actor": principal.username,
                    "authenticated": principal.authenticated, "at": at, "based_on": based_on, "status": PENDING,
                    "legacy": False}

    @staticmethod
    def is_legacy(connection, correction_id):
        row = connection.execute("SELECT legacy FROM field_corrections WHERE id = ?", (correction_id,)).fetchone()

        return bool(row and row["legacy"])

    def decide(self, principal, correction_id, decision, note=None):
        """
        Approves or rejects a pending proposal (decision "approve" or
        "reject"). Needs EXTRACTION_APPROVE and a signed-in account; the
        approver must not be the proposer; only a pending, non-legacy
        proposal can be decided, once (InvalidTransition otherwise). A
        rejection needs a reason. The decision is appended to the history; the
        extracted value and the proposal are not changed. Returns the decision
        with the document, field and shipment it concerns.
        """

        authz.require(principal, authz.EXTRACTION_APPROVE)

        if not principal.authenticated:
            raise authz.PermissionDenied("A correction can only be decided by a signed-in account.")

        if decision not in ("approve", "reject"):
            raise StoreError("The decision must be approve or reject.")

        state = APPROVED if decision == "approve" else REJECTED
        note = " ".join((note or "").split())

        if state == REJECTED and len(note) < MIN_REASON:
            raise StoreError(f"Write why the correction is rejected (at least {MIN_REASON} characters).")

        if len(note) > MAX_REASON:
            raise StoreError(f"The note is longer than {MAX_REASON} characters.")

        with self.write() as connection:
            row = connection.execute(self.CORRECTION_SELECT + "WHERE c.id = ?", (correction_id,)).fetchone()

            if row is None:
                raise StoreError(f"Unknown correction {correction_id}.")

            if row["legacy"]:
                raise InvalidTransition("This correction was recorded before approvals existed and cannot be approved: propose it again.")

            if row["decision_id"] is not None:
                raise InvalidTransition(f"Correction {correction_id} was already {row['decision']}.")

            if row["actor"] == principal.username:
                raise authz.PermissionDenied("You cannot decide your own correction: another administrator must.")

            at = now_text()
            tip = connection.execute("SELECT hash FROM correction_decisions ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = tip["hash"] if tip else ""
            digest = decision_hash(prev_hash, correction_id, state, note or None, principal.username, principal.authenticated, at)

            try:
                cursor = connection.execute(
                    "INSERT INTO correction_decisions (correction_id, decision, note, actor, authenticated, at, prev_hash, hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (correction_id, state, note or None, principal.username, int(principal.authenticated), at, prev_hash, digest),
                )
            except sqlite3.IntegrityError as error:
                raise InvalidTransition(f"Correction {correction_id} was already decided.") from error

            return {"decision_id": cursor.lastrowid, "correction_id": correction_id, "decision": state, "note": note or None,
                    "actor": principal.username, "at": at, "proposed_by": row["actor"], "extraction_id": row["extraction_id"],
                    "field_name": row["field_name"], "document_type": row["document_type"], "filename": row["filename"],
                    "sha256": row["sha256"], "shipment_key": row["shipment_key"], "corrected": json.loads(row["corrected"])}

    def approved_corrections(self, shipment_key):
        """
        The approved corrections of the documents of a shipment, oldest decision
        first: [{correction_id, decision_id, extraction_id, extractor_version,
        sha256, filename, document_type, field_name, value, proposed_by,
        approved_by, decided_at}]. Only these may reach an audit.
        """

        with closing(self.connect()) as connection:
            rows = connection.execute(
                self.CORRECTION_SELECT + "WHERE e.shipment_key = ? AND d.decision = 'approved' AND c.legacy = 0 ORDER BY d.id",
                (shipment_key,),
            ).fetchall()

        return [{"correction_id": r["id"], "decision_id": r["decision_id"], "extraction_id": r["extraction_id"],
                 "extractor_version": r["extractor_version"], "sha256": r["sha256"], "filename": r["filename"],
                 "document_type": r["document_type"], "field_name": r["field_name"], "value": json.loads(r["corrected"]),
                 "proposed_by": r["actor"], "approved_by": r["decided_by"], "decided_at": r["decided_at"]} for r in rows]

    def audit_token(self):
        """
        Changes whenever something the audit reads from this store changes: an
        approval is added, or an extraction is stored (the audit shows stored
        extractions beside what it read). (approvals, newest decision id,
        newest extraction id)
        """

        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS newest FROM correction_decisions WHERE decision = 'approved'"
            ).fetchone()
            newest = connection.execute("SELECT COALESCE(MAX(id), 0) AS newest FROM extractions").fetchone()

        return (row["n"], row["newest"], newest["newest"])

    # ---- reading for the audit ---------------------------------------------------------------

    @classmethod
    def inspect(cls, path):
        """
        StoreInspection(state, version, reason): what the file is, without creating, migrating or
        writing anything (a read-only connection). The states are ABSENT, OLDER, MIGRATION_PENDING,
        CURRENT, UNAVAILABLE and MIGRATION_FAILED; reasons never contain a path or document text.
        A damaged, too-new or structurally invalid file is UNAVAILABLE: it is never reported as absent.
        """

        path = Path(path)

        if not path.is_file() or path.stat().st_size == 0:
            return StoreInspection(ABSENT, None, "There is no extraction store.")

        try:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)

            try:
                row = connection.execute("SELECT version FROM schema_info").fetchone()
                tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                triggers = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return StoreInspection(UNAVAILABLE, None, "The extraction store cannot be read.")

        version = row[0] if row else 0

        if version > SCHEMA_VERSION:
            return StoreInspection(UNAVAILABLE, version, "The extraction store is from a newer version.")

        if version >= MIN_READABLE_VERSION:
            needed_tables = REQUIRED_TABLES_V5 if version >= 5 else REQUIRED_TABLES
            needed_triggers = REQUIRED_TRIGGERS_V5 if version >= 5 else ()

            if not set(needed_tables) <= tables or not set(needed_triggers) <= triggers:
                return StoreInspection(UNAVAILABLE, version, "The extraction store is structurally invalid (a required table or trigger is missing).")

        # The schema above decides the state. The status record (migration_status) can only add a notice to a store that
        # is really at an older schema, or ask for the checks to run again on a current one.
        info = status.reconcile(path, version, SCHEMA_VERSION)

        if version == SCHEMA_VERSION:
            if info["attempt"] == "unverified":
                if not cls.passes_verification(path):
                    return StoreInspection(UNAVAILABLE, version, "The extraction store failed verification after its migration.", info)

                info = {**info, "attempt": "none", "record": "ignored"}

            return StoreInspection(CURRENT, version, "The extraction store is current.", info)

        if info["attempt"] == "failed":
            return StoreInspection(MIGRATION_FAILED, version, "A migration attempt failed and was rolled back; the store is unchanged.", info)

        if version >= MIN_READABLE_VERSION:
            return StoreInspection(MIGRATION_PENDING, version, "The extraction store needs a schema upgrade; approvals are still read.", info)

        return StoreInspection(OLDER, version, "The extraction store is older than the first version that holds approvals.", info)

    @staticmethod
    def passes_verification(path):
        """The integrity check and the history verification, read-only. False when either fails or the file cannot be read."""

        try:
            connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=10)

            try:
                return [r[0] for r in connection.execute("PRAGMA integrity_check")] == ["ok"] and verify_connection(connection)["ok"]
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return False

    @staticmethod
    def readable(inspection):
        """True when approvals and extractions can be read from a file in this state."""

        return inspection.state in (CURRENT, MIGRATION_PENDING) or (
            inspection.state == MIGRATION_FAILED and inspection.version is not None and inspection.version >= MIN_READABLE_VERSION)

    @classmethod
    def open_for_reading(cls, path):
        """
        A read-only store, or None when there is nothing to read: no file (ABSENT) or a file older than
        MIN_READABLE_VERSION (no approval can exist; its legacy corrections never apply). A CURRENT file and a
        valid older file that still holds approvals (MIGRATION_PENDING, or a failed upgrade rolled back) are
        opened as they are: nothing is created, migrated or written. An unreadable, too-new or structurally
        invalid file raises StoreUnavailable, never None: that would look like "no corrections".
        """

        inspection = cls.inspect(path)

        if inspection.state == UNAVAILABLE:
            raise StoreUnavailable(inspection.reason)

        if not cls.readable(inspection):
            return None

        store = cls.__new__(cls)
        store.path = Path(path)
        store.readonly = True

        return store

    @classmethod
    def open_if_current(cls, path):
        """Kept for existing callers: the same as open_for_reading (it no longer hides a pending upgrade)."""

        return cls.open_for_reading(path)

    @classmethod
    def open_for_write(cls, path, create=False):
        """
        A store that can be written, or None when the file is absent and create is false. It never upgrades a
        file: one at an older schema raises MigrationPending (or MigrationFailed after an attempt that was rolled
        back) and only the explicit migration command changes a schema. An unreadable, too-new or invalid file
        raises StoreUnavailable. A missing file is created at the current schema when create is true.
        """

        found = cls.inspect(path)

        if found.state == ABSENT:
            return cls(path) if create else None

        if found.state != CURRENT:
            raise cls.refusal(found)

        store = cls.__new__(cls)
        store.path = Path(path)

        return store

    def current_extractions(self, shipment_key, hashes):
        """
        {file name: newest stored extraction (with payload)} of the documents of
        a shipment as they are now: only rows whose file hash is the hash of the
        file of that name today (hashes: {name: sha256}).
        """

        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT * FROM extractions WHERE shipment_key = ? ORDER BY id DESC", (shipment_key,)).fetchall()

        found = {}

        for row in rows:
            if hashes.get(row["filename"]) == row["sha256"] and row["filename"] not in found:
                found[row["filename"]] = self.shaped(row)

        return found

    # ---- integrity -------------------------------------------------------------------------

    def verify_history(self):
        """
        Recomputes the correction hash chain, the decision hash chain and every
        extraction payload hash, and checks that no approval was made by the
        person who proposed the correction (and, from schema 5, that every alias
        agrees with its extraction). Returns {ok, corrections_checked,
        decisions_checked, extractions_checked, aliases_checked, unchained_legacy,
        problems}. Rows written before schema v3 have no hash; they are counted as
        unchained_legacy, not as problems, and are not protected by the chain.
        Detects ordinary-SQL changes; it cannot detect a rewrite that recomputes
        every hash, or deletion of the newest rows (see the module notes).
        """

        with closing(self.connect()) as connection:
            return verify_connection(connection)

    def alias_counts(self, extraction_id):
        """(alias count, has a type conflict), or (None, None) when the schema has no alias table yet."""

        found = self.aliases(extraction_id)

        if found is None:
            return None, None

        return len(found), any(a["relation"] == "type_conflict" for a in found)


def table_names(connection):
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def column_names(connection, table):
    return [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]


def verify_connection(connection):
    """
    The checks of ExtractionStore.verify_history, on any connection and any schema version (tables and columns
    that do not exist yet in an older schema are skipped), so the migration can verify before and after.
    """

    connection.row_factory = sqlite3.Row
    tables = table_names(connection)
    problems, legacy = [], 0
    rows, decisions, extractions, aliases = [], [], [], []

    def get(row, name):
        return row[name] if name in row.keys() else None

    if "field_corrections" in tables:
        rows = connection.execute("SELECT * FROM field_corrections ORDER BY id").fetchall()

    if "correction_decisions" in tables:
        decisions = connection.execute("SELECT * FROM correction_decisions ORDER BY id").fetchall()

    if "extractions" in tables:
        has_payload_hash = "payload_sha256" in column_names(connection, "extractions")
        extractions = connection.execute(
            f"SELECT id, sha256, extractor_version, document_type, payload, {'payload_sha256' if has_payload_hash else 'NULL AS payload_sha256'} "
            "FROM extractions ORDER BY id").fetchall()

    if "extraction_aliases" in tables:
        aliases = connection.execute("SELECT * FROM extraction_aliases ORDER BY id").fetchall()

    ids = {r["id"] for r in extractions}
    expected_prev, chained = "", False
    by_id = {r["id"]: r for r in rows}

    for r in rows:
        if get(r, "hash") is None:
            if chained:
                problems.append(f"correction {r['id']}: no hash after the chain started")
            else:
                legacy += 1
            continue

        chained = True

        if r["prev_hash"] != expected_prev:
            problems.append(f"correction {r['id']}: the chain is broken before it (a row was changed or removed)")

        digest = correction_hash(r["prev_hash"], r["extraction_id"], r["field_name"], r["previous"], r["corrected"], r["reason"],
                                 r["actor"], r["authenticated"], r["at"], r["based_on"])

        if digest != r["hash"]:
            problems.append(f"correction {r['id']}: its content does not match its hash")

        if r["extraction_id"] not in ids:
            problems.append(f"correction {r['id']}: its extraction {r['extraction_id']} is missing")

        expected_prev = r["hash"]

    expected_prev = ""

    for d in decisions:
        if d["prev_hash"] != expected_prev:
            problems.append(f"decision {d['id']}: the chain is broken before it (a row was changed or removed)")

        digest = decision_hash(d["prev_hash"], d["correction_id"], d["decision"], d["note"], d["actor"], d["authenticated"], d["at"])

        if digest != d["hash"]:
            problems.append(f"decision {d['id']}: its content does not match its hash")

        target = by_id.get(d["correction_id"])

        if target is None:
            problems.append(f"decision {d['id']}: its correction {d['correction_id']} is missing")
        elif get(target, "legacy"):
            problems.append(f"decision {d['id']}: it decides a correction from before approvals existed")
        elif target["actor"] == d["actor"]:
            problems.append(f"decision {d['id']}: correction {d['correction_id']} was decided by its own proposer")

        expected_prev = d["hash"]

    for r in extractions:
        if r["payload_sha256"] is None:
            legacy += 1
        elif sha256_text(r["payload"]) != r["payload_sha256"]:
            problems.append(f"extraction {r['id']}: its stored result does not match its hash")

    if "extraction_aliases" in tables:
        by_extraction = {r["id"]: r for r in extractions}
        originals = {}

        for a in aliases:
            owner = by_extraction.get(a["extraction_id"])

            if owner is None:
                problems.append(f"alias {a['id']}: its extraction {a['extraction_id']} is missing")
                continue

            if a["sha256"] != owner["sha256"] or a["extractor_version"] != owner["extractor_version"]:
                problems.append(f"alias {a['id']}: it does not match the content or extractor version of its extraction")

            same = a["inferred_type"] == owner["document_type"]

            if (a["relation"] == "type_conflict") == same:
                problems.append(f"alias {a['id']}: its relation {a['relation']} does not match its document type")

            if a["relation"] == "original":
                originals[a["extraction_id"]] = originals.get(a["extraction_id"], 0) + 1

        # Every extraction has exactly one original alias once the schema is 5; a half-applied file still at 4 is being completed
        complete = ExtractionStore.current_version(connection) >= 5

        for r in extractions if complete else []:
            if originals.get(r["id"], 0) != 1:
                problems.append(f"extraction {r['id']}: it has {originals.get(r['id'], 0)} original aliases instead of one")

    return {"ok": not problems, "corrections_checked": len(rows), "decisions_checked": len(decisions),
            "extractions_checked": len(extractions), "aliases_checked": len(aliases), "unchained_legacy": legacy, "problems": problems}
