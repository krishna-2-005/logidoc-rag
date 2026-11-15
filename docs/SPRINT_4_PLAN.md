# Sprint 4 plan: end-to-end validation, security, performance, release readiness

Status: **approved; phases 0 and 1 implemented** (section 6, and
[SPRINT_4_PHASE_0_1_REPORT.md](SPRINT_4_PHASE_0_1_REPORT.md)). Phases 2–4 are not started.

Prepared on 2026-11-15 from branch `feature/production-readiness` at `6002747` (local equals remote; all four CI jobs
passed on it; working tree clean).

## 0. Where things stand

| Fact | Value |
|---|---|
| Python tests | 2,409 (full suite about 22 min locally, 23–29 min in CI) |
| Frontend tests | 274 in 13 files |
| Main JavaScript bundle | 1,168.94 kB, gzip 327.51 kB, one chunk (988 kB at Sprint 1) |
| Measured scale | 10,000 synthetic shipments: carrier aggregation 1.7 s, recording a run 8 s, cold audit about 33 min (every PDF is read) |
| Free disk | C: 5.0 GB, D: 3.6 GB. Each full Python suite grows the Windows page files by 1–3 GB until a restart |
| Real data | 34 files: audit history, reviews, ingestion register, search index, 12 PDFs. No real backup exists yet |

Real database versions: the live files were last read at reviews **2** and history **3**. The code now writes
reviews **3** and history **6**, and both upgrade themselves **when opened**. The first start of any build since
Sprint 2 against the real data folder upgrades them, forward only.

## 1. Gaps found

Each gap below has an id (G1.1 …) that the tasks in section 2 refer to.

### Workstream 1: end-to-end logistics workflow

| # | Gap | Evidence |
|---|---|---|
| G1.1 | **No single test follows a shipment from upload to report.** Each stage is tested alone (ingestion, freight audit, corrections, re-audit, TMS, risk, reporting). Nothing proves that one correction shows the same everywhere: shipment detail, recorded history, risk score, summary report, CSV and PDF. | `tests/test_upload_workflow.py`, `test_line_item_corrections.py`, `test_risk_analytics.py`, `test_reporting.py` |
| G1.2 | **The live-server smoke test stops early.** It ends at extraction, the assistant and the shipment PDF: no correction, approval, re-audit, TMS, risk or reporting. | `scripts/smoke_test.py` |
| G1.3 | **No test runs the real browser against the real API.** Frontend tests render components with mocked responses, and the TypeScript types are written by hand beside the OpenAPI contract, so the two can drift. | `frontend/src/lib/types.ts` |
| G1.4 | **Backup and restore are not shown to reproduce the same results.** Nothing proves a restored copy gives identical audit digests, risk scores and report totals. | — |
| G1.5 | **Real-document accuracy is not measured.** There are no permitted real documents. | `docs/real_world_evaluation/PROTOCOL.md` |

### Workstream 2: security and authorization

| # | Gap | Evidence |
|---|---|---|
| G2.1 | **The signed-out check covers 21 fixed routes, not every one.** Routes added since Sprint 1 have their own checks, but nothing fails when a new route lacks a permission or the CSRF guard, and there is no role matrix for the 126 operations. | `tests/test_api.py:130` |
| G2.2 | **Reads write.** Every read that lists exceptions (Exception Center, Carrier Intelligence, risk, every report) synchronises the review store in a write transaction. That is a write on GET and lock contention under load. | `src/review_store.py:703` (`sync`), `src/api/routes_audit.py` `carrier_inputs` |
| G2.3 | **No rate limit beyond login lockout.** PDF and CSV reports and re-audit previews are CPU-heavy, and neither the application nor the shipped proxy configurations limit request rates. | `src/auth.py:70`, `deploy/` |
| G2.4 | **The audit history is append-only, but nothing would detect a rewrite.** Database triggers block ordinary edits, but only correction decisions and review events are hash-chained, not runs, snapshots or inputs. Someone with write access to the file could rewrite history unnoticed. | `src/audit_history.py` |
| G2.5 | **No Python vulnerability scan.** CI runs `pip check` only; `npm audit` covers the frontend. | `.github/workflows/ci.yml:60` |
| G2.6 | **No inventory of sensitive data.** Nobody has listed which endpoint or export carries document text, personal data (user names, contact details) or amounts, and who can read each. The older exceptions CSV still quotes document passages in its evidence column; the new reports only reference them. | `src/audit_analytics.py` `exceptions_csv` |
| G2.7 | **No written threat model** for the API, webhooks, integrations, exports and backups. | — |

