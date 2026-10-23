"""
Explicit migration and schema 5 aliases (docs/SCHEMA_V5_ALIAS_DESIGN.md). Synthetic temporary databases only.

    command     a verified backup first, history verified first (no override), one transaction, retry-safe
    v4 to v5    approvals, pending and rejected corrections, hash chains, ids and recorded runs are unchanged
    aliases     one original per extraction, same-type names recorded once, type conflicts recorded then refused (409),
                none of it reaches the audit
    requests    GET and write requests never migrate; a store awaiting its migration answers 503
"""

import hashlib
import importlib
import inspect
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS.parent

for folder in (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts", TESTS):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import audit_history  # noqa: E402,F401
import document_extraction as de  # noqa: E402
import extraction_migration as migration  # noqa: E402
import extraction_store as store_module  # noqa: E402
from extraction_store import (  # noqa: E402
    CURRENT, MIGRATION_PENDING, UNAVAILABLE, AliasTypeConflict, ExtractionStore, MigrationPending, StoreUnavailable,
)
from migration_support import store_at, upgrade  # noqa: E402
from test_correction_aware_audit import ALICE, POD_A, extract, record_run, rig, v3_store  # noqa: E402,F401
from test_parser_integrity_gaps import two_names  # noqa: E402
from test_store_states import current_with_approval, digest, shape, v4_file, v4_world  # noqa: E402,F401

import migrate_extraction_store as cli  # noqa: E402


def backups(tmp_path):
    folder = Path(tmp_path) / "backups"
    folder.mkdir(exist_ok=True)

    return folder


def listing(folder):
    return sorted(p.name for p in Path(folder).iterdir())


def rows(path, sql):
    connection = sqlite3.connect(path)

    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


# =========================================================================
# schema 5
# =========================================================================

def test_a_fresh_database_is_schema_5_with_its_alias_table_and_protections(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    store = ExtractionStore(path)

    assert store.version() == 5 and ExtractionStore.inspect(path).state == CURRENT
    assert {r[0] for r in rows(path, "SELECT name FROM sqlite_master WHERE type = 'table'")} >= {"extraction_aliases"}
    assert {r[0] for r in rows(path, "SELECT name FROM sqlite_master WHERE type = 'trigger'")} >= set(store_module.REQUIRED_TRIGGERS_V5)
    assert store.verify_history()["ok"] and store.aliases(1) == []


def test_the_extraction_unique_key_is_unchanged(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    ExtractionStore(path)
    sql = rows(path, "SELECT sql FROM sqlite_master WHERE name = 'extractions'")[0][0]

    assert "UNIQUE (sha256, extractor_version)" in sql


def test_the_v5_script_is_split_by_the_existing_statement_parser_into_complete_statements(tmp_path):
    parts = store_module.statements(store_module.MIGRATIONS[5])

    assert len(parts) == 9 and all(p.startswith(("CREATE", "INSERT")) for p in parts)
    assert sum(1 for p in parts if p.startswith("CREATE TRIGGER")) == 4 and all(p.rstrip().endswith("END") for p in parts if "TRIGGER" in p)


def alias_row(path, **over):
    row = {"extraction_id": 1, "sha256": "h", "extractor_version": "1", "filename": "b.pdf", "inferred_type": "POD", "relation": "same_type",
           "first_seen_at": "now", "seen_by": None, **over}
    connection = sqlite3.connect(path)

    try:
        connection.execute(f"INSERT INTO extraction_aliases ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def one_row(tmp_path):
    path = tmp_path / "x.sqlite3"
    ExtractionStore(path)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, payload, payload_sha256, created_at) "
                       "VALUES ('a.pdf', 'h', '1', 'POD', 'extracted', '{}', 'x', 'then')")
    connection.execute("INSERT INTO extraction_aliases (extraction_id, sha256, extractor_version, filename, inferred_type, relation, first_seen_at) "
                       "VALUES (1, 'h', '1', 'a.pdf', 'POD', 'original', 'then')")
    connection.commit()
    connection.close()

    return path


def test_aliases_are_append_only_and_checked_by_the_database(one_row):
    for sql in ("UPDATE extraction_aliases SET filename = 'z.pdf'", "DELETE FROM extraction_aliases"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection = sqlite3.connect(one_row)
            try:
                connection.execute(sql)
            finally:
                connection.close()

    with pytest.raises(sqlite3.IntegrityError):                                  # the same name twice
        alias_row(one_row, filename="a.pdf")

    with pytest.raises(sqlite3.DatabaseError, match="content hash and extractor version"):
        alias_row(one_row, sha256="other")

    with pytest.raises(sqlite3.DatabaseError, match="relation must agree"):
        alias_row(one_row, relation="type_conflict", inferred_type="POD")       # a conflict with the same type

    with pytest.raises(sqlite3.DatabaseError, match="relation must agree"):
        alias_row(one_row, relation="same_type", inferred_type="BOL")           # same type, but another type

    with pytest.raises(sqlite3.IntegrityError):                                  # a second original for one extraction
        alias_row(one_row, filename="c.pdf", relation="original")

    with pytest.raises(sqlite3.IntegrityError):
        alias_row(one_row, filename="d.pdf", relation="whatever")


# =========================================================================
# the migration: v4 to v5
# =========================================================================

def snapshot_of(store):
    return {"corrections": [(c["id"], c["status"], c["corrected"]) for c in store.corrections(1)],
            "approved": [c["correction_id"] for c in store.approved_corrections("SHJ2026001")],
            "history": store.verify_history()}


def test_a_v4_store_with_approved_pending_and_rejected_corrections_migrates_without_changing_any_of_them(rig, v4_world, tmp_path):
    pod, approved, rejected, pending, path = v4_world
    old = ExtractionStore.open_for_reading(path)
    before = snapshot_of(old)
    token_before = rig.state.service.corrections_token()

    assert [c[1] for c in before["corrections"]] == ["approved", "rejected", "pending"] and before["history"]["ok"]

    report = upgrade(path, backups(tmp_path))
    new = ExtractionStore(path)
    after = snapshot_of(new)

    assert report["status"] == "migrated" and report["after"]["version"] == 5 and report["after"]["history_unchanged"] is True
    assert after["corrections"] == before["corrections"] and after["approved"] == before["approved"] == [approved["id"]]
    assert after["history"]["ok"] and after["history"]["corrections_checked"] == before["history"]["corrections_checked"] == 3
    assert after["history"]["decisions_checked"] == before["history"]["decisions_checked"] == 2
    assert report["before"]["correction_chain_tip"] == report["after"]["baseline"]["correction_chain_tip"] is not None
    assert report["before"]["decision_chain_tip"] == report["after"]["baseline"]["decision_chain_tip"]
    assert [r["id"] for r in new.list()] == [pod] and new.row(pod)["filename"] == POD_A     # the extraction id and name are unchanged
    assert [(a["filename"], a["relation"], a["seen_by"]) for a in new.aliases(pod)] == [(POD_A, "original", None)]
    assert rig.state.service.corrections_token() == token_before                             # nothing the audit reads changed

    rig.state.service.invalidate()
    field = next(f for f in rig.state.service.shipment("SHJ-2026-001")["inputs"]["fields"] if f["field"] == "delivered_pieces")

    assert field["source"] == "approved_correction" and field["value"] == 48


def test_the_pending_proposal_can_still_be_decided_after_the_migration(rig, v4_world, tmp_path):
    _, approved, rejected, pending, path = v4_world
    upgrade(path, backups(tmp_path))

    outcome = ExtractionStore(path).decide(ALICE, pending["id"], "approve", "Checked against the paper POD.")

    assert outcome["decision"] == "approved" and ExtractionStore(path).verify_history()["ok"]


def test_the_backup_is_a_verified_consistent_copy_in_the_named_folder_and_is_never_removed(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world
    folder = backups(tmp_path)
    original = digest(path)

    report = upgrade(path, folder)
    copies = listing(folder)

    assert len(copies) == 1 and copies[0].startswith("extractions.pre-v4-to-v5-") and report["backup"]["verified"] is True
    assert str(folder) in report["backup"]["destination"] and Path(report["backup"]["destination"]).name == copies[0]
    assert shape(folder / copies[0])[0] == 4 and digest(path) != original                    # the copy is the v4 file; the store moved on
    copy = ExtractionStore.open_for_reading(folder / copies[0])

    assert copy is not None and copy.verify_history()["ok"] and [c["correction_id"] for c in copy.approved_corrections("SHJ2026001")]
    assert not any(hasattr(migration, name) for name in ("prune", "rotate", "delete_backup", "expire"))


def test_legacy_schemas_need_the_explicit_migration_and_keep_their_legacy_corrections(tmp_path):
    for version in (1, 2, 3):
        path = store_at(tmp_path / f"v{version}.sqlite3", version)
        before = digest(path)

        with pytest.raises(MigrationPending):
            ExtractionStore(path)

        assert digest(path) == before

        report = upgrade(path, backups(tmp_path))

        assert report["status"] == "migrated" and report["migration_path"] == list(range(version + 1, 6))
        assert ExtractionStore(path).version() == 5

    legacy = tmp_path / "legacy.sqlite3"
    v3_store(legacy)
    upgrade(legacy, backups(tmp_path))
    store = ExtractionStore(legacy)

    assert store.corrections(1)[0]["status"] == "legacy" and store.approved_corrections("SHJ2026001") == [] and store.verify_history()["ok"]


def test_migrating_a_current_store_changes_nothing_and_writes_no_backup(rig, tmp_path):
    _, _, _, path = current_with_approval(rig)
    before, folder = digest(path), backups(tmp_path)

    report = upgrade(path, folder)

    assert report["status"] == "already_current" and digest(path) == before and listing(folder) == []


def test_recorded_audit_history_is_byte_for_byte_unchanged(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world
    run = record_run(rig)
    history = rig.locations.history_db
    before = digest(history)
    runs = rig.state.service.history_store().runs()

    upgrade(path, backups(tmp_path))
    rig.state.service.invalidate()

    assert run and digest(history) == before and rig.state.service.history_store().runs() == runs


def test_an_extractor_version_change_after_the_migration_adds_a_row_and_an_alias_and_keeps_the_old_ones(rig, v4_world, tmp_path, monkeypatch):
    pod, _, _, _, path = v4_world
    upgrade(path, backups(tmp_path))
    monkeypatch.setattr(de, "EXTRACTOR_VERSION", "1.1+newparser")

    again = extract(rig, POD_A)
    store = ExtractionStore(path)

    assert again != pod and [r["id"] for r in store.list()] == [again, pod]
    assert [a["relation"] for a in store.aliases(pod)] == ["original"] and [a["relation"] for a in store.aliases(again)] == ["original"]
    assert store.verify_history()["ok"]


# =========================================================================
# blocked: nothing is changed, and there is no override
# =========================================================================

def test_the_command_has_no_override_and_blocks_before_any_backup_when_the_history_fails(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER field_corrections_no_update")
    connection.execute("UPDATE field_corrections SET corrected = '99' WHERE id = 1")                  # an edit the chain detects
    connection.commit()
    connection.close()
    before, folder = digest(path), backups(tmp_path)

    with pytest.raises(migration.MigrationAborted, match="failed verification") as stopped:
        upgrade(path, folder)

    assert set(inspect.signature(migration.migrate).parameters) == {"path", "backup_dir"}            # nothing to force it with
    assert not stopped.value.report["checks"]["history"]["ok"] and any("hash" in p for p in stopped.value.report["checks"]["history"]["problems"])
    assert digest(path) == before and shape(path)[0] == 4 and listing(folder) == []                 # unchanged, and no backup was made


def test_a_corrupt_missing_or_newer_store_is_refused_and_nothing_is_created(tmp_path):
    folder = backups(tmp_path)
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a database" * 50)
    newer = tmp_path / "newer.sqlite3"
    ExtractionStore(newer)
    with sqlite3.connect(newer) as connection:
        connection.execute("UPDATE schema_info SET version = 99")
    seen = {}

    for name, path in (("corrupt", corrupt), ("newer", newer), ("missing", tmp_path / "none.sqlite3")):
        before = digest(path) if path.exists() else None

        with pytest.raises(migration.MigrationAborted) as stopped:
            upgrade(path, folder)

        seen[name] = stopped.value.report["blocked"]

        assert (digest(path) if path.exists() else None) == before and listing(folder) == []

    assert len(set(seen.values())) == 3 and "newer version" in seen["newer"] and "no extraction store" in seen["missing"].lower()


def test_a_backup_directory_is_required_and_must_exist(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world
    before = digest(path)

    with pytest.raises(migration.MigrationAborted, match="backup directory is required"):
        migration.migrate(path, None)

    with pytest.raises(migration.MigrationAborted, match="does not exist"):
        migration.migrate(path, tmp_path / "nowhere")

    assert digest(path) == before


def test_an_existing_backup_file_is_never_overwritten(rig, v4_world, tmp_path, monkeypatch):
    _, _, _, _, path = v4_world
    folder = backups(tmp_path)
    taken = folder / "taken.sqlite3"
    taken.write_bytes(b"precious")
    monkeypatch.setattr(migration, "destination_of", lambda *a: taken)
    before = digest(path)

    with pytest.raises(migration.MigrationAborted, match="never overwritten"):
        migration.migrate(path, folder)

    assert taken.read_bytes() == b"precious" and digest(path) == before


def test_a_write_between_the_backup_and_the_migration_stops_it(rig, v4_world, tmp_path, monkeypatch):
    _, _, _, _, path = v4_world
    original = migration.make_backup

    def backup_then_a_writer_arrives(*args):
        original(*args)
        connection = sqlite3.connect(path)
        connection.execute("INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, payload, payload_sha256, created_at) "
                           "VALUES ('late.pdf', 'z', '1', 'POD', 'extracted', '{}', 'x', 'now')")
        connection.commit()
        connection.close()

    monkeypatch.setattr(migration, "make_backup", backup_then_a_writer_arrives)

    with pytest.raises(migration.MigrationAborted, match="changed between the backup and the migration"):
        upgrade(path, backups(tmp_path))

    assert shape(path)[0] == 4 and ExtractionStore.inspect(path).state != CURRENT and len(rows(path, "SELECT id FROM extractions")) == 2


# =========================================================================
# interrupted migration and retry
# =========================================================================

def abort_and_check(path, tmp_path, match):
    before = digest(path)

    with pytest.raises(migration.MigrationAborted, match=match) as stopped:
        upgrade(path, backups(tmp_path))

    assert digest(path) == before and shape(path)[0] == 4
    assert not rows(path, "SELECT name FROM sqlite_master WHERE name = 'extraction_aliases'")        # no partial schema
    assert "original schema is intact" in str(stopped.value)

    return stopped.value.report


def test_a_failure_after_the_backfill_rolls_everything_back_and_the_retry_succeeds(rig, v4_world, tmp_path, monkeypatch):
    _, approved, _, _, path = v4_world
    good = dict(store_module.MIGRATIONS)
    monkeypatch.setattr(store_module, "MIGRATIONS", {**good, 5: good[5] + "\nTHIS IS NOT SQL;"})

    report = abort_and_check(path, tmp_path, "intact")
    store = ExtractionStore.open_for_reading(path)

    assert report["status"] == "aborted" and store is not None and [c["correction_id"] for c in store.approved_corrections("SHJ2026001")] == [approved["id"]]
    assert ExtractionStore.inspect(path).state == store_module.MIGRATION_FAILED and rig.state.service.corrections_available() is True

    monkeypatch.setattr(store_module, "MIGRATIONS", good)

    assert upgrade(path, backups(tmp_path))["status"] == "migrated" and ExtractionStore.inspect(path).state == CURRENT


def test_a_failure_in_the_verification_inside_the_transaction_rolls_back(rig, v4_world, tmp_path, monkeypatch):
    _, _, _, _, path = v4_world
    original, calls = migration.verify_connection, []

    def verify(connection):
        calls.append(1)

        return {**original(connection), "ok": False, "problems": ["injected"]} if len(calls) == 3 else original(connection)

    monkeypatch.setattr(migration, "verify_connection", verify)
    abort_and_check(path, tmp_path, "does not verify")

    monkeypatch.setattr(migration, "verify_connection", original)

    assert upgrade(path, backups(tmp_path))["status"] == "migrated"


def test_an_incomplete_backfill_is_detected_and_rolled_back(rig, v4_world, tmp_path, monkeypatch):
    _, _, _, _, path = v4_world
    good = dict(store_module.MIGRATIONS)
    without_backfill = good[5][: good[5].index("INSERT OR IGNORE")]
    monkeypatch.setattr(store_module, "MIGRATIONS", {**good, 5: without_backfill})

    abort_and_check(path, tmp_path, "original aliases|backfill is incomplete")        # the history check and the count check both see it


def test_a_half_applied_file_made_outside_the_tool_migrates_idempotently(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world
    connection = sqlite3.connect(path)

    for statement in store_module.statements(store_module.MIGRATIONS[5])[:2]:                    # the table and an index, version still 4
        connection.execute(statement)

    connection.commit()
    connection.close()

    assert ExtractionStore.inspect(path).state == MIGRATION_PENDING
    assert upgrade(path, backups(tmp_path))["status"] == "migrated" and ExtractionStore(path).verify_history()["ok"]


def test_a_v5_file_without_its_alias_table_is_unavailable_not_current(tmp_path):
    path = tmp_path / "x.sqlite3"
    ExtractionStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE extraction_aliases")

    assert ExtractionStore.inspect(path).state == UNAVAILABLE

    with pytest.raises(StoreUnavailable):
        ExtractionStore.open_for_reading(path)


def test_concurrent_migrations_apply_once_and_both_report_a_consistent_result(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world
    results, errors = [], []

    def run(index):
        try:
            folder = tmp_path / f"b{index}"
            folder.mkdir()
            results.append(migration.migrate(path, folder)["status"])
        except Exception as error:  # noqa: BLE001 - any failure is the finding
            errors.append(repr(error))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    store = ExtractionStore(path)

    assert errors == [] and sorted(results).count("migrated") == 1 and set(results) <= {"migrated", "already_current"}
    assert store.version() == 5 and store.verify_history()["ok"] and [a["relation"] for a in store.aliases(1)] == ["original"]


# =========================================================================
# aliases
# =========================================================================

def named(tmp_path, name):
    if not (Path(tmp_path) / "same.pdf").exists():
        two_names(tmp_path)                   # writes same.pdf once: content that the file name alone classifies

    return de.extract_document(Path(tmp_path) / "same.pdf", filename=name)


def test_the_same_content_under_the_same_name_is_recorded_once(tmp_path):
    store = ExtractionStore(tmp_path / "s.sqlite3")
    first = store.save_with_alias(named(tmp_path, "BOL_a.pdf"), actor="alice")
    again = store.save_with_alias(named(tmp_path, "BOL_a.pdf"), actor="bob")

    assert first["created"] and first["alias"] == {"filename": "BOL_a.pdf", "relation": "original", "created": True}
    assert again["id"] == first["id"] and not again["created"] and again["alias"] == {"filename": "BOL_a.pdf", "relation": "original", "created": False}
    assert len(store.aliases(first["id"])) == 1 and store.save(named(tmp_path, "BOL_a.pdf")) == (first["id"], False)


def test_another_name_of_the_same_type_is_recorded_once_with_who_saw_it(tmp_path):
    store = ExtractionStore(tmp_path / "s.sqlite3")
    first = store.save_with_alias(named(tmp_path, "BOL_a.pdf"), actor="alice")
    second = store.save_with_alias(named(tmp_path, "BOL_b.pdf"), actor="bob")
    repeat = store.save_with_alias(named(tmp_path, "BOL_b.pdf"), actor="carol")

    assert second["id"] == first["id"] and second["alias"] == {"filename": "BOL_b.pdf", "relation": "same_type", "created": True}
    assert repeat["alias"]["created"] is False and repeat["alias"]["relation"] == "same_type"
    aliases = store.aliases(first["id"])

    assert [(a["filename"], a["relation"], a["seen_by"]) for a in aliases] == [("BOL_a.pdf", "original", "alice"), ("BOL_b.pdf", "same_type", "bob")]
    assert len(store.list()) == 1 and store.alias_counts(first["id"]) == (2, False) and store.verify_history()["ok"]


def test_a_conflicting_type_is_recorded_durably_then_refused_and_the_stored_type_never_changes(tmp_path):
    store = ExtractionStore(tmp_path / "s.sqlite3")
    first, second = named(tmp_path, "BOL_a.pdf"), named(tmp_path, "INVOICE_b.pdf")
    saved = store.save_with_alias(first, actor="alice")

    assert first.document_type != second.document_type

    with pytest.raises(AliasTypeConflict) as refused:
        store.save_with_alias(second, actor="bob")

    assert refused.value.existing_id == saved["id"] and refused.value.existing_type == first.document_type
    recorded = store.aliases(saved["id"])                                       # the refusal rolled nothing back: the record is committed

    assert [(a["filename"], a["relation"], a["inferred_type"], a["seen_by"]) for a in recorded] == [
        ("BOL_a.pdf", "original", first.document_type, "alice"), ("INVOICE_b.pdf", "type_conflict", second.document_type, "bob")]
    assert store.row(saved["id"])["document_type"] == first.document_type and len(store.list()) == 1

    with pytest.raises(AliasTypeConflict):                                       # again: refused again, recorded once
        store.save_with_alias(second, actor="carol")

    assert len(store.aliases(saved["id"])) == 2 and store.alias_counts(saved["id"]) == (2, True) and store.verify_history()["ok"]
    assert store.approved_corrections("SHJ2026123") == []                        # a conflicting alias has no approval and no audit use


def test_concurrent_alias_submissions_leave_one_row_and_raise_nothing_unexpected(tmp_path):
    store = ExtractionStore(tmp_path / "s.sqlite3")
    saved = store.save_with_alias(named(tmp_path, "BOL_a.pdf"))
    same, conflict = named(tmp_path, "BOL_b.pdf"), named(tmp_path, "INVOICE_c.pdf")
    outcomes = []

    def submit(item, index):
        try:
            outcomes.append(("ok", ExtractionStore(store.path).save_with_alias(item, actor=f"u{index}")["alias"]["relation"]))
        except AliasTypeConflict:
            outcomes.append(("conflict", None))
        except Exception as error:  # noqa: BLE001 - any other failure is the finding
            outcomes.append(("error", repr(error)))

    threads = [threading.Thread(target=submit, args=(item, i)) for i, item in enumerate([same] * 6 + [conflict] * 6)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    names = [(a["filename"], a["relation"]) for a in store.aliases(saved["id"])]

    assert [o for o in outcomes if o[0] == "error"] == []
    assert sorted(names) == [("BOL_a.pdf", "original"), ("BOL_b.pdf", "same_type"), ("INVOICE_c.pdf", "type_conflict")]
    assert sorted(set(outcomes)) == [("conflict", None), ("ok", "same_type")] and store.verify_history()["ok"]


def test_verify_history_detects_an_alias_that_disagrees_with_its_extraction(tmp_path):
    store = ExtractionStore(tmp_path / "s.sqlite3")
    saved = store.save_with_alias(named(tmp_path, "BOL_a.pdf"))
    connection = sqlite3.connect(store.path)
    connection.execute("DROP TRIGGER extraction_aliases_no_update")
    connection.execute("UPDATE extraction_aliases SET sha256 = 'tampered' WHERE extraction_id = ?", (saved["id"],))
    connection.commit()
    connection.close()

    result = store.verify_history()

    assert not result["ok"] and any("does not match the content" in p for p in result["problems"])


# =========================================================================
# the API
# =========================================================================

def test_the_api_reports_aliases_counts_and_conflicts_and_aliases_never_reach_the_audit(rig, monkeypatch):
    client = rig.client("alice")
    copy = rig.raw / "PROOF_copy.pdf"
    copy.write_bytes((rig.raw / POD_A).read_bytes())
    (rig.raw / "WRONG_copy.pdf").write_bytes((rig.raw / POD_A).read_bytes())
    first = client.post("/api/extractions/run", json={"document": POD_A})
    service = rig.state.service
    results = {s["shipment_key"]: s["result"] for s in service.snapshot()["shipments"]}
    inputs = {s["shipment_key"]: s["inputs"] for s in service.snapshot()["shipments"]}

    assert first.status_code == 200 and first.json()["alias"] == {"filename": POD_A, "relation": "original", "created": True}

    same = client.post("/api/extractions/run", json={"document": copy.name})

    assert same.status_code == 200 and same.json()["id"] == first.json()["id"] and same.json()["alias"]["relation"] == "same_type"
    assert client.post("/api/extractions/run", json={"document": copy.name}).json()["alias"]["created"] is False

    service.invalidate()

    assert {s["shipment_key"]: s["result"] for s in service.snapshot()["shipments"]} == results
    assert {s["shipment_key"]: s["inputs"] for s in service.snapshot()["shipments"]} == inputs       # an alias changes no audit input

    detail = client.get(f"/api/extractions/{first.json()['id']}").json()

    assert detail["aliases_available"] is True and [(a["filename"], a["relation"]) for a in detail["aliases"]] == [(POD_A, "original"), (copy.name, "same_type")]
    listed = client.get("/api/extractions").json()["extractions"][0]

    assert listed["alias_count"] == 2 and listed["has_type_conflict"] is False

    original = de.classify_document
    monkeypatch.setattr(de, "classify_document", lambda name, text: "INVOICE" if name == "WRONG_copy.pdf" else original(name, text))
    conflict = client.post("/api/extractions/run", json={"document": "WRONG_copy.pdf"})

    assert conflict.status_code == 409 and "already stored as POD" in conflict.json()["detail"] and str(rig.raw) not in conflict.json()["detail"]

    detail = client.get(f"/api/extractions/{first.json()['id']}").json()

    assert detail["document_type"] == "POD" and detail["aliases"][-1]["relation"] == "type_conflict" and detail["aliases"][-1]["inferred_type"] == "INVOICE"
    assert client.get("/api/extractions").json()["extractions"][0]["has_type_conflict"] is True

    service.invalidate()

    assert {s["shipment_key"]: s["result"] for s in service.snapshot()["shipments"]} == results        # still no effect on the audit


def test_the_run_route_still_requires_the_extraction_permission(rig):
    assert rig.client("bob").post("/api/extractions/run", json={"document": POD_A}).status_code == 403
    assert rig.client().post("/api/extractions/run", json={"document": POD_A}).status_code == 401


# =========================================================================
# older application code and requests
# =========================================================================

def test_older_application_code_opening_a_v5_database_fails_closed(rig, monkeypatch):
    _, _, _, path = current_with_approval(rig)
    before = digest(path)

    with monkeypatch.context() as older:
        older.setattr(store_module, "SCHEMA_VERSION", 4)

        assert ExtractionStore.inspect(path).state == UNAVAILABLE and "newer version" in ExtractionStore.inspect(path).reason

        with pytest.raises(StoreUnavailable):
            ExtractionStore.open_for_reading(path)

        with pytest.raises(StoreUnavailable):
            ExtractionStore(path)

        rig.state.service.invalidate()
        fields = rig.state.service.shipment("SHJ-2026-001")["inputs"]["fields"]

        assert rig.state.service.corrections_available() is False and all("correction_store_unavailable" in f["warnings"] for f in fields)

    assert digest(path) == before


def test_get_and_write_requests_never_migrate_even_for_every_legacy_version(rig, tmp_path):
    client = rig.client("alice")

    for version in (1, 2, 3, 4):
        path = rig.locations.extraction_path()
        path.unlink(missing_ok=True)
        store_at(path, version)
        before = digest(path)

        for url in ("/api/extractions", "/api/extractions/1", "/api/extractions/pending", "/api/extractions/integrity", "/api/extractions/staleness"):
            client.get(url)

        client.post("/api/extractions/run", json={"document": POD_A})
        client.post("/api/extractions/1/corrections", json={"field": "delivered_pieces", "value": "47", "reason": "The paper POD shows 47 pieces.", "based_on": None})

        assert digest(path) == before and shape(path)[0] == version


# =========================================================================
# the command line
# =========================================================================

def test_the_dry_run_reports_the_schema_the_path_the_backup_destination_and_the_checks_and_writes_nothing(rig, v4_world, tmp_path, capsys):
    _, _, _, _, path = v4_world
    folder, before = backups(tmp_path), digest(path)

    status = cli.main(["--store", str(path), "--backup-dir", str(folder), "--dry-run"])
    out = capsys.readouterr().out

    assert status == 0 and digest(path) == before and listing(folder) == []
    assert "migration_pending (schema 4)" in out and "4 -> 5" in out and str(folder) in out and "pre-v4-to-v5" in out
    assert "Integrity check:  ok" in out and "History check:    ok" in out and "approved corrections 1" in out


def test_a_real_run_needs_a_backup_directory_and_an_explicit_command(rig, v4_world, tmp_path, capsys):
    _, _, _, _, path = v4_world
    before = digest(path)

    assert cli.main(["--store", str(path)]) == 2 and digest(path) == before
    assert "needs --backup-dir" in capsys.readouterr().err


def test_a_real_run_migrates_and_a_second_run_finds_nothing_to_do(rig, v4_world, tmp_path, capsys):
    _, _, _, _, path = v4_world
    folder = backups(tmp_path)

    assert cli.main(["--store", str(path), "--backup-dir", str(folder)]) == 0
    out = capsys.readouterr().out

    assert "Migrated:         schema 5; history unchanged: True; history verifies: True; integrity: True" in out and "backup was kept" in out
    assert len(listing(folder)) == 1 and ExtractionStore.inspect(path).state == CURRENT

    assert cli.main(["--store", str(path), "--backup-dir", str(folder)]) == 0
    assert "already at the current schema" in capsys.readouterr().out and len(listing(folder)) == 1


def test_a_blocked_run_exits_1_and_leaves_the_store_and_the_folder_alone(tmp_path, capsys):
    path = tmp_path / "bad.sqlite3"
    path.write_bytes(b"not a database" * 50)
    folder, before = backups(tmp_path), digest(path)

    assert cli.main(["--store", str(path), "--backup-dir", str(folder)]) == 1
    assert "BLOCKED" in capsys.readouterr().out and digest(path) == before and listing(folder) == []
    assert cli.main(["--store", str(path), "--dry-run"]) == 1


def test_the_command_json_report_names_the_backup_and_the_baseline(rig, v4_world, tmp_path, capsys):
    import json

    _, _, _, _, path = v4_world

    assert cli.main(["--store", str(path), "--backup-dir", str(backups(tmp_path)), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "migrated" and report["backup"]["verified"] is True and report["before"]["approved_corrections"] == 1
    assert report["after"]["history_unchanged"] is True and report["after"]["baseline"]["counts"]["field_corrections"] == 3
