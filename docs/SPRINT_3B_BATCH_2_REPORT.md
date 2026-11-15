# Sprint 3B, Batch 2 report

Shipment risk and exception analytics, and freight audit and carrier reporting.

Date: 2026-11-15. Branch `feature/production-readiness`, starting from `ff1e307`.

## 0. Preflight

- **CI:** both runs on `ff1e307` (push 43620930544, pull request 43621238751) passed all four jobs.
- **Branch:** local and remote at `ff1e307`, with a clean working tree.
- **Disk:** C: 11.2 GB free, D: 4.0 GB, after a restart.
- **Real data:** the 34 files of `data/` and `chroma_db/` unchanged (`sha256sum -c` against the record made before
  Sprint 2). The real stores are git-ignored, and backups default to a project `backups/` folder, which does not
  exist. No test or script wrote outside temporary folders.

Sprint 4 was not started. Nothing was merged into `main`.

## 1. Task 1, shipment risk and exception analytics

- **Score.** Deterministic, from 0 to 100, as the sum of six capped signals:
  - unresolved audit discrepancies by severity;
  - values not verified;
  - the duplicate invoice indicator;
  - the amount at stake per currency, in bands;
  - missing documents;
  - the freight audit outcome.

  Categories are low, medium, high and critical (thresholds 25, 50 and 75 by default). Details:
  [SHIPMENT_RISK_ANALYTICS.md](SHIPMENT_RISK_ANALYTICS.md).
- **Configurable and versioned.** `LOGIDOC_RISK_MODEL` (JSON) is validated at startup. Every score carries the model
  version and digest. Changing weights or thresholds under the same version is refused, so scores stay
  reproducible.
- **Missing data.** A signal whose evidence is not available scores nothing, is listed, and makes the score
  "partial". Missing evidence is never read as "no risk".
- **No allegation.** A duplicate invoice number is "an indicator to check, not a finding of fraud". No language
  model is involved anywhere.
- **History.** Trends score recorded snapshots, which never change, so a period's figures never change under one
  model version. History has no review states or completeness, so trends use the other five signals and say so.
- **Dashboard (page *Shipment Risk*):**
  - figures: shipments scored, high or critical, mean score, partly assessed;
  - risk distribution and risk over time (month or week);
  - the exception breakdown by category and review state, and carrier risk patterns;
  - a searchable, filterable, sortable, paginated table;
  - per shipment, an explanation of every factor with its evidence references and what was not counted.

  Every chart has a "Show the numbers" table. The four category colours passed the dataviz palette validator
  (light theme; the application has no dark theme).
- **API:** `/api/risk/model`, `/overview`, `/shipments`, `/shipments/{id}`, `/trends`, also under `/api/v1`.
  Reviewers and administrators (`exceptions.view`). Pages of up to 200 rows.

## 2. Task 2, freight audit and carrier reporting

- **Reports:** freight audit summary, discrepancies and exceptions, carrier performance, and corrections and
  re-audits (proposals, decisions, controlled re-audits, TMS decisions). The shipment-level report is the existing
  per-shipment CSV and PDF. Details: [REPORTING.md](REPORTING.md).
- **Filters:** date range, carrier, shipment, freight audit outcome, exception status and risk category. A filter
  a report cannot take is refused (422), never ignored.
- **Each report states** its UTC generation time, the filters in words, its sources (no path or database name)
  and notes (undated shipments left out, amounts not determinable).
- **Totals:**
  - money is per currency only, with "Not stated" as its own group;
  - a missing value is NOT AVAILABLE, never 0;
  - a rate shows its count and denominator;
  - the preview's totals cover every row, not only the page shown.
- **Files:**
  - CSV: every cell through `safe_cell` (formula injection).
  - PDF: plain text drawn with the standard fonts, with nothing embedded, linked or scripted.
  - Evidence is referenced (file, page, type), never quoted.
- **API:** `/api/reporting/catalog`, `/{report}`, `/{report}.csv`, `/{report}.pdf`, also under `/api/v1`. They
  need `reports.download`; the corrections report also needs `history.view`.
- **Page *Reports*:**
  - report selection, the filters that report takes, the preview with totals and rows, CSV and PDF downloads
    with the same filters;
  - the existing shipment report and quick exports are kept.

## 3. Problems found and fixed

- **A component left out of git.** `.gitignore` ignores every folder named `reports/` (meant for generated
  reports), so `frontend/src/components/reports/ReportBuilder.tsx` would have been left out of the commit. It
  now lives in `frontend/src/components/reporting/`, and no other new source file is ignored.
- **A crash on evidence references.** The discrepancies report first crashed: exception evidence is a list of
  strings, not objects. It now keeps the reference (file, page, type) and leaves out the quoted passage.
- **An unknown completeness entry read as complete.** In the first draft, a shipment without a completeness entry
  had nothing missing. It is now "unavailable" (tested).
- **Duplicate accessible labels.** A chart label and its table caption were the same text, and are now distinct.
- **A setting not documented.** `LOGIDOC_RISK_MODEL` is now in `.env.example`; the guard test that every setting
  is documented caught it.
