# Sprint 4, phases 0 and 1 report

Backup and baseline; migration rehearsal, API authorization, read-only reads, rate limits, dependency security and
the exceptions CSV.

Date: 2026-11-15. Branch `feature/production-readiness`, starting from `6002747`. Plan:
[SPRINT_4_PLAN.md](SPRINT_4_PLAN.md) (its section 6 has the status).

## 0. Preflight

- **Branch:** `feature/production-readiness` at `6002747`, equal to the remote. CI on `6002747` passed all four
  jobs.
- **Working tree:** only the uncommitted plan and the benchmark's API timings.
- **Disk:** C: 3.4 GB and D: 3.6 GB free at the start. The backup needed under 5 MB on D:.
- **Real data:** the 34 files of `data/` and `chroma_db/` were hashed before anything ran (`sha256sum`). That
  record is checked after every step below.

The FastAPI application was never started on the real data, and no production migration was run.

## 1. Phase 0

### 1.1 The first backup of the real data

**Safety, checked before creating it** (`src/backup.py`, `scripts/manage_backups.py`):

- the databases are read through read-only connections and copied with SQLite's online backup API;
- no store class is opened, so nothing initializes or upgrades a schema;
- facts and integrity checks run on the copies, never on the sources;
- nothing is written into `data/`.

**Steps and results:**

| Step | Result |
|---|---|
| `manage_backups.py create` | `success`: 4.9 MB, the search index included. Location: `D:\LogiDoc-backups\logidoc-20261115T142522Z-manual-d4e6ee` |
| `manage_backups.py verify` | `ok`, 23 of 23 files match the manifest's SHA-256. Databases: history, reviews and ingestion register `ok`; sign-in, extractions, carrier registry and integrations absent (they do not exist in the real data) |
| `manage_backups.py restore` | `restored` into `D:\LogiDoc-restored\logidoc-20261115T142522Z-manual-d4e6ee` |
| Independent checks on the restored copy | see below: all passed |
| The live data after each step | unchanged (`sha256sum -c`, exit 0) |

**The independent checks on the restored copy:**

- `PRAGMA integrity_check` ok and `foreign_key_check` empty for all three databases;
- versions unchanged: history 3, reviews 2, register 1;
- row counts equal to the manifest;
- the 14 documents (raw and archived) byte-identical to the originals.

The backup and the restored copy are kept: nothing was deleted.

### 1.2 Performance baseline

Measured on synthetic data in temporary folders only, never the live databases. The benchmark gained API
timings: 12 endpoints, the median of 5 warm calls. [PERFORMANCE_BASELINE.md](PERFORMANCE_BASELINE.md)

At 100 shipments (400 PDFs):

- cold audit: 13.8 s;
- warm API reads: 17–66 ms;
- summary PDF: 945 ms.

## 2. Phase 1

### 2.1 Migration rehearsal (reviews 2 → 3, history 3 → 6)

`scripts/rehearse_migration.py` (new) copies a restored data folder and upgrades the copy with the application's own
store classes. It refuses the live data folder, index and databases as source or destination, and its report
holds counts and digests, never a row. [MIGRATION_REHEARSAL.md](MIGRATION_REHEARSAL.md)

**It ran on fresh copies of the restored backup, on Python 3.12 and 3.10. Every check passed:**

- versions 2 → 3 and 3 → 6;
- every earlier row kept, by digest over the earlier columns:
  - reviews: 10 reviews and 10 events;
  - history: 1 run, 4 snapshots, 4 findings, 4 input sets, 20 inputs;
  - register: 11 files, 33 events;
- the additions as expected: `freight_audit_records`, `run_inputs.tms`, 2 indexes, 2 append-only triggers;
- no index or trigger lost;
- integrity ok, no foreign-key problem;
- the review hash chain verified: 10 events, 0 problems;
- the `.before-v3` copy kept, at version 2 with identical rows;
- opening again changes nothing;
- 4 of 4 snapshots read back;
- the application answered 200 on 28 read endpoints over the upgraded copy.

**The sources were left alone:** the restored databases are byte-identical to the backup's, the backup still
matches its manifest, and the live data is unchanged.

**Irreversible changes:**

- Both upgrades are forward-only: an older build refuses the newer schema.
- The review upgrade rebuilds its table; the copy it keeps beside the database is the in-place way back.
- **Rollback** is restoring the backup made before the upgrade. Anything recorded after it is lost.

### 2.2 API authorization coverage

