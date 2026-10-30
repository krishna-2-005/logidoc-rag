# Implementation sprint report

Date: 2026-10-30. Branch `feature/production-readiness`, based on `62e0931`. **Nothing is committed
or pushed**; every change below is in the working tree for review.

## Starting point (verified, not assumed)

- **Git:** the local branch and `origin/feature/production-readiness` were both at `62e0931` (0 ahead,
  0 behind, clean tree). `origin/main` is `fc63d00` (PR #12, merged 2026-10-29 19:15 +0530) and holds
  the same files.
- **GitHub Actions** (public API, since `gh` is not installed): the last 6 runs passed, including
  `main` at `fc63d00`. Each ran four jobs: Python 3.10 tests, Python 3.12 tests, Frontend
  typecheck/tests/build, and Whitespace and authorship checks.
- **Pull requests and issues:** 0 open pull requests and 0 issues. The PR titles, descriptions and
  comments carry no AI attribution.
- **Baseline on that code:** 1,726 Python tests passed (run 2026-10-29), 211 frontend tests,
  `pip check` clean, deterministic evaluation PASS, and a single frontend chunk of 959.87 kB (gzip
  280.64 kB).
- **Constraint on this machine:** drive C: had 27-415 MB free and less than 2 GB of memory could be
  committed. Parallel jobs failed (the Vite build ran out of memory and Python could not start), so
  every heavy job ran alone, with temporary files on D:.

---

## Part 1: Freight audit engine (three-way match)

### Business requirement

A freight auditor pays a carrier invoice only when it agrees with what was contracted (the rate
confirmation) and what happened (the BOL and POD), charge by charge. Before this change the
application compared the invoice only with itself (do the line items add up to the total?) and the
BOL with the POD. The rate confirmation was extracted but never compared with anything.

### What was built

`src/freight_audit.py` is a pure function. It takes the typed extractions of **one** shipment's
documents (`document_extraction`) and returns a structured, explained result.

**Charge-level reconciliation.** Each invoice line item is classified as base freight, fuel surcharge
or an accessorial. Every comparison records the expected amount, billed amount, variance, currency,
source file and page of each side, the tolerance rule used, the outcome and the reason.

- **Base freight and fuel** are compared with the rate confirmation's agreed rate and fuel surcharge.
- **A rate confirmation with one rate and no separate fuel surcharge** is not guessed. It is read as
  all-in if base plus fuel equals it, or as line haul if the base freight equals it (the fuel then
  waits for review). Otherwise the line is PENDING_REVIEW and explains the ambiguity.
- **Accessorials** (detention, lumper, liftgate, and so on) the rate confirmation does not list are
  not assumed invalid. If the rate confirmation's text (contractual evidence) or the BOL/POD
  (execution evidence) mentions the charge, it is PENDING_REVIEW, with that passage, file and page.
  With no evidence it is INSUFFICIENT_EVIDENCE and the reviewer is told to request support. A generic
  label ("Other Accessorial Charges") cannot be evidenced and says so.
- **The invoice total** is compared with the contracted amount. A difference that is exactly the
  uncovered charges becomes PENDING_REVIEW, and one that is exactly the tolerated charge differences
  becomes MATCHED_WITH_TOLERANCE. The line items are also checked against the stated total.
- **Missing, unreadable or conflicting values** are never zero or a match. A missing agreed rate is
  INSUFFICIENT_EVIDENCE, conflicting rates are PENDING_REVIEW, invoice charges that cannot all be read
  are INSUFFICIENT_EVIDENCE, and several invoices or rate confirmations, or mixed currencies, are
  PENDING_REVIEW.

**The rest of the three-way match** covers the shipment reference (shipment-ID collisions), carrier,
origin and destination (rate confirmation, BOL/POD, invoice), pickup date (rate confirmation against
BOL), delivery date (rate confirmation against POD), weight (with its own tolerance; units are never
converted), currency and payment terms. An invoice number that appears on another invoice gives
DUPLICATE. A reference that only one document states is shown as INSUFFICIENT_EVIDENCE but does not
decide the outcome.

**Outcomes:** MATCHED, MATCHED_WITH_TOLERANCE, DISCREPANCY, INSUFFICIENT_EVIDENCE, DUPLICATE and
PENDING_REVIEW. The shipment's outcome is its worst deciding line, and `explanation` lists the lines
that decided it. `financial_impact` gives the amount billed above the contract, billed without
evidence, and waiting for a decision.

**Configurable tolerances** are set in one place: `config.FREIGHT_TOLERANCES`, from
`LOGIDOC_FREIGHT_TOLERANCES` (JSON). The rules are `default`, `base_freight`, `fuel_surcharge`,
`accessorial`, `invoice_total`, `invoice_arithmetic` and `weight`, each with `absolute` and/or
`percent`. The default is exact matching; no threshold is set anywhere else. Unknown rules or keys,
negative values and non-numbers are refused (`PolicyError`). A broken setting is reported on every
shipment, and the reconciliation still runs.

### Integration

- **Audit service** (`src/audit_service.py`, `add_freight_audits`): the audit snapshot runs the
  freight audit for every shipment from the same reading as the reconciliation, and it is cached with
  it. Each PDF is extracted once, read-only.
  - A failure is reported on the shipment and never breaks the audit.
  - A shipment whose documents print colliding IDs is not audited.
  - When approved extraction corrections exist, the result says they are not yet applied to the
    freight audit.
- **Exceptions** (`src/audit_analytics.py`, `add_freight_exceptions`): when a rate confirmation
  makes the three-way match possible, each actionable charge becomes an exception in the existing
  workflow. So does a duplicate invoice, and a contract carrier, route, currency or payment terms
  that the shipment documents disagree with.
  - Each exception has its outcome, category, severity, financial impact, evidence and a recommended
    action.
  - The key is `shipment|freight-<charge>`, so there is one unresolved exception per issue (a test
    re-audits and checks that no duplicates appear). The review lifecycle and the hash-chained
    history apply unchanged.
  - The arithmetic and total lines are not repeated, because the reconciliation's own exceptions
    cover them. Without a rate confirmation no freight exception is raised, so the sample data's
    exceptions are unchanged.
- **Explanations** (`src/audit_explanations.py`): a freight exception is explained with its contract
  and billed amounts, its rule and outcome, its sources (rate confirmation, invoice and evidence
  passages), the recommended action as the next step, and a note that nothing is approved or paid
  automatically.
- **API:** `GET /api/shipments/{id}` now returns `freight_audit`. No other response changed.
- **Frontend:** a "Freight audit: invoice against contract" section on the shipment page shows the
  outcome and why, the contract, execution and invoice documents, the amounts at stake, a charge
  table (contract, billed, variance, outcome, why, evidence quotes, source file and page) and the
  reference checks. The six outcomes have their own badges in `StatusBadge`. There was no redesign.

### A real defect found by running it on PDFs

Running the audit on generated PDFs showed that **a rate confirmation was classified as an
invoice**. Its freight, fuel and payment-terms lines outscore the single "rate confirmation" keyword.
Every rate confirmation would therefore have counted as a second invoice. `document_classifier.py`
now lets the type a document names in its heading decide:

- The heading is the first three lines, and the earliest named type wins.
- A phrase followed by ":", "number", "no" or "#" is a reference ("Bill of Lading No: 123"), not a
  title.
- Without such a heading, keyword counts decide as before.
- Regression tests cover each case.

---

## Part 2: Earlier in this session

### Shipment-ID collisions are detected, never merged

`identifier_key` makes `AB-12-3` and `AB-1-23` both `AB123`. The key is unchanged, so stored data
keeps its keys. `identifier_collisions()` compares the printed forms: forms that only drop or add
separators are the same identifier, and separators in different places collide.

- **Audit:** not reconciled, with the reason and `identity_collision`.
- **Questions:** the scope abstains.
- **Extraction cross-check:** `shipment_id_collision`.
- **Freight audit:** not run.

### Pickup and delivery dates in an impossible order

A BOL expected or POD actual delivery date before the BOL pickup date makes the delivery NEEDS
REVIEW, and the chat abstains with the same reason (`chronology_problem`). Without a readable pickup
date nothing changes.

### Cross-document checks on extracted fields

`validate_shipment()` (report-only) adds these checks:

- `shipment_id_collision`
- `invoice_above_rate_confirmation` and `invoice_below_rate_confirmation`
- `payment_terms_mismatch` and `pickup_date_mismatch`
- `delivery_before_pickup`
- `duplicate_invoice_number`

`DocumentExtraction.from_dict()` rebuilds stored payloads.
`GET /api/extractions?shipment_id=…` returns `cross_document_issues`, and the shipment's Extractions
section shows them as "Checks across documents".

---

## Behaviour changes to review before committing

These change outcomes only for data that has the condition. The sample data and the frozen
evaluation sets have none of these conditions; see the results below.

1. **Colliding shipment IDs:** the documents give NOT RECONCILED with a reason, not a merged audit.
2. **Delivery before pickup:** the delivery is NEEDS REVIEW instead of DELAYED, EARLY or ON TIME.
3. **Headings decide document types:** a document whose heading names a type is classified as that
   type even when keyword counts favour another.
4. **Freight exceptions:** shipments with a rate confirmation and an invoice get them.

## Test results

All results below are on the final working tree, with Python 3.10.11 (`venv`) and Node 22.

| Check | Result |
|---|---|
| Full Python suite (`pytest`) | **1,840 passed, 0 failed** (4 min 52 s). Baseline 1,726, plus 114 new: 48 freight audit, 8 document titles, 33 collisions, 9 dates, 16 cross-document |
| Frontend type check (`tsc --noEmit`) | clean |
| Frontend tests (`vitest run`) | **8 files, 215 passed, 0 failed** (baseline 211, plus 4 new) |
| Production build (`vite build`) | succeeds. Main chunk **965.64 kB** (gzip 281.98 kB), against 959.87 kB (gzip 280.64 kB) before: **+5.8 kB** for the two new panels, no new dependency. Vite's 900 kB advisory is unchanged (bundle splitting not done) |
| Deterministic evaluation (`scripts/evaluate_assistant.py`) | **PASS, identical to the baseline report line for line**: case accuracy 100% (127 scored), security 28/28, extraction fields 100%, RAG recall@5 100%, citation precision 100%, abstention 100%, cross-shipment leakage 0%, structured value accuracy 100%, 0 false extractions |
| Blind set v4 (`--no-rag`) | **identical to the baseline**: 81.4% case accuracy, 0 false answers (diagnostic set; its exit code 1 is the below-threshold exit it had before) |
| `pip check` | No broken requirements found |
| `git diff --check` | clean |
| Attribution scan of every changed and new file | no AI attribution |

Not run in this session: the Python 3.12 suite (CI runs it on push), the live-model evaluation with
Ollama, and the smoke test against a running server.

## Files

Modified:

| File | Change |
|---|---|
| `config.py` | `FREIGHT_TOLERANCES` (`LOGIDOC_FREIGHT_TOLERANCES`) |
| `src/freight_audit.py` (new) | the freight audit engine |
| `src/audit_service.py` | `add_freight_audits`, `freight_skipped`; freight audit in the snapshot |
| `src/audit_analytics.py` | `add_freight_exceptions`, `FREIGHT_ACTIONS`; `add` accepts extra fields |
| `src/audit_explanations.py` | freight exception explanations (`freight_line`, `freight_sources`) |
| `src/api/routes_audit.py` | `freight_audit` in the shipment detail |
| `src/document_classifier.py` | the heading decides the document type (`title_type`) |
| `src/shipment_identity.py` | `separator_positions`, `identifier_collisions`, `collision_message` |
| `src/reconciliation.py` | collision check |
| `src/rag_scope.py` | collision check |
| `src/shipment_calculations.py` | `chronology_problem`, used by `audit_delivery` and `date_answer` |
| `src/document_extraction.py` | `DocumentExtraction.from_dict`; new `validate_shipment` checks |
| `src/api/routes_extraction.py` | `cross_document_issues` |
| `frontend/src/components/common/FreightAuditPanel.tsx` (new) | the freight audit section |
| `frontend/src/components/common/StatusBadge.tsx` | the six outcome badges |
| `frontend/src/pages/ShipmentDetail.tsx` | shows the freight audit section |
| `frontend/src/lib/types.ts` | `FreightAudit`, `FreightCharge`, `FreightCheck`, `CrossDocumentIssue` |
| `frontend/src/components/extraction/ExtractionPanel.tsx` | "Checks across documents" |
| `README.md` | freight audit, tolerances, classification, collisions, the date rule |
| `docs/IMPLEMENTATION_SPRINT_REPORT.md` (new) | this report |

New tests:

- `tests/test_freight_audit.py`: 48 tests
  - policy and tolerance (14)
  - charge-level three-way match (12)
  - single-rate readings (3)
  - accessorial evidence (6)
  - references and duplicates (8)
  - real PDFs through the audit service, exceptions, explanations and the API (5)
  - Of these, 12 are parametrized cases.
- `tests/test_document_titles.py`: 8 tests (the heading decides the document type)
- `tests/test_shipment_id_collisions.py`: 33 tests
- `tests/test_date_chronology.py`: 9 tests
- `tests/test_cross_document_checks.py`: 16 tests
- `frontend/src/test/pages.test.tsx`: +2 tests
- `frontend/src/test/extraction.test.tsx`: +2 tests

## Remaining risks and not done

- **Approved extraction corrections** are applied to the reconciliation but not to the freight audit,
  which reads the documents as printed. The result says so when corrections exist.
- **Accessorial evidence** is a text search for the charge's name in the rate confirmation, BOL and
  POD. It is shown to the reviewer as a passage; it does not prove the service was performed.
- **The rate's meaning** (all-in or line haul) is not recorded by the extractor; the engine infers it
  only when the amounts show it, and otherwise asks for review.
- **The exception lifecycle** is the existing one (open, in review, resolved, dismissed, with notes,
  assignment and a hash-chained history). DISPUTED and WAIVED were not added as separate states; a
  dismissal with its reason note covers a waiver.
- **The Exception Center list** shows freight exceptions with their description and evidence. The
  recommended action and financial impact are in the API and the shipment's explanations, not yet in
  the list's own columns.
- **Not started:** the assistant routing of "payment terms" on the main Assistant page, a
  cross-process writer lock, frontend bundle splitting, and a deployment definition.
- **Real-document accuracy** is still not measured. Every figure comes from synthetic,
  project-authored documents.
