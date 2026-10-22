"""
LogiDoc-RAG exception review store

A small SQLite database that keeps the review state of freight audit
exceptions, separate from the audit itself. It never stores document
text or evidence: only the exception key, its shipment and category, a
signature of the audited values, the review status, the reviewer, a
note and timestamps.

Review states:

    open        detected and not yet handled (every new exception)
    in_review   someone is looking at it
    resolved    handled; a resolution note is optional
    dismissed   not a real problem; a reason is required

The audit decides whether an exception exists; the store only records
what people did about it. Each time the exceptions are shown, sync()
compares them with the stored states:

    - a new exception is recorded as open
    - an exception that is no longer detected keeps its state and is
      marked absent (it is not resolved by disappearing)
    - a resolved or dismissed exception whose audited values changed
      (another signature), or that comes back after being absent, is
      reopened and flagged needs_recheck, so a stale resolution is
      never kept silently

Every change is one transaction and is logged in review_events.

Workflow (schema version 2)
---------------------------

Version 2 adds the exception workflow on top of the same states:

    assign       an exception can be assigned to a reviewer
    start        open -> in_review (the exception must have an assignee)
    note         a note on any state, without changing it
    resolve      in_review -> resolved; needs a note of at least
                 Policy.min_note characters AND at least one evidence
                 reference (a document, page or ticket the resolution
                 rests on)
    dismiss      open or in_review -> dismissed; needs a note of at
                 least Policy.min_note characters
    reopen       resolved or dismissed -> open
    approve /    when Policy.require_approval is on, a resolution or a
    reject       dismissal waits for approval (approval_state
                 'pending'). Approving it records it as 'approved'.
                 Rejecting it (a note is required) puts the exception
                 back in review. The person who approves cannot be the
                 person who resolved it, unless both are the
                 unauthenticated local-development principal.

Every event records the acting user name, role and whether that user was
authenticated. review_events cannot be updated or deleted (triggers), and
events written by version 2 form a hash chain (prev_hash, event_hash), so
verify_integrity() shows any edit or removal made behind the triggers'
back. The chain detects accidental and naive changes; someone who can
rewrite the whole database file can recompute it.

Workflow state is separate from the audit: nothing here ever changes a
reconciliation result.

Migration: a version 1 database is upgraded in one transaction (all the
new columns, the triggers and the version number, or nothing). Existing
events keep their data; they get actor = their free-typed reviewer name
and authenticated = 0, and no hash.
"""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


OPEN = "open"
IN_REVIEW = "in_review"
RESOLVED = "resolved"
DISMISSED = "dismissed"

STATUSES = (OPEN, IN_REVIEW, RESOLVED, DISMISSED)

STATUS_LABELS = {
    OPEN: "Open",
    IN_REVIEW: "In review",
    RESOLVED: "Resolved",
    DISMISSED: "Dismissed",
}

# Allowed changes: anything handled can be reopened
TRANSITIONS = {
    OPEN: {IN_REVIEW, RESOLVED, DISMISSED},
    IN_REVIEW: {OPEN, RESOLVED, DISMISSED},
    RESOLVED: {OPEN, IN_REVIEW},
    DISMISSED: {OPEN, IN_REVIEW},
}

SCHEMA_VERSION = 2

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS exception_reviews (
    exception_key TEXT PRIMARY KEY,
    shipment_key  TEXT NOT NULL,
    shipment_id   TEXT NOT NULL,
    category      TEXT NOT NULL,
    status        TEXT NOT NULL
                  CHECK (status IN ('open', 'in_review', 'resolved', 'dismissed')),
    signature     TEXT NOT NULL,
    reviewer      TEXT,
    note          TEXT,
    needs_recheck INTEGER NOT NULL DEFAULT 0 CHECK (needs_recheck IN (0, 1)),
    absent_since  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    CHECK (status != 'dismissed' OR length(trim(coalesce(note, ''))) > 0)
);

CREATE TABLE IF NOT EXISTS review_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_key TEXT NOT NULL REFERENCES exception_reviews (exception_key),
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    reviewer      TEXT,
    note          TEXT,
    signature     TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS review_events_by_key
    ON review_events (exception_key, id);
