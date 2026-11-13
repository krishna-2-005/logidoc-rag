"""
Backup integrity checks and restore drills: every kind of damage named
(manifest, incomplete, missing file, checksum, integrity, contents), restore
drills into an isolated folder that is removed afterwards, the recorded
history, the schedule, overlap protection, and the administrator API.

Synthetic stores and documents in temporary folders only; no live data.

Run from the project root:
    python -m pytest tests/test_backup_verification.py
"""

import hashlib
import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import backup  # noqa: E402
import backup_automation as automation  # noqa: E402
import metrics  # noqa: E402
from file_lock import FileLock  # noqa: E402
from review_store import ReviewStore  # noqa: E402
from test_backup_automation import Free, at, small_sources  # noqa: E402

PLENTY = lambda path: Free(100_000)  # noqa: E731


def two_store_sources(tmp_path):
    """The synthetic history store, a review store with one exception, and one PDF."""

    sources = small_sources(tmp_path)
    reviews = Path(sources.raw_dir).parent / "audit_reviews.sqlite3"
    ReviewStore(reviews).sync([{"exception_key": "SHJ-1|Invoice", "shipment_key": "SHJ1", "shipment_id": "SHJ-1",
                                "category": "Invoice", "signature": "s1"}])

    return backup.Sources(sources.raw_dir, sources.chroma_dir, sources.archive_dir, sources.quarantine_dir,
                          {**sources.databases, "audit_reviews": reviews})


def made(tmp_path):

    sources = two_store_sources(tmp_path)
    manifest, folder = backup.create_backup(sources, tmp_path / "backups", origin="manual", now=at("2026-11-12T09:00:00"),
                                            disk_usage=PLENTY, min_free_bytes=0)

    return sources, manifest, folder


def digest_of(folder):

    hashes = hashlib.sha256()

    for path in sorted(Path(folder).rglob("*")):
        if path.is_file():
            hashes.update(str(path.relative_to(folder)).encode() + path.read_bytes())

    return hashes.hexdigest()


def database_copy(folder, manifest, name):

    return folder / manifest["components"]["databases"]["items"][name]["file"]


# ============================================================
# VERIFICATION: EVERY KIND OF DAMAGE IS NAMED
# ============================================================

def test_a_sound_backup_passes_every_check(tmp_path):

    _, manifest, folder = made(tmp_path)
    result = backup.verify_backup(folder)

    assert result["ok"] and result["categories"] == [] and result["problems"] == []
    assert result["databases"]["audit_history"] == result["databases"]["audit_reviews"] == "ok"
    assert result["files_checked"] == len(manifest["files"])


def tamper_manifest(folder, manifest):
    (folder / backup.MANIFEST).write_text(json.dumps({**manifest, "label": "edited"}), encoding="utf-8")


def make_incomplete(folder, manifest):
    folder.rename(folder.with_name(folder.name + backup.PARTIAL))
    return folder.with_name(folder.name + backup.PARTIAL)


def remove_a_document(folder, manifest):
    next(folder.rglob("*.pdf")).unlink()


def change_a_document(folder, manifest):
    pdf = next(folder.rglob("*.pdf"))
    pdf.write_bytes(pdf.read_bytes() + b"\n%% appended")


def corrupt_a_database(folder, manifest):
    copy = database_copy(folder, manifest, "audit_reviews")
    copy.write_bytes(b"this is no longer a SQLite database" + copy.read_bytes()[64:])


def rewrite_a_database_consistently(folder, manifest):
    """A valid database with other rows, and a manifest re-signed to match the new file: only the contents differ."""

    copy = database_copy(folder, manifest, "audit_reviews")
    with closing(sqlite3.connect(copy)) as connection, connection:
        connection.execute("DROP TRIGGER IF EXISTS review_events_no_delete")
        connection.execute("DELETE FROM review_events")
    relative = str(copy.relative_to(folder)).replace("\\", "/")
    for item in manifest["files"]:
        if item["path"] == relative:
            item["size"], item["sha256"] = copy.stat().st_size, backup.sha256_file(copy)
    manifest["components"]["databases"]["items"]["audit_reviews"]["sha256"] = backup.sha256_file(copy)
    backup.write_manifest(folder, manifest)


