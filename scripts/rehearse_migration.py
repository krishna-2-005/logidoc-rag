"""
Rehearses the schema upgrades on a copy of the data, never on the data itself:
the review store (version 2 -> 3) and the audit history (version 3 -> 6), with
the application's own store classes, then checks the result.

    python scripts/rehearse_migration.py --source RESTORED_DATA --work NEW_FOLDER [--chroma RESTORED_INDEX]

RESTORED_DATA is a data folder restored from a backup (scripts/manage_backups.py
restore); it is only read. Everything is copied into NEW_FOLDER, which must not
exist yet, and upgraded there. The installation's own data folder, index and
databases are refused as source and as work folder, and so is any folder that
holds them.

The report (NEW_FOLDER/rehearsal-report.json, and the exit code: 0 when every
check passed) holds versions, counts, names and digests, never a row's content:
- the schema version of each database before and after;
- every table that existed before keeps exactly its rows (a digest of all rows
  over the columns the table had before; the review table is rebuilt by its
  upgrade, so this is what proves nothing was lost or changed);
- the tables, columns, indexes and triggers each upgrade adds, and that the
  append-only triggers are still there;
- PRAGMA integrity_check and foreign_key_check, before and after;
- the review history's hash chain (ReviewStore.verify_integrity);
- the copy the review upgrade keeps beside its database (<name>.before-v3.sqlite3):
  still version 2, with the rows as they were;
- opening the upgraded databases again changes nothing;
- the application on the upgraded copy (local mode, no sign-in, nothing
  started in the background): every read endpoint listed in API_ROUTES answers
  200, and every recorded snapshot reads back through the history store.
"""

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config  # noqa: E402

DATABASES = {
    "reviews": "audit_reviews.sqlite3",
    "history": "audit_history.sqlite3",
    "ingestion_register": "ingestion_register.sqlite3",
}

# Read endpoints the application must still answer on the upgraded copy
API_ROUTES = (
    "/api/health", "/api/overview", "/api/shipments", "/api/exceptions", "/api/exceptions/activity",
    "/api/carriers", "/api/completeness", "/api/exposure", "/api/documents", "/api/history/runs",
    "/api/trends", "/api/re-audits", "/api/risk/overview", "/api/risk/shipments",
    "/api/risk/trends", "/api/reporting/catalog", "/api/reporting/freight-audit-summary",
    "/api/reporting/discrepancies", "/api/reporting/carrier-performance", "/api/reporting/corrections",
    "/api/reports/exceptions.csv", "/api/ingestion/register",
)


class RehearsalError(Exception):
    pass


# ============================================================
# Where it may run
# ============================================================

def protected_paths():
    """The installation's own data: never a source or a work folder, never inside one."""

    places = [PROJECT_ROOT / "data", PROJECT_ROOT / "chroma_db", config.DATA_DIR, config.CHROMA_DIR,
              config.REVIEW_DB_PATH, config.HISTORY_DB_PATH, config.AUTH_DB_PATH, config.INGESTION_DB_PATH]

    return {Path(place).resolve() for place in places}


def overlaps(path, protected):

    path = Path(path).resolve()

    return any(path == place or place in path.parents or path in place.parents for place in protected)


def check_locations(source, work, chroma=None):

    protected = protected_paths()

    for name, path in (("source", source), ("work", work), ("chroma", chroma)):

        if path is not None and overlaps(path, protected):
            raise RehearsalError(f"The {name} folder is, or holds, the installation's own data; use a restored copy.")

    if not Path(source).is_dir():
        raise RehearsalError("The source folder does not exist.")

    if Path(work).exists():
        raise RehearsalError("The work folder already exists; give a new one (nothing is overwritten).")

    if overlaps(work, {Path(source).resolve()}):
        raise RehearsalError("The work folder must be outside the source folder.")


# ============================================================
# Facts about a database (read only)
# ============================================================

def read_only(path):

    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    return connection


def table_digest(connection, table, columns):
    """SHA-256 over every row's values in the given columns, in a fixed order (independent of rowids)."""

    quoted = ", ".join(f'"{column}"' for column in columns)
    rows = sorted(repr(tuple(row)) for row in connection.execute(f'SELECT {quoted} FROM "{table}"'))
    digest = hashlib.sha256()

    for row in rows:
        digest.update(row.encode("utf-8"))
        digest.update(b"\n")

    return len(rows), digest.hexdigest()


