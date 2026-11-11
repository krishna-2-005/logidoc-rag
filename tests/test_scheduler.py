"""
The integration scheduler: job execution and history, leases (no overlap),
recovery after a stop, retries with limits, manual triggers and their
permissions, single ownership across processes, graceful shutdown, and the
separation from the liveness endpoint.

Jobs run on temporary integration stores; no network.

Run from the project root:
    python -m pytest tests/test_scheduler.py
"""

import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from integration_support import ALICE, BOB, integration_rig, register

import integration_store  # noqa: E402
import scheduler as scheduler_module  # noqa: E402
from scheduler import JobBusy, JobSpec, Scheduler  # noqa: E402


class Clock:

    def __init__(self):

        self.now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

    def __call__(self):

        return self.now

    def advance(self, seconds):

        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():

    return Clock()


def make(tmp_path, clock, specs, **options):

    path = tmp_path / "integrations.sqlite3"
    integration_store.IntegrationStore(path)

    return Scheduler(path, specs, clock=clock, **options)


def counting(name="count.job", interval=60, **options):

    calls = []

    def run(context):
        calls.append(context.trigger)
        return {"calls": len(calls)}

    return JobSpec(name, "Counts its runs.", interval, run, **options), calls


def failing(name="fail.job", interval=600, **options):

    def run(context):
        raise RuntimeError("The partner said no (token=abcd1234 at C:\\secret\\path.txt).")

    return JobSpec(name, "Always fails.", interval, run, **options)


def rows(scheduler, query, *params):

    with closing(sqlite3.connect(scheduler.path)) as connection:
        return connection.execute(query, params).fetchall()


# ============================================================
# EXECUTION
# ============================================================

def test_a_due_job_runs_once_and_records_its_run(tmp_path, clock):

    spec, calls = counting()
    scheduler = make(tmp_path, clock, [spec])

    assert scheduler.tick() == ["count.job"]
    assert scheduler.tick() == []                                       # not due again before its interval
    clock.advance(61)
    assert scheduler.tick() == ["count.job"]

    job = scheduler.job("count.job")
    runs = scheduler.runs("count.job")

    assert calls == ["schedule", "schedule"] and runs["total"] == 2
    assert runs["runs"][0]["status"] == "succeeded" and runs["runs"][0]["summary"] == {"calls": 2}
    assert job["last_success_at"] == clock.now.isoformat(timespec="seconds") and job["consecutive_failures"] == 0
    assert datetime.fromisoformat(job["next_run_at"]) == clock.now + timedelta(seconds=60)
    assert job["running"] is False and job["delayed"] is False


def test_nothing_runs_and_nothing_is_created_before_anything_is_configured(tmp_path, clock):

    spec, calls = counting()
    scheduler = Scheduler(tmp_path / "integrations.sqlite3", [spec], clock=clock)

    assert scheduler.tick() == [] and scheduler.jobs() == [] and calls == []
    assert not scheduler.path.exists()
    assert scheduler.status()["configured"] is False


def test_a_disabled_job_does_not_run_and_the_interval_can_change(tmp_path, clock):

    spec, calls = counting()
    scheduler = make(tmp_path, clock, [spec])

    scheduler.set_job(ALICE, "count.job", enabled=False)
    assert scheduler.tick() == [] and calls == []

    job = scheduler.set_job(ALICE, "count.job", enabled=True, interval_seconds=30)
    assert job["enabled"] and job["interval_seconds"] == 30

    with pytest.raises(scheduler_module.SchedulerError):
        scheduler.set_job(ALICE, "count.job", interval_seconds=5)
    with pytest.raises(PermissionError):
        scheduler.set_job(BOB, "count.job", enabled=False)
    with pytest.raises(LookupError):
        scheduler.set_job(ALICE, "no.such.job", enabled=False)


# ============================================================
# LOCKING AND OVERLAP
# ============================================================

