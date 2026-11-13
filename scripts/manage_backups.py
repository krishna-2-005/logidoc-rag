"""
LogiDoc-RAG backups from the command line (docs/AUTOMATED_BACKUP_OPERATIONS.md).

    python scripts/manage_backups.py create [--label TEXT] [--require-index]
    python scripts/manage_backups.py list
    python scripts/manage_backups.py status
    python scripts/manage_backups.py verify BACKUP_ID
    python scripts/manage_backups.py drill BACKUP_ID        (restore into an isolated folder, check, remove)
    python scripts/manage_backups.py restore BACKUP_ID --target NEW_FOLDER [--skip-index-open]
    python scripts/manage_backups.py prune                  (shows what retention would remove)
    python scripts/manage_backups.py prune --apply          (removes it: scheduled backups only)
    python scripts/manage_backups.py activate --restored FOLDER --backup-id ID --confirm ID

The folders come from the same settings as the server (LOGIDOC_DATA_DIR,
LOGIDOC_CHROMA_DIR, LOGIDOC_BACKUP_DIR, ...; config.py).

create    When the API server is stopped, this command holds the server's
          data-folder lock, so nothing can write while it copies: the backup
          includes the vector index. While the server runs, the databases and
          documents are still copied consistently but the index is left out
          (the manifest says why; --require-index fails instead). The server's
          own backups (schedule, POST /api/admin/backups) always include it.
restore   Always into a new or empty folder, never over live data; prints the
          restore report (also written to the folder). Point the application
          at the result with LOGIDOC_DATA_DIR=<folder>/data and
          LOGIDOC_CHROMA_DIR=<folder>/chroma_db.
activate  Replaces the live data with a validated restore: needs the backup
          ID typed twice and the server stopped; the live folders are renamed
          aside (.pre-restore-<time>), never deleted.

Exit status: 0 done, 1 failed or refused, 2 bad arguments.
"""

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import backup  # noqa: E402
import backup_automation  # noqa: E402
import config  # noqa: E402
from audit_service import Locations  # noqa: E402
from file_lock import FileLock, LockBusy  # noqa: E402


def settings():

    return backup_automation.BackupSettings.from_env(os.environ, config.BACKUP_DIR)


def sources():

    return backup.Sources.from_locations(Locations.from_config(config), config.AUTH_DB_PATH)


def show(value):

    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def create(arguments):

    chosen = settings()
    lock = FileLock(backup.server_lock_path(config.DATA_DIR))

    try:
        lock.acquire()
        quiesced, reason = True, None
    except LockBusy:
        quiesced = False
        reason = ("The API server was running: the vector index was left out. Make a full backup from the server "
                  "(its schedule or POST /api/admin/backups) or with the server stopped.")

    if not quiesced and arguments.require_index:
        print("The API server is running; a backup with the vector index needs it stopped (or use the server's "
              "own backup).", file=sys.stderr)
        return 1

    @contextmanager
    def held():
        yield  # the data-folder lock is already held for the whole run

    runner = backup_automation.BackupRunner(
        sources(), chosen, quiesce=held if quiesced else None, include_index=quiesced, index_skip_reason=reason,
        configuration=backup_automation.recovery_configuration(config, os.environ),
        application=backup.application_version(ROOT),
    )

    try:
        result = runner.run(backup.MANUAL, arguments.label)
    except backup.BackupBusy as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        lock.release()

    show(result)

    return 0 if result["outcome"] == "success" else 1


def list_command(arguments):

    show(backup.list_backups(settings().root))

    return 0


def status(arguments):

    chosen = settings()
    show({"schedule": chosen.schedule.describe(), "status": backup_automation.read_status(chosen.root),
          "retention": vars(chosen.retention)})

    return 0


def verify(arguments):

    return check(arguments, drill=False)


def drill(arguments):

    return check(arguments, drill=True)


