"""
The integration store: webhooks, their event outbox and deliveries, the
scheduled jobs, and the TMS/ERP integrations with their synchronisation runs
(docs/WEBHOOKS_AND_DELIVERY.md, docs/SCHEDULED_SYNCHRONISATION.md,
docs/TMS_ERP_INTEGRATION_FRAMEWORK.md).

One SQLite file, integrations.sqlite3, beside the other stores
(LOGIDOC_INTEGRATION_DB to put it elsewhere). It is created only when an
administrator first configures a webhook endpoint or an integration: reading
never creates it, and the scheduler does nothing until it exists.

What it never holds

    signing secrets      only a random salt and a version per endpoint: the
                         secret is derived from LOGIDOC_WEBHOOK_SECRET_KEY when
                         a request is signed (src/webhooks.py), so a copy of
                         this file (a backup) cannot sign anything
    credentials          an integration names the environment variable that
                         holds its credential, never the value
    business records     records imported from a sandbox live in the sandbox
                         folder (src/integration_framework.py), never here

History is kept

    endpoint_changes, integration_changes, delivery_attempts and
    webhook_events are append-only (triggers refuse UPDATE and DELETE).
    Deliveries, job runs and sync runs change state while they run and are
    never deleted. An endpoint or integration is archived or disabled, never
    removed, so its delivery and run history stays readable.

Schema changes are versioned: SCHEMA_VERSION and MIGRATIONS (one transaction
per step, the version read again inside it), as in src/audit_history.py.
"""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS webhook_endpoints (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    name                       TEXT NOT NULL,
    url                        TEXT NOT NULL,
    event_types                TEXT NOT NULL,
    enabled                    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    secret_version             INTEGER NOT NULL,
    secret_salt                TEXT NOT NULL,
    previous_secret_version    INTEGER,
    previous_secret_salt       TEXT,
    previous_secret_expires_at TEXT,
    secret_rotated_at          TEXT NOT NULL,
    created_by                 TEXT NOT NULL,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL,
    archived_at                TEXT,
    archived_by                TEXT
);

CREATE TABLE IF NOT EXISTS endpoint_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id INTEGER NOT NULL REFERENCES webhook_endpoints (id),
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    before      TEXT,
    after       TEXT
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id          TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    source      TEXT NOT NULL,
    source_ref  TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    UNIQUE (source, source_ref, event_type)
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL REFERENCES webhook_events (id),
    endpoint_id     INTEGER NOT NULL REFERENCES webhook_endpoints (id),
    status          TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'retrying', 'succeeded', 'failed', 'cancelled')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    cycle_attempts  INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_attempt_at TEXT,
    last_status_code INTEGER,
    last_error      TEXT,
    lease_until     TEXT,
    test            INTEGER NOT NULL DEFAULT 0 CHECK (test IN (0, 1)),
    manual_retries  INTEGER NOT NULL DEFAULT 0,
    last_retry_by   TEXT,
    last_retry_at   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT,
    UNIQUE (event_id, endpoint_id)
);

CREATE INDEX IF NOT EXISTS deliveries_due ON webhook_deliveries (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS deliveries_by_endpoint ON webhook_deliveries (endpoint_id, id);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES webhook_deliveries (id),
    attempt     INTEGER NOT NULL,
    trigger     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    duration_ms INTEGER,
    status_code INTEGER,
    outcome     TEXT NOT NULL CHECK (outcome IN ('succeeded', 'retry', 'failed')),
    error       TEXT,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS attempts_by_delivery ON delivery_attempts (delivery_id, id);

CREATE TABLE IF NOT EXISTS source_cursors (
    source     TEXT PRIMARY KEY,
    position   INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    name                 TEXT PRIMARY KEY,
    enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    interval_seconds     INTEGER NOT NULL,
    next_run_at          TEXT NOT NULL,
    run_requested        TEXT,
    requested_by         TEXT,
    lease_owner          TEXT,
    lease_until          TEXT,
    last_started_at      TEXT,
    last_finished_at     TEXT,
    last_success_at      TEXT,
    last_failure_at      TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job            TEXT NOT NULL,
    trigger        TEXT NOT NULL,
    requested_by   TEXT,
    owner          TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'interrupted')),
    duration_ms    INTEGER,
    delay_seconds  INTEGER,
    summary        TEXT,
    error_category TEXT,
    error_message  TEXT
);

CREATE INDEX IF NOT EXISTS job_runs_by_job ON job_runs (job, id);

