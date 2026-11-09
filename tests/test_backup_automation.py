"""
Automated backups (src/backup_automation.py, src/operations.py,
scripts/manage_backups.py, /api/admin/backups): the schedule, the status record,
overlap protection, failure reporting, retention, the monitor's alerts and
the administrators' endpoints. Synthetic installations in temporary folders.

Run from the project root:
    python -m pytest tests/test_backup_automation.py
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
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
from test_trend_analytics import make_v1_history  # noqa: E402

UTC = timezone.utc


def at(text):

    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def small_sources(tmp_path):

    data = tmp_path / "data"
    (data / "raw").mkdir(parents=True)
    (data / "raw" / "a.pdf").write_bytes(b"%PDF-1.4 synthetic")
    make_v1_history(data / "audit_history.sqlite3")

    return backup.Sources(data / "raw", tmp_path / "chroma", data / "archive", data / "quarantine",
                          {"audit_history": data / "audit_history.sqlite3"})


class Free:
    """A disk with this much free space."""

    def __init__(self, megabytes):
        self.free = megabytes * 1048576


# ============================================================
# SCHEDULE
# ============================================================

@pytest.mark.parametrize("text, kind, described", [
    ("off", "off", "off"), ("", "off", "off"), (None, "off", "off"),
    ("daily@02:30", "daily", "daily at 02:30 UTC"), ("every:6h", "interval", "every 6 h"),
])
def test_schedules_that_parse(text, kind, described):

    schedule = automation.parse_schedule(text)

    assert schedule.kind == kind and schedule.describe() == described


@pytest.mark.parametrize("text", ["daily", "daily@24:00", "daily@2:30", "every:0h", "every:6", "hourly", "cron * * *"])
def test_schedules_that_are_refused(text):

    with pytest.raises(ValueError, match="LOGIDOC_BACKUP_SCHEDULE"):
        automation.parse_schedule(text)


def success(moment):

    return {"at": backup.iso(moment), "outcome": "success"}


def failure(moment):

    return {"at": backup.iso(moment), "outcome": "failed"}


def test_a_daily_backup_is_due_once_per_slot():

    daily = automation.parse_schedule("daily@02:30")

    assert automation.is_due(daily, at("2026-11-09T02:31:00"), {})                       # never ran
    assert not automation.is_due(daily, at("2026-11-09T02:29:00"),
                                 {"last_success": success(at("2026-11-08T02:30:05"))})    # before today's slot
    assert automation.is_due(daily, at("2026-11-09T02:31:00"),
                             {"last_success": success(at("2026-11-08T02:30:05"))})        # today's slot passed
    assert not automation.is_due(daily, at("2026-11-09T09:00:00"),
                                 {"last_success": success(at("2026-11-09T02:30:05"))})    # done today


def test_an_interval_backup_and_the_retry_delay_after_a_failure():

    every = automation.parse_schedule("every:6h")
    done = {"last_success": success(at("2026-11-09T00:00:00"))}

    assert not automation.is_due(every, at("2026-11-09T05:59:00"), done)
    assert automation.is_due(every, at("2026-11-09T06:00:00"), done)

    failed = {**done, "last_attempt": failure(at("2026-11-09T06:00:00")), "retry_minutes": 60}
    assert not automation.is_due(every, at("2026-11-09T06:30:00"), failed)                # waits, no loop
    assert automation.is_due(every, at("2026-11-09T07:00:00"), failed)

    assert not automation.is_due(automation.parse_schedule("off"), at("2026-11-09T07:00:00"), {})


def test_the_status_record_keeps_the_last_success_and_counts_failures(tmp_path):

    automation.record_attempt(tmp_path, "failed", at("2026-11-08T02:30:00"), "scheduled", category="insufficient_space",
                              message=r"no room on D:\backups\logidoc")
    assert automation.read_status(tmp_path)["last_failure"]["message"] == "no room on <path>/logidoc"

    automation.record_attempt(tmp_path, "failed", at("2026-11-08T03:30:00"), "scheduled", category="storage_error")
    status = automation.read_status(tmp_path)

    assert status["consecutive_failures"] == 2 and "last_success" not in status

    automation.record_attempt(tmp_path, "success", at("2026-11-08T04:30:00"), "scheduled", backup_id="logidoc-x")
    status = automation.read_status(tmp_path)

    assert status["consecutive_failures"] == 0 and status["last_success"]["backup_id"] == "logidoc-x"
    assert status["last_failure"]["category"] == "storage_error"


# ============================================================
# RETENTION
# ============================================================

def listed(backup_id, created, origin="scheduled", usable=True):

    return {"backup_id": backup_id, "created_at": backup.iso(created), "origin": origin, "usable": usable}


def test_the_retention_plan_keeps_daily_weekly_and_monthly_and_protects_the_rest():

    start = at("2026-07-01T02:30:00")
    scheduled = [listed(f"s{day:03d}", start + timedelta(days=day)) for day in range(120)]
    manual = listed("m-old", start - timedelta(days=400), origin="manual")
    incomplete = listed("s-partial", start + timedelta(days=200), usable=False)

    plan = automation.retention_plan(scheduled + [manual, incomplete], automation.Retention(daily=7, weekly=4, monthly=3))

    newest = [f"s{day:03d}" for day in range(113, 120)]
    assert set(newest) <= set(plan["keep"])                               # the last 7 days
    assert "m-old" in plan["protected"] and "s-partial" in plan["protected"]
    assert "m-old" not in plan["remove"] and "s-partial" not in plan["remove"]
    # 7 daily + at most 4 weekly + at most 3 monthly (some overlap)
    assert 7 <= len(plan["keep"]) <= 14
    assert set(plan["keep"]) | set(plan["remove"]) == {item["backup_id"] for item in scheduled}


def test_the_newest_usable_backup_is_always_kept():

    only = [listed("s1", at("2026-11-01T00:00:00"))]

    assert automation.retention_plan(only, automation.Retention(0, 0, 0))["keep"] == ["s1"]


def test_retention_removes_only_scheduled_backup_folders(tmp_path):

    sources = small_sources(tmp_path)
    root = tmp_path / "backups"
    days = [backup.create_backup(sources, root, origin="scheduled", now=at(f"2026-11-0{day}T02:30:00"))[0]["backup_id"]
            for day in range(1, 6)]
    manual = backup.create_backup(sources, root, origin="manual", now=at("2026-10-01T02:30:00"))[0]["backup_id"]

    # Things retention must never touch
    (root / "my-notes").mkdir()
    (root / "my-notes" / "keep.txt").write_text("mine", encoding="utf-8")
    partial = root / "logidoc-20261001T000000Z-scheduled-abcdef.partial"
    partial.mkdir()

    preview = automation.apply_retention(root, automation.Retention(daily=2, weekly=0, monthly=0), dry_run=True)
    assert preview["remove"] == days[:3][::-1] and preview["removed"] == []
    assert all((root / backup_id).is_dir() for backup_id in days)

    applied = automation.apply_retention(root, automation.Retention(daily=2, weekly=0, monthly=0))

    assert sorted(applied["removed"]) == sorted(days[:3])
    assert [item["backup_id"] for item in backup.list_backups(root) if item["usable"]] == days[3:][::-1] + [manual]
    assert (root / "my-notes" / "keep.txt").is_file() and partial.is_dir()


def test_a_folder_whose_manifest_was_changed_is_skipped_not_removed(tmp_path):

    sources = small_sources(tmp_path)
    root = tmp_path / "backups"
    first = backup.create_backup(sources, root, origin="scheduled", now=at("2026-11-01T02:30:00"))[0]["backup_id"]
    backup.create_backup(sources, root, origin="scheduled", now=at("2026-11-02T02:30:00"))

    plan = automation.retention_plan(backup.list_backups(root), automation.Retention(daily=1, weekly=0, monthly=0))
    assert plan["remove"] == [first]

    (root / first / backup.MANIFEST).write_text("{}", encoding="utf-8")
    applied = automation.apply_retention(root, automation.Retention(daily=1, weekly=0, monthly=0))

    assert first not in applied["removed"] and (root / first).is_dir()


# ============================================================
# THE RUNNER
# ============================================================

def runner_for(tmp_path, schedule="daily@02:30", **options):

    settings = automation.BackupSettings(tmp_path / "backups", automation.parse_schedule(schedule),
                                         automation.Retention(daily=2, weekly=0, monthly=0), min_free_mb=0)
    options.setdefault("disk_usage", lambda path: Free(100_000))

    return automation.BackupRunner(small_sources(tmp_path), settings, **options)


def test_a_run_records_its_status_metrics_and_applies_retention(tmp_path):

    registry = metrics.Registry()
    clock = iter(at(f"2026-11-0{day}T02:30:00") + timedelta(seconds=step) for day in range(1, 9) for step in (0, 1))
    runner = runner_for(tmp_path, metrics=registry, clock=lambda: next(clock))

    results = [runner.run(backup.SCHEDULED) for _ in range(4)]

    assert all(result["outcome"] == "success" for result in results)
    assert runner.status()["last_success"]["backup_id"] == results[-1]["backup_id"]
    assert registry.value("logidoc_backup_runs_total", {"outcome": "success"}) == 4
    assert registry.value("logidoc_backup_last_success_timestamp_seconds") is not None
    assert len([item for item in backup.list_backups(tmp_path / "backups") if item["usable"]]) == 2


def test_a_failed_run_reports_and_alerts_and_removes_nothing(tmp_path):

    reported = []
    runner = runner_for(tmp_path, on_failure=lambda category, message: reported.append((category, message)))
    first = runner.run(backup.SCHEDULED)

    runner.disk_usage = lambda path: Free(0)
    runner.settings = automation.BackupSettings(runner.settings.root, runner.settings.schedule,
                                                automation.Retention(0, 0, 0), min_free_mb=10)
    failed = runner.run(backup.SCHEDULED)

    assert failed["outcome"] == "failed" and failed["category"] == "insufficient_space"
    assert reported and reported[0][0] == "insufficient_space"
    assert runner.status()["consecutive_failures"] == 1
    # The earlier backup is still there: no cleanup when space is short
    assert (tmp_path / "backups" / first["backup_id"]).is_dir()


def test_runs_never_overlap(tmp_path):

    runner = runner_for(tmp_path)
    runner.lock.acquire()

    try:
        with pytest.raises(backup.BackupBusy):
            runner.run()
        assert runner.run_if_due(at("2026-11-09T03:00:00")) is None
    finally:
        runner.lock.release()

    # Another process holding the backup folder: the attempt fails as busy
    (tmp_path / "backups").mkdir()
    with FileLock(tmp_path / "backups" / backup.LOCK_NAME):
        assert runner.run()["category"] == "busy"


def test_run_if_due_follows_the_schedule(tmp_path):

    runner = runner_for(tmp_path, clock=lambda: at("2026-11-09T02:31:00"))

    assert runner.run_if_due(at("2026-11-09T02:31:00"))["outcome"] == "success"
    assert runner.run_if_due(at("2026-11-09T05:00:00")) is None                  # already done today
    assert runner_for(tmp_path / "off", schedule="off").run_if_due(at("2026-11-09T05:00:00")) is None


# ============================================================
# THE COMMAND LINE
# ============================================================

@pytest.fixture
def cli(tmp_path, monkeypatch):
    """scripts/manage_backups.py on a synthetic installation (config pointed at temporary folders)."""

    import importlib.util

    import config

    sources = small_sources(tmp_path)
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "RAW_DATA_DIR", data / "raw")
    monkeypatch.setattr(config, "CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr(config, "REVIEW_DB_PATH", data / "audit_reviews.sqlite3")
    monkeypatch.setattr(config, "HISTORY_DB_PATH", data / "audit_history.sqlite3")
    monkeypatch.setattr(config, "AUTH_DB_PATH", data / "auth.sqlite3")
    monkeypatch.setattr(config, "CARRIER_DB_PATH", data / "carrier_registry.sqlite3")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    for name in ("LOGIDOC_BACKUP_DIR", "LOGIDOC_INGESTION_DB", "LOGIDOC_EXTRACTION_DB", "LOGIDOC_BACKUP_SCHEDULE"):
        monkeypatch.delenv(name, raising=False)
    # The free-space reserve is tested on its own; here it must not depend on this machine's disk
    monkeypatch.setenv("LOGIDOC_BACKUP_MIN_FREE_MB", "0")

    spec = importlib.util.spec_from_file_location("manage_backups", PROJECT_ROOT / "scripts" / "manage_backups.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    # Every folder the command uses is the test's own
    used = script.sources()
    for path in [used.raw_dir, used.chroma_dir, used.archive_dir, used.quarantine_dir, *used.databases.values(),
                 script.settings().root]:
        assert tmp_path in Path(path).parents, path

    return script, sources, data


def test_the_command_line_creates_lists_verifies_and_restores(cli, tmp_path, capsys):

    script, sources, data = cli

    assert script.main(["create", "--label", "cli"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["outcome"] == "success" and created["index_included"] is True

    assert script.main(["list"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert listed["backup_id"] == created["backup_id"] and listed["usable"]

    assert script.main(["verify", created["backup_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]

    assert script.main(["restore", created["backup_id"], "--target", str(tmp_path / "restored")]) == 0
    output = capsys.readouterr().out
    assert '"status": "restored"' in output and "LOGIDOC_DATA_DIR=" in output

    # Never into the live data
    assert script.main(["restore", created["backup_id"], "--target", str(data / "x")]) == 1
    assert "separate folder" in capsys.readouterr().err

    assert script.main(["prune"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n\n")[0])["removed"] == []


def test_the_command_line_leaves_the_index_out_while_the_server_runs(cli, capsys):

    script, sources, data = cli

    with FileLock(backup.server_lock_path(data)):
        assert script.main(["create", "--require-index"]) == 1
        assert "server is running" in capsys.readouterr().err

        assert script.main(["create"]) == 0
        assert json.loads(capsys.readouterr().out)["index_included"] is False

    [made] = backup.list_backups(sources.raw_dir.parent.parent / "backups")
    manifest, _ = backup.load_manifest(sources.raw_dir.parent.parent / "backups" / made["backup_id"])
    assert "server was running" in manifest["components"]["vector_index"]["reason"]


def test_activation_from_the_command_line_needs_the_id_twice(cli, tmp_path, capsys):

    script, sources, data = cli
    script.main(["create"])
    backup_id = json.loads(capsys.readouterr().out)["backup_id"]
    script.main(["restore", backup_id, "--target", str(tmp_path / "restored")])
    capsys.readouterr()

    assert script.main(["activate", "--restored", str(tmp_path / "restored"), "--backup-id", backup_id,
                        "--confirm", "yes"]) == 1
    assert "Type the backup ID" in capsys.readouterr().err
