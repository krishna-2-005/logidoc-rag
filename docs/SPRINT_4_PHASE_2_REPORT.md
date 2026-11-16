# Sprint 4, phase 2 report

End-to-end validation, with the migration guard first.

Date: 2026-11-16. Branch `feature/production-readiness`, starting from `41a9b2b`. Plan:
[SPRINT_4_PLAN.md](SPRINT_4_PLAN.md).

## 0. Preflight

- **CI for `41a9b2b`:** the push run is green on all five jobs, including the new dependency vulnerability audit.
  The pull-request run failed one step, "No whitespace errors in the changes": phase 1 left a blank line at the
  end of `tests/test_api_authorization.py`. My local check had missed it because the file was untracked then.
  Fixed here (one line). New files are now checked for it too.
- **Branch:** local and remote at `41a9b2b`, with a clean working tree. `main` has since been fast-forwarded by the
  merge of pull request #23; nothing was merged by this work.
- **Disk:** C: 4.1 GB and D: 3.4 GB free at the start, after a restart.
- **The 2026-11-15 backup:** intact. Its 23 files match the manifest and the manifest its own digest, checked
  read-only (`manage_backups.py verify` was not used, as it appends to the verification history).
- **Real data:** the 34 files of `data/` and `chroma_db/` unchanged (`sha256sum -c`, exit 0).

## 1. Task 0, the migration guard

`src/migration_guard.py` (new). [MIGRATION_REHEARSAL.md](MIGRATION_REHEARSAL.md), section 6.

**At startup**, in the API (`create_app` for the installation's own data, so `serve.py` and uvicorn's factory)
and in the Streamlit app (once per process, before any page opens a store):

1. The review and history databases are registered as guarded, and their schema versions are read read-only.
2. If either is older than the code, the newest backups in `LOGIDOC_BACKUP_DIR` (at most 5) are examined. One
   qualifies only if:
   - it passes `backup.verify_backup`: manifest, every checksum, each database copy's integrity and recorded
     contents;
   - its copy of every database to upgrade holds exactly the rows of the live file, compared by a digest of
     every table. A backup made before the last change is stale.
3. **With one**, the upgrade runs there and then, before anything is served.
4. **Without one**, nothing is opened for writing:
   - the server exits with code 4;
   - the Streamlit app shows the error;
   - both say which databases need which upgrade, why no backup qualifies, and the steps: stop every process,
     `python scripts/manage_backups.py create`, optionally rehearse, start again;
   - no path is printed.

**In the stores:** `ReviewStore` and `HistoryStore` call `migration_guard.check` before opening an existing file for
writing. A guarded database older than the code, whose upgrade the start did not authorize, is refused, so no
request or background job ever upgrades it. Not covered by the check:

- a database that does not exist yet: it is created at the current version, which is not an upgrade;
- paths never registered (tests, rehearsal copies, restore drills): they behave as before.

Nothing in the guard makes a backup.

**`tests/test_migration_guard.py` (new, 13 tests)**, on isolated copies of a version 2 review database and a
version 3 history:

| Condition | Result |
|---|---|
| No backup | refused, with the steps; the files byte-identical, no `.before-v3` copy |
| Stale backup (a row changed after it) | refused: "does not hold the current exception review database"; versions unchanged |
| Corrupted backup (a database copy altered) | refused: "fails verification (checksum…)" |
| Incomplete backup (`.partial`) | refused |
| Valid backup | both upgraded once; rows kept; the backup unchanged; the next start needs nothing |
| Current or missing databases | no backup needed; new files created normally |
| Unreadable database | refused with a clear message |
| After a refused start | neither store upgrades; reads through `ReviewView` still work, nothing written |
| Unregistered paths | upgrade as before |
| API `create_app`, `serve.main`, the Streamlit app | refused (exit code 4 for the server), nothing upgraded; with a fresh backup, the API upgrades and serves |

**The real data, checked read-only, nothing upgraded:**

- **Pending:** reviews 2 → 3 and history 3 → 6.
- **The default backup folder:** empty, so a start would be refused.
- **With `LOGIDOC_BACKUP_DIR` at `D:\LogiDoc-backups`:** the 2026-11-15 backup still holds the current data, so a
  start would upgrade.

The live hashes are unchanged.

**The test session is now hermetic:** `tests/conftest.py` points `REVIEW_DB_PATH`, `HISTORY_DB_PATH` and
`BACKUP_DIR` at a session folder, so a test that does not choose its own never reads the real databases. It
first records the real paths for the "production data unchanged" check.

## 2. Task 1, one shipment from upload to report

`tests/test_end_to_end_workflow.py` (new) follows SHJ-2026-101 through the real API. Its PDFs are a bill of lading,
a proof of delivery, an invoice billing fuel at $200, and a rate confirmation contracting $180. The users are two
administrators (alice, dave) and a reviewer (bob). Embeddings are stand-ins; there is no model and no network.