**Inventory:** `scripts/api_permissions.py` (new) reads every operation's sign-in and permission from the routes
themselves and writes [API_PERMISSIONS.md](API_PERMISSIONS.md). Of the 126 operations:

- 5 are open to anyone (health, readiness, session, sign-in, sign-out);
- 60 are for administrators and reviewers;
- 61 are for administrators only.

**`tests/test_api_authorization.py` (new, 15 tests):**

- it calls every operation under `/api` and `/api/v1`:
  - without a session → 401, except the 5 public ones;
  - as a reviewer → 403 on every administrator-only operation, and past authorization on the others;
  - as an administrator → past authorization everywhere;
  - without the CSRF header → 403 on every POST and PATCH, signed in or not;
- the published inventory must match the code;
- the set of public operations and the reviewer's permissions are pinned;
- the five writes a reviewer may make are listed;
- a refused document change creates no store.

A deliberately weakened permission made three of the tests fail.

**Fixes (authorization only made stricter, never weaker):**

- **Six document operations** (upload, retry, resolve, reprocess, replace, remove) now check `ingestion.write`
  before anything else. They used to:
  - parse the request first (a reviewer got 422 for a missing file instead of 403);
  - or create the ingestion register before the service refused (reprocess, remove).

  The services keep their own check.
- **`GET /api/exceptions` and `POST /api/exceptions/{key}/actions`** now declare `exceptions.view` like every
  other exception route. The first checked it inside the handler, after other work. Each action still checks
  its own permission.
- **`POST /api/admin/verify-history`** verifies a read-only copy: verifying no longer creates or upgrades the
  review database.

### 2.3 Read requests no longer write

**Before:** every read that listed exceptions synchronised the review store in a write transaction. That covered
the exception queue and detail, the overview, shipment detail, carriers, risk, every report and the exceptions
CSV. Opening the store also created or upgraded it.

**Now:**

- **A read copies the database into memory** through a read-only connection (`review_store.ReviewView`): never
  creating, upgrading, locking or writing the file.
- **It records the detected exceptions on that copy** with the same code as the sync (`record_detected`). The
  answers are what they were before: new exceptions open, reopenings and recheck flags, the queue, its filters
  and its history.
- **A new exception keeps one creation time across reads:** when the server first saw it.

**Who records detections:**

- **a change to an exception** (before the action, so the exception exists in the store);
- **the server's background loop**, every 30 s, which also keeps the `exception.created` webhooks timely. It does
  nothing when the store already holds the current audit.

**`tests/test_read_only_requests.py` (new, 9 tests)** calls every GET operation on three review databases (none,
a current one with reviews, and one at version 2) and checks:

- the file comes out byte for byte, and is never created or upgraded;
- the answers still show every detection;
- an action on a never-recorded exception records the audit first, with the time shown before;
- the background loop records once;
- a reopening shows on a read before it is recorded;
- the view refuses writes and newer schemas.

With reads made to record again, two of these tests fail.

### 2.4 Rate limiting

`src/api/rate_limit.py` (new) puts token buckets per caller on the expensive and sensitive operations.
[RATE_LIMITING.md](RATE_LIMITING.md)

**The rules:**

- failed sign-ins: 10 a minute per client address (successful ones never count);
- the assistant: 20 a minute per session;
- document changes: 30 a minute per session;
- audit, extraction, backup and integration jobs: 30 a minute per session;
- report downloads: 60 a minute per session;
- account changes: 20 a minute per session.

**A refusal** is 429 with `Retry-After`, a JSON reason, the security headers and a `logidoc_rate_limited_total`
count. Memory is bounded (10,000 buckets, least recently used dropped first).

**Forwarded addresses:**

- The application never reads `X-Forwarded-For` itself: uvicorn takes it from the trusted proxy only, from the
  right.
- `LOGIDOC_FORWARDED_ALLOW_IPS=*`, with which a client could choose its address, is now refused in production and
  warned about otherwise.
- The nginx example adds `limit_req` zones (sign-in, and the API with a burst for screens), and now replaces
  `X-Forwarded-For` with the address it sees instead of appending to it.
- The Caddyfile notes that Caddy has no limits of its own.

**Normal workflows:** the whole existing test suite runs with the limits on.

**`tests/test_rate_limit.py` (new, 13 tests):**

