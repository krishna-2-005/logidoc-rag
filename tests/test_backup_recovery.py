"""
Backups and recovery (src/backup.py): consistency, interruption, corruption,
restore integrity and compatibility, and replacing live data.

Every test builds its own synthetic installation in a temporary folder: the
sample shipment PDFs, a contracted shipment, an upload indexed with
deterministic stand-in embeddings, a recorded audit history run, an approved
correction, a disputed exception and a registered carrier, all through the
real application. The real data/, chroma_db/ and databases are never opened.

Run from the project root:
    python -m pytest tests/test_backup_recovery.py
"""

import json
import shutil
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_history  # noqa: E402
import backup  # noqa: E402
import ingestion_manager  # noqa: E402
from file_lock import FileLock  # noqa: E402
from test_freight_operations import correct_fuel, rig  # noqa: E402,F401  (the fixture)
from test_ingestion_manager import fake_embedding, make_bol  # noqa: E402
from test_trend_analytics import make_v1_history  # noqa: E402

DETENTION = "SHJ2026101|freight-detention-charge"
NOW = datetime(2026, 11, 9, 2, 30, tzinfo=timezone.utc)


# ============================================================
# A SYNTHETIC INSTALLATION
# ============================================================

@pytest.fixture
def world(rig, monkeypatch, tmp_path):
    """Every store populated through the application."""

    monkeypatch.setattr(ingestion_manager, "create_embedding", fake_embedding)
    admin = rig.client("alice")

    source = tmp_path / "upload_source"
    source.mkdir()
    pdf = make_bol(source / "BOL_UPLOADED.pdf", "SHJ-2026-555", 5).read_bytes()
    uploaded = admin.post("/api/ingestion/upload", files=[("files", ("BOL_UPLOADED.pdf", pdf, "application/pdf"))])
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["indexed"] == 1, uploaded.json()

    assert admin.post("/api/history/runs", json={"label": "Before"}).status_code == 201
    correct_fuel(rig)
    assert admin.get("/api/exceptions").status_code == 200
    assert admin.post(f"/api/exceptions/{DETENTION}/actions",
                      json={"action": "dispute", "note": "No detention receipt with the POD."}).status_code == 200
    assert admin.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc", "scac": "SWFT",
                                                     "mc_number": "MC-123456"}).status_code == 201

    rig.sources = backup.Sources.from_locations(rig.locations, rig.state.user_db)
    rig.backups = tmp_path / "backups"

    return rig


def make(world, **options):

    options.setdefault("quiesce", lambda: ingestion_manager.INGESTION_LOCK)
    options.setdefault("application", backup.application_version(PROJECT_ROOT))
    options.setdefault("configuration", {"auth_mode": "required"})

    return backup.create_backup(world.sources, world.backups, **options)


def rows(path, table):

    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# ============================================================
# MAKING A BACKUP
# ============================================================

def test_a_backup_holds_every_store_verified_and_described(world):

    manifest, folder = make(world, label="first")

    assert folder.name == manifest["backup_id"] and not folder.name.endswith(".partial")
    assert manifest["status"] == "complete" and manifest["origin"] == "manual" and manifest["label"] == "first"
    assert manifest["format"] == "logidoc-backup" and manifest["format_version"] == 1

    databases = manifest["components"]["databases"]["items"]
    supported = backup.supported_versions()

    for name in ("audit_reviews", "audit_history", "auth", "ingestion_register", "extractions", "carrier_registry"):
        item = databases[name]
        assert item["present"] and item["integrity"] == "ok" and item["foreign_key_problems"] == 0, name
        assert item["schema_version"] == supported[name], name
        assert item["sha256"] == backup.sha256_file(folder / item["file"])

    # The populated records are there (the history run and the one the approved correction recorded)
    assert databases["audit_history"]["tables"]["audit_runs"] == rows(world.sources.databases["audit_history"],
                                                                      "audit_runs") >= 1
    assert databases["audit_history"]["tables"]["freight_audit_records"] >= 1
    assert databases["audit_reviews"]["tables"]["review_events"] >= 1
    assert databases["carrier_registry"]["tables"]["carriers"] == 1
    assert databases["ingestion_register"]["tables"]["ingestion_files"] == 1

    documents = manifest["components"]["documents"]
    pdfs = sorted(path.name for path in world.raw.glob("*.pdf"))
    assert documents["pdfs"] == len(pdfs) and "BOL_UPLOADED.pdf" in pdfs

    index = manifest["components"]["vector_index"]
    assert index["included"] and index["present"] and index["readable"]
    assert index["embeddings"] >= 1 and "BOL_UPLOADED.pdf" in index["indexed_documents"]
    assert index["consistency"]["vectors_without_document"] == []

    assert manifest["restore_compatibility"]["schema_versions"]["audit_history"] == audit_history.SCHEMA_VERSION
    assert manifest["application"]["git_commit"] is None or len(manifest["application"]["git_commit"]) == 40
    assert manifest["totals"]["files"] == len(manifest["files"])

    assert backup.verify_backup(folder) == {"backup_id": manifest["backup_id"], "ok": True, "problems": [],
                                            "files_checked": len(manifest["files"])}
    listed = backup.list_backups(world.backups)
    assert [item["backup_id"] for item in listed] == [manifest["backup_id"]] and listed[0]["usable"]


