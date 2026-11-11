# Sprint 2 implementation report

Carrier operations and enterprise integrations.

Date: 2026-11-10. Branch `feature/production-readiness`, starting from `c889815` (both CI runs passed on it).
**Nothing is committed or pushed, and nothing was merged into `main`.**

---

## 1. Status

| Workstream | Built | Tested | Documented |
|---|---|---|---|
| 1. Carrier registry management (screens and paged API) | yes | backend 38 tests, screens 11 tests | `docs/CARRIER_REGISTRY_OPERATIONS.md` |
| 2. Secure webhooks | yes | 73 tests, one over real HTTP | `docs/WEBHOOKS_AND_DELIVERY.md` |
| 3. Scheduled synchronisation | yes | 17 tests | `docs/SCHEDULED_SYNCHRONISATION.md` |
| 4. TMS/ERP integration framework with a sandbox TMS | yes | 26 tests | `docs/TMS_ERP_INTEGRATION_FRAMEWORK.md` |
| Contracts, OpenAPI, backups, settings | yes | 10 contract tests, existing suites | this report |

What this sprint does **not** claim:

- No real TMS or ERP vendor is integrated or was contacted.
- No webhook was sent to an external partner: the receivers in the tests are fakes, plus one real HTTP receiver
  on this machine.
- Nothing was deployed, and no real data was migrated.

## 2. Starting point

- `git status`: clean.
- Branch: `feature/production-readiness`.
- `HEAD` and `origin/feature/production-readiness` both at `c889815`.
- Git identity: `krishna-2-005 <kuchurusaikrishnareddy@gmail.com>`.
- The SHA-256 of every file in `data/` and `chroma_db/` (34 files) was recorded before any change.

## 3. Workstream 1, carrier registry management

- **Screens:**
  - `/carrier-registry`: list with search, status filter, sorting and paging done by the server; empty, loading
    and error states.
  - `/carrier-registry/{id}`: master data, linked names, approvals waiting, Carrier Intelligence figures,
    decisions and history.
  - `/carrier-registry/links`: approvals awaiting a decision, the carrier names on the documents with their
    shipments and documents, and decided requests.
- **Form:** register and edit. The server's validation is shown as it is, and a malformed identifier is never
  cleaned. A duplicate identifier is explained by naming the carrier that holds it. A possible duplicate name is
  registered only after ticking "this is a different company".
- **Approval:** two people, unchanged. The proposer sees "another administrator decides" instead of buttons, and
  the server refuses them anyway (403). A rejection needs a note. Every step is in the append-only
  `carrier_events`.
- **No duplicated figures:** the detail page reads Carrier Intelligence (`GET /api/carriers/registered:{id}`)
  instead of computing figures again.
- **Schema version 2** of `carrier_registry.sqlite3` adds the registration status (active, on hold, inactive) and
  the address. It is one transaction when the store is opened, and no row or event is rewritten.
- **Permissions:**
  - Reading needs `exceptions.view`.
  - Every change needs `carriers.manage`.
  - The carrier history is for administrators only (null for reviewers).

## 4. Workstream 2, secure webhooks

- **Events:** seven types (freight audit completed and discrepancy, exception created and status changed, carrier
  created and updated, name link approved). They are collected from the three append-only logs that already exist,
  so only committed changes become events; this was tested with an uncommitted insert held open.
- **No duplicates:** event ids are deterministic, the outbox is unique per (source, row, type), and the cursor
  moves in the same transaction as the events.
- **Signatures:** HMAC-SHA256 of `timestamp.event_id.body`, with a 300 s tolerance. `verify_signature()` and
  `ReplayGuard` are the receiver-side reference.
- **Secrets are never stored:** they are derived from `LOGIDOC_WEBHOOK_SECRET_KEY` and a per-endpoint salt, shown
  once, and rotated with a 24-hour overlap.
- **SSRF protection:** HTTPS only, public addresses only, checked when saved and again after a fresh DNS lookup
  before every attempt. The connection is pinned to the checked address, redirects are never followed, ports are
  restricted, and there is a 10 s timeout, a 64 KB answer limit and a 256 KB payload limit.
- **Retries:** exponential back-off with jitter, honouring Retry-After, with at most 8 attempts. Permanent 4xx
  and 3xx answers fail at once.
