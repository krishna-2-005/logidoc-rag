"""
The integration scheduler: background jobs with persistent status
(docs/SCHEDULED_SYNCHRONISATION.md).

No task queue is introduced: the application is one server process per data
folder (src/api/serve.py holds the data-folder lock), so one thread runs the
jobs, and SQLite (the integration store) keeps their state.

    jobs            scheduled_jobs: enabled, interval, next run, the last
                    start, success and failure, consecutive failures
    history         job_runs: one row per run (trigger, owner, start, end,
                    duration, how late it started, summary, error category);
                    never deleted
    one owner       start() takes a lock file beside the store
                    (.integrations-scheduler.lock). A second process (another
                    worker, a second server) does not get it and runs no job;
                    its status says the schedule is owned elsewhere
    no overlap      a run first claims the job's lease in one transaction
                    (lease free, or expired); a second claim is refused
                    (JobBusy), whether it comes from the schedule or an
                    administrator's trigger
    deadline        each job has a timeout; JobContext.should_stop() is
                    checked between items, so a job stops early on shutdown
                    or at its deadline and the next run continues (every job
                    is idempotent). should_stop() also renews the lease
    failures        a failed run is retried after retry_delay * 2^(n-1)
                    (within the interval) up to max_retries consecutive
                    failures; after that the job waits its normal interval and
                    on_failure (an alert) is called
    restart         a run whose lease expired while 'running' (the process
                    stopped) is marked 'interrupted' and the job is due at
                    once: nothing is silently lost. A job overdue at start
                    runs once (not once per missed interval)
    shutdown        stop() lets the running job finish its current item, then
                    releases the lease, the owner row and the lock

Scheduler health is its own endpoint (GET /api/integrations/jobs), never part
of /api/health (liveness) or /api/ready.
"""

import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import authz
import integration_store as istore
from file_lock import FileLock, LockBusy
from integration_store import dumps, later, loads, parse_time, text_time, utc_now
from redaction import redact

log = logging.getLogger("logidoc.scheduler")

TRIGGERS = ("schedule", "manual", "retry")
HEARTBEAT_SECONDS = 15
MIN_INTERVAL = 10
MAX_INTERVAL = 7 * 86400


class JobBusy(RuntimeError):
    """The job is running (its lease is held)."""


class SchedulerError(ValueError):
    """A request that is refused (the message says why)."""


@dataclass(frozen=True)
class JobSpec:

    name: str
    description: str
    interval_seconds: int
    run: Callable
    timeout_seconds: int = 300
    max_retries: int = 3
    retry_delay_seconds: int = 60


@dataclass
class JobContext:
    """What a running job can ask: whether to stop, and its trigger."""

    scheduler: "Scheduler"
    job: str
    run_id: int
    trigger: str
    deadline: float
    requested_by: str | None = None
    last_renewal: float = field(default_factory=time.monotonic)

    def should_stop(self):

        now = time.monotonic()

        if now - self.last_renewal > 30:
            self.scheduler.renew(self.job)
            self.last_renewal = now

        return self.scheduler.stopping.is_set() or now >= self.deadline