| Step | Checked |
|---|---|
| 1. Upload | the reviewer is refused (403); the administrator's 4 PDFs indexed, typed BOL, POD, INVOICE, RATE_CONFIRMATION, all for SHJ-2026-101 |
| 2. Extract and classify | every document parsed and tied to the shipment; the invoice extracted, fuel line $200 |
| 3. Reconcile | reconciled, NEEDS REVIEW |
| 4. Freight audit | DISCREPANCY on the fuel surcharge; risk taken; recorded as the month-end run |
| 5. Correction | alice proposes fuel at $180 with a reason; still pending, the audit unchanged |
| 6. Self-approval | refused: alice's own correction (403); the reviewer (403) |
| 7. Approval | dave approves; no run is recorded by the approval ("re-audit required") |
| 8. Re-audit preview | needed, against the month-end run, adding the correction |
| 9. Re-audit | refused for the reviewer; alice confirms with a reason; the new run supersedes the first, and the fuel surcharge passes |
| 10. Immutability | the first run's snapshot is identical before and after, superseded by the second; UPDATE and DELETE on snapshots and freight audit records are refused by the database ("append-only") |
| 11. Exception lifecycle | detention shown as open; the reviewer may not dismiss (403); assign, start, note and resolve with evidence; history `detected, assign, start, note, resolve`; the hash chain verifies |
| 12. Risk | factors add up to the score, which is lower than before; model version and digest |
| 13. Report | the freight audit summary row: invoice total $1,675, risk score and category equal to the risk endpoint's |
| 14. CSV and PDF | the CSV holds the row; the PDF is plain (no JavaScript, link or embedded file) |
| 15. Provenance and permissions | provenance names the risk model, never a path. The corrections report lists exactly: proposed by alice, approved by dave, re-audit by alice. The reviewer may download (200) but not record (403); signed out gets 401 |

## 3. Task 2, backup and restore

In the same file (`test_a_restored_backup_reproduces_the_audit`), after the journey:

1. **Backup:** the whole environment is backed up (`backup.create_backup`) and verified.
2. **Restore:** the backup is restored into a new folder. A restore over the environment itself is refused, and
   the installation's data and index are passed as protected.
3. **A new application on the restored folder** reproduces the original exactly, signing in with the restored
   accounts:
   - the shipment's status, freight outcome and every charge (billed, contracted, outcome);
   - the corrections applied and the approved line items;
   - the recorded runs, timeline digests and supersession;
   - every review state, and every review event with its hash;
   - the risk score, category, factors, model version and digest;
   - the report rows and totals, and the CSV.

   Only volatile times are left out: audit times, report generation time, the CSV's "Generated" line.
4. **The journey's results survived:** the correction, both runs (the first superseded), the resolved exception,
   and a hash chain that verifies.

## 4. Task 3, Streamlit and exception consistency

**Before:** three Streamlit views wrote the review database just by showing exceptions:

- Audit Analytics;
- the Exception Queue;
- Audit Operations, through `audit_workflow_ui`.

All three called `ReviewStore.sync`, and opening the store created or upgraded the database. The Administration
check of the review history opened it too.

**Now:**

- **`review_store.ReviewSession` (new):** reads come from the read-only view, and the first saved review records
  the detected exceptions and then makes the change.
- **`FirstSeen`** keeps one creation time per exception across reruns.
- **The Administration check** reads the history read-only (`ReviewView`).
- **A database that cannot be created** (its folder missing or not writable) is still reported, checked without
  creating anything.
- **Legitimate changes are kept:** saving a review state, and every exception action in the queue.

**`tests/test_exception_consistency.py` (new, 4 tests):**

- the API without the background loop: a read creates no `exception.created` event; the first action creates one
  per detection plus its own status change;
- the API with the loop: one tick records every detection, with the creation time the reads showed; later ticks
  record nothing; an action then writes only itself;
- the Streamlit views on a missing database: nothing created, exceptions shown as open;
- the Streamlit views on a version 2 database: byte-identical and still version 2.

**Four existing Streamlit tests assumed a page view recorded exceptions.** They now record through the page (saving
a review), as a user does, and two of them check that the page views wrote nothing.

The remaining eventual consistency is written down in [EXCEPTION_RECORDING.md](EXCEPTION_RECORDING.md):
- until an exception is recorded, it has no `exception.created` webhook and no review history;
- with the API's background jobs, it is recorded within about 30 s;
- the Streamlit app, used alone, records exceptions when a review is saved.

## 5. Validation

Every Python run used `-p no:cacheprovider`, one at a time, with its temporary folder outside the project.

