# Scheduled synchronisation

The integration scheduler runs the background work behind webhooks and TMS/ERP integrations. It keeps every job's
state and history in the integration store, so nothing depends on memory and nothing is lost when the server
stops. This document is for the operators who run the server and the engineers who add jobs.

Code: `src/scheduler.py` (scheduler), `src/integration_runtime.py` (the jobs), `src/api/routes_integrations.py`
(API). Screen: Integrations, then Scheduled Jobs (administrators).

---

## 1. Why no task queue

The application is one server process per data folder: `src/api/serve.py` holds a lock beside the data folder,
and SQLite allows one writer. A distributed queue (Celery, Redis, RabbitMQ) would add infrastructure without
adding safety here. One thread runs the jobs, and SQLite keeps their state.

## 2. The jobs

| Job | Default interval | What it does |
|---|---|---|
| `audit_feed.poll` | 60 s | Reads newly recorded audit runs from the audit-run feed and creates the freight audit events |
| `webhooks.collect_activity` | 30 s | Reads new exception review and carrier registry events and creates their webhook events |
| `webhooks.deliver_pending` | 15 s | First attempt of every new delivery (up to 50 per run) |
| `webhooks.retry_failed` | 60 s | Returns interrupted deliveries to the queue, then makes the retries that are due |
| `integrations.sync` | 60 s | Synchronises each enabled integration whose own sync interval has passed |
| `jobs.detect_overdue` | 300 s | Alert `integration_jobs_overdue` for late jobs, late syncs, and deliveries waiting over an hour without an attempt |
| `integrations.health` | 300 s | Alerts `webhook_endpoint_failing` (5 failed attempts in a row) and `integration_connection_failing` (connection test) |

- Every job is idempotent. A run that stops early (shutdown, deadline) leaves the rest to the next run.
- An administrator can change an interval (10 seconds to 7 days) and enable or disable a job.
- The scheduler does nothing until the integration store exists, which happens when the first webhook endpoint
  or integration is configured.

## 3. Reliability

| Concern | How it is handled |
|---|---|
| Persistent status | `scheduled_jobs`: enabled, interval, next run, last start, success and failure, last error, failures in a row |
| Execution history | `job_runs`: one row per run, with trigger (schedule, manual or retry), who asked, start, end, duration, how late it started, summary, error category and message (redacted). Never deleted |
| Job locking, no overlap | A run first claims the job's lease in one `BEGIN IMMEDIATE` transaction: free, or expired. A second claim, from the schedule or an administrator, is refused (`JobBusy`, 409 at the API) |
| Long runs | Each job has a timeout (5 minutes; 15 for syncs). `should_stop()`, checked between items, renews the lease and says when to stop |
| Retry limits | A failed run is retried after 60 s × 2^(n−1), within the interval, up to 3 failures in a row. After that the job waits its normal interval and the critical alert `job_failing:<job>` is raised |
| Restart recovery | A run left `running` when the process stopped is marked `interrupted` once its lease expires, and the job is due at once. Nothing is silently lost. A manual run interrupted by a stop is run again the same way |
| Catch-up | A job overdue at start runs once, not once per missed interval |
| Graceful shutdown | The server's shutdown stops the loop: the running job ends at its next check, then the lease, the owner row and the lock are released |
| Idempotency | Every job can run again on the same data without a second effect (the outbox, deliveries and imported records are keyed) |

### One owner

