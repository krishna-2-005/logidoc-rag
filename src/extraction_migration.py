"""
LogiDoc-RAG extraction store: the explicit schema migration

A running application never changes the schema of an existing extraction store.
Requests only read it (extraction_store.open_for_reading) or write to a store that is
already current (open_for_write); a store at an older schema answers "needs a schema
upgrade" until an operator runs this migration:

    python scripts/migrate_extraction_store.py --backup-dir <existing directory>
    python scripts/migrate_extraction_store.py --dry-run

What migrate() does, in order, and where it stops

    1. inspect     the file's schema (read-only). A missing, unreadable, newer or structurally
                   invalid file is refused; a current file needs nothing.
    2. preflight   PRAGMA integrity_check and the full correction-history verification
                   (hash chains, payload hashes, proposer/approver separation, aliases when
                   present). A failure blocks the migration. There is no override.
    3. baseline    row counts, ids and a content digest of every history table, the ids of
                   all corrections and of the approved ones, and both chain tips.
    4. status      the backup directory is checked (an operator error is not an attempt and
                   writes nothing), then a durable record "started" is written next to the
                   store (migration_status). If it cannot be written, nothing is changed.
    5. backup      a SQLite online backup (Connection.backup: consistent even while another
                   process writes) into the directory the operator names, under a new file
                   name; an existing file is never overwritten. The copy is opened and
                   verified (integrity, history, baseline) before anything is changed.
    6. migrate     ONE transaction (BEGIN IMMEDIATE): the schema version is read again under
                   the write lock; the baseline is recomputed and must equal the one the
                   backup was made from; every missing step is applied; the baseline of the
                   old rows must be unchanged, the history must still verify and (schema 5)
                   there must be one original alias per extraction; only then the version row
                   is replaced and the transaction committed. Any failure rolls the whole
                   transaction back, so the original schema is intact and the same command can
                   be run again.
    7. verify      the committed file is opened again and checked once more. Only when that
                   passes is "succeeded" recorded and success reported; a commit whose
                   verification fails is recorded as committed_unverified and reported as such.

The status record (docs/MIGRATION_STATUS_DESIGN.md) is advisory and the database is authoritative: the server
reconciles it with the schema, so a record can never make a store look healthier or sicker than the database says.
Failures are recorded with a fixed code, never a message, an exception name or a path.

Backups are the operator's responsibility: this module writes them and never deletes,
rotates or expires one. Choose where they live and how long to keep them.

Recorded audit runs live in another database; nothing here opens it.
"""

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import extraction_store as store
import migration_status as status
from extraction_store import ABSENT, CURRENT, UNAVAILABLE, ExtractionStore, StoreError, table_names, verify_connection
from migration_status import baseline, columns_of, differences, summary


class MigrationAborted(StoreError):
    """The migration did not change the store. .report says what was found and why it stopped."""

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def read_only(path):
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=30)


def integrity_of(connection):
    """(ok, detail) of PRAGMA integrity_check."""

    rows = [r[0] for r in connection.execute("PRAGMA integrity_check")]

    return rows == ["ok"], "ok" if rows == ["ok"] else "; ".join(rows[:5])


def destination_of(path, backup_dir, version):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = Path(path)

    return Path(backup_dir) / f"{path.stem}.pre-v{version}-to-v{store.SCHEMA_VERSION}-{stamp}{path.suffix or '.sqlite3'}"


