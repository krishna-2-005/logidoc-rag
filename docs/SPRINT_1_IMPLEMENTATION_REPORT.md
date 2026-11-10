# Sprint 1: production foundation

The work starts from commit `068074e` on `feature/production-readiness` and
covers four workstreams:

- backup and database recovery
- deployment, HTTPS and environment configuration
- monitoring, structured logs and alerting
- automated backup and restore procedures

**Status.** The sprint was committed and pushed on the owner's instruction in
three commits:
- `0a1420d`: backups
- `7444d1d`: the server
- `1e592a7`: documentation

Release validation on 2026-11-10 found two defects in that pushed code
(section 7). Their fixes, with regression tests, and this updated report
are uncommitted.

**How to read the results.** Every result below was produced on the
development machine (Windows 11) during this sprint, on synthetic data in
temporary folders. Nothing was deployed to a server, no certificate was
issued, and no restore was performed on real data. Section 7 says, for each
result, whether it was measured, estimated, synthetic, or untested in a real
environment.

## 1. What was implemented

### Backup and recovery (`src/backup.py`, `src/file_lock.py`)

- **What a backup holds:**
  - **Databases:** all six SQLite stores, copied with SQLite's online backup
    API (a consistent snapshot while the application writes), then
    integrity-checked, with schema version, per-table row counts,
    foreign-key state, size and SHA-256 recorded.
  - **Documents:** the PDFs and ingestion journal, archived originals and
    retry copies, each copied with its SHA-256 and compared with the source
    afterwards.
  - **Vector index:** the ChromaDB index, copied only while no ingestion can
    write it (the server's ingestion lock, or the server stopped), and left
    out with a recorded reason otherwise.
  - **Configuration:** the non-secret settings that change results.
- **The manifest** records the backup ID, times, the components, schema
  versions, every file's checksum, the status, the Git commit, the Python and
  ChromaDB versions, and restore compatibility. A checksum beside it detects
  edits.
- **Completeness:** a backup is written as `<id>.partial` and renamed only
  after every file has been read back and verified, so interrupted and failed
  backups are never usable.
- **Restores** go to a new folder only. Steps:
  1. validate the manifest, checksums and compatibility
  2. copy and compare every file
  3. compare each database's integrity, version, row counts and foreign keys
     with the backup
  4. open the index with ChromaDB and compare it
  5. write `restore-report.json`
- **Replacing live data** needs the backup ID typed twice and the server
  stopped. The live folders are moved aside, never deleted.

### Deployment, HTTPS and configuration (`src/environment.py`, `src/api/core.py`, `src/api/middleware.py`, `deploy/`)

- **Profiles:** `development`, `test` and `production`. Production refuses
  to start without sign-in, HTTPS origins, trusted host names, and explicit
  absolute data, index and backup folders, and it refuses values left from
  the example files. Malformed values are refused in every profile.
  `serve.py --check` validates without starting.
- **New request checks:**
  - trusted Host names (with JSON 400 errors)
  - request-size limits, checked on both the declared and the received bytes
  - an optional HTTP-to-HTTPS redirect, and HSTS
  - `X-Forwarded-*` trusted only from the proxy's address
- **Deployment files:** Caddy and nginx configurations (TLS, redirect,
  upload limit, timeouts, metrics blocked from outside), a hardened systemd
  unit, and a production environment template holding placeholders only.
- **The data-folder lock:** the server holds a lock on its data folder, so a
  second server, or a command-line backup or restore, knows it is running.
- **Readiness** also checks the core databases, the vector index and the
  free disk space. An offline model service, an unreadable index or critical
  disk space make it `degraded`, never `not_ready`.

### Monitoring and logs (`src/observability.py`, `src/redaction.py`, `src/metrics.py`, `src/alerts.py`, `src/operations.py`)

- **Logs:** JSON lines in production (text in development) with timestamp,
  level, component, request and correlation IDs, route template, method,
  status, duration, outcome and error category. Credentials, URL paths, file
  folders and e-mail addresses are removed from every line and traceback.
  Bodies, document text and prompts are never logged.
- **Request IDs:** every response carries `X-Request-ID` and
  `X-Correlation-ID`, taken from the caller or proxy when valid. An
  unhandled error becomes a bare 500 that is logged with its redacted
  traceback and request ID.
- **Metrics** count real events only:
  - API requests, latency and errors by category
  - database errors
  - ingestion outcomes and per-PDF duration
  - Ollama call durations by outcome, and Ollama availability
  - freight audit duration
  - backup runs and the last success
  - disk free space and readiness

  They are served as JSON or Prometheus text at `GET /api/admin/metrics`.
- **Alerts:** rules for failed and stale backups, low disk, repeated 5xx,
  database errors, the AI service down and high latency, with thresholds,
  a cooldown and resolution. They go to the log and to an optional webhook,
  and can be read at `GET /api/admin/alerts`.

### Automated backups (`src/backup_automation.py`, `scripts/manage_backups.py`, `src/api/routes_operations.py`)

- **Schedule:** `off`, `daily@HH:MM` UTC or `every:<N>h`, run by a thread in
  the server, with a retry delay after a failure.
- **No overlap:** one backup at a time in the process and across processes.
  The free space is checked before each backup; nothing is deleted to make
  room.
- **Status record** (`backup-status.json`): the last attempt, the last
  success, the last failure and consecutive failures. A failure raises a
  critical alert.
- **Retention:** daily, weekly and monthly, applied to scheduled backups
  only, after a success, with each folder re-checked before removal. Manual,
  incomplete and foreign folders are never removed.
- **Command line:** `create`, `list`, `status`, `verify`, `restore`, `prune`
  (a preview unless `--apply`) and `activate`.
- **API:** `GET` and `POST /api/admin/backups` for administrators.

## 2. Files

**New (code):**
- `src/backup.py`
- `src/backup_automation.py`
- `src/file_lock.py`
- `src/environment.py`
- `src/observability.py`
- `src/redaction.py`
- `src/metrics.py`
- `src/alerts.py`
- `src/operations.py`
- `src/api/middleware.py`
- `src/api/routes_operations.py`
- `scripts/manage_backups.py`

**New (deployment):**
- `deploy/Caddyfile`
- `deploy/nginx/logidoc.conf`
- `deploy/logidoc-api.service`
- `deploy/logidoc.production.env.example`

**New (tests):**
- `tests/test_backup_recovery.py`
- `tests/test_backup_automation.py`
- `tests/test_observability.py`
- `tests/test_production_config.py`

**New (documentation):**
- `docs/PRODUCTION_BACKUP_AND_RECOVERY.md`
- `docs/PRODUCTION_DEPLOYMENT.md`
- `docs/OBSERVABILITY_AND_MONITORING.md`
- `docs/AUTOMATED_BACKUP_OPERATIONS.md`
- this report

**Modified:**

| File | Change |
|---|---|
| `src/api/app.py` | middleware stack, readiness with the new checks, error categories, background jobs at startup, operations router |
| `src/api/core.py` | new settings (profile, trusted hosts, limits, HTTPS, proxy, background jobs), startup validation, backup folder and operations on `ApiState` |
| `src/api/serve.py` | validates before anything else, `--check`, logging, the data-folder lock, background jobs, proxy and access-log handling |
| `src/api/schemas.py` | response models of the operations endpoints |
| `src/readiness.py` | databases, vector index and disk checks |
| `src/model_client.py` | call duration metric |
| `src/ingestion_service.py` | ingestion metrics |
| `src/audit_service.py` | freight audit duration metric |
| `config.py` | `BACKUP_DIR` |
| `.env.example` | every new setting, and the previously undocumented `LOGIDOC_FREIGHT_TOLERANCES` and `LOGIDOC_CARRIER_DB` |
| `.gitignore` | `data/carrier_registry.sqlite3*` (missing since Phase 3), `backups/`, the server lock |
| `README.md` | the deploying section, the database table (it said audit history v3 and reviews v2; they are v5 and v3; carrier registry added), limitations |
| `docs/api/openapi-v1.json` | regenerated: three new paths, additions only |
| `frontend/src/components/layout/AppShell.tsx` | names of the three new readiness checks |
| `frontend/src/test/navigation.test.tsx` | a test for them |
| `tests/test_readiness.py` | the expected checks, and tests for the new ones; the free-space check reads a stand-in disk in this file |

## 3. Database schema changes

None. No store's schema or version changed, and no migration was added.
The new persistent files are outside the stores:
- the backup folders with their manifests
- `backup-status.json` in the backup folder
- the lock files `<backups>/.backup.lock` and `.<data folder>-server.lock`
  beside the data folder

## 4. Commands

```
python src/api/serve.py --check                 # validate the settings, start nothing
python src/api/serve.py                         # API + React build, background jobs

python scripts/manage_backups.py create --label "before upgrade"
python scripts/manage_backups.py list
python scripts/manage_backups.py status
python scripts/manage_backups.py verify <backup id>
python scripts/manage_backups.py restore <backup id> --target <new folder>
python scripts/manage_backups.py prune            # preview; --apply to remove
python scripts/manage_backups.py activate --restored <folder> --backup-id <id> --confirm <id>
```

Deployment step by step:
[PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md). Backup operations and
the restore runbook:
[AUTOMATED_BACKUP_OPERATIONS.md](AUTOMATED_BACKUP_OPERATIONS.md).

## 5. Configuration

New settings, all in `.env.example`; production values are in
`deploy/logidoc.production.env.example`:

| Group | Settings |
|---|---|
| Profile | `LOGIDOC_ENV` (development, test, production) |
| Transport | `LOGIDOC_TRUSTED_HOSTS`, `LOGIDOC_FORWARDED_ALLOW_IPS`, `LOGIDOC_HTTPS_REDIRECT`, `LOGIDOC_HSTS_SECONDS`, `LOGIDOC_MAX_REQUEST_KB`, `LOGIDOC_MAX_UPLOAD_FILES` |
| Logs | `LOGIDOC_LOG_FORMAT`, `LOGIDOC_LOG_LEVEL`, `LOGIDOC_LOG_FILE` |
| Backups | `LOGIDOC_BACKUP_DIR`, `LOGIDOC_BACKUP_SCHEDULE`, `LOGIDOC_BACKUP_KEEP_DAILY`, `_WEEKLY`, `_MONTHLY`, `LOGIDOC_BACKUP_MIN_FREE_MB`, `LOGIDOC_BACKUP_RETRY_MINUTES` |
| Monitor and alerts | `LOGIDOC_BACKGROUND_JOBS`, `LOGIDOC_MONITOR_INTERVAL`, `LOGIDOC_ALERT_WEBHOOK_URL` (secret), `LOGIDOC_ALERT_DISK_WARNING_MB`, `_DISK_CRITICAL_MB`, `_WINDOW_SECONDS`, `_5XX_COUNT`, `_DB_ERROR_COUNT`, `_LATENCY_MS`, `_MIN_REQUESTS`, `_OLLAMA_FAILURES`, `_BACKUP_MAX_AGE_HOURS`, `_COOLDOWN_SECONDS` |

## 6. Security

- **Production refuses to start** when it is incomplete or insecure:
  - local sign-in
  - HTTP origins, `*` origins or catch-all hosts
  - relative folders, or backups inside the data folder
  - Secure cookies turned off
  - an HTTP webhook
  - placeholder values from the example files

  The messages name settings, never values.
- **No new secret.** The application has no signing key: session tokens are
  random and stored hashed. The only secret setting is the alert webhook
  URL. It is never logged (the redaction removes URL paths, and a webhook
  failure logs only the error type), never returned and never written into a
  backup.
- **Errors keep one shape,** `{"detail": "..."}`. That includes the new 400
  (untrusted host), the 413s and the 500. None shows a path or a stack
  trace, and every response carries a request ID for support.
- **CSRF, CORS, cookies and roles** are unchanged and still enforced. The new
  admin endpoints need `admin.maintenance`, and their POST needs the CSRF
  header (tested: 401 anonymous, 403 reviewer, 403 without the header).
- **Restores are command-line only.** They never write into live, backup or
  project folders and never overwrite. Activation moves the live data aside
  instead of deleting it.
- **Backups are not encrypted.** They hold password hashes and documents, so
  the backup folder must be private and copies must be encrypted where they
  go (not built).
- **Not done:** an external security test (penetration test, TLS scan)
  needs a deployed server and was not performed.

## 7. Test results

### Final results (release validation, 2026-11-10, on the code with both fixes)

Each check ran on its own (no heavy jobs in parallel), with temporary files
on `C:\Users\kuchu\AppData\Local\Temp\ldt`. Exit codes were read from the
runs.

| Check | Command | Result |
|---|---|---|
| Full Python 3.12 suite | `venv312\Scripts\python.exe -m pytest -q -p no:cacheprovider tests` | **2,127 passed, 0 failed, 0 skipped, exit 0** (6 min 12 s; 10:39:35 to 10:45:49 UTC). Ollama was running, so its live tests ran. One warning (Starlette's httpx deprecation, already there before) |
| Python 3.10 relevant tests | `venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` on `test_backup_recovery`, `test_backup_automation`, `test_observability`, `test_production_config`, `test_readiness`, `test_api`, `test_api_contracts` | **262 passed, 0 failed, exit 0** (1 min 26 s), Python 3.10.11 |
| Frontend type check | `npm run typecheck` (`tsc --noEmit`) | exit 0 |
| Frontend tests | `npm test` (vitest) | **9 files, 229 passed, exit 0** (14.9 s) |
| Production frontend build | `npm run build` | exit 0; main bundle 988.20 kB (988.12 kB at 068074e); the existing large-chunk warning is unchanged |
| Dependencies | `python -m pip check` on both environments | no broken requirements, exit 0 on both |
| OpenAPI contract | `python scripts/export_openapi.py --check` | up to date, exit 0 (three new admin paths, additions only) |
| Evaluation | `python scripts/evaluate.py` (offline, deterministic, Python 3.10) on an extracted copy of 068074e and on the working tree | every metric identical: recall@5 1.00, hit@1 1.00, answer match 1.00, citation precision 1.00, abstention 1.00, cross-shipment leakage 0.00; every threshold passes. Only the two timing lines differ (retrieval p95 6 vs 5 ms, total 0.8 vs 0.7 s) |
| Real data | SHA-256 of every file in `data/` and `chroma_db/`, compared with the hashes taken on 2026-11-09 before any Sprint 1 testing | **all 34 files byte-for-byte identical**; no `backups/` folder or server lock was created in the project |
| Whitespace | `git diff --check` | clean |

The suite grew between runs: 2,118 in the first run on 2026-11-09, 2,122 on
the pushed code (also run again on 2026-11-10: 2,122 passed, exit 0), and
2,127 now with the five regression tests below.

Tests per new or extended file (collected):

| File | Tests |
|---|---|
| `test_backup_recovery.py` (backup, interruption, corruption, restore, migration compatibility, activation, locks) | 37 |
| `test_backup_automation.py` (schedule, status, retention, runner, monitor and alerts, admin endpoints, command line) | 36 |
| `test_observability.py` (request IDs, logs, redaction, metrics) | 31 |
| `test_production_config.py` (environment validation, hosts, HTTPS, size limits, deployment files) | 47 |
| `test_readiness.py` (5 more than the 15 before) | 20 |

### Defects found by the release validation, fixed and re-tested

1. **A momentary Windows file lock could fail a backup.** In the first
   Python 3.10 run, `test_an_administrator_makes_a_backup_and_sees_it`
   failed. The backup it started had failed with `[WinError 5] Access is
   denied`, renaming its own `manifest.sha256` temporary file. Another
   process, typically antivirus scanning a file just written, held the file
   for a moment. The failure handling worked (the attempt was marked failed,
   and the critical `backup_failed` alert fired), but a lock that brief
   should not fail a backup, and the test passed when run again.
   - **Fix:** renames in the backup (the atomic writes and the final
     `.partial` rename) are retried on `PermissionError` for up to 2 s, and
     a failed atomic write removes its own temporary file.
   - **Tests:** `test_a_file_held_open_for_a_moment_does_not_fail_the_backup`
     and `test_a_file_that_stays_locked_fails_the_backup_cleanly`.
2. **Folder names leaked through redaction in Python's own error messages.**
   The same failure's log line showed `<path>/\Users<path>/\AppData...`.
   Python quotes Windows paths with doubled backslashes in `OSError`
   messages (`'C:\\Users\\...'`), a format the path pattern did not cover,
   so folder names such as `Users`, `AppData` and `Temp` stayed in logs,
   alerts and backup error messages.
   - **Fix:** the Windows and UNC path pattern accepts doubled separators.
   - **Test:** `test_redaction_of_pythons_own_error_messages`, with three
     real `OSError` formats.

After both fixes, everything was rerun, with the results in the table
above: the 3.10 set, the full 3.12 suite, the frontend, the dependency,
contract and evaluation checks, and the data hashes.

### What the evidence covers

| Kind | What |
|---|---|
| Measured, on synthetic data in temporary folders | all test suites; backup, verify, restore and activation; interrupted, corrupted and locked backups; retention; alerts; the middleware and readiness timings; backup speed |
| Measured, against the application's real settings and files | the deployment files checked against the application's settings (ports, upload limit, timeouts); `.env.example` checked against every setting the code reads; the production template refused until its placeholders are replaced; the OpenAPI contract; the evaluation; the real data's hashes (read only) |
| Estimated, not measured | how backups scale beyond 50 MB; upload waits during a backup on large data; anything about a server other than this Windows machine |
| Implemented but not tested in a real environment | HTTPS through Caddy or nginx with a real certificate; the systemd unit; a deployment; alert delivery to a real webhook; Prometheus scraping; a backup and restore of the real data; activation on a real installation |

### Disk and storage during validation

- **Free space moved a lot.** On 2026-11-09 the drives fell to about 1.1 GB
  free on C: and 226 MB on D:, mostly because of other work on the machine.
  The validation on 2026-11-10 started with 12 GB free on C: and 4.2 GB on D:.
- **Space can fail a test.** One test failed on 2026-11-09 only because C:
  was under the backup's default 1,024 MB free-space reserve, which is the
  intended refusal. The test now sets the reserve to 0, and the reserve is
  tested on its own.
- **Path length on Windows.** Python 3.10's SQLite (3.40.1) cannot open paths
  over 260 characters, which failed tests run from a deep temporary folder.
  Backups and restores now refuse such paths up front, and the test suites
  are run from a short temporary folder.

### Earlier results (2026-11-09, before the release-validation fixes)

These are kept for the record. The table above supersedes them.

- Python 3.12: 2,118 passed, then 2,122 passed on the pushed code.
- Python 3.10: 254 passed, then the backup tests 70 passed.

**Problems the new tests found during development, all fixed:**
- Verifying a truncated database copy raised an error instead of reporting
  it.
- A path over 260 characters cannot be opened by the SQLite in Python 3.10
  (3.40.1) on Windows; 3.12's (3.45.3) can. Backups and restores now refuse
  such a path before writing, with a clear message.
- An unreadable extraction store would have made the server not ready,
  against its designed `degraded`, so it was left out of the new databases
  check.
- `tests/test_backup.py` matched the old `*_backup.py` pattern in
  `.gitignore` and would never have been committed. It was renamed to
  `tests/test_backup_recovery.py`.
- `LOGIDOC_FREIGHT_TOLERANCES` was missing from `.env.example`.

**Blocked, not run:**
- `nginx -t`, `caddy validate`, `systemd-analyze verify`: none is installed
  on this Windows machine. The files were checked by tests instead.
- A deployment, a TLS handshake with a real certificate, an external
  security scan: these need a server and your approval.
- A backup or restore of the real data: excluded by the brief.
- A Docker build: there is no Docker configuration, and the disks are full.

## 8. Performance impact

All measurements are on the development machine with synthetic data, so
read them as orders of magnitude, not as server figures.

### Middleware cost per request

Measured on 2026-11-10 with Python 3.12.7, FastAPI 0.156.8, Starlette 1.7.0
and uvicorn 0.54.0, on 20 logical CPUs at about 5% load, with 2.6 GB of RAM
free.

**Method.** The same route (`GET /api/x/{item}`, an async handler returning
a small JSON object) was built three times:
- `old`: the middleware of commit 068074e
- `new`: the Sprint 1 stack, with logging off
- `new+log`: the Sprint 1 stack, writing the production JSON access log to
  a null stream, formatting and redaction included

Each was called directly through ASGI in one event loop (no test client, no
network), 300 warm-up calls each, then 10 rounds of 1,000 requests per
scenario. The order rotated each round, so drift in machine load affects all
three alike. Every request's latency was recorded. The benchmark was run
twice:

| Scenario | Median | p95 | p99 | Throughput (one client, sequential) | Round medians |
|---|---|---|---|---|---|
| old | 147.9 / 152.7 µs | 182.7 / 236.8 µs | 240.5 / 329.1 µs | 6,247 / 5,904 req/s | 146.6-149.9 / 148.4-159.1 µs |
| new | 181.0 / 189.1 µs | 227.5 / 274.9 µs | 288.0 / 371.8 µs | 5,276 / 4,923 req/s | 178.0-183.1 / 185.5-195.7 µs |
| new+log | 258.9 / 267.0 µs | 319.0 / 382.9 µs | 384.8 / 497.4 µs | 3,728 / 3,510 req/s | 257.6-261.4 / 261.3-274.0 µs |

Each cell is run 1 / run 2, with 10,000 requests per scenario per run.

| Result | Run 1 | Run 2 |
|---|---|---|
| Middleware alone (new minus old, medians) | +33 µs | +36 µs |
| Middleware with the access log (new+log minus old) | +111 µs | +114 µs |

The access line (JSON formatting and redaction regexes) costs about 80 µs of
that.

**Cross-check through the test client.** 900 requests per scenario, logging
off. Medians were old 1,502 / 1,647 µs and new 1,611 / 1,658 µs. At that
level the difference (+109 / +11 µs) is within the client's own run-to-run
noise, so the direct ASGI figures are the result.

**Limitations.**
- Medians are stable between rounds (±2 µs in run 1, ±5 µs in run 2), but
  p99 moves by about 25% between the runs, so treat the tails as indicative.
- The figures cover the middleware only. A real request also pays for the
  network, the reverse proxy and its own work; the audit endpoints take tens
  to hundreds of milliseconds. An added 0.11 ms is therefore under 1% of a
  typical request.
- An earlier estimate of "about 0.9 ms" (2026-11-09, through the test client
  while the machine was overloaded, when the client itself cost about 11 ms
  per request) is withdrawn.

### Other measurements

| What | Measured | Note |
|---|---|---|
| `GET /api/ready` | 26 ms median (50 calls, 2026-11-09) | now also opens the core databases read-only and reads the free space; the Ollama call was stubbed out, so its own time (bounded at 2 s) is not included |
| Backup of 47.6 MB (400 PDFs of 100 KB, a 12,000-row database) | 11 to 27 s to create; 1.1 s to verify; 6.1 s to restore (2026-11-09) | measured with both disks 99-100% full and antivirus scanning; see below |
| Freight audit, ingestion, model calls | one timing call each | no measurable change expected or observed; not benchmarked separately |

**What a backup costs.** Profiling the backup showed where the time goes:
10 of 13.5 s was spent opening files, at about 6 ms per open, typical of
antivirus scanning on Windows; `fsync` took 1.2 s. The copy was changed to
compare each source's size, modification time and file ID before and after
copying, instead of opening and hashing the source a second time. That
removes one of the four opens per file, and the test
`test_a_file_rewritten_during_its_copy_is_copied_again` shows that a write
during the copy is still detected. On this busy machine the saving was within
the noise (11.2 and 14.3 s against 13.5 s before), so no speed-up is claimed.

**Uploads wait during a backup.** The server's backups hold the ingestion
lock while they copy, so an upload waits until the backup ends. For today's
data (about 5 MB) that is about a second; for 50 MB of documents it is the
backup time above. Schedule backups outside working hours.

The evaluation's retrieval and answer figures are unchanged (section 7).

## 9. Known limitations

- **No real deployment.** Nothing ran on a server, behind a real proxy,
  with a real certificate. The proxy configurations and the systemd unit
  were checked by tests against the application's settings, not by
  `nginx -t`, `caddy validate` or systemd (none is installed on this
  machine).
- **No real restore.** Backups and restores were tested on synthetic
  installations only. The real data was not backed up or restored, by the
  brief's rule.
- **Backups stay on one machine.** Off-machine copies and encryption are not
  built.
- **Streamlit** does not take the data-folder lock. A command-line backup
  with the server stopped but Streamlit writing could copy an index in the
  middle of a change. This is documented: stop Streamlit first.
- **Metrics and alerts** live in one process and reset on restart. There is
  no long-term history without an external collector, and no dashboard page.
- **Prometheus scraping** needs an administrator's session; there is no
  scrape token.
- **Retention** counts days, weeks and months in UTC.
- **Short paths on Windows.** Keep the backup and restore folders' paths
  short (for example `D:\backups\logidoc`). A backup or restore that would
  write a path over 259 characters is refused before it writes anything.
- **Windows file locks.** A rename that another process blocks (an
  antivirus scan, the search indexer) is retried for up to 2 s. A longer
  lock still fails the backup: the attempt is marked failed and the
  `backup_failed` alert fires.
- **No Docker files.** No container image was built: the disks are nearly
  full, and the plain service setup needs none.

## 10. Remaining manual steps

1. Choose a host name and a server; install Caddy, or nginx with certbot;
   issue the certificate.
2. Fill in `/etc/logidoc/logidoc.env` from the template, and run
   `serve.py --check`.
3. Put the backup folder on a separate disk, and arrange off-machine,
   encrypted copies.
4. Point the alert webhook at a receiver that reaches a person, or collect
   the JSON logs.
5. Run the staging procedure in
   [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md), including a real
   restore of a production backup into staging.
6. Run an external security test against staging.

## 11. Needs your approval

- **Committing and pushing the release-validation fixes.** The Sprint 1 work
  is on GitHub (`1e592a7`). The two defect fixes, their regression tests and
  this report are local changes, waiting for a manual commit.
- **Any deployment** to a real server, or opening ports to the internet.
- **A first backup of your real data** (`scripts/manage_backups.py create`
  reads the live stores read-only and writes to `backups/`). It was not run
  during development, by the brief's rule.
- **Retention `--apply`** on a real backup folder. A preview is the default.
- **The disk space.** D: and C: are nearly full; see the test results.
  Backups of today's data are small (about 5 MB), but the default 1024 MB
  reserve (`LOGIDOC_BACKUP_MIN_FREE_MB`) will make a backup on a nearly full
  drive fail on purpose.