| Check | Result |
|---|---|
| New: `tests/test_migration_guard.py` (3.12) | 13 passed, exit 0 |
| New: `tests/test_end_to_end_workflow.py` (3.12) | 2 passed, exit 0; three runs in a row after the fix below, all passed |
| New: `tests/test_exception_consistency.py` (3.12) | 4 passed, exit 0 |
| Streamlit tests after the hermetic paths (3.12): export, answers, multi-shipment, unreadable documents, production UI, upload, analytics cache and dashboard, operations, dependencies | 107 passed, exit 0; after the read-only sessions, the four tests that assumed a page view records exceptions were updated: 22 passed, exit 0 |
| **Full suite (3.12)** | **2,473 passed, 1 failed** (12 min 20 s). The failure was the restore comparison. The CSV's "Source" line carries the time the audit was computed, which differed by a second between the original and the restored application; times in the CSV are now masked, and everything else is compared exactly. The file then passed three times in a row (exit 0). The full suite was not run again |
| Affected files (3.10): the three new files, read-only requests, review store, exception workflow, webhooks, analytics dashboard and cache, operations UI, authorization, rehearsal, production configuration, backup verification, line-item corrections | 300 passed, exit 0 |
| Deterministic evaluation guard (3.12 and 3.10) | `RESULT: PASS`, exit 0 both |
| OpenAPI | `--check` up to date: no route, schema or description changed |
| Permission inventory | `--check` up to date |
| Database integrity, audit immutability, permissions | in the end-to-end test: hash chain verified, history append-only, every permission step |
| Backup verification | the 2026-11-15 backup still matches its manifest (23 of 23, read-only); the test environment's backup verified and restored |
| Real data | the 34 files of `data/` and `chroma_db/` unchanged (`sha256sum -c`, exit 0); nothing upgraded |
| `git diff --check`, blank lines at end of file, attribution | clean |
| Frontend | not changed; not run |

## 6. Changed files

- **New:**
  - `src/migration_guard.py`
  - `tests/test_migration_guard.py`, `tests/test_end_to_end_workflow.py`, `tests/test_exception_consistency.py`
  - `docs/EXCEPTION_RECORDING.md`, this report
- **Changed, source:**
  - `src/review_store.py`: the guard check, `ReviewSession`, `FirstSeen`, `creatable`
  - `src/audit_history.py`: the guard check
  - `src/api/app.py`: the guard at startup
  - `src/api/serve.py`: exit code 4 with the steps
  - `app.py`: the guard at startup, read-only review sessions, a read-only history check
  - `src/audit_workflow_ui.py`: reads the states instead of synchronising
- **Changed, tests:**
  - `tests/conftest.py`: hermetic database paths
  - `tests/test_analytics_dashboard.py`, `tests/test_analytics_cache.py`: record through the page
  - `tests/test_webhooks.py`: a comment
  - `tests/test_api_authorization.py`: the blank line at the end
- **Changed, documentation:** `docs/MIGRATION_REHEARSAL.md` (section 6 and the upgrade timing),
  `docs/SPRINT_4_PLAN.md` (status), `README.md`

**Not committed and not deleted:** the backups, restored copies and rehearsal folders on D:, and the temporary
folders (`D:\ldt3a`, and the session's on C:).

**Not touched:** `data/`, `chroma_db/`, the PDFs, the evaluation files, `main`, and any other project.

## 7. Remaining limitations

- **The real databases are still at reviews 2 and history 3.** The guard allows their upgrade at the next start
  only with `LOGIDOC_BACKUP_DIR` pointing at a fresh, verified backup: today the 2026-11-15 backup in
  `D:\LogiDoc-backups`, as long as nothing changes the data first.
- **The guard covers the review and history databases only.** The carrier registry (v1 → v2) and the integration
  store also upgrade on open, but neither exists in the real data. The extraction store already has its own
  migration script and never upgrades on open.
- **A history read can still create an empty history database** when none exists (the Streamlit trends tab, and
  some API reads). That is a new file at the current version, not an upgrade.
- **The eventual consistency of detections**, documented in [EXCEPTION_RECORDING.md](EXCEPTION_RECORDING.md): until
  an exception is recorded (within about 30 s with the API's background jobs, or at the first change), it has no
  webhook and no review history.
- **The end-to-end journey runs one shipment through the API.** The Streamlit pages and a real browser are not
  part of it, and real-document accuracy is still unmeasured.

## 8. Disk space

- **Start:** C: 4.1 GB and D: 3.4 GB free (after a restart).
- **End:** C: 1.3 GB and D: 1.3 GB free. The Windows page files grew again during the full suite; a restart
  returns the space.

## 9. Commit and push

Two commits on `41a9b2b`, staged by exact paths, by the repository owner's account (`krishna-2-005`):

1. the guard, the read-only Streamlit views, the end-to-end and consistency tests;
2. the documentation with this report.

Nothing was merged into `main`, and phase 3 was not started.