def test_a_running_job_cannot_be_claimed_again(tmp_path, clock):

    spec, _ = counting()
    scheduler = make(tmp_path, clock, [spec])
    store = scheduler.store()
    scheduler.ensure_jobs(store)

    scheduler.claim(store, "count.job", "schedule")

    with pytest.raises(JobBusy):
        scheduler.claim(store, "count.job", "manual")

    # Another scheduler (another process) is refused as well
    other = Scheduler(scheduler.path, [spec], clock=clock)
    with pytest.raises(JobBusy):
        other.claim(store, "count.job", "schedule")


def test_a_manual_trigger_while_the_job_runs_is_refused(tmp_path, clock):

    started, release = threading.Event(), threading.Event()

    def slow(context):
        started.set()
        release.wait(10)
        return {}

    scheduler = make(tmp_path, clock, [JobSpec("slow.job", "Waits.", 60, slow)])
    run_id = scheduler.trigger(ALICE, "slow.job")
    assert started.wait(10)

    with pytest.raises(JobBusy):
        scheduler.trigger(ALICE, "slow.job")
    assert scheduler.tick() == []                                       # the schedule waits for it too

    release.set()
    for worker in scheduler.workers:
        worker.join(10)

    run = scheduler.run(run_id)
    assert run["status"] == "succeeded" and run["trigger"] == "manual" and run["requested_by"] == "alice"


# ============================================================
# RESTART RECOVERY
# ============================================================

def test_a_run_interrupted_by_a_stop_is_recorded_and_the_job_runs_again(tmp_path, clock):

    spec, calls = counting(interval=3600)
    scheduler = make(tmp_path, clock, [spec])
    store = scheduler.store()
    scheduler.ensure_jobs(store)
    run_id, _ = scheduler.claim(store, "count.job", "schedule")          # the process then stops

    # A new process after the lease ran out
    clock.advance(spec.timeout_seconds + 120)
    restarted = Scheduler(scheduler.path, [spec], clock=clock)

    assert restarted.tick() == ["count.job"] and calls == ["schedule"]
    interrupted = restarted.run(run_id)
    assert interrupted["status"] == "interrupted" and interrupted["error_category"] == "interrupted"
    assert restarted.runs("count.job")["runs"][0]["status"] == "succeeded"


def test_a_job_overdue_at_start_runs_once_not_once_per_missed_interval(tmp_path, clock):

    spec, calls = counting(interval=60)
    scheduler = make(tmp_path, clock, [spec])
    scheduler.tick()

    clock.advance(3600)                                                 # an hour down: 60 intervals missed
    assert scheduler.tick() == ["count.job"] and scheduler.tick() == []
    assert len(calls) == 2
    assert scheduler.runs("count.job")["runs"][0]["delay_seconds"] == 3600 - 60


def test_an_interrupted_manual_trigger_is_not_lost(tmp_path, clock):

    spec, calls = counting(interval=3600)
    scheduler = make(tmp_path, clock, [spec])
    scheduler.tick()
    store = scheduler.store()
    scheduler.claim(store, "count.job", "manual", "alice")              # asked for, then the server stopped

    clock.advance(spec.timeout_seconds + 120)
    assert Scheduler(scheduler.path, [spec], clock=clock).tick() == ["count.job"]
    assert len(calls) == 2


# ============================================================
# FAILURES AND RETRIES
# ============================================================

def test_a_failing_job_is_retried_with_back_off_then_waits_its_interval_and_alerts(tmp_path, clock):

    alerts = []
    spec = failing(interval=3600, max_retries=2, retry_delay_seconds=60)
    scheduler = make(tmp_path, clock, [spec], on_failure=lambda *args: alerts.append(args))

    waits = []
    for _ in range(3):
        scheduler.tick()
        job = scheduler.job("fail.job")
        waits.append((datetime.fromisoformat(job["next_run_at"]) - clock.now).total_seconds())
        clock.advance(waits[-1])

    assert waits == [60, 120, 3600]                                     # 2 retries with back-off, then the interval
    assert job["consecutive_failures"] == 3 and alerts == [("fail.job", 3, "RuntimeError")]

    run = scheduler.runs("fail.job")["runs"][0]
    assert run["status"] == "failed" and run["error_category"] == "RuntimeError"
    assert "abcd1234" not in run["error_message"] and "secret" not in run["error_message"]     # redacted
    assert "abcd1234" not in job["last_error"]