- **Recovery and manual control:**
  - An interrupted attempt is recovered and sent again.
  - Manual retry and test events.
  - Disabling holds waiting deliveries; archiving is final and keeps the history.
- **Screens:** endpoints with seven-day success rate, failed and waiting counts; endpoint detail with deliveries,
  test, rotate, archive and change history; delivery detail with every attempt and a masked payload preview.
- **Without the signing key** nothing can be registered or delivered, and the screen says why.

## 5. Workstream 3, scheduled synchronisation

- **No task queue:** one thread, with state in SQLite, since the application is one process per data folder.
- **Seven jobs:** audit-feed poll, activity collection, first deliveries, retries, integration syncs, overdue
  detection and health checks.
- **Persistence:** persistent job status and run history.
- **Locking:** a lease per job prevents overlap with the schedule or a manual trigger (409).
- **Failures:** up to 3 retries with back-off, then the normal interval and a critical alert.
- **Restarts:** after a restart, interrupted runs are marked and run again, and an overdue job runs once rather
  than once per missed interval.
- **Shutdown:** graceful, with a stop check between items and a deadline per job.
- **One owner:** an operating-system file lock beside the store. A second process runs nothing.
- **Monitoring:** metrics for jobs run, failed, delayed, retried, last success and duration, plus delivery and
  sync metrics. Alerts go through the existing alert manager. The scheduler's health is its own endpoint, never
  part of `/api/health` or `/api/ready` (tested).
- **Controls:** enable or disable, change the interval, run now, retry a failed job, run history, last failure
  and next run.

## 6. Workstream 4, TMS/ERP framework

- **Layers:**
  - Typed, provider-neutral contracts.
  - A `Provider` interface: authentication, transport and format only.
  - Normalized error categories.
  - Sync state in the integration store.
  - Imported records with provenance in their own store.
- **Kept apart from the audit:** nothing imported touches documents, audits, reviews or the carrier registry,
  and the freight audit engine is unchanged.
- **Sync engine:**
  - One sync per integration at a time.
  - Pages from a stored cursor, saved after each page (a crash re-reads one page, and re-reading changes nothing).
  - Rate limits and timeouts retried with back-off, honouring Retry-After.
  - Malformed records rejected with a reason.
  - Duplicates detected, and changes detected by a content hash.
  - Manual corrections kept, with the received value recorded and the difference counted as a conflict.
  - Recorded audit runs handed to providers that accept them.
- **Sandbox TMS:** synthetic data, 6 carriers and 40 shipments with 4 deliberately malformed. It supports
  paging, increments and simulated partner changes, and can simulate authentication failures, rate limits,
  timeouts, duplicates and unavailability.
  - It is offered only with an explicit `LOGIDOC_INTEGRATION_SANDBOX_DIR` apart from the data, index and backup
    folders, never in production.
  - Its data and its imports stay in that folder (tested).
- **Credentials:** an integration names a `LOGIDOC_INTEGRATION_<NAME>` environment variable; the value is never
  stored, logged or shown.
- **Screens:** integrations with connection, last success and failure, next sync and counts; integration detail
  with status, cursor, settings, sync runs (and their rejected records), imported records with provenance and
  versions, corrections, and configuration history. Actions: test connection, sync now, enable or disable, and
  simulate partner changes (sandbox only).
- **Real vendors:** the steps to add one are in the framework document (section 7). None was claimed.

## 7. API and database changes

### API

- **Paths:** 29 new, 68 to 97 in `docs/api/openapi-v1.json`; schemas 88 to 144. Everything is also served under
  `/api/v1`.
- **Compatibility,** compared with `c889815`:
  - No path or schema was removed.
  - Every existing operation is identical; one path gained a `GET`.
  - Three schemas (`CarrierBody`, `CarrierChanges`, `RegisteredCarrierRecord`) only gained optional fields, with
    the same required fields.
- **Conventions:** every new route has a typed response model, the one error shape (401, 403, 404, 409, 422
  declared), pagination where a list grows, input validation and backend permissions.
- **New permission:** `integrations.manage`, for administrators. It joins the existing role system; no new role.

### Databases

