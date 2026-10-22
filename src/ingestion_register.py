"""
LogiDoc-RAG ingestion register

A small SQLite database (separate from the audit, review, history and
user databases) that records what happened to every file that was
offered for ingestion: its status, the reason and the corrective action
when it was not indexed, how often it was tried, and what shipment and
document type were found. It holds metadata about files and never their
contents.

Status of a file

    queued       accepted for processing, not started
    processing   being validated, embedded, indexed and stored
    indexed      its text is in the search index and the PDF is stored
                 (also for an upload whose content was already indexed:
                 code "duplicate", duplicate_of names the stored file)
    partial      indexed, with a problem to know about: pages without
                 text, no shipment ID, or an unknown document type
    failed       not indexed; a corrected file or a retry is needed
    unsupported  not something this system can read (not a PDF, an
                 image-only scan that needs OCR)

Allowed changes: queued -> processing, failed or unsupported (rejected
before processing) or indexed (a duplicate); processing -> indexed,
partial, failed, unsupported. A file in any other status is processed
again only through requeue() (a retry or a reprocess), which starts a new
attempt. A record in processing or queued after a crash is not left
looking busy: recover_interrupted() marks it failed (code
"interrupted").

Error register

    A failed, unsupported or interrupted record is an open error until it
    is resolved: automatically when a later upload of the same file name
    is indexed, or by a person who writes down what was done (resolve()).
    errors() lists the open ones with their corrective action. They are
    also what the audit holds against every shipment (audit_problems()):
    a file that could not be ingested might belong to a shipment, so no
    shipment can be reported clean while it is open.

Every change is one transaction. ingestion_events is append-only
(triggers). Nothing here touches a PDF.
"""

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1

QUEUED = "queued"
PROCESSING = "processing"
INDEXED = "indexed"
PARTIAL = "partial"
FAILED = "failed"
UNSUPPORTED = "unsupported"

STATUSES = (QUEUED, PROCESSING, INDEXED, PARTIAL, FAILED, UNSUPPORTED)

STATUS_LABELS = {
    QUEUED: "Queued", PROCESSING: "Processing", INDEXED: "Indexed", PARTIAL: "Partial",
    FAILED: "Failed", UNSUPPORTED: "Unsupported",
}

TRANSITIONS = {
    QUEUED: {PROCESSING, FAILED, UNSUPPORTED, INDEXED},
    PROCESSING: {INDEXED, PARTIAL, FAILED, UNSUPPORTED},
}

ERROR_STATUSES = (FAILED, UNSUPPORTED)
BUSY_STATUSES = (QUEUED, PROCESSING)

INTERRUPTED = "interrupted"

MIN_RESOLUTION = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    sha256          TEXT,
    size            INTEGER,
    status          TEXT NOT NULL
                    CHECK (status IN ('queued', 'processing', 'indexed', 'partial', 'failed', 'unsupported')),
    code            TEXT,
    message         TEXT,
    action          TEXT,
    document_type   TEXT,
    shipment_id     TEXT,
    shipment_key    TEXT,
    pages           INTEGER,
    text_pages      INTEGER,
    chunks_indexed  INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    duplicate_of    TEXT,
    quarantine_name TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolved_by     TEXT,
    resolved_at     TEXT,
    resolution      TEXT,
    actor           TEXT,
    authenticated   INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ingestion_files_by_name ON ingestion_files (filename, id);

CREATE TABLE IF NOT EXISTS ingestion_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id   INTEGER NOT NULL REFERENCES ingestion_files (id),
    at        TEXT NOT NULL,
    status    TEXT NOT NULL,
    code      TEXT,
    message   TEXT,
    attempt   INTEGER,
    actor     TEXT
);

CREATE TABLE IF NOT EXISTS ingestion_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    action        TEXT NOT NULL,
    filename      TEXT NOT NULL,
    sha256        TEXT,
    detail        TEXT,
    actor         TEXT,
    authenticated INTEGER
);