def preflight(path, backup_dir=None):
    """
    Read-only report: the detected schema, the migration path, the backup destination, and the checks that decide
    whether the migration may start. Writes nothing and creates nothing. report["blocked"] is the reason it may not
    start (None when it may); report["needs_migration"] is False for a current store; report["blocked_code"] is the
    fixed status code of a history or integrity failure.
    """

    path = Path(path)
    found = ExtractionStore.inspect(path)
    report = {
        "store": str(path), "detected": {"state": found.state, "version": found.version, "reason": found.reason},
        "target_version": store.SCHEMA_VERSION, "migration_path": [], "needs_migration": False, "blocked": None, "blocked_code": None,
        "backup": {"directory": str(backup_dir) if backup_dir else None, "destination": None},
        "checks": {}, "baseline": None, "baseline_fingerprint": None,
        "migration_status": found.migration,
    }

    if found.state == ABSENT:
        report["blocked"] = "There is no extraction store to migrate."
    elif found.state == UNAVAILABLE:
        report["blocked"] = found.reason
    elif found.state == CURRENT:
        report["detected"]["reason"] = "The store is already at the current schema; nothing to do."
    else:
        report["needs_migration"] = True
        report["migration_path"] = list(range(found.version + 1, store.SCHEMA_VERSION + 1))

        if backup_dir:
            report["backup"]["destination"] = str(destination_of(path, backup_dir, found.version))

    if report["blocked"] or not report["needs_migration"]:
        return report

    try:
        with closing(read_only(path)) as connection:
            ok, detail = integrity_of(connection)
            history = verify_connection(connection)
            base = baseline(connection)
    except sqlite3.DatabaseError as error:
        report["blocked"] = f"The store could not be read for verification ({type(error).__name__})."

        return report

    report["checks"] = {"integrity": {"ok": ok, "detail": detail},
                        "history": {key: history[key] for key in ("ok", "corrections_checked", "decisions_checked", "extractions_checked",
                                                                     "unchained_legacy", "problems")}}
    report["baseline"] = summary(base)
    report["baseline_fingerprint"] = status.fingerprint(base)

    if not ok:
        report["blocked"] = "The database failed its integrity check; the migration will not run."
        report["blocked_code"] = "integrity_check_failed"
    elif not history["ok"]:
        report["blocked"] = "The correction history failed verification; the migration will not run."
        report["blocked_code"] = "history_verification_failed"

    return report


def make_backup(path, destination, expected):
    """
    A SQLite online backup of path into destination (a new file), verified before it is trusted: it opens, passes the
    integrity check and the history verification, and holds exactly the baseline the migration will start from.
    """

    destination = Path(destination)

    if destination.exists():
        raise StoreError("The backup file already exists; backups are never overwritten.")

    with closing(read_only(path)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)

    with closing(read_only(destination)) as copy:
        ok, detail = integrity_of(copy)
        history = verify_connection(copy)
        copied = baseline(copy, columns_of(expected))

    problems = []

    if not ok:
        problems.append(f"integrity check of the backup: {detail}")

    if not history["ok"]:
        problems.append("history verification of the backup failed")

    problems.extend(f"the backup differs from the store: {d}" for d in differences(expected, copied))

    if problems:
        raise StoreError("The backup could not be verified (" + "; ".join(problems) + "). It was kept for inspection and nothing was migrated.")