| Store | Change |
|---|---|
| `carrier_registry.sqlite3` | v1 → v2 when opened: 7 added columns, 4 indexes, one transaction, no row rewritten. Tested on synthetic v1 stores, including a failed upgrade that leaves v1 intact. **The real `data/` has no carrier registry, so nothing real was migrated** |
| `integrations.sqlite3` (new, v1) | Endpoints, outbox, deliveries, attempts, cursors, jobs, job runs, scheduler owner, integrations, sync runs, rejections. Append-only triggers on the history tables. Created only when an administrator configures the first endpoint or integration |
| Sandbox folder (new) | `sandbox_tms.sqlite3`, `sandbox_imports.sqlite3`, only under `LOGIDOC_INTEGRATION_SANDBOX_DIR` |
| `audit_reviews`, `audit_history` | No schema change. Read-only methods added (`events_after`, `last_event_id`, `last_run_id`) |

- **Backups** now include `integrations.sqlite3`; the backup and restore tests cover it.
- **Readiness** is unchanged: the integration store is not one the audit needs.
- **Startup validation** refuses in production:
  - a signing key under 32 characters
  - the development switches `LOGIDOC_WEBHOOK_ALLOW_HTTP` and `LOGIDOC_WEBHOOK_ALLOW_PRIVATE`
  - a sandbox folder (in any profile, also a sandbox folder inside a data, index or backup folder)
- **Documentation:** every new setting is in `.env.example`.

## 8. Security review

| Area | What protects it | Tested by |
|---|---|---|
| Permissions | Every route needs `integrations.manage` (or `carriers.manage` for registry changes); the services check again | 401 and 403 on every new route; direct service calls with a reviewer |
| CSRF | `X-LogiDoc-Client` on every change, also under `/api/v1` | 403 without it |
| SSRF | Checks at save and before each attempt, pinned connection, no redirects | 28 refused URL forms; DNS rebinding to 169.254.169.254 never contacted; mixed public and private answers refused |
| HTTPS only | `http://` refused; production ignores the development switches and refuses to start with them | URL and policy tests; environment validation |
| Signing secrets | Derived, never stored, shown once, never logged | Secret, key and URL token absent from every API answer, the logs and the database file |
| Replay | Timestamp tolerance; unique event ids; receiver guidance and `ReplayGuard` | Expired, future and replayed cases |
| Credentials | Environment variable names only (`LOGIDOC_INTEGRATION_*`); unknown settings refused | `PATH` refused; `api_key` setting refused; value absent from answers |
| Sensitive values | Payload preview and URL query masked; errors redacted (`src/redaction.py`) | Masking and redaction tests |
| History integrity | Append-only triggers on outbox, attempts, change logs, record versions and corrections | UPDATE and DELETE refused |
| Isolation | Sandbox only in its own folder; no write to the audit stores | Folder listings before and after a sync; the shipment list contains no `SBX-` record |

Problems found and fixed during this work:

1. An endpoint's change history recorded the URL unmasked, so a token in its query string was readable there. It
   is now masked like everywhere else (found by `test_the_secret_is_shown_once_and_never_again`).
2. A new request model named `CorrectionBody` collided with the existing extraction model of the same name, and
   FastAPI then renamed the existing published component. A client generated from the contract would have
   broken. It was found by comparing the regenerated contract with `c889815`, fixed by renaming the new model, and
   is guarded by `test_no_two_models_share_a_published_name`.
3. The scheduler opened three write transactions every 5 seconds. Job rows and recovery now read first and write
   only when needed, and the heartbeat is written every 15 seconds.

## 9. Validation

All on the final working tree, run one at a time (never two heavy runs together), with `TEMP` on the short path
`C:\Users\kuchu\AppData\Local\Temp\ldt`.

