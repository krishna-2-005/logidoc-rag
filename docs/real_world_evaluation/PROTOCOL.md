# Real-world extraction evaluation: protocol

Status: the harness is built and tested. **No real-world result exists yet**, because no
permitted, independently labelled real documents have been supplied. Until they are,
`scripts/evaluate_real_documents.py evaluate` reports `NOT MEASURED`. The 97-field
synthetic evaluation (`tests/eval/structured_extraction_labels.json`) is unchanged and
measures only layouts this project wrote itself.

## 1. What goes where

| Item | Where | In git? |
|---|---|---|
| Documents, manifest, labels, freeze file, reports | `eval_real/` (or `--dir` elsewhere) | **never** (ignored; the tool refuses a directory inside a repository that does not ignore it) |
| Harness, protocol, the two example files here | `src/real_eval.py`, `scripts/evaluate_real_documents.py`, `docs/real_world_evaluation/` | yes |

Access to `eval_real/` is limited to the people named in the manifest as labellers and
verifiers. Do not copy documents into the repository, a chat, a ticket or a screenshot.

## 2. Which documents (the sample)

Documents we have the right to process, redacted wherever possible. For each, record in
`manifest.json` (see `manifest.example.json`):

- `document_type`: BOL, POD, INVOICE, RATE_CONFIRMATION, or OTHER (for a negative example)
- `layout_family`: a pseudonymous label for the layout (`family-07`), **never** a carrier or
  customer name
- `split`: `development` or `heldout` (section 5)
- `source_permission`: `basis` (own_document, customer_consent, licensed, public_domain,
  synthetic), who confirmed it, and the date. A document without all three is not processed
- `redaction`: `none`, `partial` or `full`, and the method (what was removed and how)
- `properties`: any of multi_page, table_layout, credit, previous_balance, tax, fuel_surcharge,
  foreign_currency, unusual_charge_description, repeated_fields, conflicting_values,
  missing_fields, ambiguous_signature, image_only, encrypted, malformed, partial_pages
- `limitations`: anything that makes the document unrepresentative (free text, no content)

Aim for the minimums in `real_eval.COVERAGE` (for example 20 verified documents and 4 layout
families per document type, and at least 5 image-only, 5 conflicting-value and 5 multi-page
documents). They are a judgement about what makes a first report worth reading, **not** a
statistical guarantee. `coverage` lists what is still missing. Do not assume every document
has every field: label absent fields as absent.

## 3. Labelling (independent ground truth)

For each document, in `labels.json` (see `labels.example.json`):

1. The **labeller** reads the PDF only. They must not run the extractor, open its output or the
   reconciliation result first. They set `labelled_without_extractor_output: true` to attest it.
2. For every field they can judge, they write the **state** and, when it is there, the **value**
   and the **page** (1-based) it is on:
   - `EXTRACTED`: the field is on the document; give `value` and `page`
   - `MISSING`: the document readable and the field is not there
   - `UNREADABLE`: the field may be there but cannot be read (scan, blank page, garbled)
   - `UNPARSEABLE`: it is there but is not a valid value of its kind (`TBD`, an impossible date)
   - `UNSUPPORTED`: the extractor has no way to read this kind of field (for example a drawn signature)
   - `CONFLICTING`: the document gives different values for it (give none)
   Typed values: dates as `YYYY-MM-DD`; amounts and counts as numbers; weights as
   `{"amount": 12500, "unit": "lb"}`; parties as `{"name": "...", "address_lines": ["..."]}`.
   Where the page cannot be known write `"page_unknown": true`.
3. Document level: `document_status` (extracted, partial, needs_ocr, unreadable), whether it
   should be accepted without review (`expected_complete`), for invoices the `arithmetic`
   expectation (`match`, `mismatch`, `unchecked`; line items against the stated total, with tax),
   and optionally `line_items` as `[description, amount]` pairs.
4. A **different person**, the verifier, checks every label against the PDF and writes
   `verified_by` and `verified_on`. The two names must differ. Disagreements are settled by
   the verifier and a third person if needed; the final label is what the document says, not
   what the extractor says.
5. Labels are kept in `labels.json` only, apart from the implementation. Never edit a label to
   make a score better. A label found wrong is corrected for a stated reason and the correction
   is noted in `notes`; if the set was frozen, it is not independent any more (section 5).

The harness scores a document only when it is valid, verified by a second person and attested.
Everything else is listed as excluded or not scored, with the reason.

## 4. What is measured

Every figure carries its numerator and denominator and a Wilson 95% interval. A figure with
fewer than 10 labelled cases is printed as `not measured`, with the counts, instead of a rate.

- exact-match accuracy of typed values, overall, by document type, by field, by value kind
  (money, count, weight, date, text, party), and by layout family
- numeric parsing and date parsing accuracy (subsets of the above)
- currency: correct code, and **assumed when the document does not state one** (should be 0)
- conflict detection recall and false conflicts
- **false extraction**: a field that is missing, unreadable, unparseable, unsupported or
  conflicting reported as extracted; and a wrong value reported as extracted
- missing-field detection, and the full expected-to-observed state confusion matrix
- provenance: page correct, and a page given where the label says it cannot be known
- document type recognised; document status; review state (a document that should need review
  reported as complete is the dangerous error)
- invoice arithmetic: agreement, mismatch recall and mismatch precision
- failures on malformed, encrypted and image-only files; crashes (there should be none)

Failure examples show document ids, field names and the **shape** of values (`9999-99-99`,
`Aaaa 99`), not the values. `--show-values` prints real values to the console only.

## 5. Independence (do not tune on the held-out set)

- Split the documents by **layout family**, not at random, so the held-out set contains
  layouts the extractor was not developed on. Develop and tune only on `development`.
- `freeze` records the manifest, the labels and the extractor version, and locks the held-out
  set. It cannot be repeated for the same directory.
- `evaluate --split heldout` works only after a freeze, and appends to `heldout_runs.jsonl`.
- The report calls a held-out score **independent** only when the labels, the manifest and the
  extractor are unchanged since the freeze and no other extractor version has scored the set.
  Otherwise it prints `NO` with the reasons, and the number must be reported as a development
  score. Once the held-out set has guided any change, replace it with fresh documents.

## 6. Reporting rules

- Report the sample: how many documents, which types, how many layout families, how they were
  obtained, and the redaction status. Do not describe a convenience sample as representative of
  the logistics industry.
- Report denominators with every figure. Say "not measured" for anything below the threshold.
- Put reports in `eval_real/reports/` (redacted by default). Do not paste documents, values,
  file names or customer names into reports, logs, issues or screenshots.
- Keep the synthetic evaluation separate and label it as synthetic wherever it is quoted.