Already sound, and only reviewed in this sprint:

- sessions: 30-minute idle timeout, 8-hour lifetime, HttpOnly and SameSite=Strict cookies, Secure in production;
- login lockout and the CSRF header;
- webhooks: outbound address checks with the address pinned, HMAC signatures, idempotency keys;
- redacted logs, generic 500 answers, security headers and CSP;
- CSV formula-injection protection and plain-text PDFs.

### Workstream 3: performance

| # | Gap | Evidence |
|---|---|---|
| G3.1 | **No code splitting.** Every page and Recharts load up front. | `frontend/src/App.tsx` (static imports) |
| G3.2 | **Every request computes three cache keys.** They are a folder listing, the extraction store opened, and the integration store plus each record store opened. Risk and reporting then rebuild carrier facts and risk rows each time. | `src/audit_service.py` `snapshot`, `tms_token` |
| G3.3 | **A cold start re-reads every PDF.** Extractions are cached in memory only. One changed document makes the whole audit recompute. | `audit_service.add_freight_audits` |
| G3.4 | **SQLite defaults everywhere.** No WAL mode. Time-window queries scan `webhook_deliveries` and `sync_runs`, and history trends scan every observation. | Sprint 3A report, section 8 |
| G3.5 | **No performance budgets or API latency baseline.** | — |

### Workstream 4: production release readiness

| # | Gap | Evidence |
|---|---|---|
| G4.1 | **The real-data upgrades have never been rehearsed on the current code.** The copies from 2026-11-10 predate history version 6. Upgrades run on open with no guard against starting new code on live data before a backup. | section 0 |
| G4.2 | **No backup of the real data exists, and none was ever restored.** | `config.BACKUP_DIR` (absent) |
| G4.3 | **Staging was never run.** The procedure is written; there is no server, domain, certificate or penetration test. | `docs/PRODUCTION_DEPLOYMENT.md:204`, `:257` |
| G4.4 | **Operational knowledge is spread over 26 documents.** No release runbook, no configuration reference beyond `.env.example`, and no playbooks for the Sprint 2–3B features (re-audit backlog, TMS differences, a broken risk model, a failed verification). | `docs/` |
| G4.5 | **No release candidate.** No version, changelog or tag, and no rollback beyond restoring a backup (upgrades are forward-only). | — |

## 2. Execution plan, prioritized

- **Priority:** P0 = must be done for the release; P1 = should be done in Sprint 4; P2 = defer unless time allows.
- **Effort:** focused engineering days, plus the machine time this computer needs.

### Phase 0: baselines and safety (0.5–1 day)

**T4.1 (P0): back up the real data first.** *Needs your approval* (D1).
- **How:** `manage_backups.py create` into a folder on D: (the data is small, well under 1 GB), then
  `verify`, then `drill` into a temporary folder. Nothing in `data/` is written; the server is not running.
- **Acceptance:** the backup is `complete`, verification passes, and the drill says `restored`. The real data's 34
  hashes are unchanged.

**T3.0 (P0): baselines before anything is optimized.**
- **How:** extend `scripts/benchmark_scale.py` with API timings (risk overview and table, report previews and
  CSV/PDF, re-audit preview, shipment detail) at 100 and 1,000 synthetic shipments, and record the bundle sizes.
- **Acceptance:** a baseline table in `docs/PERFORMANCE_BASELINE.md`, repeatable with one command.

### Phase 1: protect data and access (3–4 days)

**T4.2 (P0): rehearse the upgrades on copies.**
- **How:** restore the backup from T4.1 into a temporary folder. Record the audit's digests, risk and report
  totals with the code the data was last used with, then open the copy with the release candidate. Check:
  - schema versions, row counts and append-only triggers;
  - that no recorded snapshot was rewritten;
  - that audit digests and figures are identical before and after.
- **Acceptance:** `docs/MIGRATION_REHEARSAL.md` with every check passed. Rollback is written down: restore the
  T4.1 backup.

**T4.7 (P1): a migration guard.**
- **How:** in the production profile, refuse to upgrade a store unless a verified backup exists that is younger than
  a configured age, or unless the operator gives an explicit override.