@pytest.mark.parametrize("damage, category", [
    (tamper_manifest, "manifest"), (make_incomplete, "incomplete"), (remove_a_document, "missing_file"),
    (change_a_document, "checksum"), (corrupt_a_database, "integrity"), (rewrite_a_database_consistently, "contents"),
])
def test_each_kind_of_damage_is_detected_and_named(tmp_path, damage, category):

    _, manifest, folder = made(tmp_path)
    folder = damage(folder, manifest) or folder

    result = backup.verify_backup(folder)

    assert not result["ok"] and category in result["categories"] and result["problems"]
    if category in ("integrity", "contents"):
        assert result["databases"]["audit_reviews"] == "failed" and result["databases"]["audit_history"] == "ok"
    if category == "contents":
        assert result["categories"] == ["contents"]                       # every checksum matched
        assert any("other rows than recorded" in problem for problem in result["problems"])


# ============================================================
# RESTORE DRILLS
# ============================================================

def test_a_restore_drill_restores_into_an_isolated_folder_and_removes_its_copy(tmp_path):

    sources, _, folder = made(tmp_path)
    live_before = digest_of(Path(sources.raw_dir).parent)
    drills = tmp_path / "drills"

    result = backup.restore_drill(folder, drills, protected=[Path(sources.raw_dir).parent, sources.chroma_dir])

    assert result["ok"] and result["kind"] == "restore_drill" and result["categories"] == []
    assert result["steps"]["databases"] == {"audit_history": "ok", "audit_reviews": "ok"}
    assert result["steps"]["documents"] == 1 and result["copy_removed"] is True
    assert list(drills.iterdir()) == []                                   # the restored copy is gone
    assert digest_of(Path(sources.raw_dir).parent) == live_before          # the live data was only read


def test_a_drill_of_a_damaged_backup_fails_clearly_and_leaves_nothing(tmp_path):

    _, manifest, folder = made(tmp_path)
    change_a_document(folder, manifest)
    drills = tmp_path / "drills"

    result = backup.restore_drill(folder, drills)

    assert not result["ok"] and result["categories"] == ["refused"]
    assert "failed verification" in result["problems"][0] and list(drills.iterdir()) == []


def test_the_history_is_read_again_after_each_new_entry(tmp_path):
    """The parsed history is cached per file state: a new entry is always seen, a damaged line skipped."""

    root = tmp_path / "backups"
    automation.record_verification(root, {"backup_id": "b1", "ok": True, "kind": automation.VERIFY}, backup.SCHEDULED)
    assert [entry["backup_id"] for entry in automation.read_verifications(root)] == ["b1"]
    assert automation.read_verifications(root) is not automation.verification_history(root)

    with open(root / automation.VERIFICATION_LOG, "a", encoding="utf-8") as handle:
        handle.write("{damaged\n")
    automation.record_verification(root, {"backup_id": "b2", "ok": False, "kind": automation.DRILL}, backup.MANUAL)

    assert [entry["backup_id"] for entry in automation.read_verifications(root)] == ["b2", "b1"]
    assert automation.last_scheduled_check(root, automation.VERIFY)["backup_id"] == "b1"
    assert automation.last_scheduled_check(root, automation.DRILL) is None
    assert automation.read_verifications(tmp_path / "nowhere") == []


def test_a_drill_copy_that_cannot_be_removed_fails_the_drill(tmp_path, monkeypatch):
    """A leftover copy takes disk space: the drill reports it as a failure, so the alert asks for it to be removed."""

    sources, _, folder = made(tmp_path)
    monkeypatch.setattr(backup, "discard_drill", lambda target, root: False)

    result = backup.restore_drill(folder, tmp_path / "drills", protected=[Path(sources.raw_dir).parent, sources.chroma_dir])

    assert not result["ok"] and result["categories"] == ["cleanup"] and result["copy_removed"] is False
    assert "could not be removed" in result["problems"][-1]


@pytest.mark.parametrize("place", ["inside the live data", "inside the backups"])
def test_a_drill_is_never_restored_beside_the_live_data_or_the_backups(tmp_path, place):

    sources, _, folder = made(tmp_path)
    drills = Path(sources.raw_dir).parent / "drills" if place == "inside the live data" else folder.parent / "drills"

    result = backup.restore_drill(folder, drills, protected=[Path(sources.raw_dir).parent, sources.chroma_dir])

    assert not result["ok"] and result["categories"] == ["refused"] and "separate folder" in result["problems"][0]
    assert list(drills.iterdir()) == []