def test_a_backup_never_changes_the_live_data(world):

    def state():
        return {path: backup.sha256_file(path) for path in sorted(world.tmp.rglob("*"))
                if path.is_file() and "backups" not in path.parts and "upload_source" not in path.parts}

    before = state()
    make(world)

    assert state() == before


def test_uncommitted_changes_are_not_in_the_backup_and_committed_ones_are(world):
    """The SQLite online backup is a consistent snapshot while another connection writes."""

    history = world.sources.databases["audit_history"]
    before = rows(history, "audit_runs")

    writer = sqlite3.connect(history, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO audit_runs (created_at, label, shipments_checked, snapshots_written) "
                   "VALUES ('2026-11-09T00:00:00+00:00', 'uncommitted', 0, 0)")

    manifest, folder = make(world)
    writer.execute("COMMIT")
    writer.close()

    assert manifest["components"]["databases"]["items"]["audit_history"]["tables"]["audit_runs"] == before
    assert rows(folder / "databases/audit_history.sqlite3", "audit_runs") == before

    second, _ = make(world)
    assert second["components"]["databases"]["items"]["audit_history"]["tables"]["audit_runs"] == before + 1


def test_a_missing_store_is_recorded_as_absent(world):

    world.sources.databases["carrier_registry"].unlink()
    manifest, folder = make(world)

    assert manifest["components"]["databases"]["items"]["carrier_registry"] == {"present": False}
    assert backup.verify_backup(folder)["ok"]


def test_without_quiescence_the_index_is_left_out_and_says_why(world):

    manifest, folder = make(world, quiesce=None, include_index=False, index_skip_reason="The API server was running.")

    assert manifest["components"]["vector_index"] == {"included": False, "reason": "The API server was running."}
    assert not any(item["component"] == "vector_index" for item in manifest["files"])
    assert backup.verify_backup(folder)["ok"]


def test_one_backup_at_a_time_per_folder(world):

    world.backups.mkdir()

    with FileLock(world.backups / backup.LOCK_NAME):
        with pytest.raises(backup.BackupBusy):
            make(world)

    assert backup.list_backups(world.backups) == []


def test_without_enough_space_nothing_is_written_and_nothing_deleted(world):

    earlier, _ = make(world)

    class Full:
        free = 10 * 1048576

    with pytest.raises(backup.InsufficientSpace, match="Nothing was deleted"):
        make(world, disk_usage=lambda path: Full(), min_free_bytes=1024 * 1048576)

    assert [item["backup_id"] for item in backup.list_backups(world.backups)] == [earlier["backup_id"]]


def test_a_backup_folder_whose_paths_windows_cannot_open_is_refused_before_writing(world, monkeypatch):

    monkeypatch.setattr(backup, "MAX_PATH", len(str(world.backups.resolve())) + 40)

    with pytest.raises(backup.PathTooLong, match="shorter path"):
        make(world)

    assert not world.backups.exists() or backup.list_backups(world.backups) == []


def test_a_restore_target_whose_paths_windows_cannot_open_is_refused(world, monkeypatch):

    _, folder = make(world)
    target = world.tmp / "restored"
    monkeypatch.setattr(backup, "MAX_PATH", len(str(target.resolve())) + 20)

    with pytest.raises(backup.RestoreRefused, match="shorter path"):
        backup.restore_backup(folder, target)

    assert not target.exists()