| Check | Command | Result |
|---|---|---|
| Full Python suite, 3.12.7 | `venv312\Scripts\python.exe -m pytest -q -p no:cacheprovider` (`OLLAMA_HOST` unreachable, as in CI) | **2,288 passed, 0 failed, 3 skipped, exit 0** (21 min 40 s) |
| Python 3.10.11, relevant set | `venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` on `test_backup_recovery`, `test_backup_automation`, `test_observability`, `test_production_config`, `test_readiness`, `test_api`, `test_api_contracts`, `test_carrier_registry`, `test_carrier_registry_operations`, `test_webhooks`, `test_scheduler`, `test_tms_integration`, `test_integration_feed`, `test_exception_workflow`, `test_audit_history` | **538 passed, 0 failed, exit 0** (3 min 49 s) |
| Frontend type check | `npx tsc --noEmit` | exit 0 |
| Frontend tests | `npx vitest run` | **11 files, 251 passed, 0 failed** (229 before; 22 new) |
| Production build | `npx vite build` | exit 0. Main chunk 1,095.72 kB (gzip 310.77 kB), against 988.20 kB before. Vite's 900 kB advisory was already exceeded |
| `pip check` | both environments | clean |
| Published contract | `scripts/export_openapi.py --check` | up to date; additive against `c889815` (section 7) |
| Offline evaluation | `scripts/evaluate.py`, Python 3.10, on an extracted copy of `c889815` and on the working tree | **identical** apart from the retrieval timing line (p95 6 vs 5 ms): recall@5 1.00, hit@1 1.00, answer match 1.00, citation precision 1.00, abstention 1.00, cross-shipment leakage 0.00; every target passes |
| CI evaluation guard | `scripts/evaluate_assistant.py --json ...` on 3.10 and 3.12 | **PASS** on both (13 checks pass, 0 fail) |
| Whitespace | `git diff --check` | clean |
| Attribution | the two checks in the attribution rule, plus a wider grep of the working tree | nothing in history or tracked files. The wider grep matches only existing wording ("Generated by a language model" in the assistant's warning, a certificate sentence, and the CI guard's own description) |
| Real data | SHA-256 of every file in `data/` and `chroma_db/` before and after | **all 34 identical**; no file added to `data/` |
| Dependencies | `requirements*.txt`, `package*.json` | unchanged; nothing new installed |

Notes on the runs:

- **New tests:** 164 Python tests (`test_webhooks` 73, `test_carrier_registry_operations` 38,
  `test_tms_integration` 26, `test_scheduler` 17, `test_api_contracts` +10) and 22 frontend tests.
- **Ollama:** the three Ollama tests are skipped because CI's unreachable `OLLAMA_HOST` was used.
- **Run times:** an earlier full run on this tree (before the last two fixes) passed 2,287 with the same 3 skips
  in 20 min 30 s. Full runs took longer than Sprint 1's 6 min 12 s; the new tests account for about 3 minutes,
  and the machine was carrying other load.

## 10. Observations on the real installation

- **The real stores are on older schema versions** than the code: `audit_reviews` at version 2 (the code writes 3)
  and `audit_history` at version 3 (the code writes 5). The existing upgrade-on-open logic, not this sprint, will
  migrate them the next time the application opens them, as earlier reports said for `audit_history`.
- **Tested on copies only:** I opened copies (never the originals) with the stores and the new readers.
  - Every row was kept; only empty new tables and indexes were added.
  - The new readers return the 10 recorded review events and the 1 recorded audit run.
  - The originals are unchanged (SHA-256).
- **Before the next start:** make a verified backup first (`scripts/manage_backups.py create`).
- **No carrier registry and no integration store exist in `data/`:** nothing was created there.

## 11. Known limitations and unfinished work

- **No real TMS or ERP vendor.** The framework and the sandbox are ready; a vendor needs its documentation,
  sandbox and credentials (`docs/TMS_ERP_INTEGRATION_FRAMEWORK.md`, section 7).
- **Imported records are reference data.** They are not matched to the shipments read from documents and do not
  feed the freight audit; that is a design decision for a later sprint.
- **Events are collected on a schedule,** so they leave within about 30 to 60 seconds of the commit.
  `exception.created` is recorded when the exception queue is read, as before this sprint.
- **One scheduler thread:** a slow receiver or a long sync delays the other jobs, within their timeouts and batch
  sizes.
- **`job_runs`** grows about 10,000 rows a day at the default intervals and is not pruned automatically
  (deleting integration history needs a deliberate decision).
- **No alert for a stale heartbeat:** the scheduler would have to raise it about itself. The status endpoint
  shows it, for an external monitor.
- **The Streamlit application** does not show the new screens; its code was not changed.
- **Frontend bundle:** it grew by 107 kB. Code splitting would bring it under Vite's advisory; it was not done in
  this sprint.