- **A slow test under load.** An existing webhook test that types a URL key by key takes about 1.5 s alone, but
  went over 5 s when every frontend test file ran in parallel on this machine. Its own limit is now 15 s; nothing
  it checks changed.

## 4. Validation

Every Python run used `-p no:cacheprovider` with its temporary folder on D:, one at a time.

| Check | Result |
|---|---|
| New: `tests/test_risk_analytics.py` (3.12) | 30 passed: correctness, caps, the eight threshold boundaries, resolved exceptions, missing evidence, currency separation, no allegation, determinism, versioning, invalid models stopping the server, reproducible trends, the API (totals add up, factors add up, dismissing an exception lowers the score, filters, sorting, pages, 401, 503, empty) |
| New: `tests/test_reporting.py` (3.12) | 11 passed: per-currency totals, status and currency splits, every filter and its description, refused filters, CSV formula injection, a PDF with no link, embedded file or script, empty reports, catalog and previews, downloads matching the preview, 401, the corrections report's permission, the corrections history |
| Related (3.12): PDF and CSV export, API contracts, carrier intelligence, production config, history | 209 passed, 1 failed: `LOGIDOC_RISK_MODEL` was not yet in `.env.example`. Documented, then 47 passed |
| Related (3.12): API, extraction store, line items, TMS, freight operations | 157 passed, exit 0 |
| New and affected files (3.10) | 159 passed, exit 0 |
| Full suite (3.12) | **2,409 passed**, exit 0 (22 min) |
| Frontend typecheck and build | exit 0; main bundle 1,168.94 kB (+23.3 kB) |
| Frontend tests | 13 files, 274 tests. The last three full runs and the new file alone passed. Of the full runs before them, one hit the webhook test's 5 s limit (section 3). Another failed one existing assertion in `store_state.test.tsx` (extraction panel, untouched here) after 1.2 s, which fits a 1 s wait expiring under parallel load: it passes alone and in the three later full runs |
| OpenAPI | `--check` up to date. Against `ff1e307`: 0 removals, 37 additions (9 paths, 28 schemas) |
| Evaluation (`scripts/evaluate.py`, 3.10) | identical to the recorded baseline apart from timings |
| CI evaluation guard (3.10 and 3.12) | `RESULT: PASS`, exit 0 |
| Real data | the 34 files of `data/` and `chroma_db/` unchanged (`sha256sum -c`), none added |

Not deleted: `D:\ldt3a` and four folders the smoke runs left inside it (`tmpibt14tok`, `tmpghygjriv`,
`tmps0r97te0`, `tmpussd8_dy`, 328 KB each, synthetic data only; their cleanup was refused while the search
index file was still open).

## 5. Changed files

- **Backend, new:** `src/risk_analytics.py`, `src/reporting.py`, `src/api/routes_risk.py`,
  `src/api/routes_reporting.py`
- **Backend, changed:**
  - `src/api/app.py`: routers
  - `src/api/schemas.py`: the risk and report models
  - `src/audit_history.py`: `re_audits`, `freight_facts` (read only)
  - `src/extraction_store.py`: `all_corrections` (read only)
  - `src/audit_pdf_export.py`: the page header can be named; the shipment report is unchanged
  - `src/environment.py`: `LOGIDOC_RISK_MODEL` validated at startup
  - `.env.example`
  - `docs/api/openapi-v1.json`: regenerated
- **Tests, new:** `tests/test_risk_analytics.py`, `tests/test_reporting.py`
- **Frontend, new:** `frontend/src/pages/Risk.tsx`, `frontend/src/components/reporting/ReportBuilder.tsx`,
  `frontend/src/test/risk_reporting.test.tsx`
- **Frontend, changed:**
  - `frontend/src/App.tsx`, `frontend/src/components/layout/AppShell.tsx`: route and menu
  - `frontend/src/pages/Reports.tsx`
  - `frontend/src/hooks/queries.ts`, `frontend/src/lib/types.ts`, `frontend/src/lib/chart.ts`
  - `frontend/src/test/integrations.test.tsx`: one test's time limit
- **Documentation:** `docs/SHIPMENT_RISK_ANALYTICS.md` (new), `docs/REPORTING.md` (new), `README.md`, this report

Not touched: historical audit records, approved corrections and re-audit semantics, TMS decisions, freight audit
outcomes, the missing-document evidence rules, `data/`, `chroma_db/`, the PDFs, the backups, the frozen
evaluation outputs, and any other project.

## 6. Remaining risks

- **Weights not calibrated.** The built-in risk weights are a reasoned starting point, not calibrated on outcomes.
  Changing them is a configuration decision with a new version.
- **No exchange rates.** The amount bands and the report totals stay within one currency.
- **Large reports.** A very large report makes a long PDF. The CSV is the file to analyse; the PDF prints the main
  columns.
- **TMS differences not scored.** Open TMS differences are not a risk signal yet.

## 7. Commit, push and CI

Three commits on `ff1e307`, staged by exact paths, by the repository owner's account (`krishna-2-005`):
the backend with its API, contract and tests; the screens with their tests; the documentation with this report.
Their hashes and the CI result are given with the push.
