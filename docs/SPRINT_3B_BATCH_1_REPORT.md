# Sprint 3B, Batch 1 report

Invoice line-item corrections with two-person approval and a controlled re-audit, and TMS shipments in the
audit.

Date: 2026-11-14. Branch `feature/production-readiness`, starting from `53706fc`. Both CI runs on it (push and
pull request) had passed all four jobs. Before starting, C: had 10.8 GB free and D: 4.0 GB, after a restart.
Batch 2 was not started. Nothing was merged into `main`.

## 1. What was built

| Part | Backend | Screens | Documented |
|---|---|---|---|
| Line-item corrections, two-person approval | `src/line_item_corrections.py`, `src/extraction_store.py`, `src/freight_audit.py`, `src/audit_inputs.py` | invoice charges table and dialog (shipment, Extractions) | `docs/CORRECTION_AWARE_AUDIT.md` section 9 |
| Controlled re-audit | `src/audit_service.py`, `src/audit_history.py` | Re-audit section (shipment), Re-audits waiting (Correction approvals) | same, section 10 |
| TMS shipment-to-audit integration | `src/tms_audit.py`, `src/integration_store.py`, `src/audit_service.py` | TMS shipment record section (shipment), TMS differences (Correction approvals) | `docs/TMS_SHIPMENT_AUDIT.md` |

### Line-item corrections

- **A charge's amount** can be proposed by a reviewer and approved by a different, signed-in administrator, or
  rejected with a reason.
- **Same machinery as other corrections.** A line-item correction is stored as an ordinary correction: same
  tables, hash chain and rules, and no extraction store migration.
- **Pinned to its line.** It is applied only to the line it was made on, while that line still reads as it did.
- **Read by both audits.** The freight audit reads the corrected amount (classification, contract comparison,
  line total), and so does the reconciliation's invoice audit for the four charges it reads by label.
- **Recorded as inputs.** The four charges are now recorded audit inputs (calculation version 3).

### Controlled re-audit

- **No automatic run.** Approving a line-item correction, or choosing a TMS value, no longer records a run.
- **Preview.** It shows what recording would change against the latest recorded run: statuses, the freight
  outcome and financial impact, values, findings, and the corrections and decisions recorded.
- **Confirmation.** An administrator confirms with a reason. Exactly the previewed state is recorded, for that one
  shipment. A change in between (or a second administrator confirming the same preview) is refused inside the
  write transaction.
- **The plain recompute** leaves these shipments to the re-audit.

### TMS shipments in the audit

- **Matching.** An imported TMS shipment record is matched to the audited shipment its reference names.
- **Comparison.** Seven fields are compared, each with its provenance (integration, record, TMS change time,
  sync run).
- **An open difference changes no result.** An administrator decides it with a reason: keep the documents' value
  (any field) or use the TMS value (expected pieces, actual delivery date). A decision applies only while both
  values stay as they were, so a sync with another value reopens it.
- **Correction protection:**
  - An approved correction always wins: the field is `protected`.
  - A TMS value typed by hand in LogiDoc can never be chosen, so the two-person rule cannot be bypassed
    through the TMS.
  - Two records naming one shipment are not compared.

## 2. Decisions worth knowing

- **Correction code stays out of the extractor version.** The line-item correction code is in its own module.
  Any edit to `document_extraction.py` or `shipment_calculations.py` changes the extractor version, which would
  mark every stored extraction (real ones included) as outdated. Neither file was changed.
- **No extraction store migration.** A line-item correction is an ordinary correction row; its field name and
  value carry the line.
- **Two schema versions, both migrated on open with one transaction per step:**
  - **History version 6** adds one column (`run_inputs.tms`). Its migration skips a column that is already
    there, so a repeated step is harmless.
  - **Integration store version 3** adds the append-only `tms_resolutions` table.
- **Single-administrator TMS decisions.** Choosing a TMS value is one administrator's decision with a reason. It
  is a choice between two received values; values typed in LogiDoc are excluded. Whether it should also need a
  second person is a decision for you.

## 3. Tests

Each new rule has a test that fails without it. Most were run before the code they cover. New tests:

| File | Tests | Covers |
|---|---|---|
| `tests/test_line_item_corrections.py` | 15 | proposal, the two-person rule, both audits, rejection, invalid and stale proposals, pinning to a line, the extractor version, the re-audit preview and confirmation, concurrent confirmation, the in-transaction check, append-only rows |
| `tests/test_tms_audit.py` | 14 | matching and provenance, choosing and keeping values, stale and duplicate decisions, cross-checks, reopening after a sync, protection by a correction, hand-typed values, two records, disabled integrations, no store, append-only and version 3 |
| `frontend/src/test/audit_review.test.tsx` | 11 | the charges table and dialog, the approval outcome, the re-audit panel (preview, confirmation, reload after a change, reviewer), the TMS panel (comparison, decision, reviewer, several records, not configured) |

Existing tests changed only where they pinned a schema version (`test_tms_integration.py`: integration store 3;
`test_scale_safeguards.py`: history 6).

### Bug found by the full suite