- the buckets: bursts, even refills, the wait, bounded memory;
- which operation each rule covers, and that ordinary reads and previews are not limited;
- 429 and `Retry-After` through the API and `/api/v1`;
- success never counted;
- a spoofed `X-Forwarded-For` ignored without a trusted proxy, and honoured from the right behind one;
- per-session separation;
- the production refusal of `*`;
- the nginx example.

### 2.5 Dependency security

**A new CI job, *Dependency vulnerability audit*:**

- `pip-audit` 2.9.0 over the pinned `requirements-dev.txt` (which includes `requirements.txt`), from the lockfile
  pins alone, with nothing installed;
- `npm audit --omit=dev --audit-level=high` from the lockfile.

It runs on every push and pull request, and weekly on the default branch. The other jobs skip the weekly run.

[DEPENDENCY_SECURITY.md](DEPENDENCY_SECURITY.md) sets out:

- how a finding is reported, assessed and resolved, with timelines by severity;
- that major versions are never upgraded automatically;
- that a finding is accepted only with a written justification, a named person, a date and a review date.

**Today:** no known vulnerability in the 109 pinned Python packages (`pip-audit`, exit 0), and none in the
frontend's runtime dependencies (`npm audit`, exit 0).

### 2.6 The legacy exceptions CSV

**The `evidence` column held each item's quoted document passage.** It now lists only the distinct references:
file, page and document type. `audit_analytics.evidence_reference` gives the part before the passage, and
anything that is not a reference is left out, because it could be document text. The newer reports use the
same function, instead of splitting on `": "`.

**The other columns** (shipment, kind, category, severity, description, difference and unit, review state,
reviewer, note, update time) and the header are unchanged.

**`tests/test_exceptions_csv.py` (new, 5 tests):**

- no passage, card number, e-mail address or amount from the evidence reaches the file;
- a bare string is left out;
- the structured fields are kept;
- every cell is protected against formula injection (`=`, `+`, `-`, `@`, the full-width `＝`, tab, carriage return,
  a hostile file name), while a negative number stays a number;
- through the API, none of the sample documents' quoted passages appears in the download.

### 2.7 Kept as decided

- **TMS conflicts** stay one administrator's decision, with a mandatory reason and the audit history.
- **Deferred, as decided:** tamper-evident audit history, WAL mode and real-browser tests.

## 3. Problems found and fixed during the work

- **The first rehearsal run failed two checks**, both in the script:
  - it counted the `schema_info` version row as "changed", but that row changes on purpose and is now checked
    on its own;
  - it called `/api/history/compare` without its required parameters (422).

  Fixed, then two fresh runs (and a final one) passed every check.
- **The permission sweep hit the new rate limits.** Hundreds of calls in seconds made the job endpoints answer
  429 before the CSRF check. The sweep's application now runs without limits, and the limits are tested on
  their own.
- **A rounding error in the wait.** 0.95 of a token gave a 2 s `Retry-After` instead of 1 s; the wait is now
  rounded before it is rounded up.
- **A misleading slowdown.** One benchmark run after this phase was up to 8 times slower than the baseline. The
  committed code and the new code, run alternately under the same conditions, were equally slow: the machine
  was short of memory, not the code. [PERFORMANCE_BASELINE.md](PERFORMANCE_BASELINE.md), section 4.

## 4. Validation

Every Python run used `-p no:cacheprovider`, one at a time, with its temporary folder outside the project.

| Check | Result |
|---|---|
| New tests (3.12): authorization 15, read-only requests 9, rate limits 13, exceptions CSV 5, rehearsal script 4 | all passed, exit 0 |
| Focused batch (3.12): the new files and 16 affected ones (API, contracts, workflow, review store, webhooks, feed, risk, reporting, analytics, production configuration, observability, operations, freight operations, carriers, uploads, TMS) | 546 passed, 1 failed (the rate-limit collision in section 3); after the fix the file passes, exit 0 |
| The other tests that read review states (3.12): analytics cache and dashboard, operations UI, backup verification, freight audit, production UI | 113 passed, exit 0 |
| **Full suite (3.12)** | **2,455 passed, exit 0** (18 min 38 s) |
| New and affected files (3.10) | 124 passed, exit 0 |
| Deterministic evaluation guard, `scripts/evaluate_assistant.py` (3.12 and 3.10) | `RESULT: PASS`, exit 0 both |
| OpenAPI | `--check` up to date. Against `6002747`: no path, operation or schema added or removed; two operation descriptions reworded |
| Permission inventory | `scripts/api_permissions.py --check` up to date |
| Migration rehearsal, final run with the final code | `RESULT: PASS`, 20 of 20 checks |
| Dependencies | `pip-audit`: no known vulnerability (exit 0); `npm audit --omit=dev`: 0 vulnerabilities |
| Real data | the 34 files of `data/` and `chroma_db/` unchanged (`sha256sum -c`, exit 0) after every step |
| `git diff --check`; secrets scan of the changed lines | clean: no key, token, e-mail, user path or real-data path; only loopback, private and documentation IP addresses |
| Attribution | no AI attribution in any commit or tracked file |
| Frontend | not changed in this phase, and not run locally (to save disk); CI runs the typecheck, tests and build |
| `scripts/evaluate.py` (real-document extraction) | not run: no extraction code changed |

