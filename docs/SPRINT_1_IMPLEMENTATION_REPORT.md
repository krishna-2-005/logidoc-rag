# Sprint 1: production foundation

The work starts from commit `068074e` on `feature/production-readiness` and
covers four workstreams:

- backup and database recovery
- deployment, HTTPS and environment configuration
- monitoring, structured logs and alerting
- automated backup and restore procedures

Everything is uncommitted, waiting for review.

Every result below was produced on the development machine (Windows 11) during
this sprint, on synthetic data in temporary folders. Nothing was deployed to a
server, no certificate was issued, and no restore was performed on real data.

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

Every check below was run during this sprint. The memory-heavy suites ran
one at a time, with temporary files kept off the nearly full drives where
possible.

| Check | Result |
|---|---|
| Full Python 3.12 suite (final code) | **2,122 passed, 0 failed, 0 skipped** (10 min 43 s); Ollama was running |
| Full Python 3.12 suite (first run, before the last fixes) | 2,118 passed, 0 failed, 0 skipped (15 min 54 s). Ollama was running, so its 3 live tests ran instead of being skipped |
| Relevant tests on Python 3.10 (the new files, readiness, the API and the contracts) | 254 passed; the backup tests again after the last fixes: 70 passed |
| New tests | `test_backup_recovery.py` 35, `test_backup_automation.py` 36, `test_observability.py` 28, `test_production_config.py` 47; `test_readiness.py` has 5 more (20) |
| Frontend type check (`tsc --noEmit`) | clean |
| Frontend tests (vitest) | 9 files, 229 passed (228 before, plus the readiness-names test) |
| Production frontend build | succeeds; main bundle 988.20 kB (988.12 kB before); the large-chunk warning is the same as before |
| `pip check` | no broken requirements on Python 3.10 and 3.12 |
| OpenAPI contract | `export_openapi.py --check` up to date; three new paths, additions only; the contract and compatibility tests pass |
| Evaluation (offline, deterministic, Python 3.10) | identical to commit 068074e: recall@5 1.00, hit@1 1.00, answer match 1.00, citation precision 1.00, abstention 1.00, cross-shipment leakage 0.00, every threshold passes. Only the timing lines differ |
| Real data | all 34 files in `data/` and `chroma_db/` hashed before and after: identical; no backup or lock file was created in the project |
| `git diff --check` | clean |

**Problems the new tests found, all fixed:**
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

These were measured on the development machine while it was busy: both
disks were 99-100% full and other work was running. Read them as orders of
magnitude, not as server figures. All data was synthetic.

| What | Measured | Note |
|---|---|---|
| New middleware per request (request IDs, access log, metrics, Host check, size limit, HTTPS policy, proxy headers) | about 0.9 ms added to about 11.2 ms per in-process request (8%) | one run, test client, the same route with the old and the new middleware stack; the base cost itself was inflated by the machine's load |
| `GET /api/ready` | 26 ms median (50 calls) | now also opens the core databases read-only and reads the free space; the Ollama call was stubbed out, so its own time (bounded at 2 s) is not included |
| Backup of 47.6 MB (400 PDFs of 100 KB, a 12,000-row database) | 11 to 27 s to create; 1.1 s to verify; 6.1 s to restore | |
| Freight audit, ingestion, model calls | one timing call each | no measurable change |

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

- **Committing and pushing** these changes. They are left uncommitted.
- **Any deployment** to a real server, or opening ports to the internet.
- **A first backup of your real data** (`scripts/manage_backups.py create`
  reads the live stores read-only and writes to `backups/`). It was not run
  during development, by the brief's rule.
- **Retention `--apply`** on a real backup folder. A preview is the default.
- **The disk space.** D: and C: are nearly full; see the test results.
  Backups of today's data are small (about 5 MB), but the default 1024 MB
  reserve (`LOGIDOC_BACKUP_MIN_FREE_MB`) will make a backup on a nearly full
  drive fail on purpose.