def check(arguments, drill):
    """Verify (and with drill, restore into an isolated folder and check); the result joins the history."""

    runner = backup_automation.BackupRunner(sources(), settings())

    try:
        results = runner.verify(arguments.backup_id, drill=drill, origin=backup.MANUAL)
    except (backup.RestoreRefused, backup.BackupBusy) as error:
        print(str(error), file=sys.stderr)
        return 1

    show(results if drill else results[0])

    return 0 if all(result["ok"] for result in results) else 1


def restore(arguments):

    chosen = settings()
    live = sources()

    try:
        folder = backup.find_backup(chosen.root, arguments.backup_id)
        report = backup.restore_backup(
            folder, Path(arguments.target),
            protected=[config.DATA_DIR, live.raw_dir, live.chroma_dir, chosen.root, ROOT],
            open_index=not arguments.skip_index_open, collection_name=config.COLLECTION_NAME,
        )
    except backup.RestoreRefused as error:
        print(f"Restore refused: {error}", file=sys.stderr)
        return 1

    show(report)

    if report["status"] == "restored":
        print(f"\nRestored into {arguments.target}. To use it, start the application with "
              f"LOGIDOC_DATA_DIR={Path(arguments.target) / 'data'} and "
              f"LOGIDOC_CHROMA_DIR={Path(arguments.target) / 'chroma_db'}.")

    return 0 if report["status"] == "restored" else 1


def prune(arguments):

    chosen = settings()
    plan = backup_automation.apply_retention(chosen.root, chosen.retention, dry_run=not arguments.apply)
    show(plan)

    if not arguments.apply and plan["remove"]:
        print("\nNothing was removed. Run again with --apply to remove the backups listed under remove.")

    return 0


def activate(arguments):

    try:
        result = backup.activate_restore(
            Path(arguments.restored), sources(), backup_id=arguments.backup_id, confirm=arguments.confirm,
            server_lock=backup.server_lock_path(config.DATA_DIR), refuse_inside=ROOT,
        )
    except backup.RestoreRefused as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1

    show(result)

    return 0


def main(argv=None):

    parser = argparse.ArgumentParser(description="LogiDoc-RAG backups: create, verify, restore, retention.")
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("create", help="make a verified backup now")
    command.add_argument("--label", help="a short note kept in the manifest (80 characters)")
    command.add_argument("--require-index", action="store_true",
                         help="fail instead of leaving the vector index out when the server is running")
    command.set_defaults(run=create)

    commands.add_parser("list", help="list the backups, newest first").set_defaults(run=list_command)
    commands.add_parser("status", help="the schedule and the last attempts").set_defaults(run=status)

    command = commands.add_parser("verify", help="check a backup's manifest, checksums and databases")
    command.add_argument("backup_id")
    command.set_defaults(run=verify)

    command = commands.add_parser("drill", help="verify, restore into an isolated folder, check it, remove the copy")
    command.add_argument("backup_id")
    command.set_defaults(run=drill)

    command = commands.add_parser("restore", help="restore a backup into a new folder and validate it")
    command.add_argument("backup_id")
    command.add_argument("--target", required=True, help="a new or empty folder (never the live data)")
    command.add_argument("--skip-index-open", action="store_true",
                         help="do not open the restored vector index with ChromaDB")
    command.set_defaults(run=restore)

    command = commands.add_parser("prune", help="apply the retention policy (a preview without --apply)")
    command.add_argument("--apply", action="store_true", help="remove the scheduled backups the policy lets go")
    command.set_defaults(run=prune)

    command = commands.add_parser("activate", help="replace the live data with a validated restore")
    command.add_argument("--restored", required=True, help="the folder a restore wrote")
    command.add_argument("--backup-id", required=True)
    command.add_argument("--confirm", required=True, help="the backup ID again, to confirm")
    command.set_defaults(run=activate)

    arguments = parser.parse_args(argv)

    return arguments.run(arguments)


if __name__ == "__main__":
    sys.exit(main())
