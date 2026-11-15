# Reporting

Freight audit and carrier reports, previewed on the *Reports* page and downloaded as CSV or PDF. Every figure comes
from the audit as the application holds it; nothing is recalculated.

Code: `src/reporting.py` (reports, CSV, PDF), `src/api/routes_reporting.py` (API),
`frontend/src/components/reporting/ReportBuilder.tsx` (page *Reports*). The shipment-level audit report is the
existing per-shipment CSV and PDF (`src/audit_export.py`, `src/audit_pdf_export.py`).

## 1. Reports

| Report | One row per | Totals |
|---|---|---|
| Freight audit summary | shipment: statuses, freight audit outcome, invoice total, contracted amount and variance, discrepancies, open exceptions, amount at stake, risk | shipments; final status; freight audit outcome; invoiced, contract variance and amount at stake **per currency**; risk category |
| Discrepancies and exceptions | exception: category, kind, severity, review status, description, amount and currency, freight audit outcome, evidence reference | exceptions; review status; category; amount at stake per currency (all listed, and unresolved only) |
| Carrier performance | carrier: deliveries measured, on-time rate, audits against a contract, pass rate, duplicates, invoice accuracy, exceptions, incomplete documents, mean risk, high-risk shipments, invoiced per currency | carriers and shipments (and shipments without one carrier); rates over all selected shipments; invoiced per currency |
| Corrections and re-audits | event: correction proposed, approved or rejected; re-audit recorded; TMS decision | events; by kind |

## 2. Filters

| Filter | Applies to | Meaning |
|---|---|---|
| Date range | all | the shipment date (BOL pickup, else POD delivery, else invoice date); for corrections, the date of each event (UTC) |
| Carrier | summary, discrepancies, carriers | the carrier's key, as Carrier Intelligence identifies it |
| Shipment | summary, discrepancies, corrections | the shipment ID contains the text |
| Freight audit outcome | summary, discrepancies | including `NOT_RUN` |
| Exception status | summary, discrepancies | summary: shipments with an exception in that state; discrepancies: the exceptions in that state |
| Risk category | summary, discrepancies, carriers | the shipment's current risk category ([SHIPMENT_RISK_ANALYTICS.md](SHIPMENT_RISK_ANALYTICS.md)) |

A filter a report cannot take is refused (422), never silently ignored, and so is a start date after the end
date. With a date range, undated shipments are left out and counted in a note.

## 3. What every report states

- **When it was generated**, in UTC: in the preview, at the top of the CSV and on every PDF page.
- **The filters applied**, in words ("No filter: every shipment" when none).
- **Where the figures come from:** the current audit and when it was computed, the review store, the risk model's
  version and digest, and for corrections the extraction store, the audit history and the integration store.
  Never a path or a database file name.
- **Notes:** undated shipments left out, discrepancies without a determinable amount, sources that could not be
  read.

## 4. Rules

- **Currencies are never added together.** Every money total is per currency, and an unstated currency is its own
  group ("Not stated").
- **Missing is never zero.** A missing value is NOT AVAILABLE. A rate with nothing to count is NOT AVAILABLE, and
  every rate shows its count and denominator. An invoice without a readable total is counted apart.
- **The preview and the files agree.** They come from one build. The preview shows a page of rows (up to 500);
  totals always cover every row.
- **CSV is protected against formula injection.** The file is UTF-8 with a byte-order mark, laid out as a header
  block, the table, then the totals. Every cell goes through `audit_export.safe_cell`: a cell a spreadsheet could
  run as a formula (`=`, `+`, `-`, `@`, their full-width forms, a tab or a carriage return) gets an apostrophe in
  front; a plain number stays a number.
- **The PDF is plain text.** It is A4, drawn with the PDF standard fonts (`audit_pdf_export.Report`): nothing is
  interpreted as markup, nothing is embedded, linked or scripted, and characters the fonts cannot show are
  replaced. Wide reports print their main columns; the CSV has every column.
- **Evidence is referenced, not quoted.** A report gives the file, page and document type, never the passage.
  The older exceptions CSV (`/api/reports/exceptions.csv`) does the same since Sprint 4: its `evidence` column
  lists the distinct references, and anything that is not a reference is left out
  (`audit_analytics.evidence_reference`).

## 5. API (`reports.download`; the corrections report also needs `history.view`; also under `/api/v1`)

| Method and path | Answer |
|---|---|
| `GET /api/reporting/catalog` | `ReportCatalog`: the reports, the filters each takes, the carriers and other filter values |
| `GET /api/reporting/{report}?filters&offset=&limit=` | `ReportPreview`: a page of rows; totals over all of them |
| `GET /api/reporting/{report}.csv?filters` | the whole report as CSV |
| `GET /api/reporting/{report}.pdf?filters` | the whole report as PDF |

`{report}` is one of `freight-audit-summary`, `discrepancies`, `carrier-performance`, `corrections`. The filters are
`start`, `end`, `carrier`, `shipment`, `outcome`, `exception_status` and `risk_category`; the last three may repeat.
