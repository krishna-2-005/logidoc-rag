# Known extraction gaps and defects (hypotheses, not measurements)

Source: hand-built **synthetic** layout probes (2026-10-22) run against `document_extraction`.
No real document was used. These say what the extractor does with plausible layout variants;
they do **not** say how often real carrier documents look like them. The real-document
evaluation (`PROTOCOL.md`) is what measures that. Nothing below was observed on a real document.

## A. Defects D1 to D6: fixed for the synthetic variants (2026-10-23)

Regression tests: `tests/test_known_extraction_defects.py` (original case, nearby cases that must
keep working, and cases that must go to review). **These are synthetic regression results. They do
not show that real carrier documents are now read correctly.**

| # | Variant (synthetic) | Before | Now |
|---|---|---|---|
| D1 | A blank signature line: underscores, dashes, an empty box, `X ______`, `(signature)` | read as the signature | MISSING, `placeholder_only`; a signature or received-by gap needs review. A name under a blank line is read and flagged |
| D2 | `Shipper: A   Consignee: B` on one line | shipper absorbed the consignee | one pair per known label, each value stops at the next label. Lines under such a line (columns) are flagged `columns_ambiguous` |
| D3 | A line labelled `Total:` | counted as a charge | `Total`, `Total Payable`, `Amount Payable` are invoice-total labels; any other label with the word total is excluded from the charges |
| D4 | `VAT 20%:` then an amount | amount dropped, invoice accepted | a rate in a label is not part of the label: `VAT 20%`, `GST (5%)` are the tax field. A tax with no field of its own (`Provincial Sales Tax`) is unresolved and needs review |
| D5 | `Base Freight: $100.00   Fuel: $18.00` | no charges, invoice accepted | split into charges when every part is `label: amount` (marked `split_from_one_line`); otherwise unresolved and needs review |
| D6 | `Fuel Surcharge:` then `18%` | dropped | recorded as a percentage charge, no amount invented, invoice needs review, arithmetic unchecked |

General rule added with these: an amount (marked with a currency symbol or code) that is not
read as a charge, a total, a tax or an excluded item is listed as unresolved and the invoice needs
review. An amount on a line after a label's value, or on a line with no label, is no longer dropped.

### Ambiguities that remain (decisions, not hidden defects)

- Any label containing the word `total` is excluded from the charges, so a real charge named
  `Total Logistics Fee` would be excluded; the arithmetic check then flags the difference.
