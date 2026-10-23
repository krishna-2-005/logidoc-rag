"""
Upgrade the extraction store to the current schema, explicitly.

The web application never changes an existing store's schema: until this command has run, an
older store answers "needs a schema upgrade" (reads of a valid schema 4 store keep working).

    python scripts/migrate_extraction_store.py --dry-run
    python scripts/migrate_extraction_store.py --backup-dir D:\\backups\\logidoc

    --store PATH         the extraction store (default: config, LOGIDOC_EXTRACTION_DB or
                         <data>/extractions.sqlite3)
    --backup-dir DIR     an existing directory for the backup. Required for a real run. The backup
                         is a SQLite online backup, verified before anything is changed, written
                         under a new file name and never overwritten.
    --dry-run            report the detected schema, the migration path, the backup destination and
                         the preflight result; write nothing

Backups are your responsibility: this command never deletes, rotates or expires one. Decide where
they live and how long to keep them. Stop the application first when you can; the migration is
safe against concurrent writers (one transaction under the write lock) but a quiet store is simpler.

A verification failure (integrity check, correction hash chains, payload hashes, proposer/approver
separation) blocks the migration. There is no override: repair or restore, then run it again.

Every attempt leaves a status record next to the store (<store>.migration-status.json) that the server reads, so
the outcome is visible across processes: started, failed (with a fixed code), succeeded, or committed_unverified.
The database stays authoritative; the record is advisory. Success is reported only after the commit and a passing
verification.

Exit status: 0 migrated, already current, or a clean dry run; 1 blocked, failed (the store is unchanged) or
committed without passing verification; 2 bad arguments.
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config
import extraction_migration as migration


def default_store():
    return config.ingestion_locations()["extractions"]


def show(report):
    detected = report["detected"]

    print(f"Store:            {report['store']}")
    print(f"Detected:         {detected['state']}" + (f" (schema {detected['version']})" if detected["version"] is not None else ""))
    print(f"                  {detected['reason']}")
    print(f"Target schema:    {report['target_version']}")

    if report["migration_path"]:
        print("Migration path:   " + " -> ".join(str(v) for v in [detected["version"], *report["migration_path"]]))

    if report["backup"]["destination"]:
        print(f"Backup file:      {report['backup']['destination']}")

    checks = report.get("checks") or {}

    if checks:
        history = checks["history"]
        print(f"Integrity check:  {'ok' if checks['integrity']['ok'] else 'FAILED: ' + checks['integrity']['detail']}")
        print(f"History check:    {'ok' if history['ok'] else 'FAILED'} ({history['corrections_checked']} corrections, "
              f"{history['decisions_checked']} decisions, {history['extractions_checked']} extractions)")

        for problem in history["problems"][:10]:
            print(f"                  - {problem}")

    if report.get("baseline"):
        base = report["baseline"]
        print(f"Baseline:         {base['counts']}, approved corrections {base['approved_corrections']}")

    recorded = report.get("migration_status")

    if recorded and recorded.get("attempt") not in (None, "none"):
        print(f"Last attempt:     {recorded['attempt']}" + (f" ({recorded['code']})" if recorded.get("code") else ""))

    if report.get("blocked"):
        print(f"BLOCKED:          {report['blocked']}")

    for warning in report.get("warnings", []):
        print(f"WARNING:          {warning}")

    if report.get("status") == "committed_unverified":
        after = report["after"]
        print(f"NOT VERIFIED:     committed, but integrity: {after['integrity']['ok']}; history verifies: {after['history']['ok']}; "
              f"history unchanged: {after['history_unchanged']}. The backup was kept. Do not rely on this store until it is repaired or restored.")

    if report.get("status") == "migrated":
        after = report["after"]
        print(f"Migrated:         schema {after['version']}; history unchanged: {after['history_unchanged']}; "
              f"history verifies: {after['history']['ok']}; integrity: {after['integrity']['ok']}")
        print("The backup was kept. You decide where it lives and when it is removed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit, backed-up upgrade of the extraction store.")
    parser.add_argument("--store", help="the extraction store (default: from config)")
    parser.add_argument("--backup-dir", help="an existing directory for the verified backup (required unless --dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    arguments = parser.parse_args(argv)

    store = Path(arguments.store) if arguments.store else default_store()

    if not arguments.dry_run and not arguments.backup_dir:
        print("A real run needs --backup-dir (or use --dry-run).", file=sys.stderr)

        return 2

    if arguments.dry_run:
        report = migration.preflight(store, arguments.backup_dir)
        report["status"] = "dry_run"
    else:
        try:
            report = migration.migrate(store, arguments.backup_dir)
        except migration.MigrationAborted as stopped:
            report = stopped.report

    if arguments.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        show(report)

    return 0 if report.get("status") in ("migrated", "already_current", "dry_run") and not (report.get("blocked") and report["status"] == "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