`Scheduler.start()` takes a lock file beside the store (`.integrations-scheduler.lock`, using the operating
system's file lock, released by the OS if the process dies).

- A second process does not get the lock and runs no job: another uvicorn worker, a second server, or a Streamlit
  session.
- Its status says the schedule is owned elsewhere.
- The owner records a heartbeat every few seconds. A heartbeat older than a minute is shown as stale ("has not
  checked in"), a sign that background jobs stopped.

The owner is shown only as "this process" or "another process": host names and process ids never leave the
server.

### Where it runs

- The scheduler starts with the server's background jobs (`src/api/serve.py`, `LOGIDOC_BACKGROUND_JOBS`, on by
  default there). It never starts in the tests or under `LOGIDOC_ENV=test`.
- Without a running server, an administrator can still run any job from the screen: it runs in the API process
  under the same lease.

## 4. Monitoring

### Metrics

Exposed with the others at `GET /api/admin/metrics`, also in Prometheus format:

| Metric | Labels | Meaning |
|---|---|---|
| `logidoc_jobs_total` | job, outcome | Runs, succeeded or failed |
| `logidoc_job_seconds` | job | Run duration (histogram) |
| `logidoc_job_delay_seconds` | job | How late the last scheduled run started |
| `logidoc_job_retries_total` | job | Retried runs |
| `logidoc_job_last_success_timestamp` | job | Unix time of the last success |
| `logidoc_jobs_delayed` | | Jobs late by more than their interval |
| `logidoc_jobs_interrupted_total` | job | Runs found interrupted after a stop |
| `logidoc_integrations_overdue`, `logidoc_webhook_deliveries_stuck` | | From `jobs.detect_overdue` |
| `logidoc_webhook_endpoints_failing`, `logidoc_integrations_failing` | | From `integrations.health` |
| `logidoc_webhook_deliveries_total` | outcome | Delivery attempts |
| `logidoc_webhook_delivery_seconds` | | Attempt duration |
| `logidoc_sync_runs_total` | provider, outcome | Sync runs |
| `logidoc_sync_records_total` | provider, change | Records imported, updated, rejected, duplicates, conflicts |

### Alerts

Raised through the existing alert manager (log and optional alert webhook, with its cooldown):

- `integration_jobs_overdue`
- `webhook_endpoint_failing`
- `integration_connection_failing`
- `job_failing:<job>`

### Status

`GET /api/integrations/jobs` is the scheduler's own health: configured, running here, owner and heartbeat, and
every job with its state, running and delayed flags.

It is deliberately **not** part of `/api/health` (liveness: the process answers) or `/api/ready` (the audit can
work). A stopped scheduler must not make a load balancer take the application out of service.

## 5. API

| Method and path | Answer |
|---|---|
| `GET /api/integrations/jobs` | `SchedulerStatus` |
| `PATCH /api/integrations/jobs/{name}` | `{"enabled": bool, "interval_seconds": int}` → `ScheduledJob` |
| `POST /api/integrations/jobs/{name}/run` | 202 `JobStarted`; 409 while it runs; 422 before anything is configured |
| `POST /api/integrations/jobs/{name}/retry` | 202 `JobStarted`; 422 unless its last run failed |
| `GET /api/integrations/jobs/{name}/runs?status=&offset=&limit=` | `JobRunPage` |
| `GET /api/integrations/job-runs/{id}` | `JobRun` |

All need `integrations.manage`, and the CSRF header for changes.

## 6. Operating it

- **Is it running?** The Scheduled Jobs screen shows the owner and its last check-in. Jobs marked "Late" have not
  run for longer than their interval.
- **A job keeps failing.** Open its runs: each failed run shows its error category and message. Fix the cause,
  then use Retry. The job also retries by itself (3 times, then every interval).
- **After a crash.** Nothing to do. On start, runs left `running` are marked `interrupted` and their jobs run at
  once. Deliveries interrupted mid-attempt are sent again (receivers deduplicate by event id).
- **Pausing integrations.** Disable a job (for example `integrations.sync`). Its state is kept and it resumes when
  enabled.
- **Two servers on one data folder.** Not supported: the data-folder lock refuses the second server. If two
  processes do share a store, only the lock holder runs jobs.

## 7. Limitations

- One thread runs the jobs one after another: a slow job delays the others (bounded by its timeout and the batch
  sizes).
- The history (`job_runs`) is never pruned automatically. At the default intervals it grows by roughly 10,000
  rows a day. Pruning is a future, deliberate operation, as the brief forbids deleting integration history silently.
- The heartbeat staleness is shown, but no alert is raised for it, because the scheduler would have to raise it
  about itself. An external monitor can read `GET /api/integrations/jobs`.
