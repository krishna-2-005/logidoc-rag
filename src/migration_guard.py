"""
The migration guard: the installation's review and history databases are
upgraded only when a fresh, verified backup holds their current contents
(docs/MIGRATION_REHEARSAL.md, section 6).

Both stores upgrade themselves when opened, and an upgrade cannot be undone. So:

- prepare(locations, backup_root), called when the API server or the
  Streamlit app starts, registers the installation's two database paths as
  guarded. If either needs an upgrade, it looks for a backup that passes
  backup.verify_backup (manifest, every checksum, each database copy's
  integrity and recorded contents) AND whose copy of every database to upgrade
  holds exactly the rows the live file holds now (a digest of every table,
  read-only): a backup made before the last change is stale. With one, the
  upgrade runs once, there and then, before anything is served; without one,
  MigrationBlocked says what to do and nothing is opened for writing.
- check(path, ...), called by ReviewStore and HistoryStore just before they
  upgrade, refuses an upgrade of a guarded path that prepare did not
  authorize: a request or a background job never upgrades the real databases.

Paths never registered (tests, rehearsal copies, restore drills) upgrade as
before. Nothing here makes a backup, and inspecting a database never writes
to it. Messages name settings and commands, never a path.
"""

import hashlib
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

STORES = {
    # store name in the backup manifest: (label, the module and class that upgrades it)
    "audit_reviews": "exception review database",
    "audit_history": "audit history database",
}

# The newest backups looked at (each is verified, so this bounds the work at startup)
MAX_CANDIDATES = 5

LOCK = threading.Lock()
GUARDED = set()
AUTHORIZED = set()


class MigrationBlocked(RuntimeError):
    """An upgrade of a real database was refused: no fresh, verified backup holds its current contents."""


@dataclass(frozen=True)
class Pending:

    store: str
    path: Path
    version: int
    target: int

    @property
    def label(self):

        return STORES[self.store]


def key(path):

    return os.path.normcase(str(Path(path).resolve()))


# ============================================================
# Reading a database without touching it
# ============================================================

def read_only(path):

    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def schema_version(path):
    """The schema version of an existing database, read-only (None when it has none)."""

    with closing(read_only(path)) as connection:
        try:
            row = connection.execute("SELECT version FROM schema_info").fetchone()
        except sqlite3.OperationalError:
            return None

    return row[0] if row else None


def contents_digest(path):
    """
    SHA-256 over every row of every table (sorted by table, rows in a fixed
    order): two files with the same digest hold the same data, whatever their
    page layout. Read-only.
    """

    digest = hashlib.sha256()

    with closing(read_only(path)) as connection:

        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            rows = sorted(repr(tuple(row)) for row in connection.execute(f"SELECT * FROM {quoted}"))
            digest.update(f"{table}\n{len(rows)}\n".encode("utf-8"))
            for row in rows:
                digest.update(row.encode("utf-8"))
                digest.update(b"\n")

    return digest.hexdigest()


def targets():

    import audit_history
    import review_store

    return {"audit_reviews": review_store.SCHEMA_VERSION, "audit_history": audit_history.SCHEMA_VERSION}


def pending(locations):
    """[Pending] for the installation's review and history databases that exist and are older than the code."""

    paths = {"audit_reviews": Path(locations.review_db), "audit_history": Path(locations.history_db)}
    wanted = targets()
    found = []

    for store, path in paths.items():

        if not path.is_file():
            continue  # created at the current version when first used: nothing to upgrade

        try:
            version = schema_version(path)
        except sqlite3.DatabaseError as error:
            raise MigrationBlocked(
                f"LogiDoc-RAG did not start: the {STORES[store]} cannot be read ({type(error).__name__}). "
                "Restore it from a backup (python scripts/manage_backups.py restore) before starting again."
            ) from None

        if version is not None and version < wanted[store]:
            found.append(Pending(store, path, version, wanted[store]))

    return found


# ============================================================
# Finding a fresh, verified backup
# ============================================================