# ============================================================
# INTERRUPTED AND FAILED BACKUPS
# ============================================================

def test_an_interrupted_backup_stays_partial_and_is_never_usable(world, monkeypatch):

    copies = {"count": 0}
    original = backup.file_copy

    def interrupted(source, destination):
        copies["count"] += 1
        if copies["count"] == 3:
            raise KeyboardInterrupt
        return original(source, destination)

    monkeypatch.setattr(backup, "file_copy", interrupted)

    with pytest.raises(KeyboardInterrupt):
        make(world)

    [listed] = backup.list_backups(world.backups)
    assert listed["incomplete"] and not listed["usable"] and listed["status"] == "failed"
    assert listed["error"]["category"] == "interrupted"

    with pytest.raises(backup.RestoreRefused, match="incomplete"):
        backup.find_backup(world.backups, listed["backup_id"])

    with pytest.raises(backup.RestoreRefused, match="not complete"):
        backup.restore_backup(world.backups / (listed["backup_id"] + ".partial"), world.tmp / "restored")

    # The lock was released: the next backup runs
    manifest, _ = make(world)
    assert manifest["status"] == "complete"


def test_a_backup_killed_before_it_finished_is_not_trusted(world):
    """A folder still named .partial is incomplete even if its manifest says complete."""

    manifest, folder = make(world)
    folder.rename(folder.with_name(folder.name + ".partial"))

    [listed] = backup.list_backups(world.backups)
    assert listed["incomplete"] and not listed["usable"] and listed["status"] == "in_progress"


def test_a_source_that_keeps_changing_fails_the_backup(world, monkeypatch):

    original = backup.fingerprint
    calls = {"count": 0}

    def changing(path):
        if Path(path).name == "BOL_UPLOADED.pdf":
            calls["count"] += 1
            return (0, calls["count"], 0)             # another modification time at every look
        return original(path)

    monkeypatch.setattr(backup, "fingerprint", changing)

    with pytest.raises(backup.SourceChanged):
        make(world)

    [listed] = backup.list_backups(world.backups)
    assert listed["status"] == "failed" and listed["error"]["category"] == "source_changed" and not listed["usable"]
    assert calls["count"] == 2 * backup.COPY_ATTEMPTS


def test_a_file_rewritten_during_its_copy_is_copied_again(world, monkeypatch):
    """A real write between the start and the end of a copy changes the fingerprint; the retry gets the new content."""

    target = world.raw / "BOL_UPLOADED.pdf"
    original_open = open
    state = {"rewritten": False}

    def open_and_rewrite(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if Path(path) == target and "r" in mode and not state["rewritten"]:
            state["rewritten"] = True
            data = target.read_bytes()
            handle.close()
            target.write_bytes(data + b"\n% appended during the copy\n")
            return original_open(path, mode, *args, **kwargs)
        return handle

    monkeypatch.setattr("builtins.open", open_and_rewrite)
    manifest, folder = make(world)
    monkeypatch.undo()

    copied = folder / "files" / "raw" / "BOL_UPLOADED.pdf"
    assert state["rewritten"] and copied.read_bytes() == target.read_bytes()
    assert backup.verify_backup(folder)["ok"]


def test_a_failure_message_names_no_path(world, monkeypatch):

    def broken(source, destination):
        raise OSError(f"cannot read {source} password=hunter2")

    monkeypatch.setattr(backup, "file_copy", broken)

    with pytest.raises(OSError):
        make(world)

    [listed] = backup.list_backups(world.backups)
    message = listed["error"]["message"]
    assert listed["error"]["category"] == "storage_error"
    assert str(world.tmp) not in message and "hunter2" not in message and "<path>" in message


# ============================================================
# CORRUPTED BACKUPS
# ============================================================

def corrupt_one(folder, prefix):

    manifest, _ = backup.load_manifest(folder)
    item = next(item for item in manifest["files"] if item["path"].startswith(prefix))
    path = folder / item["path"]
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))

    return item["path"]


