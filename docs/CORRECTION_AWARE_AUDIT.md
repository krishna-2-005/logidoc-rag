# Correction-aware audit integration

Status: implemented. Sections 1-7 are the plan written before the change; "What was built"
at the end records where the implementation differs or goes further.

## 1. What each calculation reads today

| Calculation | Where | Fields it reads | Reader |
|---|---|---|---|
| Quantity audit | `shipment_calculations.audit_quantity` via `reconciliation.reconcile_shipment` | expected_pieces (BOL), delivered_pieces (POD) | `read_field` over chunk text (legacy) |
| Delivery audit | `audit_delivery` | expected_date (BOL), actual_date (POD) | `read_field` |
| Invoice audit | `audit_invoice` / `invoice_figures` | base_freight, fuel_surcharge, detention_charge, other_charges, invoice_total, invoice_summary | `read_field` |
| Carrier / route | `reconciliation.extract_field` | carrier, origin, destination | first-match regex (legacy) |
| Financial exposure | `financial_exposure.exposure_rows(snapshot)` | nothing of its own: the invoice audit result | derived from the snapshot |
| Document completeness | `document_completeness.scan_folder` | document type, shipment ID, page counts | document scan; no extracted field values |
| Assistant | `assistant_tools` | the snapshot, exposure rows, scan | derived; citations come from the audit evidence |

## 2. Which fields can be corrected and consumed

A reviewer correction names an extraction field (`document_extraction` schema).
Only these map onto a field a calculation reads:

| Extraction field | Audit field | Consumed by |
|---|---|---|
| BOL.piece_count | expected_pieces | quantity audit |
| POD.delivered_pieces | delivered_pieces | quantity audit |
| BOL.expected_delivery_date | expected_date | delivery audit |
| POD.delivery_date | actual_date | delivery audit |
| INVOICE.invoice_total | invoice_total | invoice audit, and so financial exposure |

Everything else (shipper, consignee, weight, pickup date, tax, currency, charges
by label, carrier, route, ...) can still be proposed and approved, but **no
calculation consumes it yet**: the approval is recorded and the field reports
"consumed by: nothing". Charges (base freight etc.), carrier and route stay on
the legacy readers. Completeness consumes no correctable field. Shipment IDs
cannot be corrected.

## 3. Source-selection policy

For each audit field of a shipment:

1. An **approved** correction, if one applies, is the value (source
   `approved_correction`). Proposed, rejected and legacy corrections never apply.
2. Otherwise the **legacy reader's** value (source `legacy_reader`), exactly as
   before. Structured extraction without a correction is not consumed: it is
   recorded beside the legacy value as a cross-check (`structured`, with
   `agrees`), never silently preferred and never mixed in.
3. A value that is missing, conflicting or unparseable stays unresolved
   (`resolved: false`, value null): the audit says NEEDS REVIEW as before.