def backup_for(items, backup_root, limit=MAX_CANDIDATES):
    """
    (backup_id, None) for the newest backup that verifies and holds the current
    contents of every database in items, else (None, the reason, in words).
    """

    import backup

    candidates = [item for item in backup.list_backups(backup_root) if item["usable"]][:limit]

    if not candidates:
        return None, "there is no complete backup in the backup folder (LOGIDOC_BACKUP_DIR)"

    live = {item.store: contents_digest(item.path) for item in items}
    reasons = []

    for candidate in candidates:

        folder = Path(backup_root) / candidate["backup_id"]
        result = backup.verify_backup(folder)

        if not result["ok"]:
            reasons.append(f"backup {candidate['backup_id']} fails verification ({', '.join(result['categories'])})")
            continue

        manifest, _ = backup.load_manifest(folder)
        recorded = ((manifest.get("components") or {}).get("databases") or {}).get("items", {})
        stale = []

        for item in items:
            entry = recorded.get(item.store) or {}
            copy = folder / entry["file"] if entry.get("present") and entry.get("file") else None

            if copy is None or not copy.is_file() or contents_digest(copy) != live[item.store]:
                stale.append(item.label)

        if stale:
            reasons.append(f"backup {candidate['backup_id']} does not hold the current {' and '.join(stale)} "
                           "(it was made before the last change)")
            continue

        return candidate["backup_id"], None

    return None, "; ".join(reasons)


def blocked_message(items, reason):

    upgrades = "; ".join(f"the {item.label} (schema version {item.version} to {item.target})" for item in items)

    return (
        f"LogiDoc-RAG did not start: {upgrades} must be upgraded, and an upgrade cannot be undone. It runs only "
        f"when a fresh, verified backup holds the current data, and none does: {reason}.\n"
        "To continue:\n"
        "  1. Stop every LogiDoc-RAG process using this data folder (API server, Streamlit app, scripts).\n"
        "  2. python scripts/manage_backups.py create    (makes and verifies a backup)\n"
        "  3. Optionally rehearse on a copy first: python scripts/manage_backups.py restore <backup id> "
        "--target <new folder>, then python scripts/rehearse_migration.py (docs/MIGRATION_REHEARSAL.md).\n"
        "  4. Start LogiDoc-RAG again: the upgrade then runs once, at startup."
    )


# ============================================================
# Startup and the stores' check
# ============================================================

def prepare(locations, backup_root, upgrade=True):
    """
    At startup: registers the installation's review and history databases as
    guarded, and when either needs an upgrade, authorizes and runs it if a
    fresh, verified backup exists, or raises MigrationBlocked. Returns
    {"pending": [...], "backup_id", "upgraded": [...]}.
    """

    paths = (Path(locations.review_db), Path(locations.history_db))

    with LOCK:
        GUARDED.update(key(path) for path in paths)

    items = pending(locations)

    if not items:
        return {"pending": [], "backup_id": None, "upgraded": []}

    backup_id, reason = backup_for(items, backup_root)

    if backup_id is None:
        raise MigrationBlocked(blocked_message(items, reason))

    with LOCK:
        AUTHORIZED.update(key(item.path) for item in items)

    upgraded = []

    if upgrade:

        from audit_history import HistoryStore
        from review_store import ReviewStore

        for item in items:
            (ReviewStore if item.store == "audit_reviews" else HistoryStore)(item.path)
            upgraded.append(item.store)

    return {"pending": [f"{item.store} {item.version}->{item.target}" for item in items], "backup_id": backup_id,
            "upgraded": upgraded}


def check(path, store, target):
    """
    Called by a store before it opens an existing file for writing: when the
    path is guarded and prepare did not authorize its upgrade, the version is
    read (read-only) and MigrationBlocked raised if the file is older than
    target. A new file, or one at the current version, passes.
    """

    with LOCK:
        guarded = key(path) in GUARDED and key(path) not in AUTHORIZED

    if not guarded or not Path(path).is_file():
        return

    version = schema_version(path)

    if version is not None and version < target:
        raise MigrationBlocked(blocked_message(
            [Pending(store, Path(path), version, target)],
            "it was not checked when LogiDoc-RAG started (only a start may upgrade it)",
        ))


def reset():
    """Forget every registration (the tests)."""

    with LOCK:
        GUARDED.clear()
        AUTHORIZED.clear()