The audit's cache key for TMS records used their change time, which has whole seconds. A sync or a correction
by hand in the same second as the previous change left the key unchanged, so a stale comparison was served. Two
new tests failed in the full run (they passed alone). The key now follows the record versions, which are
append-only and get a new row with every change. A test makes a correction stamped with the record's exact
previous time: it fails with the old key and passes with the new one.

### Validation

Every Python run used `-p no:cacheprovider` with its temporary folder on D:, one at a time.

| Check | Result |
|---|---|
| Affected suites (3.12): correction-aware audit, freight operations and audit, API contracts, history, scale | 179 passed, 1 failed: an exact comparison of the recompute answer. The answer now names `re_audit_required` only when there are some, and the file passed in the next row |
| Extraction store, end to end, fixes, states, migration, TMS integration (3.12) | 247 passed, exit 0 |
| Backups, authorization, webhooks, feed, assistant, API, migration status (3.12) | 318 passed, exit 0 |
| Full suite (3.12) | 2,365 passed, 2 failed: the cache bug above, fixed and then re-run (14 passed, three times) |
| New and affected files (3.10) | 111 passed, exit 0 |
| Frontend typecheck, tests, build | exit 0; 12 files, 267 tests; main bundle 1,145.68 kB (+28.2 kB) |
| OpenAPI | `--check` up to date. Against `53706fc`: 0 removals, 32 additions (6 paths with 7 operations, `InputLineItem.corrected`, 25 schemas) |
| Evaluation (`scripts/evaluate.py`, 3.10) | identical to the recorded baseline apart from timings |
| CI evaluation guard (3.10 and 3.12) | `RESULT: PASS`, exit 0 |
| Real data | the 34 files of `data/` and `chroma_db/` unchanged (`sha256sum -c`), none added |
| Secrets and attribution | nothing found. The only matches are the synthetic test shipment IDs and file names already used by the tests |

## 4. Changed files

- **Backend, new:** `src/line_item_corrections.py`, `src/tms_audit.py`, `src/api/routes_audit_review.py`
- **Backend, changed:**
  - `src/extraction_store.py`: `propose_line_item`, `line_items`
  - `src/freight_audit.py`: line-item corrections applied, `consumes`
  - `src/audit_inputs.py`: charges as inputs, TMS as a source, version 3
  - `src/audit_history.py`: version 6, `latest_recorded`, `expect_latest`, idempotent added columns
  - `src/audit_service.py`: TMS in the audit, the controlled re-audit, staleness and recompute
  - `src/integration_store.py`: version 3
  - `src/api/app.py`, `src/api/core.py`, `src/api/routes_extraction.py`, `src/api/schemas.py`
  - `docs/api/openapi-v1.json`: regenerated
- **Tests, new:** `tests/test_line_item_corrections.py`, `tests/test_tms_audit.py`
- **Tests, changed (pinned schema versions):** `tests/test_tms_integration.py`, `tests/test_scale_safeguards.py`
- **Frontend, new:**
  - `frontend/src/components/extraction/LineItems.tsx`
  - `frontend/src/components/shipment/ReAuditPanel.tsx`
  - `frontend/src/components/shipment/TmsPanel.tsx`
  - `frontend/src/test/audit_review.test.tsx`
- **Frontend, changed:**
  - `frontend/src/components/extraction/ExtractionPanel.tsx`, `RecomputeStatus.tsx`, `fields.tsx`
  - `frontend/src/hooks/queries.ts`, `frontend/src/lib/types.ts`
  - `frontend/src/pages/Corrections.tsx`, `frontend/src/pages/ShipmentDetail.tsx`
- **Documentation:**
  - `docs/TMS_SHIPMENT_AUDIT.md` (new)
  - `docs/CORRECTION_AWARE_AUDIT.md`, `docs/TMS_ERP_INTEGRATION_FRAMEWORK.md`, `docs/OBSERVABILITY_AND_MONITORING.md`
  - this report

Not touched: `data/`, `chroma_db/`, the PDFs, the backups, `.env` files, `document_extraction.py` and
`shipment_calculations.py` (the extractor version is unchanged), and any other project.

## 5. Remaining risks

- **Sandbox only.** No real TMS was connected. Matching is by reference only: a TMS that keys shipments by
  another number matches nothing.
- **What can be chosen.** Only expected pieces and the actual delivery date can be chosen from the TMS. Carrier,
  route, pickup date and the agreed charge are cross-checks only.
- **Cost per request.** The audit reads every enabled integration's shipment records when the audit is
  recomputed, and checks them for changes on every audit request (one small query per integration).
- **Amount only.** A line-item correction corrects the amount only. A mis-read description (which decides the
  charge's classification) or a missed charge cannot be corrected yet.
- **No forced re-audit.** The re-audit is not forced: an approved charge correction is read by the live audit at
  once and waits in "Re-audits waiting" until an administrator records it.

## 6. Commit, push and CI

Three commits on `53706fc`, staged by exact paths, by `krishna-2-005 <kuchurusaikrishnareddy@gmail.com>`:
the backend with its API, contract and tests, the screens with their tests, and the documentation with this
report. Their hashes and the CI result are given with the push.
