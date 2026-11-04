# Post-migration verification: freight audit operations

Date: 2026-11-04. Branch `feature/production-readiness`, based on `076fe15`. Machine: Windows 11, 16 GB RAM,
Python 3.12.7 (`venv312`) and 3.10.11 (`venv`), Node 22.

**Verdict: SAFE TO COMMIT.** Two defects found during verification were fixed and re-tested (section 7).

## 1. Database migrations

The real databases in `data/` were only **read**, through SQLite's read-only mode. Their SHA-256 values
before and after the whole verification are identical:

| Database | Version now | Rows | Integrity | SHA-256 (first 16) |
|---|---|---|---|---|
| `audit_reviews.sqlite3` | 2 (upgrades to 3 on next start) | 10 exceptions, 10 events | ok | `6792759c6d4d060c` |
| `audit_history.sqlite3` | 3 (upgrades to 4 on next start) | 1 run, 4 snapshots, 20 inputs | ok | `b5d4027c42cabdea` |

Both migrations were run on **copies** in a temporary folder. 31 of 31 checks passed:

**Review database, version 2 to 3** (`exception_reviews` rebuilt so its CHECK admits `disputed` and `waived`):

- **Rows:** every `exception_reviews` row is identical, column by column (10 rows).
- **History:** `review_events` is untouched (10 events), and its hash chain still verifies (`verify_integrity`: ok,
  10 checked).
- **Schema:** the index `review_events_by_key` and both append-only triggers are unchanged.
  `PRAGMA foreign_key_check` finds nothing, and `integrity_check` is ok.
- **Backup:**
  - `audit_reviews.before-v3.sqlite3` is written beside the database before the rebuild, only when it holds
    reviews.
  - The copy is made with SQLite's backup API, so it's consistent even while the database is open.
  - The backup is version 2, its rows equal the original's, and its integrity check is ok.
- **Transactional:** a step made to fail inside the rebuild raises an error and leaves version 2, every row and
  the old table, with no half-built `exception_reviews_v3`. The next start then migrates cleanly.
- **Idempotent:** opening the database again changes nothing and takes no second backup. The version is re-read
  inside the transaction (`BEGIN IMMEDIATE`), so two processes starting at once cannot apply a step twice.
- **Recovery:** copying the backup back over the database gives the version 2 database, which migrates again to
  the same rows. That is the manual rollback.

**Audit history, version 3 to 4** (one new table):

- **Rows:** every data table is identical, and only `schema_info` changes (3 to 4).
- **Schema:** every existing table, index and trigger is unchanged. `freight_audit_records` and its two
  append-only triggers are added, and `integrity_check` is ok.
- **Earlier snapshots:** they read back with `freight_audit` None ("not recorded"); nothing is reconstructed.
- **Idempotent:** re-opening changes nothing.

No backup is written for the history upgrade: it only adds a table and rewrites no row.

## 2. Authorization (checked at the API)

`tests/test_operations_authorization.py`, 15 tests, all pass:

- **Signed out:** a signed-out user gets 401 for dispute, waive, resolve and approve, and nothing changes.
- **Waiving:** a reviewer cannot waive, even by calling the API directly (403), whether the exception is open or
  disputed. The screen doesn't offer the action either. A reviewer cannot dismiss (403).
- **Second-administrator approval** (`LOGIDOC_REQUIRE_APPROVAL`):
  - A waiver waits for approval.
  - The administrator who waived it gets 422, and a reviewer gets 403.
  - A second administrator can approve it. A rejection sends it back to *In review*.
  - The history records who did what.
- **Corrections:**
  - A reviewer cannot approve a correction (403).
  - An administrator cannot approve their own proposal: 403, "another administrator must".
  - An unapproved correction never reaches the freight audit.
- **Audit history:** a reviewer cannot record a run (403), and no history file is created.
- **Write routes:** the only route that writes history is `POST /api/history/runs`. No route updates or deletes a
  recorded run, snapshot, freight audit or decision.
