"""
Durable migration status (docs/MIGRATION_STATUS_DESIGN.md). Synthetic temporary databases only.

    the record     <store>.migration-status.json: written atomically by the migration command, read by the server
    authority      the database decides; a record can only add a notice to a store that is really at an older schema
    reconcile      missing, malformed, unreadable, another store's, stale: ignored, never a claim
    outcomes       success only after the commit and a passing verification
"""

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS.parent

for folder in (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts", TESTS):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import extraction_migration as migration  # noqa: E402
import extraction_store as store_module  # noqa: E402
import migration_status as status  # noqa: E402
from extraction_store import CURRENT, MIGRATION_FAILED, MIGRATION_PENDING, OLDER, UNAVAILABLE, ExtractionStore  # noqa: E402
from migration_support import store_at, upgrade  # noqa: E402
from test_correction_aware_audit import POD_A, rig  # noqa: E402,F401
from test_store_migration import backups, listing, rows  # noqa: E402
from test_store_states import digest, v4_file, v4_world  # noqa: E402,F401

import migrate_extraction_store as cli  # noqa: E402


def record_of(path):
    return json.loads(status.status_path(path).read_text(encoding="utf-8"))


def tamper_history(path):
    """An edit the correction chain detects: the preflight must refuse to migrate."""

    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER field_corrections_no_update")
    connection.execute("UPDATE field_corrections SET corrected = '99' WHERE id = 1")
    connection.commit()
    connection.close()


def put(path, **over):
    """Writes a status record for the store at path: a valid one unless fields are overridden."""

    base = ExtractionStore.inspect(path)

    import sqlite3

    connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    fingerprint = status.fingerprint(status.baseline(connection))
    connection.close()
    record = status.new_record(path, over.pop("outcome", "failed"), over.pop("code", "rolled_back"), over.pop("from_version", base.version),
                               over.pop("to_version", store_module.SCHEMA_VERSION), over.pop("baseline", fingerprint))
    record.update(over)
    status.write(path, record)

    return record


@pytest.fixture
def plain(tmp_path):
    return store_at(tmp_path / "extractions.sqlite3", 4)


# =========================================================================
# the record
# =========================================================================

def test_a_record_holds_names_codes_and_a_fingerprint_and_never_a_path_or_a_message(tmp_path, plain):
    put(plain)
    record = record_of(plain)

    assert set(record) == set(status.KEYS) and record["format"] == 1 and record["store_name"] == "extractions.sqlite3"
    assert status.valid(record) and str(tmp_path) not in json.dumps(record) and record["code"] in status.CODES
    assert status.status_path(plain).parent == plain.parent and status.status_path(plain).name == "extractions.sqlite3.migration-status.json"


def test_the_process_local_registry_is_gone(plain):
    assert not hasattr(store_module, "MIGRATION_FAILURES")


def test_a_write_is_atomic_a_crash_before_the_replace_leaves_the_old_record(tmp_path, plain, monkeypatch):
    put(plain, outcome="started", code=None)
    old = status.status_path(plain).read_bytes()

    def crash(*args):
        raise OSError("disk went away")

    monkeypatch.setattr(status.os, "replace", crash)

    with pytest.raises(OSError):
        put(plain, outcome="failed", code="rolled_back")

    assert status.status_path(plain).read_bytes() == old and status.read(plain)[0] == "ok"
    leftovers = [p for p in plain.parent.iterdir() if p.name.endswith(".tmp")]

    assert len(leftovers) == 1 and status.read(plain)[1]["outcome"] == "started"          # a leftover is never read


# =========================================================================
# reconciliation: the database decides
# =========================================================================

def state_of(path):
    found = ExtractionStore.inspect(path)

    return found.state, (found.migration or {}).get("attempt"), (found.migration or {}).get("record")


def test_a_failed_record_for_this_store_at_an_older_schema_is_a_confirmed_failure(plain):
    put(plain, outcome="failed", code="backup_failed")

    assert state_of(plain) == (MIGRATION_FAILED, "failed", "ok") and ExtractionStore.inspect(plain).migration["code"] == "backup_failed"


def test_a_started_record_is_reported_unfinished_and_never_as_a_failure(plain):
    put(plain, outcome="started", code=None)

    assert state_of(plain) == (MIGRATION_PENDING, "unfinished", "ok")


def test_an_older_than_v4_store_reports_older_not_failed_for_an_unfinished_attempt(tmp_path):
    path = store_at(tmp_path / "extractions.sqlite3", 3)
    put(path, outcome="started", code=None)

    assert state_of(path) == (OLDER, "unfinished", "ok")


@pytest.mark.parametrize("outcome, code", [("succeeded", None), ("committed_unverified", "post_verification_failed")])
def test_a_success_record_next_to_an_older_store_is_ignored_as_a_restored_or_replaced_database(plain, outcome, code):
    put(plain, outcome=outcome, code=code)

    assert state_of(plain) == (MIGRATION_PENDING, "none", "ignored")


@pytest.mark.parametrize("outcome, code", [("failed", "rolled_back"), ("started", None)])
def test_a_failed_or_started_record_next_to_a_current_store_is_stale_and_ignored(tmp_path, outcome, code):
    path = tmp_path / "extractions.sqlite3"
    ExtractionStore(path)
    put(path, outcome=outcome, code=code, from_version=4, baseline="x" * 64)

    assert state_of(path) == (CURRENT, "none", "ignored")


def test_a_succeeded_record_next_to_a_current_store_is_reported(plain, tmp_path):
    upgrade(plain, backups(tmp_path))

    assert state_of(plain) == (CURRENT, "succeeded", "ok")


def test_a_record_for_another_version_or_another_history_is_ignored(tmp_path, plain):
    put(plain, outcome="failed", code="rolled_back", from_version=3)
    assert state_of(plain) == (MIGRATION_PENDING, "none", "ignored")

    put(plain, outcome="failed", code="rolled_back", baseline="0" * 64)
    assert state_of(plain) == (MIGRATION_PENDING, "none", "ignored")


def test_a_record_copied_to_another_store_is_ignored(rig, tmp_path, monkeypatch):
    (tmp_path / "one").mkdir()
    one = store_at(tmp_path / "one" / "extractions.sqlite3", 4)
    two_dir = tmp_path / "two"
    two_dir.mkdir()
    two = two_dir / "extractions.sqlite3"
    v4_file(two, monkeypatch, rig.raw)                                   # the same file name, a different history
    put(one, outcome="failed", code="rolled_back")

    status.status_path(two).write_bytes(status.status_path(one).read_bytes())

    assert state_of(one)[0] == MIGRATION_FAILED and state_of(two) == (MIGRATION_PENDING, "none", "ignored")

    other_name = two_dir / "other.sqlite3"
    other_name.write_bytes(two.read_bytes())
    status.status_path(other_name).write_bytes(status.status_path(one).read_bytes())

    assert state_of(other_name) == (MIGRATION_PENDING, "none", "ignored")                  # right history, wrong store name


def test_a_change_to_the_history_after_the_attempt_makes_the_record_stale(rig, v4_world):
    _, _, _, _, path = v4_world
    put(path, outcome="failed", code="rolled_back")

    assert state_of(path)[0] == MIGRATION_FAILED

    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, payload, payload_sha256, created_at) "
                       "VALUES ('late.pdf', 'z', '1', 'POD', 'extracted', '{}', 'x', 'now')")
    connection.commit()
    connection.close()

    assert state_of(path) == (MIGRATION_PENDING, "none", "ignored")