def database_facts(path):

    with closing(read_only(path)) as connection:

        objects = connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
        tables = {}

        for item in objects:

            if item["type"] != "table":
                continue

            columns = [row["name"] for row in connection.execute(f'PRAGMA table_info("{item["name"]}")')]
            rows, digest = table_digest(connection, item["name"], columns)
            references = sorted({row["table"] for row in connection.execute(f'PRAGMA foreign_key_list("{item["name"]}")')})
            tables[item["name"]] = {"columns": columns, "rows": rows, "digest": digest, "references": references}

        version = None

        if "schema_info" in tables:
            found = connection.execute("SELECT version FROM schema_info").fetchone()
            version = found[0] if found else None

        return {
            "version": version,
            "tables": tables,
            "indexes": sorted(item["name"] for item in objects if item["type"] == "index"),
            "triggers": sorted(item["name"] for item in objects if item["type"] == "trigger"),
            "integrity": [row[0] for row in connection.execute("PRAGMA integrity_check")],
            "foreign_key_problems": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        }


def file_digest(path):

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ============================================================
# The rehearsal
# ============================================================

def compare(before, after):
    """What an upgrade kept and added. kept: every table and column of before, with the same rows."""

    lost_tables = [name for name in before["tables"] if name not in after["tables"]]
    changed = []

    for name, table in before["tables"].items():

        # schema_info holds the version, which the upgrade changes on purpose (compared above)
        if name in lost_tables or name == "schema_info":
            continue

        missing = [column for column in table["columns"] if column not in after["tables"][name]["columns"]]

        if missing:
            changed.append(f"{name}: columns {missing} missing")
            continue

        if (table["rows"], table["digest"]) != after["tables"][name]["kept"]:
            changed.append(f"{name}: rows differ")

        if table["references"] != after["tables"][name]["references"]:
            changed.append(f"{name}: foreign keys differ")

    return {
        "version": [before["version"], after["version"]],
        "tables_lost": lost_tables,
        "tables_changed": changed,
        "tables_added": sorted(name for name in after["tables"] if name not in before["tables"]),
        "columns_added": {
            name: [column for column in after["tables"][name]["columns"] if column not in table["columns"]]
            for name, table in before["tables"].items()
            if name in after["tables"] and len(after["tables"][name]["columns"]) > len(table["columns"])
        },
        "indexes_lost": [name for name in before["indexes"] if name not in after["indexes"]],
        "indexes_added": [name for name in after["indexes"] if name not in before["indexes"]],
        "triggers_lost": [name for name in before["triggers"] if name not in after["triggers"]],
        "triggers_added": [name for name in after["triggers"] if name not in before["triggers"]],
    }


def facts_after(path, before):
    """The facts of the upgraded database, with each earlier table digested over its earlier columns."""

    after = database_facts(path)

    with closing(read_only(path)) as connection:

        for name, table in before["tables"].items():

            if name in after["tables"] and all(column in after["tables"][name]["columns"] for column in table["columns"]):
                after["tables"][name]["kept"] = table_digest(connection, name, table["columns"])

    return after


def summary(facts):
    """What the report keeps of a database's facts: no digest of content beyond what proves preservation."""

    return {
        "version": facts["version"],
        "rows": {name: table["rows"] for name, table in facts["tables"].items()},
        "integrity": facts["integrity"],
        "foreign_key_problems": facts["foreign_key_problems"],
        "triggers": facts["triggers"],
    }


def application_check(work_data, work_chroma):
    """The application on the upgraded copy, local mode: each read endpoint's status code."""

    from fastapi.testclient import TestClient

    from api.app import create_app
    from api.core import CLIENT_HEADER, ApiSettings, ApiState
    from audit_service import Locations
    from review_store import Policy

    raw = work_data / "raw"
    locations = Locations(
        raw, work_chroma, work_data / DATABASES["reviews"], work_data / DATABASES["history"],
        work_data / DATABASES["ingestion_register"], work_data / "quarantine", work_data / "archive",
    )
    settings = ApiSettings(environ={"LOGIDOC_AUTH_MODE": "local"})
    state = ApiState(settings, locations, work_data / "auth.sqlite3", Policy())
    client = TestClient(create_app(state), raise_server_exceptions=False, headers={CLIENT_HEADER: "web"})
    answers = {route: client.get(route).status_code for route in API_ROUTES}

    shipments = client.get("/api/shipments").json()
    sample = (shipments.get("shipments") or [{}])[0].get("shipment_id") if isinstance(shipments, dict) else None

    if sample:
        for route in ("/api/shipments/{id}", "/api/risk/shipments/{id}", "/api/shipments/{id}/re-audit",
                      "/api/shipments/{id}/freight-audit", "/api/shipments/{id}/tms", "/api/history/shipments/{id}"):
            answers[route] = client.get(route.format(id=sample)).status_code

    return answers


def history_readback(path):
    """Every recorded snapshot, read back through the upgraded history store."""

    from audit_history import HistoryStore

    store = HistoryStore(path)
    keys = [item["shipment_key"] if isinstance(item, dict) else item for item in store.shipments()]
    snapshots = sum(len(store.snapshots(key)) for key in keys)

    return {"shipments": len(keys), "snapshots": snapshots, "runs": len(store.runs(limit=100000))}