- **Acceptance:** tested. Starting new code on live data without a backup stops with a clear message.

**T2.1 (P0): a route permission matrix.**
- **How:** a test walks every FastAPI route and checks:
  - signed out → 401, except an explicit public list (health, ready, sign-in);
  - a reviewer calling an administrator-only route → 403;
  - every write method requires the CSRF header;
  - `/api/v1` is the same.

  The matrix is built from the route's permission dependency, so a new route without one fails CI.
- **Acceptance:** 100% of operations covered, and a published table in `docs/API_PERMISSIONS.md`.

**T2.2 (P0): no writes on GET.**
- **How:** synchronise the review store only when the detected exceptions change (keyed by the audit's state), not
  on each read.
- **Acceptance:** a test shows a GET of the exceptions, risk, reports and carriers leaves the review database
  unchanged (SQLite `total_changes`). Review states of newly detected exceptions still appear (tested).

**T2.6 (P0): a sensitive-data inventory.**
- **How:** a table of every endpoint and export carrying document text, personal data or money, and who may read
  it.
- **Fix:** stop quoting document passages in the exceptions CSV, as the new reports already do. *Behaviour change,
  your decision* (D9).
- **Acceptance:** tests that errors and exports name no path, secret or token.

### Phase 2: the workflow end to end (4–5 days)

**T1.1 (P0): a synthetic scenario of about 12 shipments** that between them trigger every risk signal and every
report total:
- clean; quantity shortage; late delivery; invoice arithmetic mismatch; fuel above the contract;
- a duplicate invoice number; a missing POD; an unreadable scan; an EUR invoice;
- a TMS-linked shipment with a difference; carrier name variants.

Generated deterministically, synthetic only.

**T1.2 (P0): one end-to-end test through the real API**, with fake embeddings and no network:

1. upload and ingestion, then the audit and extraction;
2. exception actions;
3. a reviewer's line-item correction, a second administrator's approval, and the re-audit preview and confirmation;
4. a TMS sync and a decision;
5. risk overview and explanation, then every report as preview, CSV and PDF;
6. a backup, a restore drill and the restored copy reopened.

The test checks the same facts at every stage against a golden file, and that the restored copy gives identical
digests, scores and totals (G1.4).

**Acceptance:** green on Python 3.10 and 3.12 in CI, adding under 3 minutes.

**T1.3 (P1): extend the smoke test** (`--full`): the same stages against a live server on temporary folders, with
Ollama optional. **Acceptance:** exit 0 on this machine; used in the staging rehearsal (T4.3).

**T1.4 (P2): keep the frontend types honest.** A test checks the frontend's API fixtures against the published
OpenAPI schemas, so drift fails a test without generating code or adding a dependency.

**T1.5 (P2, deferred): real-browser tests** (Playwright). They need about 400 MB of browsers, which this disk
cannot spare; your decision (D5).

### Phase 3: performance (3–4 days)

**T3.1 (P0): code splitting.**
- **How:** lazy-load each route, put Recharts in its own chunk, and add a size budget checked in CI by a small
  script, with no new dependency.
- **Acceptance:** first-load JavaScript at most 220 kB gzip (from 327.5) and no chunk above 500 kB; every test
  still passes.

**T3.2 and T3.3 (P1): cheaper requests.**
- **How:** memoise the cache keys on file size and modification time, and skip the TMS key when no integration store
  exists. Cache exceptions-with-reviews, carrier facts and risk rows per audit state and review-store version.
- **Acceptance:** at 1,000 shipments, warm p95 latency on this machine is at most:

  | Endpoint | Target |
  |---|---|
  | overview and shipments | 300 ms |
  | risk overview | 500 ms |
  | report preview | 1 s |
  | CSV | 2 s |
  | PDF | 5 s |

  Every invalidation is tested (a change is always seen), and the evaluation is identical.

**T3.4 (P1): reuse stored extractions on a cold start.**
- **How:** extractions stored in the extraction store, keyed by content hash and extractor version, are reused
  instead of reading the PDF again.
- **Acceptance:** a cold audit of 1,000 synthetic shipments is at least 3× faster when extractions are stored, with
  identical results.

**T3.7 (P1): indexes.** Integration store version 4 adds `webhook_deliveries(created_at)` and
`sync_runs(started_at)`; the history trend query is checked with EXPLAIN. **Acceptance:** the query plans use the
indexes, and the migration test is extended.

**T3.5 (P2): recompute only the shipments whose documents changed.** Defer: it is a larger redesign.

**T3.6 (P2): WAL mode.** Evaluate first: the backup copies through SQLite's online backup, which works under WAL;
Windows locking needs a test. Your decision (D6).

### Phase 4: release readiness (3–4 days)

**T4.3 (P1): a staging rehearsal on this machine.**
- **How:** the release candidate under a production-like profile, against the restored copy from T4.2. Run
  `serve.py --check`, the full smoke test, a manual walkthrough as administrator and as reviewer, a backup, and a
  watch on metrics and alerts.
- **Acceptance:** a checklist with results in `docs/STAGING_REHEARSAL.md`. A real server, domain, certificate and
  penetration test remain outside this machine (G4.3).

**T4.4 (P1): operational documentation.**
- `RELEASE_RUNBOOK.md`: pre-flight, backup, deploy, upgrade, verify, roll back.
- `CONFIGURATION_REFERENCE.md`: every `LOGIDOC_*` setting with its default, validation and profile rule, kept
  complete by a test.
- Playbooks: re-audit backlog, TMS differences, a broken risk model, a failed backup verification, a failed
  upgrade.
- The deployment checklist updated for Sprints 2–3B.

**Acceptance:** reviewed by you; the completeness test is green.

**T4.5 (P1): a release candidate.** A changelog for Sprints 1–4, the application version in `/api/health`, and a
proposed tag (no merge into `main`). **Acceptance:** the changelog matches the commit history.

**T4.6 (P1): CI gates.** Fail CI when:
- the OpenAPI contract removes anything compared with the base branch;
- the bundle exceeds its budget;
- `pip-audit` finds a known vulnerability (if approved, D4).

**Acceptance:** each gate fails on a deliberate violation (tested once).

**Other security tasks (P1):**
- **T2.3:** rate limits on the expensive endpoints (reports, re-audit preview, risk) per user, and an nginx
  `limit_req` example (D3).
- **T2.5:** `pip-audit` (D4).
- **T2.7:** a threat model.

**T2.4 (P2): a hash chain over the audit history.** History version 7, a verify endpoint, and the check included in
backup verification. It adds a schema step on the real data: your decision (D2).

**Final validation (last day):**
- one full Python 3.12 suite, after a restart for disk;
- Python 3.10 on the affected files;
- the frontend typecheck, tests and build;
- OpenAPI and the evaluations;
- the real-data hashes;
- the attribution and secrets checks;
- the Sprint 4 report.

### Effort

| Phase | Effort |
|---|---|
| 0, baselines and safety | 0.5–1 day |
| 1, data and access | 3–4 days |
| 2, end to end | 4–5 days |
| 3, performance | 3–4 days |
| 4, release readiness and final validation | 3–4 days |
| **P0 only** | **about 8–10 days** |
| **P0 and P1** | **about 14–18 days** |

Machine time adds about 30 minutes per full suite plus restarts for disk. Full suites are planned only at the
end of each phase.

## 3. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| New code started on the live data folder upgrades it before a backup exists | medium | high (forward-only) | T4.1 before anything else; T4.7 guard; every rehearsal on copies only |
| Disk runs out during full suites or the staging rehearsal | high | medium | suites only at phase ends; restart before final validation; staging copy on D: (data is small); no Playwright |
| A new cache serves stale results | medium | high | each cache keyed on state, with an invalidation test per key; evaluations and full suites must stay identical |
| Moving the review sync out of reads delays the review state of a new exception | low | medium | sync on audit-state change, with a test |
| Lazy routes break existing frontend tests or the deep links | medium | low | Suspense fallbacks; route tests; the build checked |
| Flaky frontend tests under load (two seen in Batch 2) | medium | low | longer waits only where justified; consider fewer parallel workers locally |
| Rate limits block legitimate use | low | medium | per-user limits only on expensive endpoints, configurable, 429 with Retry-After |
| No real server, domain or penetration test is available | certain | medium | staging rehearsal on this machine; the rest stays a documented release prerequisite |

## 4. Decisions for you before starting

| # | Decision | Recommendation |
|---|---|---|
| D1 | Make the first backup of the real data (read-only for `data/`; writes a new folder on D:) | **yes**: everything in phase 1 depends on it |
| D2 | Hash-chain the audit history (schema version 7 on the real data) | after the rehearsal, in Sprint 5 |
| D3 | Rate limiting in the application, or only at the proxy | both: small limits in the application for the expensive endpoints, and the proxy example |
| D4 | `pip-audit` in CI (a development tool, not a runtime dependency) | yes |
| D5 | Real-browser end-to-end tests (Playwright) | defer: disk |
| D6 | WAL mode for the SQLite stores | evaluate in Sprint 4, switch in Sprint 5 if the tests agree |
| D7 | TMS decisions by two people (open since Batch 1) | your policy call |
| D8 | Is a real staging server available? | if not, the local rehearsal (T4.3) stands in |
| D9 | Stop quoting document passages in the exceptions CSV | yes, aligned with the new reports |

## 5. Out of scope for Sprint 4

- merging into `main`;
- a real production deployment;
- certificates and an external penetration test;
- n8n;
- real-document accuracy (needs permitted documents);
- the separate deepfake project.

## 6. Implementation status

Updated 2026-11-15, after phases 0 and 1.

**Decisions taken:**

| Decision | Outcome |
|---|---|
| D1, first backup of the real data | approved, done |
| D3, rate limits | in the application and in the nginx example |
| D4, `pip-audit` in CI | approved, with a written triage policy |
| D7, TMS decisions | kept: one administrator, with a mandatory reason and the audit history |
| D9, the exceptions CSV | stops quoting document passages |
| D2, D5, D6 | deferred: tamper-evident history, real-browser tests, WAL mode |

| Task | Status | Where |
|---|---|---|
| T4.1 backup of the real data | **done**: created, verified (23 of 23 files), restored and checked independently; the live data unchanged | [SPRINT_4_PHASE_0_1_REPORT.md](SPRINT_4_PHASE_0_1_REPORT.md) |
| T3.0 baselines | **done**, on synthetic data; the API timings added to `scripts/benchmark_scale.py` | [PERFORMANCE_BASELINE.md](PERFORMANCE_BASELINE.md) |
| T4.2 upgrade rehearsal | **done** on copies of the restored backup (reviews 2 → 3, history 3 → 6), Python 3.12 and 3.10; repeatable with `scripts/rehearse_migration.py` | [MIGRATION_REHEARSAL.md](MIGRATION_REHEARSAL.md) |
| T2.1 permission matrix | **done**: 126 operations, each called without a session, as a reviewer and as an administrator, under `/api` and `/api/v1`; CSRF on every write | [API_PERMISSIONS.md](API_PERMISSIONS.md), `tests/test_api_authorization.py` |
| T2.2 no writes on GET | **done**, differently from the plan: reads use a read-only in-memory view of the review store; detections are recorded by changes and the server's background loop | `src/review_store.py` `ReviewView`, `tests/test_read_only_requests.py` |
| T2.3 rate limits | **done**: six rules (failed sign-ins per client; assistant, documents, jobs, exports, accounts per session), 429 with Retry-After; forwarded addresses trusted from the proxy only; nginx `limit_req` | [RATE_LIMITING.md](RATE_LIMITING.md) |
| T2.5 `pip-audit` | **done**: a CI job, also weekly; no known vulnerability today | [DEPENDENCY_SECURITY.md](DEPENDENCY_SECURITY.md) |
| T2.6 the exceptions CSV | **done**: references only, never a passage; tests for formula injection and sensitive text | `audit_analytics.evidence_reference` |
| T2.6 sensitive-data inventory (the table) | not started: carried to phase 4 with T2.7 | — |
| T4.7 migration guard | not started (not in the phase 0–1 brief): carried to phase 4, before any production upgrade | — |

**Plan changes:**

- **T2.3 leaves ordinary reads unlimited.** The re-audit preview and the risk screens take 17–65 ms
  ([PERFORMANCE_BASELINE.md](PERFORMANCE_BASELINE.md)), so limiting them would only risk normal work. The limits
  cover what is heavy or sensitive: downloads, jobs, the assistant, document changes, accounts, failed sign-ins.
- **T2.2 never writes on a read, not even when the audit changes.** Synchronising on an audit-state change, as
  planned, would still have written on a read.
