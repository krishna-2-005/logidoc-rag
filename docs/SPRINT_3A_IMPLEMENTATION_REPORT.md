# Sprint 3A implementation report

Integration reliability and recovery.

Date: 2026-11-13. Branch `feature/production-readiness`, starting from `ab46461`. Local and remote were at the
same commit, and both CI runs on it (push and pull request) had passed all four jobs. Nothing was merged into
`main`, and no history was rewritten.

---

## 1. Status

| Task | Built | Tests | Documented |
|---|---|---|---|
| 1. Integration sync concurrency | yes | 10 new backend tests | `docs/TMS_ERP_INTEGRATION_FRAMEWORK.md`, `docs/SCHEDULED_SYNCHRONISATION.md` |
| 2. Backup integrity and recovery | yes | 22 new backend tests (16 functions) | `docs/AUTOMATED_BACKUP_OPERATIONS.md`, `docs/PRODUCTION_BACKUP_AND_RECOVERY.md`, `docs/OBSERVABILITY_AND_MONITORING.md` |
| 3. Webhook delivery monitoring | yes | 4 new backend tests, 3 screen tests | `docs/WEBHOOKS_AND_DELIVERY.md` |
| 4. Integration health dashboard | yes | 2 new backend tests, 2 screen tests | `docs/TMS_ERP_INTEGRATION_FRAMEWORK.md` |

Every figure on the two new screens is computed from stored records: sync runs, deliveries and their attempts.
Nothing is estimated or invented. A rate whose denominator is zero is `null` (shown as n/a), never 0 % or 100 %.

## 2. Task 1, integration sync concurrency

**How the races were found.** Each race was reproduced first by a failing test that interleaves two workers
deterministically, then fixed.

| Race or failure | Before | Now |
|---|---|---|
| The scheduler read the due list, a manual sync finished, then the scheduler claimed | a second, duplicate sync ran | a scheduled claim re-checks `enabled` and `next_sync_at` under the write lock (`SyncNotDue`) and is skipped |
| Two claims at the same moment | (already one, now tested with a barrier) | one run, the other `SyncBusy` (409) |
| A worker outlived its lease, was recovered as `interrupted`, a new run started, the old worker carried on | it rewrote its run as `partial`, released the new run's lease and could still write records | it finds it no longer owns the lease and writes nothing |
| The same late worker saved its cursor | the cursor moved back | no write without ownership |
| A partner re-sent an older copy of a record | the older content replaced the newer | counted as `stale`, never stored |
| An administrator synced after a crash, before the scheduler's recovery | the crashed run stayed `running` in the history forever | the claim closes it as `interrupted` in the same transaction |

**Design.**

- **Lease owner.** The claim inserts the run and records `lease_owner = "sync-run:<id>"` in one `BEGIN IMMEDIATE`
  transaction.