def rehearse(source, work, chroma=None):

    source, work = Path(source), Path(work)
    check_locations(source, work, chroma)

    work_data = work / "data"
    work_chroma = work / "chroma_db"
    shutil.copytree(source, work_data)

    if chroma:
        shutil.copytree(chroma, work_chroma)
    else:
        work_chroma.mkdir(parents=True)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_versions": {},
        "databases": {},
        "checks": {},
    }

    present = {name: work_data / file for name, file in DATABASES.items() if (work_data / file).is_file()}
    before = {name: database_facts(path) for name, path in present.items()}

    # The upgrades, with the application's own classes (exactly what the server does on start)
    from audit_history import SCHEMA_VERSION as HISTORY_VERSION
    from audit_history import HistoryStore
    from review_store import SCHEMA_VERSION as REVIEW_VERSION
    from review_store import Policy, ReviewStore

    report["target_versions"] = {"reviews": REVIEW_VERSION, "history": HISTORY_VERSION}
    upgraded = {}

    if "history" in present:
        HistoryStore(present["history"])
        upgraded["history"] = present["history"]

    if "reviews" in present:
        ReviewStore(present["reviews"], Policy())
        upgraded["reviews"] = present["reviews"]

    after = {name: facts_after(path, before[name]) for name, path in present.items()}
    checks = report["checks"]

    for name, path in present.items():

        changes = compare(before[name], after[name])
        report["databases"][name] = {"before": summary(before[name]), "after": summary(after[name]), "changes": changes}
        checks[f"{name}: every earlier table, column and row kept"] = not (changes["tables_lost"] or changes["tables_changed"])
        checks[f"{name}: no index or trigger lost"] = not (changes["indexes_lost"] or changes["triggers_lost"])
        checks[f"{name}: integrity ok"] = after[name]["integrity"] == ["ok"]
        checks[f"{name}: no foreign key problem"] = after[name]["foreign_key_problems"] == 0

    if "history" in upgraded:
        checks["history: at the current version"] = after["history"]["version"] == HISTORY_VERSION
        readback = history_readback(upgraded["history"])
        report["history_readback"] = readback
        checks["history: every snapshot reads back"] = (
            readback["snapshots"] == before["history"]["tables"].get("shipment_snapshots", {}).get("rows"))

    if "reviews" in upgraded:
        store = ReviewStore(upgraded["reviews"], Policy())
        chain = store.verify_integrity()
        report["review_chain"] = {key: chain[key] for key in ("ok", "checked", "legacy_events")}
        report["review_chain"]["problems"] = len(chain["problems"])
        checks["reviews: at the current version"] = after["reviews"]["version"] == REVIEW_VERSION
        checks["reviews: the event hash chain verifies"] = chain["ok"]
        checks["reviews: every review reads back"] = len(store.states()) == before["reviews"]["tables"]["exception_reviews"]["rows"]

        kept = upgraded["reviews"].with_name(upgraded["reviews"].stem + ".before-v3" + upgraded["reviews"].suffix)
        was_v2_with_rows = before["reviews"]["version"] == 2 and before["reviews"]["tables"]["exception_reviews"]["rows"]

        if was_v2_with_rows:
            copy = database_facts(kept) if kept.is_file() else None
            report["review_copy_before_upgrade"] = {"kept": kept.is_file(), "version": copy and copy["version"]}
            checks["reviews: the pre-upgrade copy is kept, at version 2, with the same rows"] = bool(
                copy and copy["version"] == 2 and copy["integrity"] == ["ok"]
                and all(copy["tables"][name]["digest"] == table["digest"] for name, table in before["reviews"]["tables"].items()))

    # Opening the upgraded databases again changes nothing
    digests = {name: file_digest(path) for name, path in upgraded.items()}

    if "history" in upgraded:
        HistoryStore(upgraded["history"])

    if "reviews" in upgraded:
        ReviewStore(upgraded["reviews"], Policy())

    checks["opening again changes nothing"] = digests == {name: file_digest(path) for name, path in upgraded.items()}

    # The application on the upgraded copy
    answers = application_check(work_data, work_chroma)
    report["application"] = answers
    checks["application: every read endpoint answers 200"] = all(code == 200 for code in answers.values())

    report["ok"] = all(checks.values())
    (work / "rehearsal-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    return report


def main(argv=None):

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", required=True, help="a restored data folder (only read)")
    parser.add_argument("--work", required=True, help="a new folder for the copy and its upgrade")
    parser.add_argument("--chroma", help="the restored index folder, copied beside the data (optional)")
    arguments = parser.parse_args(argv)

    try:
        report = rehearse(arguments.source, arguments.work, arguments.chroma)
    except RehearsalError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2

    for check, passed in report["checks"].items():
        print(f"{'PASS' if passed else 'FAIL'}  {check}")

    print("RESULT:", "PASS" if report["ok"] else "FAIL")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