- **Append-only:** `shipment_snapshots`, `snapshot_findings`, `freight_audit_records`, `audit_runs` and
  `review_events` refuse UPDATE and DELETE (triggers).
- **Invalid transitions** are refused (422) and change nothing:
  - resolve before review
  - dispute, waive, dismiss or start on a waived exception
  - an empty dispute or waiver reason

## 3. Audit history

`tests/test_freight_operations.py` and the end-to-end run both show:

- **Unchanged re-run:** an unchanged repeated run writes no new snapshot or freight audit.
- **New correction:** an approved correction of a field the freight audit reads records a recalculated run
  (`recompute: recorded`).
- **Older audits:** the earlier audit reopens exactly as recorded (fuel DISCREPANCY, no corrections), and the
  later one shows MATCHED with the correction.
- **Fingerprints:** every document's 64-character SHA-256 and extractor version are retained.
- **Tolerances** are retained.
- **Evidence:** the recorded charges are identical to the live audit's, including their evidence and sources.
- **Stored extractions:** the original extraction keeps the extracted value; the correction is applied on a copy.

## 4. Full test results (final working tree)

| Check | Result |
|---|---|
| Python suite on **3.12.7** | **1,879 passed, 0 failed, 0 skipped** (10 min 3 s). An earlier 3.12 run, before the fixes in section 7, had 1 failure |
| Frontend type check (`tsc --noEmit`) | clean |
| Frontend tests (`vitest run`) | **8 files, 221 passed, 0 failed** |
| Production build (`vite build`) | succeeds. Main chunk 971.24 kB (gzip 283.04 kB), against 965.64 kB before this milestone. Vite's 900 kB advisory is unchanged |
| `pip check` | clean on 3.12 and on 3.10 |
| `git diff --check` | clean |
| Deterministic evaluation | **PASS.** On Python 3.10 it is **identical to the baseline**, line for line. On 3.12 one figure differs: precision@5 is 43.8% instead of 43.7%, a Python-version difference (identical on 3.10 with the same code). Recall@5, hit@1, citations, abstention and leakage are identical |

**Memory.** Before the page-file change, two attempts failed because Windows ran out of commit memory:

- The suite crashed with "Windows fatal exception: stack overflow".
- The test server died at start.
- Windows logged low-virtual-memory events at 20:27, 20:32, 20:40 and 21:02, and a 0xc000012d popup.

After the change (C: 4,096 to 6,144 MB) every job ran, one at a time, with temporary files on D:. Commit memory
stayed at 2.25 GB or more.

## 5. End-to-end (real server process)

`serve.py` was started on port 8765, with every database, the index and the document folder in a fresh
temporary folder, three users (two administrators and one reviewer), real Ollama embeddings, and **synthetic**
documents. 38 of 38 checks passed, in this order:

1. **Upload:** 4 PDFs indexed, and the rate confirmation is classified as one. Before any correction, fuel was
   180 contracted and 200 billed: DISCREPANCY. A run was recorded.
2. **Extract:** the rate confirmation's fuel surcharge was read as 180.00.
3. **Propose:** the reviewer proposed 200.00 with a reason, and could not approve it (403).
4. **Approve:** a different administrator approved it, and a recalculated run was recorded.
5. **Freight audit:** fuel is now 200 contracted, marked corrected, and MATCHED.
6. **Corrected values:** the correction is listed with 180 then 200, proposed by bob and approved by alice. The
   stored extraction still says 180.
7. **History:** two snapshots. The earlier one is unchanged, and fingerprints, tolerances and evidence are
   retained. An unchanged re-run wrote nothing.
8. **Exception:** detention billed with no contract amount, with financial impact 75.00, a recommended action and
   evidence. The corrected fuel exception is no longer detected.
9. **Dispute:** the reviewer disputed it, with a reason and evidence.
10. **Waive:** the reviewer was refused (403); the second administrator waived it.
11. **Decision history:** detected (system), start (bob), dispute (bob), waive (dave). The hash chain verifies.
12. **Exception Center:** the state filter, invoice-number search and severity filter work, and the web interface
    is served.

