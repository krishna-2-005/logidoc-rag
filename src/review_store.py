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
"""

import sqlite3
from contextlib import closing
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

SCHEMA_VERSION = 1

SCHEMA = """
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

    def __init__(self, path):

        self.path = Path(path)
        self.initialize()

    # --------------------------------------------------------
    # Connection and schema
    # --------------------------------------------------------

    def connect(self):

        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    def initialize(self):

        self.path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as connection:

            with connection:

                connection.executescript(SCHEMA)

                version = connection.execute(
                    "SELECT version FROM schema_info"
                ).fetchone()

                if version is None:
                    connection.execute(
                        "INSERT INTO schema_info (version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )

                elif version["version"] != SCHEMA_VERSION:
                    raise ReviewError(
                        f"Review database {self.path} has schema version "
                        f"{version['version']}, expected {SCHEMA_VERSION}."
                    )

    # --------------------------------------------------------
    # Reading
    # --------------------------------------------------------

    def states(self):
        """
        {exception_key: stored review row as a dict}
        """

        with closing(self.connect()) as connection:

            return {
                row["exception_key"]: dict(row)
                for row in connection.execute("SELECT * FROM exception_reviews")
            }

    def history(self, exception_key):

        with closing(self.connect()) as connection:

            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM review_events WHERE exception_key = ? ORDER BY id",
                    (exception_key,),
                )
            ]

    # --------------------------------------------------------
    # Writing
    # --------------------------------------------------------

    def record_event(self, connection, key, from_status, to_status, reviewer, note, signature, at):

        connection.execute(
            "INSERT INTO review_events "
            "(exception_key, from_status, to_status, reviewer, note, signature, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, from_status, to_status, reviewer, note, signature, at),
        )

    def sync(self, exceptions, at=None):
        """
        Records the currently detected exceptions (see the module
        docstring) and returns {exception_key: review row} for them.
        exceptions: dicts with exception_key, shipment_key,
        shipment_id, category and signature.
        """

        at = at or now_text()
        detected = {item["exception_key"]: item for item in exceptions}

        with closing(self.connect()) as connection:

            with connection:

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
                                          "Detected", item["signature"], at)
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
                            "signature = ?, absent_since = NULL, updated_at = ? "
                            "WHERE exception_key = ?",
                            (OPEN, item["signature"], at, key),
                        )

                        self.record_event(connection, key, row["status"], OPEN,
                                          None, note, item["signature"], at)

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

    def set_status(self, exception, status, reviewer=None, note=None, at=None):
        """
        Changes the review state of a detected exception. A dismissal
        needs a reason (note). Returns the updated review row.
        """

        if status not in STATUSES:
            raise ReviewError(f"Unknown review status: {status}.")

        reviewer = clean(reviewer)
        note = clean(note)

        if status == DISMISSED and not note:
            raise ReviewError("A dismissal needs a reason.")

        at = at or now_text()
        key = exception["exception_key"]

        with closing(self.connect()) as connection:

            with connection:

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
                    "needs_recheck = 0, updated_at = ? WHERE exception_key = ?",
                    (status, reviewer, note, at, key),
                )

                self.record_event(connection, key, row["status"], status,
                                  reviewer, note, row["signature"], at)

        return self.states()[key]