@pytest.mark.parametrize("prefix", ["files/raw/", "databases/", "vector_index/"])
def test_a_changed_file_is_found_and_the_backup_is_not_restored(world, prefix):

    _, folder = make(world)
    changed = corrupt_one(folder, prefix)

    report = backup.verify_backup(folder)
    assert not report["ok"] and any(changed in problem for problem in report["problems"])

    with pytest.raises(backup.RestoreRefused, match="verification"):
        backup.restore_backup(folder, world.tmp / "restored")

    assert not (world.tmp / "restored").exists()


def test_a_missing_file_is_found(world):

    manifest, folder = make(world)
    (folder / manifest["files"][0]["path"]).unlink()

    assert any(problem.startswith("missing") for problem in backup.verify_backup(folder)["problems"])


def test_a_truncated_database_copy_is_found(world):

    manifest, folder = make(world)
    path = folder / manifest["components"]["databases"]["items"]["audit_history"]["file"]
    path.write_bytes(path.read_bytes()[:4096])

    problems = backup.verify_backup(folder)["problems"]
    assert any("audit_history" in problem for problem in problems)


def test_an_unreadable_or_edited_manifest_is_refused(world):

    _, folder = make(world)
    text = (folder / backup.MANIFEST).read_text(encoding="utf-8")

    # Edited by hand: the checksum beside it no longer matches
    (folder / backup.MANIFEST).write_text(text.replace('"status": "complete"', '"status": "complete" '),
                                          encoding="utf-8")
    assert "checksum" in " ".join(backup.verify_backup(folder)["problems"])

    (folder / backup.MANIFEST).write_text("{ not json", encoding="utf-8")
    report = backup.verify_backup(folder)
    assert not report["ok"] and "corrupted" in report["problems"][0]

    with pytest.raises(backup.RestoreRefused):
        backup.restore_backup(folder, world.tmp / "restored")


# ============================================================
# RESTORE
# ============================================================

def test_a_restore_reproduces_every_store_exactly_and_reports_it(world):

    manifest, folder = make(world)
    target = world.tmp / "restored"

    report = backup.restore_backup(folder, target, protected=[world.raw.parent, world.locations.chroma_dir])

    assert report["status"] == "restored", report["problems"]
    assert report["problems"] == [] and report["backup_id"] == manifest["backup_id"]
    assert json.loads((target / backup.RESTORE_REPORT).read_text(encoding="utf-8")) == report
    assert not (target / backup.RESTORE_MARKER).exists()

    # Exact equality: every database is byte for byte the backed-up copy, every document the live one
    for name, item in manifest["components"]["databases"]["items"].items():
        restored = target / "data" / backup.DATABASES[name][0]
        assert backup.sha256_file(restored) == item["sha256"], name
        assert report["steps"]["databases"][name]["ok"], name

    for pdf in world.raw.glob("*.pdf"):
        assert backup.sha256_file(target / "data" / "raw" / pdf.name) == backup.sha256_file(pdf)

    # Audit history preserved, and readable by the application
    history = audit_history.HistoryStore(target / "data" / "audit_history.sqlite3")
    live = audit_history.HistoryStore(world.sources.databases["audit_history"])
    assert history.runs() == live.runs()
    assert report["steps"]["databases"]["audit_history"]["preserved"]["runs"] == len(live.runs()) >= 1

    # The index opens with ChromaDB and holds what was backed up
    index = report["steps"]["vector_index"]
    assert index["opened_embeddings"] == index["recorded_embeddings"] == manifest["components"]["vector_index"]["embeddings"]

    # Integrity and foreign keys of every restored database
    for name in manifest["components"]["databases"]["items"]:
        facts = backup.database_facts(target / "data" / backup.DATABASES[name][0])
        assert facts["integrity"] == "ok" and facts["foreign_key_problems"] == 0, name


def test_the_restored_folder_runs_the_application(world):
    """Pointed at the restore, the API shows the same audit, history and carrier as the original."""

    from api.app import create_app
    from api.core import ApiSettings, ApiState
    from audit_service import Locations
    from fastapi.testclient import TestClient
    from review_store import Policy
    from test_api import CLIENT, PASSWORD

    _, folder = make(world)
    target = world.tmp / "restored"
    assert backup.restore_backup(folder, target)["status"] == "restored"

    data = target / "data"
    locations = Locations(data / "raw", target / "chroma_db", data / "audit_reviews.sqlite3",
                          data / "audit_history.sqlite3", data / "ingestion_register.sqlite3",
                          data / "quarantine", data / "archive")
    state = ApiState(ApiSettings(environ={"LOGIDOC_AUTH_MODE": "required"}), locations, data / "auth.sqlite3", Policy())
    restored = TestClient(create_app(state), raise_server_exceptions=False, headers=CLIENT)
    assert restored.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200

    original = world.client("alice")

    for url in ("/api/history/shipments/SHJ-2026-101", f"/api/exceptions/{DETENTION}/history", "/api/carrier-registry"):
        assert restored.get(url).json() == original.get(url).json(), url