A correction applies only while the document it was made on is unchanged (the
file's SHA-256 still equals the extraction's) and belongs to the shipment being
audited (the extraction's shipment key). It is attached to the document content,
not to an extractor version: a new extractor version does not drop an approval.
Among approvals for the same document and field, the latest approval wins and
the earlier ones are listed as superseded. Approved corrections on two
different documents of one shipment that disagree make the field unresolved.

## 4. Approval workflow

`extraction_store` schema v4. A correction is a **proposal** (`field_corrections`,
unchanged, append-only). Its state is derived: `legacy` (written before the
workflow, never applies, cannot be decided), `pending` (no decision),
`approved` or `rejected` (`correction_decisions`, append-only, hash-chained, at
most one decision per correction, enforced by a UNIQUE constraint). The original
extracted value is never changed.

Rules enforced in the store (service layer), not only in the routes:

- proposing needs `extraction.correct` (reviewer, admin); deciding needs
  `extraction.approve` (administrator only) and an authenticated account;
- the approver must not be the proposer;
- only a pending correction can be decided; a second decision, or a decision on
  a legacy correction, is refused (409);
- one pending proposal per field and extraction; a new one must name the latest
  correction it was based on (`based_on`);
- a rejection needs a reason.

## 5. Recording inputs and history

`audit_history` schema v3 adds, append-only:

- `run_inputs`: for every recorded run and shipment, every audit field consumed
  with its effective value, source (legacy reader or approved correction),
  correction id, extraction id and extractor version, source document, page
  (null when unknown), validation, warnings, calculation version and resolution
  state. No document text is stored.
- `run_supersessions`: when a run consumes an approved correction the shipment's
  previous run did not, the previous run is recorded as superseded by the new
  one (with the correction ids). Older runs keep their snapshots and inputs.
- `audit_runs.calculation_version`.

The snapshot digest includes the applied correction ids only when there are some,
so shipments without corrections look unchanged after the upgrade.

## 6. Recompute

Approving a correction changes what the audit reads (the audit cache key includes
the approved-corrections token), so the next read recalculates. Recording is a
separate step: `AuditService.record_corrected_run` records a targeted run for the
shipments whose latest recorded run did not consume the currently applicable
corrections (derived staleness), supersedes their previous runs and leaves other
shipments alone. The approval endpoint tries this for an administrator; if it
fails the approval stays, the failure is reported and the same call can be
retried. Cross-database atomicity does not exist: the decision (extraction DB) and
the run (history DB) are two transactions, and staleness is derived, so nothing
is lost between them.

## 7. Not done

The reviewer interface (shipment Extractions section, the administrators' approval queue, the
Recorded runs inspector) is described in the README. No consumption of corrections for line-item charges,
carrier, route or completeness. No withdrawal of a pending proposal. Approvals do
not expire. The same database-file limits as before apply (not tamper-proof).

## 8. What was built (and where it differs from the plan)

- `shipment_calculations.Records` / `read_field` / `read_document_field`: the one seam.
  A plain list reads the documents exactly as before; `reconcile_shipment(...,
  overrides=)` is only passed when an approved correction applies, so with none the
  call is identical to the old one. `invoice_figures` accounts for the document's
  own total when it is overridden (otherwise the document's amount would count as
  an unknown amount and the invoice audit would abstain).
- `audit_inputs`: `build_overrides` (policy), `build_inputs` (provenance, no text).
- `extraction_store` v4: `propose`, `decide`, `pending`, `approved_corrections`,
  `audit_token`, `open_if_current` (read-only, never migrates), `current_extractions`.
  `verify_history` also checks the decision chain and the proposer/approver rule.
- `audit_history` v3: `run_inputs`, `run_input_sets`, `run_supersessions`,
  `calculation_version`; `record_run(shipment_keys=)` for a targeted run;
  `staleness`, `superseded_by`.
- `AuditService`: approved-corrections token in the cache key, `inputs` on every
  shipment of the snapshot, `staleness`, `record_corrected_run`,
  `decide_correction`.
- API: decision, pending, staleness, recompute, run inputs; assistant: citations
  carry `value_origin`, answers add a note.
- Evidence entries of the reconciliation result now carry `value_origin` and
  `correction_id` (always "legacy_reader" / null without corrections).
- The audit cache key now includes the extraction store's token, so storing an
  extraction refreshes the structured cross-check shown in the inputs. The
  cross-check never changes a result.

Decisions worth knowing:

- A correction belongs to the document content, not to an extractor version:
  upgrading the extractor does not drop an approval.
- A legacy correction cannot be approved. It is not promoted; propose it again.
- Approvals by an unauthenticated development principal are refused.
- Approving does not record the run for an administrator who cannot record
  history; it stays visible through staleness.
- The decision (extraction DB) and the recorded run (history DB) are two
  transactions. Nothing couples them; staleness is derived, so a failure between
  them loses nothing.
