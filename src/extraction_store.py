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

Every change is one transaction (BEGIN IMMEDIATE). Schema changes go through
MIGRATIONS, one transaction per step; the version is read again inside each
step, so two processes starting at once do not apply a step twice. A database
from a newer version is refused, never downgraded.
"""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import authz
import document_extraction as de
from shipment_identity import identifier_key


SCHEMA_VERSION = 4

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

    def __init__(self, path):
        self.path = Path(path)
        self.initialize()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    @contextmanager
    def write(self):
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
        row = connection.execute("SELECT version FROM schema_info").fetchone()

        return row["version"] if row else 0

    def initialize(self):
        """Brings the schema to SCHEMA_VERSION one step at a time; each step is one transaction."""

        self.path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL)")
            connection.commit()

            for target in range(1, SCHEMA_VERSION + 1):
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    current = self.current_version(connection)

                    if current > SCHEMA_VERSION:
                        raise StoreError(f"Extraction store {self.path} has schema version {current}, newer than {SCHEMA_VERSION}.")

                    # Read under the write lock: a step another process applied is not applied again
                    if current >= target:
                        connection.rollback()
                        continue

                    for statement in statements(MIGRATIONS[target]):
                        connection.execute(statement)

                    connection.execute("DELETE FROM schema_info")
                    connection.execute("INSERT INTO schema_info (version) VALUES (?)", (target,))
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

    def version(self):
        with closing(self.connect()) as connection:
            return self.current_version(connection)

    # ---- extractions ----------------------------------------------------------------------

    def save(self, extraction):
        """
        Stores an extraction. Returns (id, created): the same file content read
        by the same extractor version is stored once and the existing row is
        returned. An unreadable file with no hash is not stored.
        """

        if not extraction.sha256:
            raise StoreError("An extraction without a file hash cannot be stored.")

        key = identifier_key(extraction.shipment_id) if extraction.shipment_id else None
        payload = json.dumps(persistable(extraction))

        with self.write() as connection:
            row = connection.execute(
                "SELECT id FROM extractions WHERE sha256 = ? AND extractor_version = ?",
                (extraction.sha256, extraction.extractor_version),
            ).fetchone()

            if row:
                return row["id"], False

            cursor = connection.execute(
                "INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, shipment_id, "
                "shipment_key, payload, payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (extraction.filename[:300], extraction.sha256, extraction.extractor_version, extraction.document_type,
                 extraction.status, extraction.shipment_id, key, payload, sha256_text(payload), now_text()),
            )

            return cursor.lastrowid, True

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
                entry.update(value=correction["corrected"], state=de.EXTRACTED, source="approved_correction", page=None,
                             correction_id=correction["id"])

        return result

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

            approved = connection.execute(
                "SELECT c.corrected FROM field_corrections c JOIN correction_decisions d ON d.correction_id = c.id "
                "WHERE c.extraction_id = ? AND c.field_name = ? AND d.decision = 'approved' ORDER BY d.id DESC LIMIT 1",
                (extraction_id, field_name),
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
    def open_if_current(cls, path):
        """
        The store at path when the file exists and is already at the current
        schema version; otherwise None. Never creates or migrates anything, so
        the audit can read it on any request. A file that cannot be read, or is
        from a newer version, raises StoreUnavailable (a missing file, or an
        older version that cannot hold approvals yet, gives None).
        """

        path = Path(path)

        if not path.is_file():
            return None

        try:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)

            try:
                row = connection.execute("SELECT version FROM schema_info").fetchone()
            finally:
                connection.close()
        except sqlite3.DatabaseError as error:
            raise StoreUnavailable("The extraction store cannot be read.") from error

        if row and row[0] > SCHEMA_VERSION:
            raise StoreUnavailable("The extraction store is from a newer version.")

        if not row or row[0] != SCHEMA_VERSION:
            return None

        # Already at the current version: no initialize(), so no write lock is taken
        store = cls.__new__(cls)
        store.path = path

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
        person who proposed the correction. Returns {ok, corrections_checked,
        decisions_checked, extractions_checked, unchained_legacy, problems}.
        Rows written before schema v3 have no hash; they are counted as
        unchained_legacy, not as problems, and are not protected by the chain.
        Detects ordinary-SQL changes; it cannot detect a rewrite that recomputes
        every hash, or deletion of the newest rows (see the module notes).
        """

        problems, legacy = [], 0

        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT * FROM field_corrections ORDER BY id").fetchall()
            decisions = connection.execute("SELECT * FROM correction_decisions ORDER BY id").fetchall()
            extractions = connection.execute("SELECT id, payload, payload_sha256 FROM extractions ORDER BY id").fetchall()
            ids = {r["id"] for r in extractions}

        expected_prev, chained = "", False
        by_id = {r["id"]: r for r in rows}

        for r in rows:
            if r["hash"] is None:
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
            elif target["legacy"]:
                problems.append(f"decision {d['id']}: it decides a correction from before approvals existed")
            elif target["actor"] == d["actor"]:
                problems.append(f"decision {d['id']}: correction {d['correction_id']} was decided by its own proposer")

            expected_prev = d["hash"]

        for r in extractions:
            if r["payload_sha256"] is None:
                legacy += 1
            elif sha256_text(r["payload"]) != r["payload_sha256"]:
                problems.append(f"extraction {r['id']}: its stored result does not match its hash")

        return {"ok": not problems, "corrections_checked": len(rows), "decisions_checked": len(decisions),
                "extractions_checked": len(extractions), "unchained_legacy": legacy, "problems": problems}