def test_a_success_resets_the_failure_count(tmp_path, clock):

    state = {"fail": True}

    def flaky(context):
        if state["fail"]:
            raise ConnectionError("down")
        return {"ok": True}

    scheduler = make(tmp_path, clock, [JobSpec("flaky.job", "Flaky.", 600, flaky, retry_delay_seconds=30)])
    scheduler.tick()
    assert scheduler.job("flaky.job")["consecutive_failures"] == 1

    state["fail"] = False
    run_id = scheduler.trigger(ALICE, "flaky.job", retry=True)
    for worker in scheduler.workers:
        worker.join(10)

    assert scheduler.run(run_id)["trigger"] == "retry" and scheduler.job("flaky.job")["consecutive_failures"] == 0
    with pytest.raises(scheduler_module.SchedulerError, match="failed"):
        scheduler.trigger(ALICE, "flaky.job", retry=True)              # nothing failed: nothing to retry


# ============================================================
# OWNERSHIP AND SHUTDOWN
# ============================================================

def test_only_one_process_owns_the_schedule(tmp_path, clock):

    spec, _ = counting()
    first = make(tmp_path, clock, [spec], poll_seconds=0.05)
    second = Scheduler(first.path, [spec], clock=clock, poll_seconds=0.05)

    try:
        assert first.start() is True
        assert second.start() is False and second.running is False

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and first.status()["owner"] is None:
            time.sleep(0.05)

        status = first.status()
        assert status["running_here"] is True and status["owner"]["this_process"] is True
        assert second.status()["owner"]["this_process"] is False and second.status()["running_here"] is False
    finally:
        first.stop()

    assert second.start() is True                                       # the lock was released
    second.stop()
    assert rows(second, "SELECT COUNT(*) FROM scheduler_owner")[0][0] == 0


def test_a_shutdown_stops_a_running_job_at_its_next_check(tmp_path, clock):

    progress = {"items": 0}
    started = threading.Event()

    def long_job(context):
        started.set()
        while not context.should_stop():
            progress["items"] += 1
            time.sleep(0.01)
        return {"items": progress["items"], "stopped_early": True}

    scheduler = make(tmp_path, clock, [JobSpec("long.job", "Long.", 60, long_job, timeout_seconds=120)], poll_seconds=0.05)
    scheduler.start()
    assert started.wait(10)

    began = time.monotonic()
    scheduler.stop()

    assert time.monotonic() - began < 10 and scheduler.running is False
    run = scheduler.runs("long.job")["runs"][0]
    assert run["status"] == "succeeded" and run["summary"]["stopped_early"] is True
    job = scheduler.job("long.job")
    assert job["running"] is False
    assert not scheduler.lock.held


def test_a_job_past_its_deadline_stops_and_continues_next_time(tmp_path, clock):

    def deadline_job(context):
        context.deadline = time.monotonic() - 1                         # as if the timeout had passed
        return {"stopped": context.should_stop()}

    scheduler = make(tmp_path, clock, [JobSpec("deadline.job", "Deadline.", 60, deadline_job)])
    scheduler.tick()

    assert scheduler.runs("deadline.job")["runs"][0]["summary"] == {"stopped": True}


# ============================================================
# THE API
# ============================================================

@pytest.fixture
def rig(tmp_path):

    return integration_rig(tmp_path)


