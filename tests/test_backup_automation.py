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

import alerts  # noqa: E402
import backup  # noqa: E402
import backup_automation as automation  # noqa: E402
import metrics  # noqa: E402
from file_lock import FileLock  # noqa: E402
from test_api import Rig  # noqa: E402
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
# THE SERVER'S OPERATIONS: MONITOR AND ALERTS
# ============================================================

class Collector:

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def test_the_monitor_raises_and_resolves_alerts(tmp_path):

    rig = Rig(tmp_path, "required")
    operations = rig.state.operations
    operations.registry = metrics.Registry()              # no requests of other tests in the window
    collector = Collector()
    operations.alerts.notifiers = [collector]
    operations.list_models = lambda: None                 # the model service does not answer
    operations.disk_usage = lambda path: Free(100)        # 100 MB free: critical

    for _ in range(3):
        operations.monitor_once()

    active = {alert["key"]: alert for alert in operations.alerts.view()["active"]}
    assert active["disk_low"]["severity"] == "critical"
    assert active["ai_service_down"]["severity"] == "warning"
    assert "audit" in active["ai_service_down"]["message"]               # says what still works
    assert str(tmp_path) not in json.dumps(operations.alerts.view())

    operations.disk_usage = lambda path: Free(50_000)
    operations.list_models = lambda: {"nomic-embed-text:latest", "nemotron-3-nano:4b"}
    operations.monitor_once()

    assert operations.alerts.view()["active"] == []
    assert {alert["key"] for alert in collector.sent if alert["state"] == "resolved"} == {"disk_low", "ai_service_down"}


def test_a_backup_failure_is_a_critical_alert_until_a_backup_succeeds(tmp_path):

    rig = Rig(tmp_path, "required")
    operations = rig.state.operations
    operations.alerts.notifiers = [Collector()]

    operations.runner.disk_usage = lambda path: Free(0)
    assert operations.runner.run()["outcome"] == "failed"
    [active] = operations.alerts.view()["active"]
    assert active["key"] == "backup_failed" and active["severity"] == "critical"

    operations.runner.disk_usage = lambda path: Free(100_000)
    operations.runner.settings = automation.BackupSettings(operations.runner.settings.root,
                                                           operations.runner.settings.schedule,
                                                           operations.runner.settings.retention, min_free_mb=0)
    assert operations.runner.run()["outcome"] == "success"
    assert operations.alerts.view()["active"] == []


def test_an_alert_is_not_repeated_within_its_cooldown(tmp_path):

    now = {"value": at("2026-11-09T00:00:00")}
    collector = Collector()
    manager = alerts.AlertManager(alerts.AlertSettings(cooldown_seconds=3600), [collector], clock=lambda: now["value"])

    manager.fire("disk_low", "warning", "low")
    manager.fire("disk_low", "warning", "low")
    now["value"] += timedelta(minutes=30)
    manager.fire("disk_low", "warning", "low")
    assert len(collector.sent) == 1

    manager.fire("disk_low", "critical", "lower")               # more severe: sent at once
    now["value"] += timedelta(hours=1, seconds=1)
    manager.fire("disk_low", "critical", "lower")               # cooldown passed
    assert [alert["severity"] for alert in collector.sent] == ["warning", "critical", "critical"]


def test_failures_latency_and_database_errors_in_the_window_alert(tmp_path):

    registry = metrics.Registry(clock=lambda: 1000.0)
    manager = alerts.AlertManager(alerts.AlertSettings(server_errors=3, latency_ms=500, min_requests=5), [Collector()])

    for _ in range(3):
        registry.request("GET", "/api/overview", 500, 0.01)
    for _ in range(5):
        registry.request("GET", "/api/carriers", 200, 2.0)
    registry.error("database")

    manager.evaluate(window=registry.window_stats(300, now=1000.0))
    keys = {alert["key"] for alert in manager.view()["active"]}

    assert keys == {"api_failures", "latency_high", "database_errors"}


def test_a_stale_backup_alerts_only_while_a_schedule_is_on(tmp_path):

    now = at("2026-11-09T12:00:00")
    manager = alerts.AlertManager(alerts.AlertSettings(backup_max_age_hours=26), [Collector()], clock=lambda: now)
    old = {"last_success": {"at": backup.iso(now - timedelta(hours=30))}}

    manager.evaluate(backup_status=old, schedule_enabled=False)
    assert manager.view()["active"] == []

    manager.evaluate(backup_status=old, schedule_enabled=True)
    assert [alert["key"] for alert in manager.view()["active"]] == ["backup_stale"]