CREATE TABLE IF NOT EXISTS scheduler_owner (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    owner        TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integrations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL UNIQUE,
    provider              TEXT NOT NULL,
    enabled               INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    settings              TEXT NOT NULL,
    credential_env        TEXT,
    sync_interval_seconds INTEGER NOT NULL,
    next_sync_at          TEXT,
    cursor                TEXT,
    connection_status     TEXT NOT NULL DEFAULT 'untested',
    connection_checked_at TEXT,
    connection_error      TEXT,
    last_success_at       TEXT,
    last_failure_at       TEXT,
    last_error            TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    lease_until           TEXT,
    created_by            TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integration_changes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    integration_id INTEGER NOT NULL REFERENCES integrations (id),
    at             TEXT NOT NULL,
    actor          TEXT NOT NULL,
    action         TEXT NOT NULL,
    before         TEXT,
    after          TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    integration_id INTEGER NOT NULL REFERENCES integrations (id),
    trigger        TEXT NOT NULL,
    requested_by   TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')),
    cursor_before  TEXT,
    cursor_after   TEXT,
    fetched        INTEGER NOT NULL DEFAULT 0,
    imported       INTEGER NOT NULL DEFAULT 0,
    updated        INTEGER NOT NULL DEFAULT 0,
    unchanged      INTEGER NOT NULL DEFAULT 0,
    duplicates     INTEGER NOT NULL DEFAULT 0,
    rejected       INTEGER NOT NULL DEFAULT 0,
    conflicts      INTEGER NOT NULL DEFAULT 0,
    pages          INTEGER NOT NULL DEFAULT 0,
    retries        INTEGER NOT NULL DEFAULT 0,
    error_category TEXT,
    error_message  TEXT
);

CREATE INDEX IF NOT EXISTS sync_runs_by_integration ON sync_runs (integration_id, id);

CREATE TABLE IF NOT EXISTS sync_rejections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES sync_runs (id),
    entity      TEXT NOT NULL,
    external_id TEXT,
    reason      TEXT NOT NULL,
    at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS rejections_by_run ON sync_rejections (run_id, id);
"""

APPEND_ONLY = ("endpoint_changes", "integration_changes", "delivery_attempts", "webhook_events", "sync_rejections")

TRIGGERS = "".join(
    f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} "
    f"BEGIN SELECT RAISE(ABORT, 'integration history is append-only'); END;\n"
    f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} "
    f"BEGIN SELECT RAISE(ABORT, 'integration history is append-only'); END;\n"
    for table in APPEND_ONLY
)

# Version n: the statements that bring version n-1 to n (none yet)
MIGRATIONS = {}


class StoreVersionError(RuntimeError):
    """The store was written by a newer version of the application."""


def utc_now():

    return datetime.now(timezone.utc)


def text_time(moment):

    return moment.astimezone(timezone.utc).isoformat(timespec="seconds") if moment else None


def parse_time(text):

    return datetime.fromisoformat(text) if text else None


def later(moment, seconds):

    return text_time(moment + timedelta(seconds=seconds))


def dumps(value):

    return json.dumps(value, sort_keys=True, default=str)


def loads(text, default=None):

    return json.loads(text) if text else default


class IntegrationStore:

    def __init__(self, path):

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as connection, connection:
            connection.executescript(SCHEMA + TRIGGERS)
            row = connection.execute("SELECT version FROM schema_info").fetchone()
            if row is None:
                connection.execute("INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,))
                version = SCHEMA_VERSION
            else:
                version = row[0]

        if version > SCHEMA_VERSION:
            raise StoreVersionError(
                f"The integration store has schema version {version}; this version of the application reads "
                f"{SCHEMA_VERSION}. Update the application.")

        if version < SCHEMA_VERSION:
            self.migrate()

    def migrate(self):

        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)

        try:
            for target in range(2, SCHEMA_VERSION + 1):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if connection.execute("SELECT version FROM schema_info").fetchone()[0] == target - 1:
                        for statement in MIGRATIONS[target]:
                            connection.execute(statement)
                        connection.execute("UPDATE schema_info SET version = ?", (target,))
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
        finally:
            connection.close()

    def connect(self):

        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    @contextmanager
    def write(self):
        """One write transaction, taken with the write lock first (BEGIN IMMEDIATE)."""

        with closing(self.connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection

    @contextmanager
    def read(self):

        with closing(self.connect()) as connection:
            yield connection

    def version(self):

        with self.read() as connection:
            return connection.execute("SELECT version FROM schema_info").fetchone()[0]


def open_store(path, create=False):
    """The store at path, or None when it does not exist and create is False (reading never creates it)."""

    path = Path(path)

    if not create and not path.is_file():
        return None

    return IntegrationStore(path)