- **Webhook receivers:** no SDK; the documented steps and `verify_signature()` are the reference.

## 12. Changed files

**Modified (24):**

- `.env.example`
- `.gitignore`
- `README.md`
- `config.py`
- `deploy/logidoc.production.env.example`
- `docs/api/openapi-v1.json`
- `frontend/src/App.tsx`
- `frontend/src/components/layout/AppShell.tsx`
- `frontend/src/lib/types.ts`
- `src/api/app.py`
- `src/api/core.py`
- `src/api/routes_carriers.py`
- `src/api/schemas.py`
- `src/audit_history.py`
- `src/audit_service.py`
- `src/authz.py`
- `src/backup.py`
- `src/carrier_intelligence.py`
- `src/carrier_registry.py`
- `src/environment.py`
- `src/review_store.py`
- `tests/test_api_contracts.py`
- `tests/test_backup_automation.py`
- `tests/test_backup_recovery.py`

**New (37):**

- **Backend:**
  - `src/url_safety.py`
  - `src/integration_store.py`
  - `src/webhooks.py`
  - `src/scheduler.py`
  - `src/integration_framework.py`
  - `src/sandbox_tms.py`
  - `src/integration_runtime.py`
  - `src/api/routes_integrations.py`
- **Backend tests:**
  - `tests/integration_support.py`
  - `tests/test_webhooks.py`
  - `tests/test_scheduler.py`
  - `tests/test_tms_integration.py`
  - `tests/test_carrier_registry_operations.py`
- **Frontend pages:**
  - `frontend/src/pages/CarrierRegistry.tsx`
  - `frontend/src/pages/CarrierRegistryDetail.tsx`
  - `frontend/src/pages/CarrierLinks.tsx`
  - `frontend/src/pages/Webhooks.tsx`
  - `frontend/src/pages/WebhookDetail.tsx`
  - `frontend/src/pages/ScheduledJobs.tsx`
  - `frontend/src/pages/Integrations.tsx`
  - `frontend/src/pages/IntegrationDetail.tsx`
- **Frontend components and hooks:**
  - `frontend/src/components/carriers/CarrierForm.tsx`
  - `frontend/src/components/carriers/LinkDialogs.tsx`
  - `frontend/src/components/integrations/badges.tsx`
  - `frontend/src/components/integrations/EndpointForm.tsx`
  - `frontend/src/components/common/Dialogs.tsx`
  - `frontend/src/components/common/Facts.tsx`
  - `frontend/src/components/common/Pagination.tsx`
  - `frontend/src/hooks/registry.ts`
  - `frontend/src/hooks/integrations.ts`
- **Frontend tests:**
  - `frontend/src/test/registry.test.tsx`
  - `frontend/src/test/integrations.test.tsx`
- **Documentation:**
  - `docs/CARRIER_REGISTRY_OPERATIONS.md`
  - `docs/WEBHOOKS_AND_DELIVERY.md`
  - `docs/SCHEDULED_SYNCHRONISATION.md`
  - `docs/TMS_ERP_INTEGRATION_FRAMEWORK.md`
  - `docs/SPRINT_2_IMPLEMENTATION_REPORT.md`

**Not part of the repository:** `frontend/dist/` (the build, git-ignored).

## 13. Temporary files (kept, not deleted)

In `C:\Users\kuchu\AppData\Local\Temp\ldt`:

- the pytest folders of the runs
- `base_c889815\`: an extracted copy of the previous commit, for the evaluation comparison
- `openapi_old.json`: the previous contract
- `realcopy_95i83spr\`: **copies of the real `audit_reviews` and `audit_history` databases**, upgraded as
  described in section 10. These hold business data: delete them when no longer needed.

Nothing was deleted, as the brief asks.

## 14. Decisions for you

1. **Commit and push:** the files in section 12, on `feature/production-readiness`, staged by their paths.
2. **Webhooks:** set `LOGIDOC_WEBHOOK_SECRET_KEY` (at least 32 random characters) where the server runs.
3. **Before the next server start:** a verified backup, because the existing upgrades of `audit_reviews` and
   `audit_history` will run (section 10).
4. **Temporary copies:** whether to delete the temporary folders in section 13.