"""


MIGRATION_V2 = (
    "ALTER TABLE exception_reviews ADD COLUMN assignee TEXT",
    "ALTER TABLE exception_reviews ADD COLUMN assigned_at TEXT",
    "ALTER TABLE exception_reviews ADD COLUMN assigned_by TEXT",
    "ALTER TABLE exception_reviews ADD COLUMN evidence_refs TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE exception_reviews ADD COLUMN resolved_by TEXT",
    "ALTER TABLE exception_reviews ADD COLUMN resolved_at TEXT",
    "ALTER TABLE exception_reviews ADD COLUMN resolved_authenticated INTEGER",
    "ALTER TABLE exception_reviews ADD COLUMN approval_state TEXT "
    "CHECK (approval_state IN ('pending', 'approved', 'rejected'))",
    "ALTER TABLE exception_reviews ADD COLUMN approval_by TEXT",
    "ALTER TABLE exception_reviews ADD COLUMN approval_at TEXT",
    "ALTER TABLE exception_reviews ADD COLUMN approval_note TEXT",
    "ALTER TABLE review_events ADD COLUMN event_type TEXT NOT NULL DEFAULT 'status'",
    "ALTER TABLE review_events ADD COLUMN actor TEXT",
    "ALTER TABLE review_events ADD COLUMN actor_role TEXT",
    "ALTER TABLE review_events ADD COLUMN authenticated INTEGER",
    "ALTER TABLE review_events ADD COLUMN assignee TEXT",
    "ALTER TABLE review_events ADD COLUMN evidence_refs TEXT",
    "ALTER TABLE review_events ADD COLUMN approval_state TEXT",
    "ALTER TABLE review_events ADD COLUMN prev_hash TEXT",
    "ALTER TABLE review_events ADD COLUMN event_hash TEXT",
    # Earlier events: the free-typed name is the only identity they have
    "UPDATE review_events SET actor = reviewer, authenticated = 0 WHERE actor IS NULL",
    "CREATE TRIGGER IF NOT EXISTS review_events_no_update BEFORE UPDATE ON review_events "
    "BEGIN SELECT RAISE(ABORT, 'review history is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS review_events_no_delete BEFORE DELETE ON review_events "
    "BEGIN SELECT RAISE(ABORT, 'review history is append-only'); END",
)

GENESIS = "genesis"

# Event types
EVENT_DETECTED = "detected"
EVENT_REOPENED_BY_AUDIT = "audit_reopened"
EVENT_STATUS = "status"
EVENT_ASSIGN = "assign"
EVENT_START = "start"
EVENT_NOTE = "note"
EVENT_RESOLVE = "resolve"
EVENT_DISMISS = "dismiss"
EVENT_REOPEN = "reopen"
EVENT_APPROVE = "approve"
EVENT_REJECT = "reject"

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

MAX_NOTE = 2000
MAX_EVIDENCE_ITEMS = 10
MAX_EVIDENCE_LENGTH = 300
MAX_NAME = 64


@dataclass(frozen=True)
class Policy:
    """
    The documented review rules.
    """

    min_note: int = 10
    resolve_needs_evidence: bool = True
    require_approval: bool = False


@dataclass(frozen=True)
class Actor:
    """
    Who made a change: a user name, a role and whether the user signed
    in. System changes (detection) use Actor.system().
    """

    name: str
    role: str
    authenticated: bool = False

    @staticmethod
    def system():

        return Actor("system", "system", False)


class ReviewError(ValueError):
    """
    A review change that is not allowed (unknown exception, status or
    transition, or a dismissal without a reason).
    """


def now_text():

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value):

    value = None if value is None else str(value).strip()

    return value or None


class ReviewStore:

    def __init__(self, path, policy=None):

        self.path = Path(path)
        self.policy = policy or Policy()
        self.initialize()

    # --------------------------------------------------------
    # Connection and schema
    # --------------------------------------------------------

    def connect(self):

        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    @contextmanager
    def write(self):
        """
        One write transaction. BEGIN IMMEDIATE takes the write lock
        first, so two writers cannot both read the same last event hash.
        Everything is committed together, or rolled back on any error.
        """

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

    def initialize(self):

        self.path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as connection:

            with connection:

                connection.executescript(SCHEMA_V1)

                version = connection.execute(
                    "SELECT version FROM schema_info"
                ).fetchone()

                if version is None:
                    connection.execute(
                        "INSERT INTO schema_info (version) VALUES (?)", (1,)
                    )
                    version = 1
                else:
                    version = version["version"]

        if version == 1:
            self.migrate_to_v2()

        elif version != SCHEMA_VERSION:
            raise ReviewError(
                f"Review database {self.path} has schema version "
                f"{version}, expected {SCHEMA_VERSION}."
            )

    def migrate_to_v2(self):
        """
        Version 1 -> 2 in one transaction: every column, the triggers
        and the version number are applied together or not at all.
        """

        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)

        try:

            connection.execute("BEGIN IMMEDIATE")

            current = connection.execute("SELECT version FROM schema_info").fetchone()[0]

            if current == 1:

                for statement in MIGRATION_V2:
                    connection.execute(statement)

                connection.execute("UPDATE schema_info SET version = ?", (SCHEMA_VERSION,))

            connection.execute("COMMIT")

        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

        finally:
            connection.close()

    # --------------------------------------------------------
    # Reading
    # --------------------------------------------------------

    @staticmethod
    def decode(row):

        data = dict(row)

        if "evidence_refs" in data:
            try:
                data["evidence_refs"] = json.loads(data["evidence_refs"]) if data["evidence_refs"] else []
            except ValueError:
                data["evidence_refs"] = []

        return data

    def states(self):
        """
        {exception_key: stored review row as a dict}
        """

        with closing(self.connect()) as connection:

            return {
                row["exception_key"]: self.decode(row)
                for row in connection.execute("SELECT * FROM exception_reviews")
            }

    def history(self, exception_key):

        with closing(self.connect()) as connection:

            return [
                self.decode(row)
                for row in connection.execute(
                    "SELECT * FROM review_events WHERE exception_key = ? ORDER BY id",
                    (exception_key,),
                )
            ]

    def all_events(self, limit=500):
        """
        The newest events of every exception (who changed what and
        when), newest first, with the exception's shipment and category.
        """

        with closing(self.connect()) as connection:

            return [
                self.decode(row)
                for row in connection.execute(
                    "SELECT review_events.*, exception_reviews.shipment_id, "
                    "exception_reviews.category FROM review_events "
                    "JOIN exception_reviews USING (exception_key) "
                    "ORDER BY review_events.id DESC LIMIT ?",
                    (int(limit),),
                )
            ]

    def list_reviews(
        self,
        assignee=None,
        unassigned=False,
        statuses=(),
        categories=(),
        min_age_days=None,
        max_age_days=None,
        pending_approval=False,
        search="",
        include_absent=True,
        now=None,
    ):
        """
        Stored review rows matching every filter given. assignee and
        search match regardless of case; age is whole days since the
        exception was first detected; unassigned keeps rows without an
        assignee; pending_approval keeps rows waiting for approval.
        Newest first.
        """

        clauses, parameters = [], []

        if assignee:
            clauses.append("lower(assignee) = ?")
            parameters.append(str(assignee).strip().lower())

        if unassigned:
            clauses.append("assignee IS NULL")

        if statuses:
            clauses.append("status IN (" + ",".join("?" * len(statuses)) + ")")
            parameters.extend(statuses)

        if categories:
            clauses.append("category IN (" + ",".join("?" * len(categories)) + ")")
            parameters.extend(categories)

        if pending_approval:
            clauses.append("approval_state = 'pending'")

        if search and search.strip():
            clauses.append(
                "(lower(shipment_id) LIKE ? ESCAPE '\\' OR lower(exception_key) LIKE ? ESCAPE '\\')"
            )
            like = "%" + (
                search.strip().lower()
                .replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            ) + "%"
            parameters.extend([like, like])

        if not include_absent:
            clauses.append("absent_since IS NULL")

        query = "SELECT * FROM exception_reviews"

        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        query += " ORDER BY created_at DESC, exception_key"

        moment = now or datetime.now(timezone.utc)

        with closing(self.connect()) as connection:
            rows = [self.decode(row) for row in connection.execute(query, parameters)]

        for row in rows:
            row["age_days"] = max((moment - datetime.fromisoformat(row["created_at"])).days, 0)

        if min_age_days is not None:
            rows = [row for row in rows if row["age_days"] >= min_age_days]

        if max_age_days is not None:
            rows = [row for row in rows if row["age_days"] <= max_age_days]

        return rows

    def assignees(self):

        with closing(self.connect()) as connection:

            return [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT assignee FROM exception_reviews "
                    "WHERE assignee IS NOT NULL ORDER BY assignee"
                )
            ]

    # --------------------------------------------------------
    # Event log (append-only, hash-chained)
    # --------------------------------------------------------

    HASHED_FIELDS = (
        "exception_key", "event_type", "from_status", "to_status", "actor",
        "actor_role", "authenticated", "assignee", "note", "evidence_refs",
        "approval_state", "signature", "created_at",
    )

    @staticmethod
    def event_hash(prev_hash, fields):

        payload = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)

        return hashlib.sha256(f"{prev_hash}|{payload}".encode("utf-8")).hexdigest()

    def record_event(
        self, connection, key, from_status, to_status, reviewer, note, signature, at,
        event_type=EVENT_STATUS, actor=None, assignee=None, evidence=None, approval_state=None,
    ):
        """
        Appends one event inside the caller's transaction. reviewer is
        the legacy name column; actor identifies who acted.
        """

        actor = actor or Actor(reviewer or "unknown", "legacy", False)

        last = connection.execute(
            "SELECT event_hash FROM review_events WHERE event_hash IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

        prev_hash = last["event_hash"] if last else GENESIS

        evidence_text = json.dumps(list(evidence), ensure_ascii=False) if evidence else None

        fields = {
            "exception_key": key, "event_type": event_type, "from_status": from_status,
            "to_status": to_status, "actor": actor.name, "actor_role": actor.role,
            "authenticated": int(actor.authenticated), "assignee": assignee, "note": note,
            "evidence_refs": evidence_text, "approval_state": approval_state,
            "signature": signature, "created_at": at,
        }

        connection.execute(
            "INSERT INTO review_events "
            "(exception_key, from_status, to_status, reviewer, note, signature, created_at, "
            " event_type, actor, actor_role, authenticated, assignee, evidence_refs, approval_state, "
            " prev_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, from_status, to_status, reviewer, note, signature, at, event_type,
             actor.name, actor.role, int(actor.authenticated), assignee, evidence_text,
             approval_state, prev_hash, self.event_hash(prev_hash, fields)),
        )

    def verify_integrity(self):
        """
        Recomputes the hash chain of the events written by schema
        version 2. Returns {"ok", "checked", "legacy_events",
        "problems"}: legacy_events are earlier events without a hash,
        which cannot be verified.
        """

        problems, checked, legacy = [], 0, 0
        expected_prev = GENESIS

        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT * FROM review_events ORDER BY id").fetchall()

        for row in rows:

            if row["event_hash"] is None:

                if checked:
                    problems.append(f"Event {row['id']} has no hash after hashed events.")
                else:
                    legacy += 1

                continue

            fields = {name: row[name] for name in self.HASHED_FIELDS}

            if row["prev_hash"] != expected_prev:
                problems.append(f"Event {row['id']} does not follow the previous event (chain broken).")

            if row["event_hash"] != self.event_hash(row["prev_hash"], fields):
                problems.append(f"Event {row['id']} does not match its hash (changed after it was written).")

            expected_prev = row["event_hash"]
            checked += 1

        return {"ok": not problems, "checked": checked, "legacy_events": legacy, "problems": problems}

    # --------------------------------------------------------
    # Detection (the audit decides what exists)
    # --------------------------------------------------------

    def sync(self, exceptions, at=None):
        """
        Records the currently detected exceptions (see the module
        docstring) and returns {exception_key: review row} for them.
        exceptions: dicts with exception_key, shipment_key,
        shipment_id, category and signature.
        """

        at = at or now_text()
        detected = {item["exception_key"]: item for item in exceptions}
        system = Actor.system()

        with self.write() as connection:

            stored = {
                row["exception_key"]: dict(row)
                for row in connection.execute("SELECT * FROM exception_reviews")
            }

            for key, item in detected.items():

                row = stored.get(key)

                if row is None:

                    connection.execute(
                        "INSERT INTO exception_reviews "
                        "(exception_key, shipment_key, shipment_id, category, status, "
                        " signature, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (key, item["shipment_key"], item["shipment_id"],
                         item["category"], OPEN, item["signature"], at, at),
                    )

                    self.record_event(connection, key, None, OPEN, None,
                                      "Detected", item["signature"], at,
                                      event_type=EVENT_DETECTED, actor=system)
                    continue

                changed = row["signature"] != item["signature"]
                returned = row["absent_since"] is not None

                if row["status"] in (RESOLVED, DISMISSED) and (changed or returned):

                    note = (
                        "Reopened: the audited values changed since it was "
                        f"{row['status']}" if changed else
                        f"Reopened: detected again after being {row['status']} "
                        "and no longer detected"
                    )

                    connection.execute(
                        "UPDATE exception_reviews SET status = ?, needs_recheck = 1, "
                        "signature = ?, absent_since = NULL, approval_state = NULL, "
                        "updated_at = ? WHERE exception_key = ?",
                        (OPEN, item["signature"], at, key),
                    )

                    self.record_event(connection, key, row["status"], OPEN,
                                      None, note, item["signature"], at,
                                      event_type=EVENT_REOPENED_BY_AUDIT, actor=system)

                elif changed or returned:

                    # Still being handled: keep the state, but note
                    # that the audit changed underneath it
                    connection.execute(
                        "UPDATE exception_reviews SET needs_recheck = ?, "
                        "signature = ?, absent_since = NULL, updated_at = ? "
                        "WHERE exception_key = ?",
                        (1 if changed else row["needs_recheck"], item["signature"], at, key),
                    )

            for key, row in stored.items():

                if key not in detected and row["absent_since"] is None:
                    connection.execute(
                        "UPDATE exception_reviews SET absent_since = ? "
                        "WHERE exception_key = ?",
                        (at, key),
                    )

        states = self.states()

        return {key: states[key] for key in detected}

    # --------------------------------------------------------
    # Legacy single-step change
    # --------------------------------------------------------

    def set_status(self, exception, status, reviewer=None, note=None, at=None):
        """
        The original low-level status change: any allowed transition,
        a dismissal needs a reason, nothing else is required, and the
        change is attributed to the free-typed reviewer name (not
        authenticated). The application uses apply() instead, which
        enforces the workflow rules and identifies the actor.
        """

        if status not in STATUSES:
            raise ReviewError(f"Unknown review status: {status}.")

        reviewer = clean(reviewer)
        note = clean(note)

        if status == DISMISSED and not note:
            raise ReviewError("A dismissal needs a reason.")

        at = at or now_text()
        key = exception["exception_key"]

        with self.write() as connection:

            row = connection.execute(
                "SELECT * FROM exception_reviews WHERE exception_key = ?", (key,)
            ).fetchone()

            if row is None:
                raise ReviewError(
                    "This exception has not been recorded yet; show the "
                    "exception register first."
                )

            if row["signature"] != exception["signature"]:
                raise ReviewError(
                    "The audit for this exception has changed. Reload the "
                    "analytics before changing its review state."
                )

            if status != row["status"] and status not in TRANSITIONS[row["status"]]:
                raise ReviewError(
                    f"Cannot change {STATUS_LABELS[row['status']]} to "
                    f"{STATUS_LABELS[status]}."
                )

            connection.execute(
                "UPDATE exception_reviews SET status = ?, reviewer = ?, note = ?, "
                "needs_recheck = 0, approval_state = NULL, updated_at = ? WHERE exception_key = ?",
                (status, reviewer, note, at, key),
            )

            self.record_event(connection, key, row["status"], status,
                              reviewer, note, row["signature"], at,
                              actor=Actor(reviewer or "unknown", "legacy", False))

        return self.states()[key]

    # --------------------------------------------------------
    # Workflow
    # --------------------------------------------------------

    @staticmethod
    def check_name(value, what):

        value = clean(value)

        if value is None:
            raise ReviewError(f"{what} is required.")

        if len(value) > MAX_NAME or any(ord(ch) < 32 for ch in value):
            raise ReviewError(f"{what} is not valid.")

        return value

    def check_note(self, note, what, required):

        note = clean(note)

        if note is None:
            if required:
                raise ReviewError(f"{what} needs a note of at least {self.policy.min_note} characters.")
            return None

        if len(note) > MAX_NOTE:
            raise ReviewError(f"The note is too long (at most {MAX_NOTE} characters).")

        if required and len(note) < self.policy.min_note:
            raise ReviewError(f"{what} needs a note of at least {self.policy.min_note} characters.")

        return note

    @staticmethod
    def check_evidence(evidence):

        items = []

        for item in evidence or ():

            item = clean(item)

            if item is None:
                continue

            if len(item) > MAX_EVIDENCE_LENGTH or any(ord(ch) < 32 for ch in item):
                raise ReviewError("An evidence reference is too long or has control characters.")

            if item not in items:
                items.append(item)

        if len(items) > MAX_EVIDENCE_ITEMS:
            raise ReviewError(f"At most {MAX_EVIDENCE_ITEMS} evidence references can be given.")

        return items

    def apply(self, exception, action, actor, note=None, evidence=(), assignee=None, at=None):
        """
        One workflow step on a detected exception (see the module
        docstring). action: assign, start, note, resolve, dismiss,
        reopen, approve or reject. Permissions are checked by the caller
        (exception_workflow); the rules (required notes and evidence,
        allowed transitions, approval) are enforced here, inside the
        transaction, so no caller can skip them. Returns the updated
        review row. Raises ReviewError, and writes nothing, when a rule
        is broken.
        """

        at = at or now_text()
        key = exception["exception_key"]
        actor_name = self.check_name(actor.name, "The acting user")

        with self.write() as connection:

            row = connection.execute(
                "SELECT * FROM exception_reviews WHERE exception_key = ?", (key,)
            ).fetchone()

            if row is None:
                raise ReviewError(
                    "This exception has not been recorded yet; show the "
                    "exception register first."
                )

            if row["signature"] != exception["signature"]:
                raise ReviewError(
                    "The audit for this exception has changed. Reload the "
                    "analytics before changing its review state."
                )

            status = row["status"]
            current_evidence = json.loads(row["evidence_refs"] or "[]")
            updates = {}
            event_evidence = None

            if action == EVENT_ASSIGN:

                if status in (RESOLVED, DISMISSED):
                    raise ReviewError("A resolved or dismissed exception cannot be assigned; reopen it first.")

                new_assignee = self.check_name(assignee, "The assignee")
                note = self.check_note(note, "An assignment", required=False)

                updates = {"assignee": new_assignee, "assigned_at": at, "assigned_by": actor_name}
                to_status = status

            elif action == EVENT_START:

                if status != OPEN:
                    raise ReviewError(
                        f"Only an open exception can be started; this one is {STATUS_LABELS[status]}."
                    )

                new_assignee = self.check_name(
                    clean(assignee) or row["assignee"] or actor_name, "The assignee"
                )
                note = self.check_note(note, "Starting a review", required=False)
                same = new_assignee == row["assignee"]

                updates = {
                    "status": IN_REVIEW, "assignee": new_assignee,
                    "assigned_at": row["assigned_at"] if same else at,
                    "assigned_by": row["assigned_by"] if same else actor_name,
                    "needs_recheck": 0,
                }
                to_status = IN_REVIEW

            elif action == EVENT_NOTE:

                note = self.check_note(note, "A note", required=False)
                new_evidence = self.check_evidence(evidence)

                if not note and not new_evidence:
                    raise ReviewError("A note needs text or an evidence reference.")

                merged = current_evidence + [item for item in new_evidence if item not in current_evidence]

                updates = {"evidence_refs": json.dumps(merged, ensure_ascii=False)}
                to_status, event_evidence = status, new_evidence

            elif action in (EVENT_RESOLVE, EVENT_DISMISS):

                if action == EVENT_RESOLVE and status != IN_REVIEW:
                    raise ReviewError(
                        "Only an exception in review can be resolved; start the review first "
                        f"(this one is {STATUS_LABELS[status]})."
                    )

                if action == EVENT_DISMISS and status not in (OPEN, IN_REVIEW):
                    raise ReviewError(f"A {STATUS_LABELS[status].lower()} exception cannot be dismissed.")

                label = "Resolving" if action == EVENT_RESOLVE else "Dismissing"

                note = self.check_note(note, label, required=True)
                new_evidence = self.check_evidence(evidence)
                merged = current_evidence + [item for item in new_evidence if item not in current_evidence]

                if action == EVENT_RESOLVE and self.policy.resolve_needs_evidence and not merged:
                    raise ReviewError("Resolving needs at least one evidence reference.")

                to_status = RESOLVED if action == EVENT_RESOLVE else DISMISSED

                updates = {
                    "status": to_status, "evidence_refs": json.dumps(merged, ensure_ascii=False),
                    "resolved_by": actor_name, "resolved_authenticated": int(actor.authenticated),
                    "resolved_at": at, "needs_recheck": 0,
                    "approval_state": PENDING if self.policy.require_approval else None,
                    "approval_by": None, "approval_at": None, "approval_note": None,
                }
                event_evidence = new_evidence

            elif action == EVENT_REOPEN:

                if status not in (RESOLVED, DISMISSED):
                    raise ReviewError(
                        "Only a resolved or dismissed exception can be reopened; "
                        f"this one is {STATUS_LABELS[status]}."
                    )

                note = self.check_note(note, "Reopening", required=True)

                updates = {
                    "status": OPEN, "needs_recheck": 0, "approval_state": None,
                    "approval_by": None, "approval_at": None, "approval_note": None,
                    "resolved_by": None, "resolved_at": None, "resolved_authenticated": None,
                }
                to_status = OPEN

            elif action in (EVENT_APPROVE, EVENT_REJECT):

                if row["approval_state"] != PENDING:
                    raise ReviewError("This exception is not waiting for approval.")

                same_person = (
                    row["resolved_by"] == actor_name
                    and bool(row["resolved_authenticated"] or actor.authenticated)
                )

                if same_person:
                    raise ReviewError("The person who resolved or dismissed an exception cannot approve it.")

                if action == EVENT_APPROVE:
                    note = self.check_note(note, "An approval", required=False)
                    updates = {"approval_state": APPROVED, "approval_by": actor_name,
                               "approval_at": at, "approval_note": note}
                    to_status = status
                else:
                    note = self.check_note(note, "A rejection", required=True)
                    updates = {"status": IN_REVIEW, "approval_state": REJECTED,
                               "approval_by": actor_name, "approval_at": at, "approval_note": note,
                               "resolved_by": None, "resolved_at": None, "resolved_authenticated": None}
                    to_status = IN_REVIEW

            else:
                raise ReviewError(f"Unknown workflow action: {action!r}.")

            updates["updated_at"] = at
            updates["reviewer"] = actor_name
            updates["note"] = note if note else row["note"]

            assignments = ", ".join(f"{column} = ?" for column in updates)

            connection.execute(
                f"UPDATE exception_reviews SET {assignments} WHERE exception_key = ?",
                (*updates.values(), key),
            )

            self.record_event(
                connection, key, status, to_status, actor_name, note, row["signature"], at,
                event_type=action, actor=actor,
                assignee=updates.get("assignee", row["assignee"]),
                evidence=event_evidence,
                approval_state=updates.get("approval_state", row["approval_state"]),
            )

        return self.states()[key]