## 5. Changed files

- **New, backend:** `src/api/rate_limit.py`
- **Changed, backend:**
  - `src/review_store.py`: `record_detected`, `ReviewView`
  - `src/audit_service.py`: read-only `exceptions`, `review_view`, `record_detections`
  - `src/operations.py`: detections recorded in the background loop
  - `src/api/app.py`: `ROUTERS`, the rate limit middleware
  - `src/api/core.py`: the inventory attribute, `rate_limits`
  - `src/api/routes_ingestion.py`, `src/api/routes_workflow.py`, `src/api/routes_auth.py`: permissions declared
    before the work, read-only views
  - `src/audit_analytics.py`: evidence references
  - `src/reporting.py`
  - `src/environment.py`: `*` refused
  - `src/metrics.py`
- **Scripts:** `scripts/api_permissions.py` and `scripts/rehearse_migration.py` (new); `scripts/benchmark_scale.py`
  (API timings)
- **Tests, new:** `tests/test_api_authorization.py`, `tests/test_read_only_requests.py`, `tests/test_rate_limit.py`,
  `tests/test_exceptions_csv.py`, `tests/test_rehearse_migration.py`
- **CI and deployment:** `.github/workflows/ci.yml`, `deploy/nginx/logidoc.conf`, `deploy/Caddyfile`, `.env.example`
- **Documentation:**
  - new: `docs/API_PERMISSIONS.md` (generated), `docs/RATE_LIMITING.md`, `docs/DEPENDENCY_SECURITY.md`,
    `docs/MIGRATION_REHEARSAL.md`, `docs/PERFORMANCE_BASELINE.md`, `docs/SPRINT_4_PLAN.md`, this report;
  - changed: `docs/api/openapi-v1.json`, `docs/OBSERVABILITY_AND_MONITORING.md`, `docs/REPORTING.md`, `README.md`

**Not committed, kept on D: and not deleted:**

- the backup (`D:\LogiDoc-backups\...`);
- the restored copy (`D:\LogiDoc-restored\...`);
- the four rehearsal folders (`D:\LogiDoc-rehearsal\...`);
- the temporary folders in `D:\ldt3a`, including a pip-audit environment and a copy of the committed sources
  used for the benchmark comparison.

**Not touched:** `data/`, `chroma_db/`, the PDFs, the frozen evaluation outputs, `main`, and any other project.

## 6. Remaining risks

- **History reads still upgrade the history database.** Only the review database was in scope. The first start
  of the new code upgrades both databases; the migration guard (T4.7) is still to do before production.
- **The legacy Streamlit interface still synchronises reviews when it shows them** (`app.py`,
  `src/audit_workflow_ui.py`): only the API reads were changed.
- **Detections are recorded by changes and the background loop only.** Without background jobs (tests, or
  `LOGIDOC_BACKGROUND_JOBS` off), a new exception is shown but not recorded, and its `exception.created` webhook
  waits, until someone acts on an exception.
- **Rate limits are per process and kept in memory**, so they reset on restart. The proxy's limits are the
  shared ones.
- **The exception queue is up to about 25% slower** while detections are not yet recorded (section 2.3).
- **No sensitive-data inventory table yet** (T2.6) and **no threat model** (T2.7).

## 7. Disk space

At the end:

- C: 1.7 GB free, D: 1.3 GB free.
- The page files had grown to 10.7 GB (C:, its maximum) and 6.5 GB (D:). A restart before phase 2 is advised.
- The project's own temporary folders hold under 350 MB. They were not deleted (permission needed).

## 8. Commit and push

Two commits on `6002747`, staged by exact paths, by the repository owner's account (`krishna-2-005`):

1. the code, scripts, tests, CI, deployment examples and generated inventory;
2. the documentation with this report.

Nothing was merged into `main`, and phase 2 was not started.