def migrate(path, backup_dir):
    """
    Upgrades the store at path to the current schema, as described in the module notes. Returns the report
    (status "migrated" or "already_current"). Raises MigrationAborted, with the report, when it stops: the store is
    then unchanged, except for "committed_unverified" (a commit whose verification failed; the report says so and the
    backup was kept). report["warnings"] lists what could not be recorded.
    """

    path = Path(path)
    report = preflight(path, backup_dir)
    report["warnings"] = []
    attempt = uuid.uuid4().hex
    state = {"from": report["detected"]["version"], "fingerprint": report["baseline_fingerprint"], "backup": None}

    def record(outcome, code=None):
        """Writes the status record; False (and a warning) when it cannot be written. Never raises."""

        if state["fingerprint"] is None:
            return False

        try:
            status.write(path, status.new_record(path, outcome, code, state["from"], store.SCHEMA_VERSION, state["fingerprint"],
                                                 attempt_id=attempt, backup_name=state["backup"]))

            return True
        except OSError:
            report["warnings"].append("The migration status could not be recorded; the store's state is still reported from the database.")

            return False

    def stop(message, code=None):
        report["status"] = "aborted"
        report["blocked"] = message

        if code:
            record(status.FAILED, code)

        raise MigrationAborted(message, report)

    if report["blocked"]:
        stop(report["blocked"], report["blocked_code"])

    if not report["needs_migration"]:
        report["status"] = "already_current"

        return report

    if not backup_dir:
        stop("A backup directory is required: nothing is migrated without a verified backup.")

    backup_dir = Path(backup_dir)

    if not backup_dir.is_dir():
        stop("The backup directory does not exist; create it first (it is the operator's choice and responsibility).")

    version = report["detected"]["version"]
    destination = Path(report["backup"]["destination"])

    with closing(read_only(path)) as connection:
        before = baseline(connection)

    state["fingerprint"] = status.fingerprint(before)
    state["backup"] = destination.name

    if not record(status.STARTED):
        report["warnings"].clear()
        stop("The migration status could not be recorded, so nothing was changed. Check that the folder of the store is writable.")

    try:
        make_backup(path, destination, before)
    except (StoreError, sqlite3.DatabaseError, OSError) as error:
        stop(str(error) if isinstance(error, StoreError) else f"The backup failed ({type(error).__name__}); nothing was migrated.", "backup_failed")

    report["backup"]["verified"] = True

    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    code = "rolled_back"

    try:
        connection.execute("BEGIN IMMEDIATE")

        # Everything below runs under the write lock: nobody else can change the store until commit or rollback
        locked = ExtractionStore.current_version(connection)

        if locked >= store.SCHEMA_VERSION:
            connection.rollback()
            report["status"] = "already_current"
            report["detected"]["reason"] = "Another process migrated the store first; nothing was applied."

            return report

        code = "store_changed"

        if locked != version:
            raise StoreError("The store changed after it was inspected; nothing was migrated. Run the command again.")

        found = differences(before, baseline(connection, columns_of(before)))

        if found:
            raise StoreError("The store changed between the backup and the migration (" + ", ".join(found) + "); the backup no longer "
                             "matches. Nothing was migrated. Run the command again.")

        code = "rolled_back"
        ExtractionStore.apply_migrations(connection, expected_from=version)
        code = "verification_failed"

        found = differences(before, baseline(connection, columns_of(before)))

        if found:
            raise StoreError("The migration would have changed existing history (" + ", ".join(found) + ").")

        history = verify_connection(connection)

        if not history["ok"]:
            raise StoreError("The history does not verify after the migration: " + "; ".join(history["problems"][:3]))

        if "extraction_aliases" in table_names(connection):
            originals = connection.execute("SELECT COUNT(*) FROM extraction_aliases WHERE relation = 'original'").fetchone()[0]
            extractions = connection.execute("SELECT COUNT(*) FROM extractions").fetchone()[0]

            if originals != extractions:
                raise StoreError(f"The alias backfill is incomplete ({originals} originals for {extractions} extractions).")

        connection.commit()
    except BaseException as error:
        connection.rollback()
        reason = (str(error) if isinstance(error, StoreError) else f"The migration failed ({type(error).__name__}).") + " The original schema is intact."

        connection.close()
        stop(reason, code)
    finally:
        connection.close()

    with closing(read_only(path)) as after:
        ok, detail = integrity_of(after)
        history = verify_connection(after)
        final = baseline(after, columns_of(before))
        latest = summary(baseline(after))

    unchanged = not differences(before, final)
    report["before"] = summary(before)
    report["after"] = {"version": store.SCHEMA_VERSION, "integrity": {"ok": ok, "detail": detail},
                       "history": {"ok": history["ok"], "problems": history["problems"], "aliases_checked": history["aliases_checked"]},
                       "baseline": latest, "history_unchanged": unchanged}

    if not (ok and history["ok"] and unchanged):
        # Committed, but not proven good: never reported as a success. The store answers "unavailable" until it verifies.
        report["status"] = "committed_unverified"
        report["blocked"] = "The migration was committed but the verification afterwards failed. Restore the backup or repair the store."
        record(status.UNVERIFIED, "post_verification_failed")

        raise MigrationAborted(report["blocked"], report)

    report["status"] = "migrated"
    record(status.SUCCEEDED)

    return report