@pytest.mark.parametrize("content", [
    b"", b"{", b"\xff\xfe\x00 not utf-8", b"[]", b"null", b'{"format": 1}', b"x" * 9000,
    json.dumps({**{k: None for k in status.KEYS}, "format": 2}).encode(),
])
def test_a_malformed_record_is_ignored_and_claims_nothing(plain, content):
    status.status_path(plain).write_bytes(content)

    assert state_of(plain) == (MIGRATION_PENDING, "none", "unreadable")


@pytest.mark.parametrize("field, value", [
    ("outcome", "exploded"), ("code", "free text with a path C:\\x"), ("from_version", "4"), ("from_version", True), ("to_version", -1),
    ("at", "yesterday"), ("store_name", ""), ("baseline", None), ("attempt_id", 5),
])
def test_a_record_with_one_bad_field_is_unreadable(plain, field, value):
    record = put(plain, outcome="failed", code="rolled_back")
    record[field] = value
    status.status_path(plain).write_text(json.dumps(record), encoding="utf-8")

    assert state_of(plain) == (MIGRATION_PENDING, "none", "unreadable")


def test_a_record_with_an_extra_field_is_unreadable(plain):
    record = put(plain, outcome="failed", code="rolled_back")
    record["note"] = "a stack trace"
    status.status_path(plain).write_text(json.dumps(record), encoding="utf-8")

    assert state_of(plain) == (MIGRATION_PENDING, "none", "unreadable")


