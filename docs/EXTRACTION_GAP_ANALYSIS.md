# Extraction: field and schema gap analysis

Analysis only. Nothing in the extraction pipeline was changed. Based on reading
`document_loader.py`, `document_classifier.py`, `identifier_extractor.py`,
`logistics_fields.py`, `chunker.py`, `shipment_calculations.py` (field
readers), `reconciliation.py` and `metadata_extractor.py`.

> **Status (2026-10-22):** the structured extraction milestone is implemented in
> `src/document_extraction.py` and `src/extraction_store.py` (see the README,
> "Structured extraction"). The analysis below is kept as written before the
> work. Still open: OCR, tabular line items, feeding reviewer corrections into
> the audit calculations, replacing the two legacy field readers (they are
> kept and checked against the new extractor), and any measurement on real
> carrier documents. The evaluation dataset
> `tests/eval/structured_extraction_labels.json` is synthetic/project-authored;
> fields without labels (POD.damage_report, RATE_CONFIRMATION shipper,
> consignee, terms, weight) are reported as not measured.
>
> **Audit remediation (2026-10-22):** fixed invoice line-item classification
> (currency mark required; counts, weights, identifiers, payments and balances
> excluded; discounts and unmarked numbers unclassified and sent to review),
> repeated fields with an unreadable occurrence (now CONFLICTING), value
> boundaries (line caps with review), document text in stored reasons and
> warnings, shipment ID corrections (refused), stale corrections (409 via
> `based_on`), stored-history protection (triggers, hash chain, integrity check;
> not tamper-proof against file access) and the extractor version (source
> fingerprint).
>
> **Correction-aware audit (2026-10-22):** corrections are now proposals that a
> different administrator approves; approved corrections of five fields feed the
> quantity, delivery and invoice audits, with recorded inputs and superseded runs
> (`docs/CORRECTION_AWARE_AUDIT.md`). Charges, carrier, route and completeness
> still use the original readers.

## What actually happens today

1. `extract_pdf` reads text per page with PyMuPDF (`sort=True`). Pages with no
   text are dropped silently from the page list; there is no OCR.
2. `classify_document` assigns BOL / POD / INVOICE / RATE_CONFIRMATION / UNKNOWN
   from keyword hits in the content (not the file name).
3. `extract_all_identifiers` reads 14 identifier types by label regex
   (shipment_id, load_id, bol_number, pro_number, po_number, invoice_number,
   tracking_number, booking_number, reference_number, consignment_number,
   waybill_number, carrier_reference, customer_reference, ...). The first match
   per type wins; there is no conflict detection inside a document.
4. `create_document_chunks` copies those identifiers into every chunk's metadata
   with `source` (file name) and `page`.
5. The **audit** does not use stored fields. `reconcile_shipment` re-reads values
   from chunk text through two separate mechanisms:
   - `shipment_calculations.FIELDS` readers (expected_pieces, delivered_pieces,
     expected_date, actual_date, pickup_date, base_freight, fuel_surcharge,
     detention_charge, other_charges, invoice_total, plus notes). Each reading is
     a `FieldValue` (parsed value, source text, raw value, record with
     source and page). Conflicting copies make the field NEEDS REVIEW.
   - `reconciliation.extract_field` (a simple "Label:\nvalue" regex, first match,
     no provenance) for carrier, origin and destination.

## Gap by document type