def test_a_drill_without_room_is_not_started(tmp_path):

    _, _, folder = made(tmp_path)
    drills = tmp_path / "drills"

    result = backup.restore_drill(folder, drills, disk_usage=lambda path: Free(0))

    assert not result["ok"] and result["categories"] == ["insufficient_space"] and list(drills.iterdir()) == []


def test_only_a_drill_folder_is_ever_removed(tmp_path):

    drills = tmp_path / "drills"
    other = drills / "keep-me"
    other.mkdir(parents=True)
    (other / "file.txt").write_text("x")

    assert backup.discard_drill(other, drills) is False and (other / "file.txt").is_file()
    assert backup.discard_drill(tmp_path / "elsewhere" / f"{backup.DRILL_PREFIX}x", drills) is True     # nothing there


# ============================================================
# THE RUNNER: HISTORY, SCHEDULE, OVERLAP
# ============================================================

def runner_for(tmp_path, clock, **options):

    settings = automation.BackupSettings(tmp_path / "backups", automation.parse_schedule("off"),
                                         automation.Retention(daily=7, weekly=0, monthly=0), min_free_mb=0,
                                         verify_hours=24, drill_hours=168, drill_root=tmp_path / "drills")

    return automation.BackupRunner(two_store_sources(tmp_path), settings, disk_usage=PLENTY, clock=clock, **options)


class Clock:

    def __init__(self):
        self.now = at("2026-11-12T10:00:00")

    def __call__(self):
        return self.now


def test_checks_are_recorded_scheduled_and_reported(tmp_path):

    clock, reported, registry = Clock(), [], metrics.Registry()
    runner = runner_for(tmp_path, clock, on_verification=reported.append, metrics=registry)

    assert runner.verify_if_due() is None                                 # nothing to check yet
    backup_id = runner.run()["backup_id"]

    results = runner.verify_if_due()
    assert [(result["kind"], result["ok"]) for result in results] == [("verify", True), ("restore_drill", True)]
    assert reported == [results] and runner.verify_if_due() is None       # not due again yet

    clock.now += timedelta(hours=25)
    assert [result["kind"] for result in runner.verify_if_due()] == ["verify"]       # the drill is weekly
    clock.now += timedelta(days=7)
    assert [result["kind"] for result in runner.verify_if_due()] == ["verify", "restore_drill"]

    history = automation.read_verifications(runner.settings.root)
    assert [entry["kind"] for entry in history] == ["restore_drill", "verify", "verify", "restore_drill", "verify"]
    assert all(entry["origin"] == "scheduled" and entry["backup_id"] == backup_id and entry["ok"] for entry in history)
    assert registry.value("logidoc_backup_verifications_total", {"kind": "restore_drill", "outcome": "ok"}) == 2
    assert automation.latest_verifications(runner.settings.root)[backup_id]["kind"] == "restore_drill"


def test_a_failed_check_is_recorded_and_reported(tmp_path):

    clock, reported = Clock(), []
    runner = runner_for(tmp_path, clock, on_verification=reported.append)
    backup_id = runner.run()["backup_id"]
    pdf = next((runner.settings.root / backup_id).rglob("*.pdf"))
    pdf.write_bytes(b"damaged")

    results = runner.verify(backup_id, drill=True)

    assert [(result["kind"], result["ok"]) for result in results] == [("verify", False), ("restore_drill", False)]
    entry = automation.read_verifications(runner.settings.root)[1]
    assert entry["categories"] == ["checksum"] and entry["origin"] == "manual" and entry["problems"]
    assert reported and not any(result["ok"] for result in reported[0])


def test_a_check_never_overlaps_a_backup_or_another_process(tmp_path):

    runner = runner_for(tmp_path, Clock())
    backup_id = runner.run()["backup_id"]

    with runner.lock:
        with pytest.raises(backup.BackupBusy):
            runner.verify(backup_id)

    with FileLock(runner.settings.root / backup.LOCK_NAME):                # another process is backing up
        with pytest.raises(backup.BackupBusy, match="Another process"):
            runner.verify(backup_id)

    with pytest.raises(backup.RestoreRefused):
        runner.verify("logidoc-20990101T000000Z-manual-abcdef")