def test_jobs_through_the_api(rig):

    alice, bob = rig.client("alice"), rig.client("bob")

    assert alice.get("/api/integrations/jobs").json()["configured"] is False
    assert alice.post("/api/integrations/jobs/audit_feed.poll/run").status_code == 422     # nothing configured yet

    register(alice)
    status = alice.get("/api/integrations/jobs").json()
    names = [job["name"] for job in status["jobs"]]

    assert status["configured"] and status["webhook_delivery"]["enabled"]
    assert set(names) == {"audit_feed.poll", "webhooks.collect_activity", "webhooks.deliver_pending", "webhooks.retry_failed",
                          "integrations.sync", "jobs.detect_overdue", "integrations.health"}

    started = alice.post("/api/integrations/jobs/webhooks.collect_activity/run")
    assert started.status_code == 202 and started.json()["trigger"] == "manual"
    for worker in rig.state.integrations.scheduler.workers:
        worker.join(30)

    run = alice.get(f"/api/integrations/job-runs/{started.json()['run_id']}").json()
    assert run["status"] == "succeeded" and run["requested_by"] == "alice" and "owner" not in run

    history = alice.get("/api/integrations/jobs/webhooks.collect_activity/runs").json()
    assert history["total"] == 1 and history["runs"][0]["id"] == run["id"]

    changed = alice.patch("/api/integrations/jobs/integrations.sync", json={"enabled": False, "interval_seconds": 120}).json()
    assert changed["enabled"] is False and changed["interval_seconds"] == 120
    assert alice.patch("/api/integrations/jobs/integrations.sync", json={"interval_seconds": 1}).status_code == 422
    assert alice.post("/api/integrations/jobs/webhooks.collect_activity/retry").status_code == 422   # it did not fail
    assert alice.post("/api/integrations/jobs/no.such.job/run").status_code == 404
    assert alice.get("/api/integrations/jobs/no.such.job/runs").status_code == 404

    for method, url in (("get", "/api/integrations/jobs"), ("patch", "/api/integrations/jobs/integrations.sync"),
                        ("post", "/api/integrations/jobs/integrations.sync/run"),
                        ("post", "/api/integrations/jobs/integrations.sync/retry"),
                        ("get", "/api/integrations/jobs/integrations.sync/runs"), ("get", "/api/integrations/job-runs/1")):
        assert getattr(bob, method)(url, **({"json": {}} if method != "get" else {})).status_code == 403, url

    bare = rig.client("alice")
    bare.headers.pop("X-LogiDoc-Client")
    assert bare.post("/api/integrations/jobs/integrations.sync/run").status_code == 403


def test_a_running_job_answers_409_to_a_second_trigger(rig):

    alice = rig.client("alice")
    register(alice)
    scheduler = rig.state.integrations.scheduler
    store = scheduler.store()
    scheduler.ensure_jobs(store)
    scheduler.claim(store, "integrations.sync", "schedule")

    reply = alice.post("/api/integrations/jobs/integrations.sync/run")
    assert reply.status_code == 409 and "running" in reply.json()["detail"]
    job = next(job for job in alice.get("/api/integrations/jobs").json()["jobs"] if job["name"] == "integrations.sync")
    assert job["running"] is True and job["running_here"] is True and "lease_owner" not in job


def test_scheduler_health_is_not_part_of_liveness(rig):

    alice = rig.client("alice")
    register(alice)
    assert all(not job["delayed"] for job in alice.get("/api/integrations/jobs").json()["jobs"])

    # The server was down for a long time: every job is far past its next run
    with closing(sqlite3.connect(rig.locations.integration_path())) as connection, connection:
        assert connection.execute("UPDATE scheduled_jobs SET next_run_at = '2000-01-01T00:00:00+00:00'").rowcount == 7

    jobs = alice.get("/api/integrations/jobs").json()["jobs"]
    assert all(job["delayed"] for job in jobs)

    assert alice.get("/api/health").json() == {"status": "ok"}
    assert "scheduler" not in alice.get("/api/ready").json()["checks"]

    summary = rig.state.integrations.detect_overdue(type("Context", (), {"should_stop": lambda self: False})())
    assert len(summary["overdue_jobs"]) == len(jobs)
    assert any(alert["key"] == "integration_jobs_overdue" for alert in rig.state.operations.alerts.view()["active"])


def test_the_jobs_record_metrics(rig):

    import metrics

    alice = rig.client("alice")
    register(alice)
    before = metrics.REGISTRY.value("logidoc_jobs_total", {"job": "webhooks.deliver_pending", "outcome": "succeeded"}) or 0

    rig.state.integrations.scheduler.tick()

    assert metrics.REGISTRY.value("logidoc_jobs_total", {"job": "webhooks.deliver_pending", "outcome": "succeeded"}) == before + 1
    assert "logidoc_job_seconds" in metrics.REGISTRY.prometheus()