def new_owner():

    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Scheduler:

    def __init__(self, path, specs, *, clock=utc_now, metrics=None, on_failure=None, on_recovery=None,
                 poll_seconds=5):

        self.path = Path(path)
        self.specs = {spec.name: spec for spec in specs}
        self.clock = clock
        self.metrics = metrics
        self.on_failure = on_failure
        self.on_recovery = on_recovery
        self.poll_seconds = poll_seconds
        self.owner = new_owner()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.lock = FileLock(self.lock_path)
        self.workers = []
        self.last_heartbeat = None

    @property
    def lock_path(self):

        return self.path.with_name(f".{self.path.stem}-scheduler.lock")

    def store(self, create=False):

        return istore.open_store(self.path, create=create)

    # --------------------------------------------------------
    # Jobs
    # --------------------------------------------------------

    def ensure_jobs(self, store):
        """Adds the row of every known job that has none (due now). Reads first: no write when all exist."""

        with store.read() as connection:
            known = {row["name"] for row in connection.execute("SELECT name FROM scheduled_jobs")}

        missing = [spec for spec in self.specs.values() if spec.name not in known]

        if not missing:
            return

        now = text_time(self.clock())

        with store.write() as connection:
            for spec in missing:
                connection.execute(
                    "INSERT OR IGNORE INTO scheduled_jobs (name, enabled, interval_seconds, next_run_at, updated_at) "
                    "VALUES (?, 1, ?, ?, ?)", (spec.name, spec.interval_seconds, now, now))

    def recover(self, store):
        """Runs left 'running' by a stopped process (lease expired): interrupted, and the job due now."""

        now = self.clock()
        recovered = []

        # The usual case (nothing expired) needs no write transaction
        with store.read() as connection:
            if connection.execute("SELECT 1 FROM scheduled_jobs WHERE lease_until IS NOT NULL AND lease_until < ? LIMIT 1",
                                  (text_time(now),)).fetchone() is None:
                return recovered

        with store.write() as connection:
            for job in connection.execute("SELECT * FROM scheduled_jobs WHERE lease_until IS NOT NULL AND lease_until < ?",
                                          (text_time(now),)).fetchall():
                connection.execute(
                    "UPDATE job_runs SET status = 'interrupted', finished_at = ?, error_category = 'interrupted', "
                    "error_message = 'The process stopped during the run; the job runs again.' "
                    "WHERE job = ? AND status = 'running'", (text_time(now), job["name"]))
                connection.execute("UPDATE scheduled_jobs SET lease_owner = NULL, lease_until = NULL, next_run_at = ?, "
                                   "updated_at = ? WHERE name = ?", (text_time(now), text_time(now), job["name"]))
                recovered.append(job["name"])

        for name in recovered:
            log.warning("Job %s was interrupted by a stop; it runs again.", name,
                        extra={"operation": "job", "job": name, "outcome": "interrupted"})

        if recovered and self.on_recovery is not None:
            self.on_recovery(recovered)

        return recovered

    def claim(self, store, name, trigger, requested_by=None):
        """Takes the job's lease and opens its run. Returns (run_id, delay) or raises JobBusy."""

        spec = self.specs[name]
        now = self.clock()

        with store.write() as connection:
            job = connection.execute("SELECT * FROM scheduled_jobs WHERE name = ?", (name,)).fetchone()

            if job is None:
                raise LookupError("No such job.")

            lease = parse_time(job["lease_until"])

            if lease is not None and lease > now:
                raise JobBusy(f"The job {name} is running.")

            scheduled = parse_time(job["next_run_at"])
            delay = max(int((now - scheduled).total_seconds()), 0) if trigger == "schedule" and scheduled else 0
            connection.execute(
                "UPDATE scheduled_jobs SET lease_owner = ?, lease_until = ?, last_started_at = ?, updated_at = ? WHERE name = ?",
                (self.owner, later(now, spec.timeout_seconds + 60), text_time(now), text_time(now), name))
            run_id = connection.execute(
                "INSERT INTO job_runs (job, trigger, requested_by, owner, started_at, status, delay_seconds) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?)", (name, trigger, requested_by, self.owner, text_time(now), delay)
            ).lastrowid

        return run_id, delay

    def renew(self, name):

        store = self.store()

        if store is None:
            return

        spec = self.specs[name]

        with store.write() as connection:
            connection.execute("UPDATE scheduled_jobs SET lease_until = ? WHERE name = ? AND lease_owner = ?",
                               (later(self.clock(), spec.timeout_seconds + 60), name, self.owner))

    def execute(self, store, name, run_id, delay, trigger, requested_by=None):
        """Runs a claimed job and records the outcome. Returns the run row."""

        spec = self.specs[name]
        started = time.monotonic()
        context = JobContext(self, name, run_id, trigger, started + spec.timeout_seconds, requested_by)
        error = None

        try:
            summary = spec.run(context) or {}
        except Exception as caught:  # noqa: BLE001 - a job's failure is recorded, never raised into the loop
            error, summary = caught, {}

        duration_ms = int((time.monotonic() - started) * 1000)
        now = self.clock()

        with store.write() as connection:
            job = connection.execute("SELECT * FROM scheduled_jobs WHERE name = ?", (name,)).fetchone()
            interval = job["interval_seconds"]

            if error is None:
                connection.execute("UPDATE job_runs SET status = 'succeeded', finished_at = ?, duration_ms = ?, summary = ? "
                                   "WHERE id = ?", (text_time(now), duration_ms, dumps(summary), run_id))
                connection.execute(
                    "UPDATE scheduled_jobs SET lease_owner = NULL, lease_until = NULL, last_finished_at = ?, last_success_at = ?, "
                    "last_error = NULL, consecutive_failures = 0, next_run_at = ?, updated_at = ? WHERE name = ?",
                    (text_time(now), text_time(now), later(now, interval), text_time(now), name))
                failures = 0
            else:
                category = type(error).__name__
                message = redact(str(error) or category, limit=300)
                failures = job["consecutive_failures"] + 1
                wait = (min(interval, spec.retry_delay_seconds * 2 ** (failures - 1)) if failures <= spec.max_retries
                        else interval)
                connection.execute("UPDATE job_runs SET status = 'failed', finished_at = ?, duration_ms = ?, "
                                   "error_category = ?, error_message = ? WHERE id = ?",
                                   (text_time(now), duration_ms, category, message, run_id))
                connection.execute(
                    "UPDATE scheduled_jobs SET lease_owner = NULL, lease_until = NULL, last_finished_at = ?, last_failure_at = ?, "
                    "last_error = ?, consecutive_failures = ?, next_run_at = ?, updated_at = ? WHERE name = ?",
                    (text_time(now), text_time(now), f"{category}: {message}", failures, later(now, wait), text_time(now), name))

        if self.metrics is not None:
            outcome = "succeeded" if error is None else "failed"
            self.metrics.inc("logidoc_jobs_total", {"job": name, "outcome": outcome})
            self.metrics.observe("logidoc_job_seconds", duration_ms / 1000, {"job": name})
            self.metrics.set("logidoc_job_delay_seconds", delay, {"job": name})
            if trigger == "retry" or (error is not None and failures <= spec.max_retries):
                self.metrics.inc("logidoc_job_retries_total", {"job": name})
            if error is None:
                self.metrics.set("logidoc_job_last_success_timestamp", now.timestamp(), {"job": name})

        if error is not None:
            log.warning("Job %s failed (%s consecutive): %s", name, failures, type(error).__name__,
                        extra={"operation": "job", "job": name, "outcome": "failed", "error_category": type(error).__name__})
            if failures > spec.max_retries and self.on_failure is not None:
                self.on_failure(name, failures, type(error).__name__)

        return self.run(run_id)

    def run_job(self, name, trigger="schedule", requested_by=None):
        """Claims and runs a job in this thread. Raises JobBusy when it is running."""

        store = self.store()

        if store is None:
            raise SchedulerError("Nothing is configured yet: the scheduler starts with the first webhook or integration.")

        self.ensure_jobs(store)
        run_id, delay = self.claim(store, name, trigger, requested_by)

        return self.execute(store, name, run_id, delay, trigger, requested_by)

    def trigger(self, principal, name, retry=False):
        """
        An administrator's run now (or retry of a failed job), in its own thread.
        The lease is claimed here, so a running job answers JobBusy at once.
        Returns the run id.
        """

        authz.require(principal, authz.INTEGRATIONS_MANAGE)

        if name not in self.specs:
            raise LookupError("No such job.")

        store = self.store()

        if store is None:
            raise SchedulerError("Nothing is configured yet: the scheduler starts with the first webhook or integration.")

        self.ensure_jobs(store)

        if retry:
            with store.read() as connection:
                job = connection.execute("SELECT consecutive_failures FROM scheduled_jobs WHERE name = ?", (name,)).fetchone()
            if not job["consecutive_failures"]:
                raise SchedulerError("Only a job whose last run failed can be retried.")

        trigger = "retry" if retry else "manual"
        run_id, delay = self.claim(store, name, trigger, principal.username)
        worker = threading.Thread(target=self.execute, args=(store, name, run_id, delay, trigger, principal.username),
                                  name=f"logidoc-job-{name}", daemon=True)
        self.workers = [item for item in self.workers if item.is_alive()] + [worker]
        worker.start()

        return run_id

    def set_job(self, principal, name, enabled=None, interval_seconds=None):

        authz.require(principal, authz.INTEGRATIONS_MANAGE)

        if name not in self.specs:
            raise LookupError("No such job.")

        if interval_seconds is not None and not MIN_INTERVAL <= interval_seconds <= MAX_INTERVAL:
            raise SchedulerError(f"The interval must be {MIN_INTERVAL} to {MAX_INTERVAL} seconds.")

        if enabled is None and interval_seconds is None:
            raise SchedulerError("Nothing to change.")

        store = self.store()

        if store is None:
            raise SchedulerError("Nothing is configured yet: the scheduler starts with the first webhook or integration.")

        self.ensure_jobs(store)
        now = self.clock()

        with store.write() as connection:
            if enabled is not None:
                connection.execute("UPDATE scheduled_jobs SET enabled = ?, updated_at = ? WHERE name = ?",
                                   (1 if enabled else 0, text_time(now), name))
            if interval_seconds is not None:
                connection.execute("UPDATE scheduled_jobs SET interval_seconds = ?, next_run_at = min(next_run_at, ?), "
                                   "updated_at = ? WHERE name = ?",
                                   (interval_seconds, later(now, interval_seconds), text_time(now), name))

        log.info("Job %s changed by %s (enabled=%s, interval=%s).", name, principal.username, enabled, interval_seconds,
                 extra={"operation": "job_settings", "job": name})

        return self.job(name)

    # --------------------------------------------------------
    # The loop
    # --------------------------------------------------------

    def tick(self):
        """Runs every due job once (one at a time). Returns the names run."""

        store = self.store()

        if store is None:
            return []

        self.ensure_jobs(store)
        self.recover(store)
        self.heartbeat(store)
        now = text_time(self.clock())

        with store.read() as connection:
            due = [row["name"] for row in connection.execute(
                "SELECT name FROM scheduled_jobs WHERE enabled = 1 AND next_run_at <= ? AND "
                "(lease_until IS NULL OR lease_until < ?) ORDER BY next_run_at", (now, now)) if row["name"] in self.specs]

        ran = []

        for name in due:
            if self.stopping.is_set():
                break
            try:
                run_id, delay = self.claim(store, name, "schedule")
            except JobBusy:
                continue
            self.execute(store, name, run_id, delay, "schedule")
            ran.append(name)

        return ran

    def heartbeat(self, store):
        """The owner's check-in, at most every HEARTBEAT_SECONDS (status calls one older than a minute stale)."""

        if self.thread is None:
            return

        if self.last_heartbeat is not None and time.monotonic() - self.last_heartbeat < HEARTBEAT_SECONDS:
            return

        self.last_heartbeat = time.monotonic()
        now = text_time(self.clock())

        with store.write() as connection:
            connection.execute(
                "INSERT INTO scheduler_owner (id, owner, started_at, heartbeat_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET owner = excluded.owner, heartbeat_at = excluded.heartbeat_at, "
                "started_at = CASE WHEN scheduler_owner.owner = excluded.owner THEN scheduler_owner.started_at "
                "ELSE excluded.started_at END", (self.owner, now, now))

    def start(self):
        """Starts the loop when this process can own the schedule. Returns whether it did."""

        if self.thread is not None:
            return True

        try:
            self.lock.acquire()
        except LockBusy:
            log.warning("Another process owns the integration schedule: no job runs here.",
                        extra={"operation": "scheduler", "outcome": "not_owner"})
            return False

        self.stopping.clear()
        self.last_heartbeat = None

        def loop():
            while not self.stopping.is_set():
                try:
                    self.tick()
                except Exception:
                    log.exception("The scheduler tick failed.")
                self.wake.wait(self.poll_seconds)
                self.wake.clear()

        self.thread = threading.Thread(target=loop, name="logidoc-scheduler", daemon=True)
        self.thread.start()
        log.info("Integration scheduler started (owner %s).", self.owner, extra={"operation": "scheduler"})

        return True

    def stop(self, timeout=30):
        """Graceful: the running job stops at its next check, the lease and the lock are released."""

        self.stopping.set()
        self.wake.set()

        if self.thread is not None:
            self.thread.join(timeout=timeout)
            self.thread = None

        for worker in self.workers:
            worker.join(timeout=timeout)

        self.workers = []
        store = self.store()

        if store is not None:
            with store.write() as connection:
                connection.execute("DELETE FROM scheduler_owner WHERE owner = ?", (self.owner,))

        if self.lock.held:
            self.lock.release()

    @property
    def running(self):

        return self.thread is not None and self.thread.is_alive()

    # --------------------------------------------------------
    # Reading
    # --------------------------------------------------------

    def job_view(self, row, now):

        job = dict(row)
        job["enabled"] = bool(job["enabled"])
        job["running"] = bool(job["lease_until"] and parse_time(job["lease_until"]) > now)
        due = parse_time(job["next_run_at"])
        late = (now - due).total_seconds() if due else 0
        # Delayed: due for longer than its interval (at least a minute) and not running
        job["delayed"] = bool(job["enabled"] and not job["running"] and late > max(60, job["interval_seconds"]))
        job["delay_seconds"] = max(int(late), 0) if job["enabled"] else 0
        spec = self.specs.get(job["name"])
        job["description"] = spec.description if spec else "No longer used by this version."
        job["max_retries"] = spec.max_retries if spec else 0
        job["timeout_seconds"] = spec.timeout_seconds if spec else 0
        # The owner names the host and process: shown only as "this process or another"
        job["running_here"] = job["running"] and job.pop("lease_owner") == self.owner
        job.pop("lease_owner", None)
        job.pop("lease_until", None)
        job.pop("run_requested", None)
        job.pop("requested_by", None)

        return job

    def run_view(self, row):

        run = {**dict(row), "summary": loads(row["summary"])}
        run["ran_here"] = run.pop("owner") == self.owner

        return run

    def jobs(self):

        store = self.store()

        if store is None:
            return []

        self.ensure_jobs(store)
        now = self.clock()

        with store.read() as connection:
            return [self.job_view(row, now) for row in connection.execute("SELECT * FROM scheduled_jobs ORDER BY name")
                    if row["name"] in self.specs]

    def job(self, name):

        found = next((job for job in self.jobs() if job["name"] == name), None)

        if found is None:
            raise LookupError("No such job.")

        return found

    def runs(self, name=None, statuses=(), offset=0, limit=50):

        store = self.store()

        if store is None:
            return {"runs": [], "total": 0}

        where, params = [], []

        if name:
            where.append("job = ?")
            params.append(name)

        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params += list(statuses)

        condition = f" WHERE {' AND '.join(where)}" if where else ""

        with store.read() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM job_runs{condition}", params).fetchone()[0]
            rows = connection.execute(f"SELECT * FROM job_runs{condition} ORDER BY id DESC LIMIT ? OFFSET ?",
                                      (*params, int(limit), int(offset))).fetchall()

        return {"runs": [self.run_view(row) for row in rows], "total": total}

    def run(self, run_id):

        store = self.store()

        if store is None:
            raise LookupError("No such run.")

        with store.read() as connection:
            row = connection.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()

        if row is None:
            raise LookupError("No such run.")

        return self.run_view(row)

    def status(self):
        """The scheduler's own health: who owns it, whether it runs here, its jobs."""

        store = self.store()
        owner = None

        if store is not None:
            with store.read() as connection:
                row = connection.execute("SELECT * FROM scheduler_owner WHERE id = 1").fetchone()
                owner = dict(row) if row else None

        now = self.clock()
        stale = bool(owner and (now - parse_time(owner["heartbeat_at"])).total_seconds() > max(60, self.poll_seconds * 6))
        jobs = self.jobs()

        if self.metrics is not None:
            self.metrics.set("logidoc_jobs_delayed", sum(job["delayed"] for job in jobs))

        return {
            "configured": store is not None,
            "running_here": self.running,
            "owner": {"since": owner["started_at"], "heartbeat_at": owner["heartbeat_at"], "this_process": owner["owner"] == self.owner,
                      "stale": stale} if owner else None,
            "jobs": jobs,
        }

    def overdue(self):

        return [job for job in self.jobs() if job["delayed"]]