## 6. Streamlit parity

- **Displays and filters:** disputed and waived exceptions, with their labels (`exception_ui`, `app.py`).
- **Required mode** (signed-in workflow): Resolve on a disputed exception and Reopen on a disputed or waived one
  work. **Dispute and Waive are not available in Streamlit**: there is no form for them, so they have to be done
  in the web application.
- **Local mode** (quick review form): the form can set any state. A dispute or a waiver now needs a reason; see
  section 7. As with its existing dismissal, this form attributes changes to a free-typed, unauthenticated name,
  so the administrator-only waiver rule is not enforced there. Local mode is a single user with no sign-in.
- **Charts and filters:** no Streamlit chart or filter hardcodes the old four states.

## 7. Defects found and fixed during verification

1. **A test whose premise the milestone changed.**
   `test_a_field_no_calculation_consumes_has_no_effect_and_says_so` used the BOL *weight*, which the freight audit
   now reads.
   - The test now uses the BOL *equipment*, a field no calculation reads, so its check is unchanged.
   - A new test (`test_a_field_only_the_freight_audit_reads_is_recalculated_and_leaves_the_reconciliation_alone`)
     covers the weight's new behaviour.
   - Coverage was kept, and extended.
2. **Streamlit quick form.** `ReviewStore.set_status`, the legacy path used in local mode, did not ask for a
   reason before *disputed* or *waived*. The database CHECK then raised a raw `sqlite3.IntegrityError` instead of a
   message.
   - It now raises `ReviewError` ("A dispute needs a reason" or "A waiver needs a reason"), which the form shows.
   - The form's label says so.
   - There is a regression test in `tests/test_freight_operations.py`.

## 8. Remaining limitations

- **Invoice line items** cannot be corrected (only extracted fields can), so a misread charge line cannot be
  fixed through the correction workflow.
- **Dispute and Waive** are not in Streamlit, and the local-mode quick form does not enforce the
  administrator-only waiver (section 6).
- **The review database rebuild** runs automatically on the next server start, on the real `data/` file. It is
  backed up first and verified on a copy (section 1). Keeping your own copy before the first start is still
  sensible.
- **Disk space:** drive C: has 0.8 GB free after the larger page file reserved its space.
- **Bundle size:** the frontend bundle stays above Vite's 900 kB advisory (no route splitting yet).
- **Real-document accuracy** is still not measured. Every result here comes from synthetic, project-authored
  documents.

## 9. Files safe to commit

Modified (24):

- `README.md`
- `app.py`
- `frontend/src/components/common/FreightAuditPanel.tsx`
- `frontend/src/components/common/StatusBadge.tsx`
- `frontend/src/components/common/Timeline.tsx`
- `frontend/src/components/extraction/fields.tsx`
- `frontend/src/hooks/queries.ts`
- `frontend/src/lib/chart.ts`
- `frontend/src/lib/types.ts`
- `frontend/src/pages/Exceptions.tsx`
- `frontend/src/test/dashboard.test.tsx`
- `frontend/src/test/extraction.test.tsx`
- `frontend/src/test/pages.test.tsx`
- `src/api/routes_audit.py`
- `src/api/routes_workflow.py`
- `src/audit_analytics.py`
- `src/audit_history.py`
- `src/audit_service.py`
- `src/authz.py`
- `src/exception_workflow.py`
- `src/freight_audit.py`
- `src/review_store.py`
- `tests/test_correction_aware_audit.py`
- `tests/test_exception_workflow.py`

New (3):

- `tests/test_freight_operations.py`
- `tests/test_operations_authorization.py`
- `docs/POST_MIGRATION_VERIFICATION.md`

Not included, and never part of the repository: `data/`, `chroma_db/`, and the temporary verification folders
under `D:\logidoc_tmp`.

**Verdict: SAFE TO COMMIT.**