def test_a_damaged_history_line_never_breaks_the_history(tmp_path):

    root = tmp_path / "backups"
    automation.record_verification(root, {"kind": "verify", "backup_id": "b1", "ok": True, "at": "2026-11-12T10:00:00+00:00"},
                                   "manual")
    with open(root / automation.VERIFICATION_LOG, "a", encoding="utf-8") as handle:
        handle.write("{not json\n")

    assert [entry["backup_id"] for entry in automation.read_verifications(root)] == ["b1"]


def test_the_settings_are_read_and_checked():

    import environment

    settings = automation.BackupSettings.from_env({"LOGIDOC_BACKUP_VERIFY_HOURS": "6", "LOGIDOC_BACKUP_DRILL_HOURS": "0",
                                                   "LOGIDOC_BACKUP_DRILL_DIR": "/srv/drills"}, "/srv/backups")
    assert (settings.verify_hours, settings.drill_hours, settings.drill_root) == (6, 0, Path("/srv/drills"))

    errors = environment.validate({"LOGIDOC_BACKUP_VERIFY_HOURS": "-1", "LOGIDOC_DATA_DIR": "/srv/data",
                                   "LOGIDOC_BACKUP_DRILL_DIR": "/srv/data/drills"})["errors"]
    assert any("LOGIDOC_BACKUP_VERIFY_HOURS" in error for error in errors)
    assert any("LOGIDOC_BACKUP_DRILL_DIR must not be inside LOGIDOC_DATA_DIR" in error for error in errors)


# ============================================================
# THE API (administrators)
# ============================================================

@pytest.fixture
def api(tmp_path):

    from test_api import Rig

    rig = Rig(tmp_path, "required")
    rig.state.environ.update({"LOGIDOC_BACKUP_MIN_FREE_MB": "0", "LOGIDOC_BACKUP_DRILL_DIR": str(tmp_path / "drills")})

    return rig


def wait_for(runner, seconds=120):

    deadline = time.monotonic() + seconds
    time.sleep(0.05)
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.05)


def test_administrators_check_a_backup_and_read_the_history(api):

    alice, bob = api.client("alice"), api.client("bob")
    runner = api.state.operations.runner
    backup_id = runner.run()["backup_id"]

    assert bob.post(f"/api/admin/backups/{backup_id}/verify", json={}).status_code == 403
    assert bob.get("/api/admin/backups/checks").status_code == 403

    started = alice.post(f"/api/admin/backups/{backup_id}/verify", json={"restore_drill": True})
    assert started.status_code == 202 and started.json() == {"started": True}
    wait_for(runner)

    checks = alice.get("/api/admin/backups/checks").json()["checks"]
    assert [(check["kind"], check["ok"], check["origin"]) for check in checks] == [
        ("restore_drill", True, "manual"), ("verify", True, "manual")]

    overview = alice.get("/api/admin/backups").json()
    assert overview["backups"][0]["last_check"]["kind"] == "restore_drill"
    assert overview["verification"]["verify_hours"] == 24 and overview["verification"]["last_drill"] is None

    assert alice.post("/api/admin/backups/logidoc-20990101T000000Z-manual-abcdef/verify", json={}).status_code == 404
    assert alice.post("/api/admin/backups/not-a-backup/verify", json={}).status_code == 404
    with runner.lock:
        assert alice.post(f"/api/admin/backups/{backup_id}/verify", json={}).status_code == 409

    bare = api.client("alice")
    bare.headers.pop("X-LogiDoc-Client")
    assert bare.post(f"/api/admin/backups/{backup_id}/verify", json={}).status_code == 403


def test_a_failed_check_raises_the_alert_and_a_clean_one_resolves_it(api):

    operations = api.state.operations
    backup_id = operations.runner.run()["backup_id"]
    pdf = next((operations.backup_settings.root / backup_id).rglob("*.pdf"))
    original = pdf.read_bytes()

    pdf.write_bytes(b"damaged")
    operations.runner.verify(backup_id)
    assert "backup_verification_failed" in {alert["key"] for alert in operations.alerts.view()["active"]}

    pdf.write_bytes(original)
    operations.runner.verify(backup_id)
    assert "backup_verification_failed" not in {alert["key"] for alert in operations.alerts.view()["active"]}