def test_an_inaccessible_record_is_unreadable_and_a_missing_one_is_missing(tmp_path, plain):
    assert state_of(plain) == (MIGRATION_PENDING, "none", "missing")

    status.status_path(plain).mkdir()                                    # opening a directory as a file fails: the same as no permission

    assert state_of(plain) == (MIGRATION_PENDING, "none", "unreadable")


def test_a_record_can_never_change_the_schema_state(tmp_path):
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a database" * 50)
    newer = tmp_path / "newer.sqlite3"
    ExtractionStore(newer)
    import sqlite3

    with sqlite3.connect(newer) as connection:
        connection.execute("UPDATE schema_info SET version = 99")

    for path, wanted in ((corrupt, UNAVAILABLE), (newer, UNAVAILABLE), (tmp_path / "none.sqlite3", store_module.ABSENT)):
        status.write(path, status.new_record(path, "succeeded", None, 4, 5, "a" * 64))

        assert ExtractionStore.inspect(path).state == wanted


# =========================================================================
# outcomes written by the command
# =========================================================================

def test_the_command_records_start_then_success_only_after_a_verified_commit(rig, v4_world, tmp_path, monkeypatch):
    _, _, _, _, path = v4_world
    seen = []
    original = status.write

    def spy(store_path, record):
        seen.append((record["outcome"], record["code"], ExtractionStore.inspect(path).version))
        original(store_path, record)

    monkeypatch.setattr(migration.status, "write", spy)
    report = upgrade(path, backups(tmp_path))

    assert [s[:2] for s in seen] == [("started", None), ("succeeded", None)] and [s[2] for s in seen] == [4, 5]
    assert report["status"] == "migrated" and report["warnings"] == [] and state_of(path) == (CURRENT, "succeeded", "ok")
    assert record_of(path)["backup_name"].startswith("extractions.pre-v4-to-v5-") and record_of(path)["from_version"] == 4