- A known label after an earlier colon splits the value (`Notes: Consignee: call first` is read as a
  `Consignee` field). A short label word after another label's value (`Base Freight: $100  Fuel
  Rate: $18`) is split at `Rate:`; the pieces then fail to read and go to review.
- Two tax lines (GST and PST) are CONFLICTING and need review; they are not summed.
- Two-column address blocks are flagged, not assigned.
- A signature line followed by text reads the next line as the name and flags it.
- `N/A`, `Name` and `Signature` alone count as placeholders for any field.
- An amount with no currency symbol or code (`Base Freight: 100.00`) is unresolved, as before.

## A2. Parser stabilization audit (2026-10-23)

A read-only review of the D1 to D6 fixes with synthetic probes and a mutation check (each fix switched
off in turn: 8 to 35 of its own regression tests failed every time, so the tests exercise the failures).
It found six reproducible problems, all fixed with tests in `tests/test_parser_stabilization.py`:

| Problem (synthetic) | Fix |
|---|---|
| A note containing a label (`Special Instructions: Max Weight: 40,000 lbs`) made an EXTRACTED weight: a regression the D2 fix introduced | a later label is split only when the line starts with a known label or the label stands apart (two spaces or a tab) |
| `Storage: 2 x $15.00 = $35.00` gave a complete invoice whose arithmetic passed | a quantity times rate that contradicts the amount is unresolved: review, arithmetic unchecked |
| A charge in free text before a page's first label was ignored | an invoice amount on a line before the first label is unresolved (`amount_outside_a_label`) |
| The extractor version ignored the PyMuPDF version, which decides the extracted text | the PyMuPDF version is part of the version; a test fails if extraction starts importing an unfingerprinted module |
| After a version change the new extraction did not show the approval the audit still used, and a duplicate proposal of the same value was accepted | an extraction shows, and a proposal compares against, the latest approval of the same file content and field |
| An audit cross-check from an older extractor looked current | `structured_extraction_outdated` warning on the input |

### Closed after the audit (2026-10-23, synthetic regression tests in `tests/test_parser_integrity_gaps.py`)

- **Repeated charges.** The same label and amount twice (a recap page, or two real identical charges: the text
  cannot say which) keeps both items, adds one unresolved `repeated_charge` entry, and the invoice needs review
  with its arithmetic unchecked. Nothing is deduplicated. Cost: a legitimate repeat now also goes to review.
- **Cross-check staleness.** The audit cache key now includes the extractor version, so a changed version
  recomputes the audit without `invalidate()` or a restart. `GET /api/extractions/staleness` lists
  `outdated_cross_checks` (shipment, fields, stored versions, current version). Recorded runs are not touched.
  Not done: the web interface does not show `outdated_cross_checks` yet (API and the audit inputs do).
- **Same bytes under two file names.** When the file name would give the second copy another type than the
  first, `ExtractionStore.save` raises `AliasTypeConflict` (HTTP 409 on `/api/extractions/run`) instead of
  reusing the first type. The second name is not stored. Design for storing both: see below.

### Still open

- **Percentage boilerplate.** `Late Payment: 1.5% per month` or `Discount: 2% if paid in 10 days` are
  recorded as percentage charges and send the invoice to review: a false alarm in the safe direction.
- **Two tax lines** (GST and PST, or a tax this extractor has no field for) always need review.
- **Aliases (schema 5, implemented 2026-10-23).** Each name a content was seen under is recorded in
  `extraction_aliases`; a name that classifies as another type is recorded as `type_conflict` and then refused
  (HTTP 409). Aliases do not influence the audit: letting a same-type alias serve the cross-check or approval
  validity (decision D3) is a separate change. Existing stores need the explicit migration
  (`scripts/migrate_extraction_store.py`); see `docs/SCHEMA_V5_ALIAS_DESIGN.md`.
- **Fingerprint exception.** `chunker` is imported by an extraction dependency (`shipment_identity`) but cannot
  change an extraction; it is the one reviewed exception to the fingerprint. A test fails on any other
  unfingerprinted import, and another test fails if unrelated modules are added to the list.

### Design for alias storage (not applied: needs a migration)

`extractions` is `UNIQUE (sha256, extractor_version)`, and approvals, corrections and the audit hang off
`extractions.id`. Safest change, in order:

1. Schema v5, additive only: `extraction_aliases (id, extraction_id REFERENCES extractions, filename,
   inferred_type, first_seen)`, append-only triggers like the other tables. Existing rows are not rewritten.
   `save` records each name seen for a content hash; a different inferred type is kept as an alias row and
   the extraction row stays the one made from the first name.
2. Do not change the UNIQUE key to include the document type: SQLite needs a table rebuild, and every foreign
   key, trigger and hash-chain check (`verify_history`) would have to be redone in one transaction.
3. Risk to handle first: `open_if_current` returns None for a database below the current version, so after
   raising the version the audit would ignore stored approvals until a write path migrates the file. The
   audit would need to migrate on open, or treat "older but readable" as usable, before the bump.
4. Tests to write with it: migrate a v4 file with approvals and verify the chain; both names listed; the
   audit reads approvals before and after the migration.

## B. Coverage gaps that fail safe (MISSING or UNPARSEABLE, so the document goes to review)

| Variant (synthetic) | Result |
|---|---|
| Table rows (`Description  Qty  Rate  Amount`) or label and value separated by spaces, no colon | MISSING: nothing read, and the amounts are now reported as unresolved (the most likely large gap on real invoices) |
| Labels outside the known list: `Pieces`, `Pcs`, `Ship Date`, `Delivered`, `Qty Received`, `Linehaul`, `FSC` | MISSING |
| Slash dates `10/05/2026` | UNPARSEABLE (deliberately: day/month order is ambiguous) |
| European numbers `1.200,00` | UNPARSEABLE |
| `CA$100.00`, `C$`, `AU$` | currency MISSING, amount UNPARSEABLE |
| `10 pallets`, `45 of 48`, `12500#`, `$1,850.00 all-in` | UNPARSEABLE |

## C. Behaviour that held up (synthetic)

Credits and brackets as negatives, previous balances and payments excluded from charges,
unusual charge names (`Lumper Fee`) kept as charges, `Amount Due` as a total, a code after the
amount (`100.00 CAD`), `£`, labels with a value on the same line, ISO dates, a document with no
title classified by content.

## D. Not measurable without real documents

Real layouts, how often each variant occurs, scanned-document share, multi-column forms,
handwriting, and every accuracy figure. None is claimed.

## Priority for the next extractor work (after real data says what is common)

1. Measure on real documents first: which of the variants above occur, and how often.
2. Table layouts (B, first row): measure how many real invoices need them before building.
3. Label vocabulary (`Pcs`, `Ship Date`, `FSC`, `Linehaul`): add from the labels real documents use, not from guesses.
4. Currency symbols (`CA$`) and annotated numbers (`all-in`, `45 of 48`): only where real documents show them.
5. Revisit the remaining ambiguities above against real labels (multiple taxes, two-column blocks).