| Field | BOL | POD | Invoice | Rate confirmation | Today |
|---|---|---|---|---|---|
| Document type | yes | yes | yes | type only | keyword classifier, no confidence |
| Shipment ID | yes | yes | yes | yes | identifier regex |
| Shipper / consignee | no | no | n/a | no | not extracted anywhere (aliases for receiver exist, unused) |
| Carrier | yes (audit) | yes (audit) | yes (audit) | no | `extract_field`, no provenance |
| Origin / destination | yes (audit) | n/a | yes (audit) | no | `extract_field`, no provenance |
| Pickup / expected delivery date | yes | n/a | n/a | no | FIELDS readers, with source and page |
| Actual delivery date | n/a | yes | n/a | n/a | FIELDS reader, with source and page |
| Pieces expected / delivered | yes | yes | n/a | no | FIELDS readers, with source and page |
| Weight | alias only | n/a | n/a | no | not extracted |
| Invoice number | n/a | n/a | identifier only | n/a | identifier, not validated against shipment |
| Invoice date / due date | n/a | n/a | no | n/a | not extracted |
| Currency | n/a | n/a | recognised in amounts, never returned | no | amounts parsed, currency dropped |
| Line items | n/a | n/a | only 4 fixed names (base, fuel, detention, other) | no | arbitrary line items are not read |
| Tax | n/a | n/a | no | no | not extracted |
| Total | n/a | n/a | yes | no | FIELDS reader |
| Agreed rate, equipment, terms | no | no | no | no | rate confirmations are classified, not read |
| Reference numbers (PO, PRO, booking...) | identifiers | identifiers | identifiers | identifiers | label regex, first match |

## Cross-cutting gaps

- **Provenance.** Present for the numeric and date fields (file, page, source
  text) but not for carrier, route, identifiers or document type. Provenance is
  computed on the fly during an audit and is not stored with the extraction.
- **Confidence.** None anywhere. A value is either read or not; ambiguity is only
  expressed as conflict (two different values) or absence.
- **Uncertainty states.** The audit distinguishes "not found" (unavailable),
  "different values" (conflicting) and "unreadable/inconsistent" through the
  reason text, which `audit_history.value_state` parses back out of strings. There
  is no typed status per field (absent / unreadable / unparseable / genuinely
  missing) and no distinction for an image-only page inside a mixed PDF.
- **Persistence.** Extraction is not stored. Every audit re-reads the PDFs; there
  is no extraction record to correct, version or diff.
- **Human correction.** None. The only human state is the exception review, which
  never changes values. There is no field-level correction with an audit trail.
- **Validation.** Required-field and cross-document checks exist only inside the
  reconciliation (BOL vs POD vs invoice values). There are no per-document
  required-field rules, no invoice-number or date sanity checks, no currency
  consistency check, and no check that a rate confirmation matches the invoice.
- **Dual readers.** Two field-reading mechanisms (`FIELDS` readers and
  `extract_field`) with different strictness and provenance; a document layout
  that one reads and the other does not gives inconsistent results.
- **Layout dependence.** Readers depend on the sample documents' "Label:\nvalue"
  layout (PyMuPDF sort order also moves values: in the fixtures the value of a
  field can appear after later labels). Other layouts (tables, two-column
  forms, scanned text) are not covered by any fixture.
- **OCR.** None; image-only PDFs are reported, not read.
- **Evaluation labels.** `tests/eval/extraction_labels.json` covers 12 fields on
  two fixture shipments plus synthetic documents, and explicitly lists the
  unmeasured fields (shipper, consignee, invoice number, currency, tax,
  confidence). Any new extracted field needs labels before an accuracy is claimed.

## Proposed schema for the extraction milestone (implemented differently; see the status note)

One record per document and field, stored separately from the PDF and from audit
results:

```
extracted_field:
  document_sha256, filename, page, field_name, document_type
  value (typed), raw_text, currency (for money), unit
  state: extracted | absent | unreadable | unparseable | conflicting
  confidence: 0..1 (rule-based, explainable: label match quality, layout, repeats)
  reader_version
field_correction (append-only):
  extracted_field_id, corrected_value, reason, actor, authenticated, at, prev_hash
```

Validation layer: per-document-type required fields; document-to-document checks
(shipment ID, carrier, route, currency); numeric sanity (total vs line items is
already in the audit and stays there).

## Suggested order for the milestone

1. Freeze labelled fixtures (including layouts the readers do not handle today).
2. Unify the readers behind one function returning typed states with provenance,
   keeping the existing reconciliation inputs and outputs byte-identical.
3. Add the missing fields (shipper, consignee, invoice number/date, currency, tax,
   arbitrary line items, rate-confirmation amount) with labels first.
4. Persist extractions and add append-only human corrections.
5. Only then add per-field confidence, and report it as rule-based, not calibrated.