@pytest.mark.parametrize("how, code", [
    ("backup", "backup_failed"), ("rolled_back", "rolled_back"), ("verification", "verification_failed"), ("race", "store_changed"),
])
def test_each_failure_is_recorded_with_a_fixed_code_and_the_store_reports_a_confirmed_failure(rig, v4_world, tmp_path, monkeypatch, how, code):
    _, approved, _, _, path = v4_world
    before = digest(path)
    good = dict(store_module.MIGRATIONS)

    if how == "backup":
        monkeypatch.setattr(migration, "make_backup", lambda *a: (_ for _ in ()).throw(store_module.StoreError("the backup could not be verified")))
    elif how == "rolled_back":
        monkeypatch.setattr(store_module, "MIGRATIONS", {**good, 5: good[5] + "\nTHIS IS NOT SQL;"})
    elif how == "verification":
        original, calls = migration.verify_connection, []
        monkeypatch.setattr(migration, "verify_connection", lambda c: (calls.append(1), {**original(c), "ok": False, "problems": ["x"]} if len(calls) == 3 else original(c))[1])
    else:
        original = migration.make_backup

        def writer_arrives(*args):
            original(*args)
            import sqlite3

            connection = sqlite3.connect(path)
            connection.execute("INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, payload, payload_sha256, created_at) "
                               "VALUES ('late.pdf', 'z', '1', 'POD', 'extracted', '{}', 'x', 'now')")
            connection.commit()
            connection.close()

        monkeypatch.setattr(migration, "make_backup", writer_arrives)

    with pytest.raises(migration.MigrationAborted):
        upgrade(path, backups(tmp_path))

    record = record_of(path)

    assert record["outcome"] == "failed" and record["code"] == code and "intact" not in json.dumps(record)
    assert str(tmp_path) not in json.dumps(record)

    if how == "race":                                                    # the history moved on after the attempt began: the record is stale
        assert state_of(path)[0] == MIGRATION_PENDING
    else:
        assert state_of(path) == (MIGRATION_FAILED, "failed", "ok") and digest(path) == before
        assert [c["correction_id"] for c in ExtractionStore.open_for_reading(path).approved_corrections("SHJ2026001")] == [approved["id"]]


def test_a_failed_attempt_then_a_successful_one_leaves_success(rig, v4_world, tmp_path, monkeypatch):
    _, _, _, _, path = v4_world
    good = dict(store_module.MIGRATIONS)
    monkeypatch.setattr(store_module, "MIGRATIONS", {**good, 5: good[5] + "\nTHIS IS NOT SQL;"})

    with pytest.raises(migration.MigrationAborted):
        upgrade(path, backups(tmp_path))

    assert state_of(path)[0] == MIGRATION_FAILED

    monkeypatch.setattr(store_module, "MIGRATIONS", good)
    upgrade(path, backups(tmp_path))

    assert state_of(path) == (CURRENT, "succeeded", "ok") and record_of(path)["outcome"] == "succeeded"