CREATE TRIGGER IF NOT EXISTS ingestion_actions_no_update
    BEFORE UPDATE ON ingestion_actions
    BEGIN SELECT RAISE(ABORT, 'ingestion actions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS ingestion_actions_no_delete
    BEFORE DELETE ON ingestion_actions
    BEGIN SELECT RAISE(ABORT, 'ingestion actions are append-only'); END;

CREATE TRIGGER IF NOT EXISTS ingestion_events_no_update
    BEFORE UPDATE ON ingestion_events
    BEGIN SELECT RAISE(ABORT, 'ingestion events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS ingestion_events_no_delete
    BEFORE DELETE ON ingestion_events
    BEGIN SELECT RAISE(ABORT, 'ingestion events are append-only'); END;
"""

UPDATABLE = (
    "sha256", "size", "code", "message", "action", "document_type", "shipment_id", "shipment_key",
    "pages", "text_pages", "chunks_indexed", "duplicate_of", "quarantine_name",
)


class RegisterError(ValueError):
    """
    A register change that is not allowed (unknown file, impossible
    status change, missing resolution note) or a database of another
    schema version.
    """


def now_text():

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IngestionRegister:

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

    def initialize(self):

        self.path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as connection:

            with connection:

                connection.executescript(SCHEMA)

                version = connection.execute("SELECT version FROM schema_info").fetchone()

                if version is None:
                    connection.execute("INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,))
                elif version["version"] != SCHEMA_VERSION:
                    raise RegisterError(
                        f"Ingestion register {self.path} has schema version "
                        f"{version['version']}, expected {SCHEMA_VERSION}."
                    )

    # --------------------------------------------------------
    # Writing
    # --------------------------------------------------------

    @staticmethod
    def event(connection, file_id, status, code, message, attempt, actor, at):

        connection.execute(
            "INSERT INTO ingestion_events (file_id, at, status, code, message, attempt, actor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_id, at, status, code, message, attempt, actor),
        )

    def queue(self, filename, sha256=None, size=None, actor=None, authenticated=False, at=None):
        """
        Records a file as queued. Returns its id.
        """

        at = at or now_text()

        with self.write() as connection:

            cursor = connection.execute(
                "INSERT INTO ingestion_files (filename, sha256, size, status, actor, authenticated, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(filename)[:300], sha256, size, QUEUED, actor, int(authenticated), at, at),
            )

            self.event(connection, cursor.lastrowid, QUEUED, None, "Queued", 0, actor, at)

            return cursor.lastrowid

    def update(self, file_id, status, actor=None, at=None, **fields):
        """
        Moves a file to a new status (see TRANSITIONS), with the details
        in fields (UPDATABLE). Returns the updated record.
        """

        if status not in STATUSES:
            raise RegisterError(f"Unknown status: {status}.")

        unknown = set(fields) - set(UPDATABLE)

        if unknown:
            raise RegisterError(f"Unknown fields: {sorted(unknown)}.")

        at = at or now_text()

        with self.write() as connection:

            row = connection.execute("SELECT * FROM ingestion_files WHERE id = ?", (file_id,)).fetchone()

            if row is None:
                raise RegisterError(f"There is no ingestion record {file_id}.")

            if status not in TRANSITIONS.get(row["status"], set()):
                raise RegisterError(
                    f"A {STATUS_LABELS[row['status']].lower()} file cannot become "
                    f"{STATUS_LABELS[status].lower()}."
                )

            attempts = row["attempts"] + (1 if status == PROCESSING else 0)

            # A new status replaces the old reason: nothing stale is kept
            values = {
                "status": status, "attempts": attempts, "updated_at": at,
                "code": None, "message": None, "action": None, **fields,
            }

            assignments = ", ".join(f"{column} = ?" for column in values)

            connection.execute(
                f"UPDATE ingestion_files SET {assignments} WHERE id = ?", (*values.values(), file_id)
            )

            self.event(connection, file_id, status, values["code"], values["message"], attempts, actor, at)

            if status in (INDEXED, PARTIAL):

                # A file of this name that was indexed supersedes earlier errors of the same name
                connection.execute(
                    "UPDATE ingestion_files SET resolved = 1, resolved_by = 'system', resolved_at = ?, "
                    "resolution = ? WHERE filename = ? AND id != ? AND resolved = 0 AND status IN ('failed', 'unsupported')",
                    (at, f"Superseded by ingestion record {file_id}", row["filename"], file_id),
                )

        return self.get(file_id)

    def attempt_failed(self, file_id, message, actor=None, at=None):
        """
        Records that one attempt failed and another will follow (a retry
        after a transient failure): the file stays in processing, the
        attempt is counted and the reason is kept in the event log.
        """

        at = at or now_text()

        with self.write() as connection:

            row = connection.execute("SELECT * FROM ingestion_files WHERE id = ?", (file_id,)).fetchone()

            if row is None or row["status"] != PROCESSING:
                raise RegisterError("Only a file being processed can have a failed attempt.")

            connection.execute(
                "UPDATE ingestion_files SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (at, file_id),
            )

            self.event(connection, file_id, PROCESSING, "retrying", str(message)[:500],
                       row["attempts"] + 1, actor, at)

    def log_action(self, action, filename, sha256=None, detail=None, actor=None, authenticated=False, at=None):
        """
        Records an administrator action on a stored document (replaced,
        removed, reprocessed). Append-only.
        """

        with self.write() as connection:

            connection.execute(
                "INSERT INTO ingestion_actions (at, action, filename, sha256, detail, actor, authenticated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (at or now_text(), action, str(filename)[:300], sha256, (detail or "")[:1000] or None,
                 actor, int(authenticated)),
            )

    def actions(self, limit=200):

        with closing(self.connect()) as connection:

            return [
                dict(row)
                for row in connection.execute("SELECT * FROM ingestion_actions ORDER BY id DESC LIMIT ?", (int(limit),))
            ]

    def requeue(self, file_id, actor=None, at=None):
        """
        Puts a finished file back in the queue for another attempt (a
        retry or a reprocess). A busy file cannot be requeued.
        """

        at = at or now_text()

        with self.write() as connection:

            row = connection.execute("SELECT * FROM ingestion_files WHERE id = ?", (file_id,)).fetchone()

            if row is None:
                raise RegisterError(f"There is no ingestion record {file_id}.")

            if row["status"] in BUSY_STATUSES:
                raise RegisterError("This file is already queued or being processed.")

            connection.execute(
                "UPDATE ingestion_files SET status = ?, code = NULL, message = NULL, action = NULL, "
                "resolved = 0, resolved_by = NULL, resolved_at = NULL, resolution = NULL, updated_at = ? "
                "WHERE id = ?",
                (QUEUED, at, file_id),
            )

            self.event(connection, file_id, QUEUED, None, "Queued again", row["attempts"], actor, at)

        return self.get(file_id)

    def resolve(self, file_id, actor, note, at=None):
        """
        Closes an open error: someone corrected it or decided it does
        not matter, and says what was done. Only failed or unsupported
        files have errors to resolve.
        """

        note = (note or "").strip()

        if len(note) < MIN_RESOLUTION:
            raise RegisterError(
                f"Resolving an ingestion error needs a note of at least {MIN_RESOLUTION} characters "
                "saying what was done."
            )

        at = at or now_text()

        with self.write() as connection:

            row = connection.execute("SELECT * FROM ingestion_files WHERE id = ?", (file_id,)).fetchone()

            if row is None:
                raise RegisterError(f"There is no ingestion record {file_id}.")

            if row["status"] not in ERROR_STATUSES:
                raise RegisterError("Only a failed or unsupported file has an error to resolve.")

            if row["resolved"]:
                raise RegisterError("This error is already resolved.")

            connection.execute(
                "UPDATE ingestion_files SET resolved = 1, resolved_by = ?, resolved_at = ?, "
                "resolution = ?, updated_at = ? WHERE id = ?",
                (actor, at, note[:1000], at, file_id),
            )

            self.event(connection, file_id, row["status"], "resolved", note[:1000], row["attempts"], actor, at)

        return self.get(file_id)

    def recover_interrupted(self, at=None):
        """
        Marks the files left queued or processing (by a crash or a
        stopped process) as failed with the code "interrupted". Call it
        only while no ingestion is running. Returns how many.
        """

        at = at or now_text()

        with self.write() as connection:

            rows = connection.execute(
                "SELECT * FROM ingestion_files WHERE status IN ('queued', 'processing')"
            ).fetchall()

            for row in rows:

                message = (
                    "Processing did not finish (the process stopped or crashed). Nothing was indexed "
                    "for this attempt."
                )
                action = "Retry the file, or upload it again."

                connection.execute(
                    "UPDATE ingestion_files SET status = ?, code = ?, message = ?, action = ?, "
                    "updated_at = ? WHERE id = ?",
                    (FAILED, INTERRUPTED, message, action, at, row["id"]),
                )

                self.event(connection, row["id"], FAILED, INTERRUPTED, message, row["attempts"], "system", at)

            return len(rows)

    # --------------------------------------------------------
    # Reading
    # --------------------------------------------------------

    def get(self, file_id):

        with closing(self.connect()) as connection:

            row = connection.execute("SELECT * FROM ingestion_files WHERE id = ?", (file_id,)).fetchone()

        return dict(row) if row else None

    def files(self, statuses=(), search="", open_errors_only=False, limit=500):
        """
        Records newest first. statuses: keep these statuses; search:
        part of a file name or shipment ID (any case); open_errors_only:
        failed or unsupported records that are not resolved.
        """

        clauses, parameters = [], []

        if statuses:
            clauses.append("status IN (" + ",".join("?" * len(statuses)) + ")")
            parameters.extend(statuses)

        if search and search.strip():
            like = "%" + search.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append(
                "(lower(filename) LIKE ? ESCAPE '\\' OR lower(coalesce(shipment_id, '')) LIKE ? ESCAPE '\\')"
            )
            parameters.extend([like, like])

        if open_errors_only:
            clauses.append("status IN ('failed', 'unsupported') AND resolved = 0")

        query = "SELECT * FROM ingestion_files"

        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        query += " ORDER BY id DESC LIMIT ?"

        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(query, (*parameters, int(limit)))]

    def errors(self):
        """
        The open errors, newest first, each with its corrective action.
        """

        return self.files(open_errors_only=True)

    def events(self, file_id):

        with closing(self.connect()) as connection:

            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM ingestion_events WHERE file_id = ? ORDER BY id", (file_id,)
                )
            ]

    def summary(self):
        """
        {status: count} over the latest record of each file name, plus
        "open_errors".
        """

        with closing(self.connect()) as connection:

            rows = connection.execute(
                "SELECT status, COUNT(*) FROM ingestion_files WHERE id IN "
                "(SELECT MAX(id) FROM ingestion_files GROUP BY filename) GROUP BY status"
            ).fetchall()

        counts = {status: 0 for status in STATUSES}
        counts.update({row[0]: row[1] for row in rows})
        counts["open_errors"] = len(self.errors())

        return counts

    def audit_problems(self):
        """
        What the audit must hold against every shipment: files that were
        offered but could not be ingested and have not been resolved, as
        chunker.read_chunks reports problem files ({"filename",
        "status", "error"}), one per file name (the latest attempt).
        A file name whose latest attempt is indexed is not a problem.
        """

        with closing(self.connect()) as connection:

            rows = connection.execute(
                "SELECT * FROM ingestion_files WHERE id IN (SELECT MAX(id) FROM ingestion_files GROUP BY filename) "
                "AND ((status IN ('failed', 'unsupported') AND resolved = 0) OR status IN ('queued', 'processing')) "
                "ORDER BY filename"
            ).fetchall()

        return [
            {
                "filename": row["filename"],
                "status": row["code"] or row["status"],
                "error": row["message"],
            }
            for row in rows
        ]