- **Ownership on every write.** Every write of a run (each page's records, cursor and counts, and the outcome)
  first checks, in the same transaction, that the lease owner is this run and the run is still `running`. The
  check also renews the 15-minute lease, so a long sync keeps it.
- **Late workers.** A run that lost its lease stops, logs, and counts `logidoc_sync_lease_lost_total`.
- **Store version 2.** It adds `integrations.lease_owner` and `sync_runs.stale`, applied on open in one
  transaction. Existing rows are kept, and older runs read `stale = 0`.

Manual and scheduled syncs share this one claim path, so neither can corrupt the other's state.

## 2b. Other bugs found while checking the work

| Bug | Fix | Test |
|---|---|---|
| A manual sync after a crash left the crashed run `running` (the last row of the table above) | `claim()` closes it | `test_a_manual_sync_after_a_crash_closes_the_crashed_run` |
| The delivery history's `to` day excluded deliveries in its last fraction of a second (`< T23:59:59.999` against microsecond timestamps) | the bound is the next day's midnight | `test_the_delivery_history_filters_by_event_type_status_and_day` |
| A restore drill whose copy could not be removed still reported `ok`, so nobody was told about the disk it used | it fails as `cleanup` and alerts | `test_a_drill_copy_that_cannot_be_removed_fails_the_drill` |
| Not a bug, an improvement: the scheduler re-read and parsed the whole verification history twice every 30 seconds | parsed once per change of the file | `test_the_history_is_read_again_after_each_new_entry` (a new entry is always seen) |

## 3. Task 2, backup integrity and recovery

- **Verification with categories.** `verify_backup` now classifies each problem (`manifest`, `incomplete`,
  `missing_file`, `checksum`, `integrity`, `contents`) and reports each database as `ok`, `failed` or `absent`. Besides the
  manifest, checksums and `PRAGMA integrity_check` it already ran, it now compares each database copy with what the
  manifest recorded: schema version, row count of every table, and no new foreign-key problems.
- **Restore drills.** `restore_drill` reuses the real `restore_backup` into a new `.restore-drill-*` folder under
  `LOGIDOC_BACKUP_DRILL_DIR`, with every check of a real restore, then removes the copy. It refuses a folder
  inside the live data, the index or the backups, and checks free space before copying. Its report lists the
  steps, the problems, a category (`insufficient_space`, `refused`, `restore`, `cleanup` or the error's category)
  and the duration.
- **Schedule.** By default every complete backup is verified every 24 h (`LOGIDOC_BACKUP_VERIFY_HOURS`), and the
  newest usable one is drilled every 168 h (`LOGIDOC_BACKUP_DRILL_HOURS`). Checks take the same in-process lock
  and backup-folder file lock as a backup, so they never overlap one.
- **Reports.**
  - Every result is appended to `<backups>/verification-history.jsonl`, which is append-only, with problems
    redacted.
  - A failure raises the critical alert `backup_verification_failed`; the next clean check resolves it.
  - Results are counted in `logidoc_backup_verifications_total{kind,outcome}`.
- **Interfaces.**
  - `POST /api/admin/backups/{id}/verify` (202; 404, 409 or 422 with a clear message).
  - `GET /api/admin/backups/checks`, and each backup's `last_check` in `GET /api/admin/backups`.
  - `manage_backups.py drill <id>`.
- **Live data is never written.** Every test uses synthetic databases in temporary folders. One test hashes the
  live folder before and after a drill.

## 4. Task 3, webhook delivery monitoring

`GET /api/integrations/monitoring/webhooks?days=&endpoint_id=&event_type=` reads `webhook_deliveries` and
`delivery_attempts` only, leaving test deliveries out. It reports:

- **Counts:** deliveries, succeeded, failed, waiting (pending, retrying or sending), cancelled, attempts, and
  retry attempts.
- **Success rate** = succeeded / (succeeded + failed); `null` when no delivery finished.
- **Last success and last failure,** overall and per endpoint. Endpoints with failures are listed first.
- **Breakdowns** by event type and by attempt error category.
- **Per UTC day:** outcomes, attempts, and attempt latency as median and 95th percentile (nearest rank). A day
  without an attempt has no latency, not 0 ms.

The delivery list gains `event_type` (repeatable) and `from`/`to` (UTC days, inclusive; 422 when reversed).

**Screens.** Webhooks now has three tabs: Endpoints (unchanged), **Monitoring** and **Delivery history**.

- **Monitoring:**
  - filters for period, endpoint and event type
  - six KPI cards; the success rate and retry attempts state their definitions
  - outcomes per day (stacked columns) and latency per day
  - endpoints table, error categories and event types
- **Delivery history:** filters for status, event type, endpoint and date range, with paging.

## 5. Task 4, integration health dashboard

`GET /api/integrations/monitoring/connections?days=` is computed from `sync_runs` and the integrations' own
state. Integrations has two tabs: Integrations (unchanged) and **Health**.

- **KPI cards:** runs, success rate (succeeded / finished runs, with partial counted as finished), records
  imported and rejected, retries, and failed runs.
- **Charts:**
  - sync outcomes per day: succeeded, partial, failed, interrupted;
  - records imported, updated and rejected per day;
  - runs by error category, labelled "n of N runs".
- **Per-integration table:** connection status, consecutive failures, last success, last failure, next scheduled
  sync (or "Disabled"), runs, records imported and rejected, retries, error categories, and the five latest
  runs.

## 6. UI

- **Reused components:** KpiCard, Columns, BarList, ChartCard, Tabs, Table, Pagination, EmptyState and
  ErrorState. Nothing was added as a dependency.
- **Accessible charts.** Every chart has a "Show the numbers" table (new `ChartTable`). The four outcome colours
  (done, partial, failed, interrupted) were run through the palette validator: lightness band, chroma floor,
  colour-blind separation of adjacent pairs, and normal-vision floor all pass. The amber's contrast warning is
  why the number tables are always present.
- **Tabs keep their place.** The selected tab is in the URL (`?tab=`), so a reload or a shared link keeps it.
- **Empty states say what is missing.** For example: no run in the period, no record changed, no failed
  attempt, no integration configured.

## 7. Performance

- **Sync.** Each page adds one lease check (two indexed reads and one update) inside the transaction it already
  had, plus one short write transaction before the page. At most 20 pages per run, so this is negligible.
- **Monitoring endpoints.** They read the period's rows once and aggregate in Python. Neither table has an index
  on the time column, so SQLite scans them. That takes milliseconds at the expected volumes (hourly syncs,
  thousands of deliveries) but grows linearly. See the risks.
- **Verification history.** The history is read every 30-second scheduler tick and by `GET /backups`. It is
  parsed once per change of the file (cached on its size and modification time), not on every call.
- **Frontend.** The main bundle went from 1,095.72 kB to 1,117.45 kB (+21.7 kB, gzip 315.80 kB).
- **Verification and drills.** They read every backup file and copy one backup, by default daily and weekly. They
  hold the backup lock, so a scheduled backup that falls due during a drill runs at the next tick.

## 8. Remaining risks

- **No time index for the monitoring queries.** At very large delivery volumes the scan grows linearly. The
  fix is an index on `webhook_deliveries(created_at)` and `sync_runs(started_at)`, which needs store version 3.
- **Drills need free space** of about 1.1 × the backup plus 64 MB in the drill folder. Without it, they fail as
  `insufficient_space` and alert; they never clean anything up to make room.
- **A drill runs inside the server process** and opens the restored vector index with ChromaDB. On a small
  machine, schedule it (`LOGIDOC_BACKUP_DRILL_HOURS`) at a quiet time, or run `manage_backups.py drill` by hand.
- **The lease is time-based.** A worker paused for more than 15 minutes between two pages can be recovered by the
  scheduler or overtaken by a new claim. It then stops without writing, and the next run resumes from the saved
  cursor. This is safe, but the paused run's work is repeated.
- **Real systems.** No real TMS, ERP or webhook partner was connected. All tests use the sandbox, fake
  receivers, and one real local HTTP receiver.

## 9. Validation

All Python runs used `-p no:cacheprovider` and `TEMP` on the short local path, one at a time.

| Check | Command | Result |
|---|---|---|
| Focused regression tests | the race tests of section 2 and the first and third tests of section 2b were run before their fix | failed as expected, then passed. The `to`-day test was written with its fix (the old bound sorts before a microsecond timestamp of the same second) |
| Integrations, webhooks, scheduler (3.12) | `pytest tests/test_tms_integration.py tests/test_webhooks.py tests/test_scheduler.py` | 141 passed, exit 0 (before the four fixes of section 2b, which the full suite covers) |
| Backups (3.12) | `pytest tests/test_backup_verification.py tests/test_backup_recovery.py tests/test_backup_automation.py` | 93 passed, exit 0 (likewise) |
| Config, authorization, contracts, feeds, registry (3.12) | `pytest tests/test_production_config.py tests/test_operations_authorization.py tests/test_api_contracts.py tests/test_integration_feed.py tests/test_carrier_registry_operations.py` | 141 passed, exit 0 |
| Same three groups (3.10) | same commands with `venv\Scripts\python.exe` | 141, 93 and 141 passed, exit 0 each |
| Full suite (3.12), final code | `pytest -rs tests` | 2,338 passed, 1 failed, exit 1. The failure is `test_an_administrator_makes_a_backup_and_sees_it`: the backup correctly refused to start because the temporary drive had 653 MB free, below the 1,024 MB reserve. The Windows paging file had grown on that drive during the run. The same file re-run with the temporary folder on a drive with room: 36 passed, exit 0 |
| Changed files (3.10), final code | `pytest` on the seven changed or affected test files | 255 passed, exit 0 |
| First commit alone | its tree extracted, OpenAPI generated there, `pytest tests/test_tms_integration.py tests/test_webhooks.py tests/test_api_contracts.py` | 145 passed, exit 0 |
| Frontend typecheck | `npx tsc --noEmit` | exit 0 |
| Frontend tests | `npx vitest run` | 11 files, 256 tests passed, exit 0 |
| Production build | `npx vite build` | exit 0. No frontend file changed after this build |
| OpenAPI | `scripts/export_openapi.py --check` | up to date, exit 0. Compared with `ab46461`: 0 removals, 28 additions (4 paths, 3 optional query parameters, 4 optional properties, 17 schemas) |
| Evaluation | `scripts/evaluate.py` (3.10), compared with the recorded output before Sprint 2 | exit 0; identical apart from timings (RAG and freight audit unchanged) |
| CI evaluation guard | `scripts/evaluate_assistant.py --json ...` on 3.10 and 3.12 | `RESULT: PASS` on both, exit 0 |
| Whitespace | `git diff --check` | clean, exit 0 |
| Secrets | scan of every added line for keys, tokens, passwords and private paths | nothing found in 4,529 added lines. The only matches are synthetic `.sqlite3` and `.pdf` names in test temporary folders |
| Real data | SHA-256 of the 34 files in `data/` and `chroma_db/` against the record made before Sprint 2 | all 34 unchanged (`sha256sum -c`, exit 0), no file added |
| Attribution | the two checks of the attribution rule, plus the changed files | nothing found, before and after committing |

## 10. Changed files

Backend:

- `src/integration_store.py`: schema version 2 (`lease_owner`, `stale`)
- `src/integration_framework.py`: lease owner, `SyncNotDue`, `LeaseLost`, closing orphaned runs, stale guard, the health dashboard
- `src/webhooks.py`: monitoring, delivery history filters, `percentile`
- `src/backup.py`: categorised verification, contents comparison, `restore_drill`, `discard_drill`
- `src/backup_automation.py`: verification and drill schedule, history, runner `verify`
- `src/operations.py`: scheduled checks, `start_verification`, the `backup_verification_failed` alert
- `src/environment.py`: the three new settings validated at startup
- `src/api/routes_integrations.py`, `src/api/routes_operations.py`, `src/api/schemas.py`: the new endpoints and models
- `scripts/manage_backups.py`: `verify` and `drill` through the runner, recorded in the history
- `.env.example`: the three new settings
- `docs/api/openapi-v1.json`: regenerated

Tests:

- `tests/test_backup_verification.py` (new)
- `tests/test_tms_integration.py`
- `tests/test_webhooks.py`
- `tests/test_backup_recovery.py`: the exact result of `verify_backup` now includes `categories` and `databases`

Frontend:

- `frontend/src/components/common/ChartTable.tsx` (new)
- `frontend/src/components/integrations/WebhookMonitoring.tsx` (new)
- `frontend/src/components/integrations/IntegrationHealth.tsx` (new)
- `frontend/src/pages/Webhooks.tsx`, `frontend/src/pages/Integrations.tsx`: tabs
- `frontend/src/hooks/integrations.ts`, `frontend/src/lib/types.ts`, `frontend/src/lib/chart.ts`
- `frontend/src/test/integrations.test.tsx`

Documentation:

- `docs/TMS_ERP_INTEGRATION_FRAMEWORK.md`
- `docs/SCHEDULED_SYNCHRONISATION.md`
- `docs/WEBHOOKS_AND_DELIVERY.md`
- `docs/AUTOMATED_BACKUP_OPERATIONS.md`
- `docs/PRODUCTION_BACKUP_AND_RECOVERY.md`
- `docs/OBSERVABILITY_AND_MONITORING.md`
- this report

Not touched: `data/`, `chroma_db/`, the PDFs, the backups, `.env` files, the RAG and freight audit code, and any
other project.

## 11. Commits, push and CI

Four commits on `ab46461`, staged by exact paths, by `krishna-2-005 <kuchurusaikrishnareddy@gmail.com>`. Each
one passes its own tests:

1. Integrations: sync leases and recovery, the stale guard, webhook monitoring and integration health, with
   their API, tests and the OpenAPI contract at that point.
2. Backups: verification, restore drills, history, alert, API, command line, tests, and the final contract.
3. The screens.
4. The documentation and this report.

Their hashes, the push and the CI result are recorded in section 12 by a follow-up commit, after the push.