def test_a_preflight_refusal_on_a_failing_history_is_recorded_and_changes_nothing(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world
    tamper_history(path)
    before, folder = digest(path), backups(tmp_path)

    with pytest.raises(migration.MigrationAborted):
        upgrade(path, folder)

    assert record_of(path)["code"] == "history_verification_failed" and listing(folder) == [] and digest(path) == before
    assert state_of(path) == (MIGRATION_FAILED, "failed", "ok")


def test_operator_errors_are_not_attempts_and_write_no_record(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world

    for target in (None, tmp_path / "nowhere"):
        with pytest.raises(migration.MigrationAborted):
            migration.migrate(path, target)

    assert not status.status_path(path).exists() and state_of(path) == (MIGRATION_PENDING, "none", "missing")


def test_an_unreadable_or_absent_store_gets_no_record(tmp_path):
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a database" * 50)

    for path in (corrupt, tmp_path / "none.sqlite3"):
        with pytest.raises(migration.MigrationAborted):
            upgrade(path, backups(tmp_path))

        assert not status.status_path(path).exists()


# =========================================================================
# interrupted attempts
# =========================================================================

def test_an_interrupted_attempt_is_unfinished_not_failed_and_a_retry_completes_it(rig, v4_world, tmp_path, monkeypatch):
    _, approved, _, _, path = v4_world
    before = digest(path)

    def killed(*args):
        raise KeyboardInterrupt                                           # the process dies after "started" was written

    monkeypatch.setattr(migration, "make_backup", killed)

    with pytest.raises(KeyboardInterrupt):
        upgrade(path, backups(tmp_path))

    monkeypatch.undo()

    assert record_of(path)["outcome"] == "started" and digest(path) == before
    assert state_of(path) == (MIGRATION_PENDING, "unfinished", "ok")
    assert rig.client("alice").get("/api/extractions").status_code == 200                              # reads continue
    assert ExtractionStore.inspect(path).migration["attempt"] == "unfinished"

    upgrade(path, backups(tmp_path))

    assert state_of(path) == (CURRENT, "succeeded", "ok")


def test_an_unfinished_record_becomes_stale_when_the_history_changes(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world
    put(path, outcome="started", code=None)

    assert state_of(path)[1] == "unfinished"

    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, payload, payload_sha256, created_at) "
                       "VALUES ('late.pdf', 'z', '1', 'POD', 'extracted', '{}', 'x', 'now')")
    connection.commit()
    connection.close()

    assert state_of(path) == (MIGRATION_PENDING, "none", "ignored")


def test_a_database_restored_from_the_backup_after_a_success_is_pending_again(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world
    upgrade(path, backups(tmp_path))
    copy = Path(tmp_path) / "backups" / listing(Path(tmp_path) / "backups")[0]

    path.write_bytes(copy.read_bytes())                                   # the operator restores the pre-migration file

    assert state_of(path) == (MIGRATION_PENDING, "none", "ignored")


# =========================================================================
# concurrent attempts
# =========================================================================

def test_concurrent_attempts_leave_a_consistent_record_and_the_database_decides(rig, v4_world, tmp_path):
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

    assert errors == [] and sorted(results).count("migrated") == 1
    assert ExtractionStore.inspect(path).state == CURRENT and status.read(path)[0] == "ok"
    assert ExtractionStore(path).verify_history()["ok"]

    # whatever the last writer left, a stale failure next to a current store claims nothing
    put(path, outcome="failed", code="rolled_back", from_version=4)

    assert state_of(path) == (CURRENT, "none", "ignored")


# =========================================================================
# status-write failures
# =========================================================================

def test_when_the_start_cannot_be_recorded_nothing_is_changed(rig, v4_world, tmp_path, monkeypatch):
    _, _, _, _, path = v4_world
    before, folder = digest(path), backups(tmp_path)

    def broken(*args):
        raise OSError("read-only folder")

    monkeypatch.setattr(migration.status, "write", broken)

    with pytest.raises(migration.MigrationAborted, match="could not be recorded, so nothing was changed") as stopped:
        upgrade(path, folder)

    assert digest(path) == before and listing(folder) == [] and not status.status_path(path).exists()
    assert "read-only folder" not in str(stopped.value) and state_of(path)[0] == MIGRATION_PENDING


def test_a_failure_to_record_a_failure_never_makes_the_store_look_healthy(rig, v4_world, tmp_path, monkeypatch):
    _, _, _, _, path = v4_world
    good = dict(store_module.MIGRATIONS)
    original, calls = status.write, []

    def second_write_fails(store_path, record):
        calls.append(record["outcome"])

        if record["outcome"] == "failed":
            raise OSError("disk full")

        original(store_path, record)

    monkeypatch.setattr(migration.status, "write", second_write_fails)
    monkeypatch.setattr(store_module, "MIGRATIONS", {**good, 5: good[5] + "\nTHIS IS NOT SQL;"})

    with pytest.raises(migration.MigrationAborted) as stopped:
        upgrade(path, backups(tmp_path))

    assert calls == ["started", "failed"] and any("could not be recorded" in w for w in stopped.value.report["warnings"])
    assert ExtractionStore.inspect(path).state in (MIGRATION_PENDING, MIGRATION_FAILED) and ExtractionStore.inspect(path).version == 4
    assert rig.client("alice").post("/api/extractions/run", json={"document": POD_A}).status_code == 503           # still refused


def test_when_the_success_cannot_be_recorded_the_schema_still_says_current_and_the_command_warns(rig, v4_world, tmp_path, monkeypatch):
    _, _, _, _, path = v4_world
    original = status.write

    def fail_success(store_path, record):
        if record["outcome"] == "succeeded":
            raise OSError("disk full")

        original(store_path, record)

    monkeypatch.setattr(migration.status, "write", fail_success)
    report = upgrade(path, backups(tmp_path))

    assert report["status"] == "migrated" and any("could not be recorded" in w for w in report["warnings"])
    assert ExtractionStore.inspect(path).state == CURRENT and ExtractionStore(path).verify_history()["ok"]


def test_a_commit_that_does_not_verify_is_not_a_success_and_the_store_is_unavailable_while_it_fails(rig, v4_world, tmp_path, monkeypatch):
    _, _, _, _, path = v4_world
    original, calls = migration.verify_connection, []

    def fails_after_commit(connection):
        calls.append(1)

        return {**original(connection), "ok": False, "problems": ["injected"]} if len(calls) == 4 else original(connection)

    monkeypatch.setattr(migration, "verify_connection", fails_after_commit)

    with pytest.raises(migration.MigrationAborted, match="committed but the verification afterwards failed") as stopped:
        upgrade(path, backups(tmp_path))

    monkeypatch.undo()

    assert stopped.value.report["status"] == "committed_unverified" and record_of(path)["outcome"] == "committed_unverified"
    assert ExtractionStore.inspect(path).version == 5 and state_of(path)[0] == CURRENT           # the store does verify (the failure was injected)

    tamper_history(path)                                                  # now it really fails verification

    found = ExtractionStore.inspect(path)

    assert found.state == UNAVAILABLE and found.migration["attempt"] == "unverified" and found.migration["code"] == "post_verification_failed"
    assert "verification after its migration" in found.reason
    assert rig.client("alice").get("/api/extractions").status_code == 503


# =========================================================================
# requests never migrate; the API never leaks
# =========================================================================

@pytest.mark.parametrize("kind", ["failed", "started", "malformed", "none"])
def test_get_and_write_requests_never_migrate_or_touch_the_record(rig, v4_world, kind):
    pod, approved, rejected, pending, path = v4_world
    record = status.status_path(path)

    if kind in ("failed", "started"):
        put(path, outcome=kind, code="rolled_back" if kind == "failed" else None)
    elif kind == "malformed":
        record.write_bytes(b"{")

    before, before_record = digest(path), (record.read_bytes() if record.exists() else None)
    client = rig.client("alice")

    for url in ("/api/extractions", "/api/extractions/1", "/api/extractions/pending", "/api/extractions/integrity", "/api/extractions/staleness"):
        assert client.get(url).status_code == 200, url

    assert client.post("/api/extractions/run", json={"document": POD_A}).status_code == 503
    assert client.post(f"/api/extractions/{pod}/corrections", json={"field": "delivered_pieces", "value": "47", "reason": "The paper POD shows 47 pieces.",
                                                                  "based_on": approved["id"]}).status_code == 503
    assert client.post(f"/api/extractions/corrections/{pending['id']}/decision", json={"decision": "approve", "note": "Checked."}).status_code == 503

    assert digest(path) == before and ExtractionStore.inspect(path).version == 4
    assert (record.read_bytes() if record.exists() else None) == before_record


def test_the_api_reports_the_confirmed_failure_to_a_server_process_that_did_not_run_the_command(rig, v4_world, tmp_path):
    """A real separate process runs the command and fails (the history is tampered with); the server reads the outcome."""

    _, _, _, _, path = v4_world
    tamper_history(path)
    folder = backups(tmp_path)

    run = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "migrate_extraction_store.py"), "--store", str(path), "--backup-dir", str(folder)],
                         capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=120)

    assert run.returncode == 1 and "BLOCKED" in run.stdout and "history failed verification" in run.stdout

    client = rig.client("alice")
    body = client.get("/api/extractions/staleness").json()["store"]

    assert body["state"] == "migration_failed" and body["version"] == 4
    assert body["migration"]["attempt"] == "failed" and body["migration"]["code"] == "history_verification_failed" and body["migration"]["record"] == "ok"
    assert client.get("/api/extractions").status_code == 200