def test_the_webhook_receives_the_alert_and_its_url_is_never_logged(caplog):

    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            return b""

    def opener(request, timeout):
        sent.update(url=request.full_url, body=json.loads(request.data), timeout=timeout)
        return Response()

    secret = "https://hooks.example.test/services/T000/SECRET-TOKEN"
    manager = alerts.AlertManager(alerts.AlertSettings(), [alerts.LogNotifier(), alerts.WebhookNotifier(secret, opener=opener)])
    manager.fire("backup_failed", "critical", "The backup failed (storage_error).")

    assert sent["url"] == secret and sent["timeout"] == 5.0
    assert sent["body"]["source"] == "logidoc-rag" and sent["body"]["alert"]["key"] == "backup_failed"

    def unreachable(request, timeout):
        raise OSError(f"cannot reach {secret}")

    failing = alerts.AlertManager(alerts.AlertSettings(), [alerts.WebhookNotifier(secret, opener=unreachable)])
    with caplog.at_level("WARNING"):
        failing.fire("disk_low", "warning", "low")

    assert "SECRET-TOKEN" not in caplog.text and "hooks.example.test" not in caplog.text


# ============================================================
# THE ADMINISTRATORS' ENDPOINTS
# ============================================================

def wait_until(condition, seconds=60):

    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)

    return False


def test_only_an_administrator_sees_and_starts_backups(tmp_path):

    rig = Rig(tmp_path, "required")

    assert rig.client().get("/api/admin/backups").status_code == 401
    assert rig.client("bob").get("/api/admin/backups").status_code == 403
    assert rig.client("bob").post("/api/admin/backups", json={}).status_code == 403
    assert rig.client().post("/api/admin/backups", json={}).status_code == 401

    for url in ("/api/admin/metrics", "/api/admin/alerts"):
        assert rig.client().get(url).status_code == 401 and rig.client("bob").get(url).status_code == 403

    # The CSRF rule holds here too
    bare = rig.client("alice")
    bare.headers.pop("X-LogiDoc-Client")
    assert bare.post("/api/admin/backups", json={}).status_code == 403


def test_an_administrator_makes_a_backup_and_sees_it(tmp_path):

    rig = Rig(tmp_path, "required")
    admin = rig.client("alice")

    assert admin.post("/api/admin/backups", json={"label": "before the upgrade"}).json() == {"started": True}
    assert wait_until(lambda: admin.get("/api/admin/backups").json()["status"].get("last_success"))

    overview = admin.get("/api/admin/backups").json()
    [made] = overview["backups"]

    assert made["usable"] and made["origin"] == "manual" and made["label"] == "before the upgrade"
    assert overview["schedule"] == "off" and overview["running"] is False
    assert str(tmp_path) not in json.dumps(overview)

    # Made inside the test's own folder, never the project's backups/
    assert (rig.state.backup_dir / made["backup_id"]).is_dir()


def test_a_second_backup_while_one_runs_is_refused(tmp_path):

    rig = Rig(tmp_path, "required")
    runner = rig.state.operations.runner
    runner.lock.acquire()

    try:
        reply = rig.client("alice").post("/api/admin/backups", json={})
        assert reply.status_code == 409 and reply.json() == {"detail": "A backup is already running."}
    finally:
        runner.lock.release()


def test_the_metrics_endpoint_in_json_and_prometheus_formats(tmp_path):

    rig = Rig(tmp_path, "required")
    admin = rig.client("alice")
    admin.get("/api/overview")

    snapshot = admin.get("/api/admin/metrics").json()
    routes = {item["labels"]["route"] for item in snapshot["counters"]["logidoc_http_requests_total"]}
    assert "/api/overview" in routes and snapshot["uptime_seconds"] >= 0

    text = admin.get("/api/admin/metrics", params={"format": "prometheus"})
    assert text.headers["content-type"].startswith("text/plain")
    assert "# TYPE logidoc_http_requests_total counter" in text.text
    assert 'logidoc_http_request_duration_seconds_bucket{route="/api/overview",le="+Inf"}' in text.text

    assert admin.get("/api/admin/alerts").json() == {"active": [], "recent": []}


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