@pytest.mark.parametrize("where", ["data", "inside data", "backups", "around data"])
def test_a_restore_never_writes_into_live_or_backup_folders(world, where):

    _, folder = make(world)
    data = world.raw.parent
    target = {"data": data, "inside data": data / "restored", "backups": world.backups / "restored",
              "around data": data.parent}[where]

    with pytest.raises(backup.RestoreRefused, match="separate folder"):
        backup.restore_backup(folder, target, protected=[data, world.locations.chroma_dir])


def test_a_restore_never_overwrites_a_folder_with_content(world):

    _, folder = make(world)
    target = world.tmp / "occupied"
    target.mkdir()
    (target / "keep.txt").write_text("mine", encoding="utf-8")

    with pytest.raises(backup.RestoreRefused, match="new or empty"):
        backup.restore_backup(folder, target)

    assert (target / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_a_backup_from_a_newer_application_is_refused(world):

    _, folder = make(world)

    with pytest.raises(backup.RestoreRefused, match="newer than this application"):
        backup.restore_backup(folder, world.tmp / "restored",
                              supported={**backup.supported_versions(), "audit_history": audit_history.SCHEMA_VERSION - 1})


def test_a_backup_without_the_index_restores_and_says_to_rebuild_it(world):

    _, folder = make(world, quiesce=None, include_index=False, index_skip_reason="The API server was running.")
    report = backup.restore_backup(folder, world.tmp / "restored")

    assert report["status"] == "restored"
    assert report["steps"]["vector_index"] == "not in the backup"
    assert any("Rebuild it" in warning for warning in report["warnings"])
    assert not (world.tmp / "restored" / "chroma_db").exists()


def test_a_restore_that_finds_a_difference_fails_and_keeps_its_marker(world, monkeypatch):

    _, folder = make(world)
    original = backup.database_facts

    def different(path):
        facts = original(path)
        if "restored" in Path(path).parts and Path(path).name == "audit_history.sqlite3":
            facts = dict(facts, tables=dict(facts["tables"], audit_runs=0))
        return facts

    monkeypatch.setattr(backup, "database_facts", different)
    report = backup.restore_backup(folder, world.tmp / "restored")

    assert report["status"] == "failed" and any("row counts" in problem for problem in report["problems"])
    assert (world.tmp / "restored" / backup.RESTORE_MARKER).exists()


# ============================================================
# MIGRATION COMPATIBILITY
# ============================================================

def test_an_older_store_is_restored_as_it_was_and_upgraded_when_opened(tmp_path):

    data = tmp_path / "data"
    (data / "raw").mkdir(parents=True)
    make_v1_history(data / "audit_history.sqlite3")

    sources = backup.Sources(data / "raw", tmp_path / "chroma", data / "archive", data / "quarantine",
                             {"audit_history": data / "audit_history.sqlite3"})
    manifest, folder = backup.create_backup(sources, tmp_path / "backups")

    assert manifest["restore_compatibility"]["schema_versions"] == {"audit_history": 1}
    compatibility = backup.compatibility(manifest)
    assert compatibility["compatible"] and "upgraded" in compatibility["notes"][0]

    report = backup.restore_backup(folder, tmp_path / "restored")
    restored = tmp_path / "restored" / "data" / "audit_history.sqlite3"
    assert report["status"] == "restored" and report["steps"]["databases"]["audit_history"]["schema_version"] == 1

    # The application upgrades the restored copy on open, keeping the old run
    store = audit_history.HistoryStore(restored)
    assert backup.database_facts(restored)["schema_version"] == audit_history.SCHEMA_VERSION
    assert len(store.runs()) == 1 and store.observations()[0]["final_status"] == "NO DISCREPANCIES"


# ============================================================
# REPLACING LIVE DATA
# ============================================================

def standalone_world(tmp_path):
    """A small installation laid out like the defaults (data/ with every store, chroma_db/)."""

    data = tmp_path / "live" / "data"
    (data / "raw").mkdir(parents=True)
    (data / "raw" / "a.pdf").write_bytes(b"%PDF-1.4 one")
    make_v1_history(data / "audit_history.sqlite3")

    databases = {name: data / file for name, (file, _, _) in backup.DATABASES.items()}
    sources = backup.Sources(data / "raw", tmp_path / "live" / "chroma_db", data / "archive", data / "quarantine",
                             databases)

    return sources, data


def test_live_data_is_replaced_only_when_confirmed_and_kept_aside(tmp_path):

    sources, data = standalone_world(tmp_path)
    manifest, folder = backup.create_backup(sources, tmp_path / "backups")
    backup_id = manifest["backup_id"]
    restored = tmp_path / "restored"
    assert backup.restore_backup(folder, restored)["status"] == "restored"

    (data / "raw" / "a.pdf").write_bytes(b"%PDF-1.4 changed after the backup")
    lock = backup.server_lock_path(data)

    with pytest.raises(backup.RestoreRefused, match="Type the backup ID"):
        backup.activate_restore(restored, sources, backup_id=backup_id, confirm="yes", server_lock=lock)

    with FileLock(lock):
        with pytest.raises(backup.RestoreRefused, match="stop it first"):
            backup.activate_restore(restored, sources, backup_id=backup_id, confirm=backup_id, server_lock=lock)

    with pytest.raises(backup.RestoreRefused, match="project's own data"):
        backup.activate_restore(restored, sources, backup_id=backup_id, confirm=backup_id, server_lock=lock,
                                refuse_inside=tmp_path / "live")

    assert (data / "raw" / "a.pdf").read_bytes() == b"%PDF-1.4 changed after the backup"

    result = backup.activate_restore(restored, sources, backup_id=backup_id, confirm=backup_id, server_lock=lock,
                                     refuse_inside=tmp_path / "project")

    assert (data / "raw" / "a.pdf").read_bytes() == b"%PDF-1.4 one"
    [aside] = result["kept_aside"]
    assert (data.parent / aside / "raw" / "a.pdf").read_bytes() == b"%PDF-1.4 changed after the backup"


def test_a_restore_that_did_not_validate_cannot_be_activated(tmp_path):

    sources, data = standalone_world(tmp_path)
    manifest, folder = backup.create_backup(sources, tmp_path / "backups")
    restored = tmp_path / "restored"
    backup.restore_backup(folder, restored)
    (restored / backup.RESTORE_MARKER).write_text("interrupted", encoding="utf-8")

    with pytest.raises(backup.RestoreRefused, match="not fully restored"):
        backup.activate_restore(restored, sources, backup_id=manifest["backup_id"], confirm=manifest["backup_id"],
                                server_lock=backup.server_lock_path(data))


def test_a_store_configured_outside_the_data_folder_is_not_swapped(tmp_path):

    sources, data = standalone_world(tmp_path)
    manifest, folder = backup.create_backup(sources, tmp_path / "backups")
    backup.restore_backup(folder, tmp_path / "restored")
    moved = backup.Sources(sources.raw_dir, sources.chroma_dir, sources.archive_dir, sources.quarantine_dir,
                           {**sources.databases, "auth": tmp_path / "elsewhere" / "auth.sqlite3"})

    with pytest.raises(backup.RestoreRefused, match="outside the data folder"):
        backup.activate_restore(tmp_path / "restored", moved, backup_id=manifest["backup_id"],
                                confirm=manifest["backup_id"], server_lock=backup.server_lock_path(data))


# ============================================================
# LOCKS
# ============================================================

def test_a_file_lock_excludes_a_second_holder_and_frees_on_release(tmp_path):

    path = tmp_path / "x.lock"
    first = FileLock(path).acquire()

    with pytest.raises(Exception):
        FileLock(path).acquire()

    assert backup_is_locked(path)
    first.release()
    assert not backup_is_locked(path)

    # Another thread is excluded too
    first.acquire()
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(locked=backup_is_locked(path)))
    thread.start()
    thread.join()
    assert outcome["locked"]
    first.release()


def backup_is_locked(path):

    from file_lock import is_locked

    return is_locked(path)