def test_a_successful_command_in_another_process_is_seen_by_the_server(rig, v4_world, tmp_path):
    _, _, _, _, path = v4_world
    client = rig.client("alice")

    assert client.get("/api/extractions/staleness").json()["store"]["migration"] == {"attempt": "none", "code": None, "at": None, "record": "missing"}

    run = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "migrate_extraction_store.py"), "--store", str(path), "--backup-dir", str(backups(tmp_path))],
                         capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=120)
    body = client.get("/api/extractions/staleness").json()["store"]

    assert run.returncode == 0 and "Migrated:" in run.stdout
    assert body["state"] == "current" and body["migration"]["attempt"] == "succeeded" and body["migration"]["record"] == "ok"
    assert client.post("/api/extractions/run", json={"document": POD_A}).status_code == 200


@pytest.mark.parametrize("kind", ["failed", "started", "succeeded_then_restored", "malformed", "unverified"])
def test_the_api_never_leaks_paths_exception_names_or_backup_names(rig, v4_world, tmp_path, kind):
    _, _, _, _, path = v4_world

    if kind == "failed":
        put(path, outcome="failed", code="rolled_back")
    elif kind == "started":
        put(path, outcome="started", code=None)
    elif kind == "malformed":
        status.status_path(path).write_bytes(b"Traceback (most recent call last): OperationalError at C:\\secret\\x.sqlite3")
    elif kind == "unverified":
        upgrade(path, backups(tmp_path))
        put(path, outcome="committed_unverified", code="post_verification_failed", from_version=4, to_version=5)
        tamper_history(path)
    else:
        upgrade(path, backups(tmp_path))

    client = rig.client("alice")
    texts = [client.get(url).text for url in ("/api/extractions/staleness", "/api/extractions", "/api/extractions/pending", "/api/extractions/integrity")]
    texts.append(client.post("/api/extractions/run", json={"document": POD_A}).text)

    for text in texts:
        for forbidden in (str(tmp_path), str(path.parent), ".sqlite3", "OperationalError", "Traceback", "pre-v4-to-v5", "secret", "migration-status", "Error("):
            assert forbidden not in text, (kind, forbidden, text[:200])


# =========================================================================
# the command line
# =========================================================================

def test_the_dry_run_names_the_last_attempt_and_changes_nothing(rig, v4_world, tmp_path, capsys):
    _, _, _, _, path = v4_world
    put(path, outcome="failed", code="backup_failed")
    before, record = digest(path), status.status_path(path).read_bytes()

    assert cli.main(["--store", str(path), "--backup-dir", str(backups(tmp_path)), "--dry-run"]) == 0

    assert "Last attempt:     failed (backup_failed)" in capsys.readouterr().out
    assert digest(path) == before and status.status_path(path).read_bytes() == record


def test_the_command_exits_1_and_says_not_verified_for_a_commit_that_does_not_verify(rig, v4_world, tmp_path, capsys, monkeypatch):
    _, _, _, _, path = v4_world
    original, calls = migration.verify_connection, []
    monkeypatch.setattr(migration, "verify_connection",
                        lambda c: (calls.append(1), {**original(c), "ok": False, "problems": ["x"]} if len(calls) == 4 else original(c))[1])

    assert cli.main(["--store", str(path), "--backup-dir", str(backups(tmp_path))]) == 1

    out = capsys.readouterr().out

    assert "NOT VERIFIED" in out and "Migrated:" not in out and "backup was kept" in out


def test_the_command_prints_a_warning_when_the_success_could_not_be_recorded(rig, v4_world, tmp_path, capsys, monkeypatch):
    _, _, _, _, path = v4_world
    original = status.write
    monkeypatch.setattr(migration.status, "write", lambda p, r: (_ for _ in ()).throw(OSError("full")) if r["outcome"] == "succeeded" else original(p, r))

    assert cli.main(["--store", str(path), "--backup-dir", str(backups(tmp_path))]) == 0

    out = capsys.readouterr().out

    assert "Migrated:" in out and "WARNING:" in out and "could not be recorded" in out
