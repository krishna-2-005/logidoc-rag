# LogiDoc-RAG

Question answering, reconciliation and exception reporting over freight
documents (Bills of Lading, Proofs of Delivery and freight invoices),
running locally with Ollama and ChromaDB.

**Intended users:** logistics and freight-audit staff who need to check a
shipment's paperwork quickly ("How many pieces were delivered?", "Was it
late?", "Does the invoice match?") and see the exact document behind each
answer.

**Status:** prototype. See [Limitations](#limitations).

## How it works

```
PDF upload ──► ingestion_manager ──► data/raw/*.pdf  +  chroma_db (vectors)
                 │  SHA-256 duplicate checks, journal, rollback
                 ▼
             document_loader (PyMuPDF text, document type, shipment ID)
             chunker → vector_store.create_embedding (Ollama)

Question + selected shipment
   │
   ▼
rag_scope.resolve_scope      which chunks may be used (never guesses)
   │
   ▼
rag.search_documents         keyword + vector search, filtered by shipment
   │
   ├─► shipment_calculations (per shipment): quantities, delivery dates,
   │   invoice totals, calculated in Python
   ├─► deterministic field extraction (per shipment)
   └─► LLM answer (Ollama) from the scoped chunks
   │
   ▼
rag_grounding.ground_answer  cite only chunks that support the answer;
                             abstain when nothing does
```

- **Scoping.** With a shipment selected, only chunks whose `shipment_id`
  metadata matches are searched, extracted from or shown to the model.
  IDs compare without regard to case, spaces or dashes. Chunks without a
  shipment ID are never used for a selected shipment. "All Shipments" is
  labelled as unscoped. A question naming a different shipment, an empty
  scope, or shipments that disagree produce an abstention or a request to
  clarify instead of a guess.
- **Citations.** Only the chunks that contain the facts stated in the
  answer are cited; the searched passages are listed separately.
- **Ingestion** is incremental and non-destructive (duplicates, changed and
  inconsistent files are reported, never overwritten), with a journal and a
  read-only consistency report (Diagnostics tab).
- **Reconciliation / Shipment Intelligence** compare a shipment's BOL, POD
  and invoice and report shortages, delays, invoice totals that do not
  match their line items, and exceptions. They use the same field readings
  and arithmetic as the chat calculations (`audit_quantity`,
  `audit_delivery`, `audit_invoice` in `src/shipment_calculations.py`). An
  early delivery is EARLY, not a discrepancy. A missing, unreadable or
  conflicting essential value is **NEEDS REVIEW**, never a match, on time
  or CLEAR.

## Calculations

The calculation questions below are recognised by their wording and
answered by `src/shipment_calculations.py` in Python, without the
language model, citing the exact fields used:

| Question type | Fields (document) | Convention |
|---|---|---|
| Pieces expected / delivered / missing / over-delivered / difference; comparisons ("consistent with the BOL?", "how many more were expected than delivered?", "is there a discrepancy?"); "were all pieces delivered?" | `Number of Pieces` (BOL), `Number of Pieces Delivered` (POD) | shortage = expected − delivered; over-delivery = delivered − expected |
| Days late / early, on time, "on time or early", "late or on time" | `Expected Delivery Date` (BOL), `Delivery Date` (POD) | actual − expected in calendar days: positive = late, 0 = on time, negative = early. Delivered early counts as on time, so "on time or early" is answered like "on time"; either-or and negated questions get the result without a Yes/No |
| Days between pickup and delivery | `Pickup Date` (BOL), `Delivery Date` (POD) | actual − pickup in calendar days; a delivery before pickup is not calculated |
| Pieces and timing together ("Were all pieces delivered on time?") | all four fields above | both parts are calculated; Yes only if all pieces were delivered and the delivery was not late |
| Damaged, refused, rejected or returned pieces | a count field such as `Damaged Pieces` (POD) | never the delivered count; without a count field the answer says so (quoting the POD `Damage Report`, if any) |
| Do the invoice line items add up? | `Base Freight`, `Fuel Surcharge`, `Detention Charge`, `Other Accessorial Charges`, `Total Invoice Amount` (invoice) | sum of the four line items compared with the total |

Each field is read only from its own document type (a pickup, billing or
due date is never used as a delivery date), and a POD date labelled
Expected, Promised, Required, Scheduled, Target and similar is not the
actual delivery date. The calculation is **not** done, and the answer
says why, when a field is missing (never treated as 0), has conflicting
values (e.g. two BOLs with different piece counts), or cannot be read
(including dates written in a form that is not unambiguous, such as
`10/05/2026`). For the invoice check, every amount on the invoice, with
or without a `$`, must be one of the known line items or the total; a
summary such as `Amount Due`, `Balance Due` or `Subtotal` is accepted
only when it repeats the total or the line-item sum, and is never added.
Whether a pickup was on time is not calculated (there is no scheduled
pickup date), so such questions are declined rather than answered from
the delivery dates.

With **All Shipments** selected, a calculation is answered only if every
shipment gives the same result. If they differ, you are asked to select
a shipment; if a shipment cannot be calculated (e.g. it has no POD),
the answer names it and the reason instead of reporting a disagreement.

Questions worded in ways the router does not recognise are not
calculations: they go to field extraction or to the language model,
which can then state a calculation itself that nothing checks (see
[Limitations](#limitations)).

## Audit Analytics and exception review

The **Audit Analytics** tab reconciles every identified shipment with
the same `reconcile_shipment()` as the Reconciliation tab
(`src/audit_analytics.py` only counts and adds up its results; nothing
is recalculated) and shows:

- **KPI cards**, each with its definition in the card's help text;
- two charts: final audit status, and unresolved exceptions by category;
- a **shipment audit register** (carrier, route, quantity, delivery and
  invoice status, final status, invoice total and difference, counts)
  with filters for shipment ID (case, spaces and dashes ignored),
  carrier, final status, discrepancy category, invoice mismatch,
  delayed delivery and needs review, and a **Reset filters** button;
- a **shipment drill-down**: quantity, dates, invoice line items,
  discrepancies, review reasons and evidence (source file, page,
  document type, shipment ID, field text), the per-shipment CSV and PDF
  downloads, and a button that selects the shipment in the sidebar for
  the Reconciliation workflow;
- an **exception register** with a review workflow, and CSV downloads of
  the filtered register and exceptions.

Click **Load audit analytics** to start: the PDFs are read once and the
result is cached until a PDF in the document folder is added, removed
or replaced.

| Metric | Definition |
|---|---|
| Identified shipments | distinct shipment IDs in the documents; documents without an ID are listed as a warning and never attributed to a shipment |
| Reconciled | identified shipments with a BOL, invoice and POD that could be reconciled |
| With discrepancies | reconciled, final status `DISCREPANCIES DETECTED` |
| Need manual review | not reconciled, or with at least one value that could not be verified |
| No discrepancies | reconciled, `NO DISCREPANCIES` and nothing to review |
| Total invoice amount | sum of the stated totals that were read; the help text says how many shipments it covers |
| Invoice variance | sum of \|total − line items\| where both are known (net signed value in the help text) |
| % with discrepancies | with discrepancies ÷ reconciled shipments |

A metric that cannot be calculated shows `NOT AVAILABLE`; a missing
value is never counted as 0 or as clean.

**Exceptions.** Each detected discrepancy (quantity shortage or overage,
delivery delay, invoice mismatch, carrier or route inconsistency) and
each value that needs review (including a shipment that could not be
reconciled) is one row, marked *Discrepancy* or *Needs review*.
Severity follows the rules of Shipment Intelligence: HIGH for a
shortage, a delay, an invoice mismatch and an incomplete reconciliation;
MEDIUM for an overage, a carrier or route inconsistency and a value that
needs review.

**Review states** are `open` (every new exception), `in_review`,
`resolved` and `dismissed` (a reason is required). They are stored
apart from the audit, in a SQLite file that holds only the exception
key, shipment, category, a signature of the audited values, the state,
reviewer, note and timestamps (no document text), with an event history:

- `data/audit_reviews.sqlite3` (git-ignored); set `LOGIDOC_REVIEW_DB`
  to use another file;
- every change is one transaction, with parameterised SQL and database
  checks (a dismissal without a reason is rejected by the database too);
- the audit is re-run each time: a resolved or dismissed exception whose
  audited values changed, or that comes back after no longer being
  detected, is **reopened** and marked as needing a recheck; an
  exception that is no longer detected keeps its state (it is not
  resolved by disappearing); showing an exception never resolves it.

The review state does not change any audit result, document or KPI
(the KPIs count audit results, not review states).

## Audit Operations

The **Audit Operations** tab follows the audit workflow with the same
audit results as everywhere else (nothing is recalculated in these
modules). Click **Load audit operations**; documents are read once and
reused until a PDF in the document folder changes.

1. **Document completeness** (`src/document_completeness.py`)
2. **Financial exposure** (`src/financial_exposure.py`)
3. **Audit a shipment**: select a shipment, then
   1. documents: ready, or what is needed to make it auditable;
   2. reconciliation status;
   3. findings, each with an evidence-backed explanation
      (`src/audit_explanations.py`);
   4. review (the same review states as the Audit Analytics tab);
   5. record the audit in the history;
   6. compare with earlier audits;
   7. export the shipment's CSV or PDF report.

The screens are in `src/audit_workflow_ui.py`; the logic is in the
modules named above.

### Document completeness

Each PDF of the document folder is read on its own and gets a status
from what extraction did: `parsed`, `partial` (some pages without
text), `no_text` (an image-only scan; there is no OCR), `empty` (no
pages) or `unreadable` (it could not be opened or analysed; the error is
shown). A file is usable when it is parsed or partial and its content
shows a document type and a shipment ID.

- A shipment is **ready** when a usable BOL, invoice and POD are all
  present, otherwise **incomplete**, and the register says which are
  missing and what to upload. Ready only means the documents are there:
  values can still be missing, conflicting or unreadable (NEEDS REVIEW).
- A file that is not usable has no shipment ID, so it is listed apart,
  never counted as a shipment's document and never claimed to be its
  missing one. "Missing" (no such document) and "exists but could not be
  parsed" are reported separately.
- Warnings (not blocking): partial extraction, more than one document of
  a type, byte-identical files (same SHA-256), documents that are not a
  BOL, invoice or POD, and files that are not in the search index.
- Filters: shipment ID, incomplete only, missing type, extraction issues.
  CSV download of the filtered register.
- **A PDF that cannot be used never stops the audit and never makes a
  shipment look clean.** Reconciliation (the Shipment Reconciliation and
  Shipment Intelligence tabs, Audit Analytics and Audit Operations) reads
  the folder with `chunker.read_chunks`: a file that cannot be opened or
  has no extractable text is skipped, named in the result and in warnings,
  and never attributed to a shipment, not even by its file name. Because it
  might hold a document of any shipment, no shipment is reported as
  `NO DISCREPANCIES` while it exists: the result becomes `NEEDS REVIEW`
  with a *Documents* review item (real discrepancies and every
  calculation stay as they are). If a required document is missing, the
  error says so and names the unusable files; if nothing at all can be
  read, the error says that. Every shipment row of the completeness
  register carries the same warning. The index builders and the command
  line tools still stop on such a file.

### Financial exposure

| Figure | Definition |
|---|---|
| Stated invoice total | sum of the Total Invoice Amounts that were read; an invoice without a readable total is not counted as 0 (the card says how many invoices it covers) |
| Confirmed mismatches | invoices whose line items do not add up to the total |
| Net signed difference | sum of (total − line items) over the mismatches; positive: totals above their line items |
| Absolute variance | sum of \|total − line items\| over the mismatches |
| Total above / below line items | the positive and the negative differences, each as a positive amount |
| Unverifiable invoices | a line item or the total is missing, unreadable or conflicting, or the shipment could not be reconciled |
| Amount under review | sum of the stated totals of the unverifiable invoices whose total was read: the size of what could not be checked, **not a loss** |

A mismatch is an arithmetic finding about the invoice itself. It does
not prove overbilling and is not a recoverable or payable amount, and
nothing in the application says it is. Breakdowns by carrier, by
discrepancy category (only the Invoice category has an amount; the
others are counts) and by shipment follow the filters (shipment ID,
carrier, invoice result, discrepancy category); the filtered rows can be
downloaded as CSV. Amounts are shown as stated in the documents: they
carry a `$` and no currency code, so no currency is checked or
converted. If an invoice result ever carries a currency, amounts of
different currencies are not added together.

### Explanations

Every finding is explained from the audit result and its evidence, with
no language model: expected and observed value, the signed difference,
the comparison rule, source file, page, document type, shipment ID and
field text, why a value needs review, and a suggested next
verification step. Evidence of another shipment is excluded, missing
evidence is stated, long text is clipped, and a conflict is never shown
as a verified value.

An optional **model commentary** can be requested per finding. It is
shown separately and labelled as generated, it is given only the audit's
facts, it cannot change a status, number or review state (the
explanation is checked to be unchanged), and figures in it that are not
in the audit are listed as a warning. It needs the local Ollama model
and is never requested automatically.

### Audit history

History is separate from the audit and from the review states, in its own
SQLite file (`data/audit_history.sqlite3`, git-ignored; set
`LOGIDOC_HISTORY_DB` to use another file). It is written only when you
press **Record audit for this shipment** or **Record audit for all
shipments**; opening the tab or any rerun writes nothing.

- A snapshot holds, per shipment: the statuses, the amounts, counts and
  dates, the findings (key, category, severity, a one-line summary, the
  numeric difference and a signature of the audited values), the state of
  each audited value (`verified`, `unavailable`, `conflicting`,
  `review_required`, `not_assessed`) and an **input fingerprint**, a hash
  of the shipment's documents. It holds no document text and no
  evidence. Missing values are stored as NULL, never 0.
- A run stores a snapshot only for a shipment whose result or input
  changed since its latest snapshot; otherwise the run points at the
  existing one, so recording an unchanged audit never writes a duplicate.
  A changed document gives a new snapshot even if the result is the
  same, and the timeline says the input changed.
- Snapshots are append-only: database triggers refuse any update or
  delete. Each run is one transaction; the schema version is checked.
- The timeline compares each snapshot with the one before it:
  *new*, *persistent*, *changed* (other difference or values),
  *no longer detected* (the audit behind it could be checked),
  *reappeared* (seen in an earlier snapshot), and *cannot be verified
  now* (the audit behind a finding now needs review, or the shipment
  could not be reconciled: this is **not** treated as resolved), plus the
  values that changed and the final status change.

## Production readiness

Four workstreams sit on top of the audit: sign-in and roles, an exception
assignment and approval workflow, historical trends, and reliable ingestion.
A FastAPI layer and a React interface expose the same services; the
Streamlit app keeps working. None of them recalculates an audit.

### Authentication and roles (`src/auth.py`, `src/authz.py`)

| Mode (`LOGIDOC_AUTH_MODE`) | Behaviour |
|---|---|
| `local` (default) | A single-user development mode. Nobody signs in; every action is recorded as `local-dev` and **not authenticated**. The API only starts on a loopback address. |
| `required` | Every use needs a signed-in account. There is no self-registration. |

`LOGIDOC_ENV=production` refuses `local` mode, hides the interactive API docs
and sets Secure cookies.

- Passwords: salted scrypt hashes (`scrypt$n$r$p$salt$hash`), at least 12
  characters; a password is never stored or logged.
- Sessions: a random token whose SHA-256 only is stored; 30 minutes idle and 8
  hours absolute lifetime; ended by logout, a role or password change, or
  deactivating the account (the next request fails). Five wrong passwords lock
  the account for 15 minutes; an unknown user and a wrong password answer
  identically.
- Roles: **Administrator** (users, history recording, ingestion, dismissals,
  approvals, integrity checks) and **Auditor / reviewer** (view, assign to
  self, start, note, resolve, reopen, download reports). Every service call
  checks the permission itself (`authz.require`), so hiding a button is never
  the only protection. The last active administrator cannot be removed.
- Create the first administrator on the server (no password on the command
  line):

```powershell
$env:LOGIDOC_AUTH_MODE = "required"
python scripts/manage_users.py create alice --role admin
python scripts/manage_users.py create bob --role reviewer
python scripts/manage_users.py list
```

**What this does not give you:** transport security (serve everything over
HTTPS from a reverse proxy; the application never sees TLS), protection from
someone who can write the database files or run code on the server, password
recovery by e-mail, MFA or SSO. If you need those, put an identity provider in
front of the app; `authz.Principal` is the one interface to connect it to.

### Exception workflow (`src/review_store.py`, `src/exception_workflow.py`)

`Open -> In review -> Resolved | Dismissed`, and back to `Open` by reopening.
Rules (enforced inside the database transaction, not only in the screen):

| Action | Rule |
|---|---|
| Start review | Only an open exception; assigns it to you unless already assigned |
| Resolve | Only from *In review*; a note (default 10+ characters) **and** at least one evidence reference |
| Dismiss | Administrators only; a reason note |
| Reopen | A note |
| Approve / reject | Only with `LOGIDOC_REQUIRE_APPROVAL=1`; administrators; never the person who resolved it (the single `local-dev` user excepted); rejecting needs a note |

Evidence references are short strings (a document and page, a credit memo, a
ticket), not document text. Every event records the signed-in user, their
role and whether they signed in. `review_events` cannot be updated or deleted
(triggers) and the events form a hash chain (`verify_integrity`,
Administration). The chain detects accidental or naive edits; it cannot
protect against someone who can rewrite the whole database file. Resolving
never changes a reconciliation result, and a resolved exception is reopened
if the audited values change.

Server-side settings only: `LOGIDOC_REQUIRE_APPROVAL`, `LOGIDOC_MIN_NOTE_LENGTH`.
In `local` mode the Streamlit analytics tabs keep the original quick review
form (free-typed reviewer name, marked unauthenticated and not subject to the
evidence rule); in `required` mode it is replaced by the full workflow.

### Historical trends (`src/trend_analytics.py`)

Computed only from recorded audit runs (the audit history), never from the
current documents. A shipment counts once per period (week, month or quarter
of the *audit run*) with its latest audit in it; an unchanged shipment audited
again still counts.

- Delay, shortage, invoice-mismatch and discrepancy rates come with their
  sample size and a 95% Wilson interval. A shipment whose value is missing,
  conflicting or unreadable is left out of that rate's *n*, never counted as
  on time, complete or zero.
- Invoice variance is an **arithmetic mismatch** (total versus line items),
  never "overbilling" or a recoverable amount. Amounts are added only within
  one currency; documents state none today, so the group is "Not stated".
- Carrier scorecards rank by discrepancy rate only with at least 5 decided
  shipments (adjustable); smaller samples are shown but not ranked, and
  overlapping intervals are called out. Shipments without a carrier are never
  ranked.
- Filters: carrier, final status, discrepancy category, audit date range;
  CSV export of the filtered analytics (formula-injection safe).
- With no history, or only one period, the screen says so; nothing is drawn.

Audit history schema version 2 adds `carrier` and `currency` to
`shipment_snapshots`. Older snapshots keep NULL and show as "Not stated".
The carrier name is stored as a structured field (like the shipment ID), never
the text around it.

### Reliable ingestion (`src/document_validation.py`, `ingestion_register.py`, `ingestion_service.py`, `document_lifecycle.py`)

- **Validation** before anything is written: name (no paths, no reserved
  names), size (`LOGIDOC_MAX_UPLOAD_MB`, default 25), PDF signature, damaged,
  password-protected, no pages, page limit, and image-only scans (reported as
  `unsupported: needs OCR`; **there is no OCR**).
- **Per-file status** in a separate register: `queued`, `processing`,
  `indexed`, `partial` (pages without text, no shipment ID, unknown type),
  `failed`, `unsupported`, with reason and corrective action. A duplicate
  (same content hash) is recorded as `indexed` with code `duplicate`.
- **Retries** only for transient failures (service unreachable, locked
  database), with growing pauses; each failed attempt is rolled back and
  vector IDs come from the content hash, so a retry cannot index a file twice.
  A file that keeps failing is kept (named by its hash) for a manual retry.
- **No false clean audit:** an open `failed`/`unsupported` file is held against
  every shipment like an unreadable PDF (the result becomes `NEEDS REVIEW`)
  until it is corrected, retried or resolved with a written note.
- **Replace / remove / reprocess** (administrators): the original PDF is copied
  to `data/archive/` and verified first; the new vectors are built before
  anything is swapped; on any failure the old vectors and PDF are restored.
  Originals are never deleted or modified automatically.
- **Cache consistency:** `corpus_signature` (documents + index IDs) changes on
  every add, replace or remove; the API's caches are keyed by the document
  state and the open ingestion errors.

### Structured extraction (`src/document_extraction.py`, `src/extraction_store.py`)

`extract_document(path)` reads one PDF into a typed `DocumentExtraction`:
document type, status (`extracted`, `partial`, `needs_ocr`, `unreadable`),
typed fields, invoice line items and the reasons it needs review. It is
deterministic (label/value pairs on the page text; no language model, no
network) and never raises for a damaged or scanned file.

| Type | Fields |
|---|---|
| BOL | shipment_id, shipper, consignee (name + address lines), carrier, origin, destination, pickup_date, expected_delivery_date, piece_count, weight (amount + unit), equipment, commodity |
| POD | shipment_id, carrier, delivery_date, delivered_pieces, delivery_status, received_by, signature (printed name), damage_report; signature_image is UNSUPPORTED |
| INVOICE | shipment_id, invoice_number, invoice_date, due_date, payment_terms, carrier, origin, destination, currency, subtotal, tax, invoice_total, and any number of line items (description, amount, optional quantity x rate, page); see "Invoice charges" below |
| RATE_CONFIRMATION | shipment_id, carrier, shipper, consignee, origin, destination, pickup_date, delivery_date, equipment, weight, currency, agreed_rate, fuel_surcharge, payment_terms, terms |

Every field has a state: `EXTRACTED` (with a typed value), `MISSING` (not on
the document), `UNPARSEABLE` (present but not a valid value; the text is
kept), `UNREADABLE` (some pages have no text, so absence cannot be claimed),
`UNSUPPORTED` (the extractor cannot read it) or `CONFLICTING` (different
values; the alternatives and their pages are listed and none is chosen). A
missing value is `None`, never 0 or an empty string. Provenance per field:
source file, page (only when known, never guessed), method, validation result
and warnings. There is no numeric confidence: nothing is calibrated, so none
is claimed; `basis = "exact_label"` says why a value is trusted.

A field written more than once with different values, or once readable and
once not, is `CONFLICTING`. A repeated label without a value, or a value with
more lines than the field takes (one for dates, amounts, counts, codes and
terms; four for a shipper or consignee; a few for commodity, damage report and
terms), keeps the value but marks the field `needs_review`, so the document is
not complete. Warnings and review reasons name the field, the problem and the
page; they never quote the document.

Invoice charges: a line item is a labelled amount with a currency mark ($, EUR,
GBP, US$ or an ISO code before or after it). Labels that say the number is
something else (counts, weights, identifiers, dates, amounts paid, deposits,
balances) are listed as excluded. A number without a currency mark, and any
discount, rebate, allowance or adjustment, is listed as unclassified: the
invoice needs review and its total is not checked against the line items.

Currency is never assumed: an explicit code or a symbol naming one currency
(EUR, GBP, US$) is read; a bare `$` leaves the currency MISSING with a
warning. A document that names two shipments is attributed to neither.
`validate_shipment()` reports shipment, carrier, route and currency
mismatches, invoice arithmetic differences and duplicate files; it resolves
nothing. The audit calculations still use their own field readers
(`shipment_calculations.read_field`, `reconciliation.extract_field`); a test
checks that both agree with the new extractor on the fixture shipments.

Results can be stored in `extractions.sqlite3` (one row per file hash and
extractor version; reprocessing is a no-op). The extractor version is a schema
number plus a fingerprint of the extraction source files, so any change to the
parsing stores new results instead of reusing old ones. Stored results contain
typed values, states, pages, labels and reason codes; the raw text snippets are
dropped.

**Corrections and approval.** A reviewer *proposes* a correction
(`POST /api/extractions/{id}/corrections`, reviewer or admin). It is parsed by the
same parsers, needs a reason, records who, when, the previous value and the
correction it was based on (`based_on`, required, `null` when there was none;
a stale request gets 409), and has **no effect on anything** until a different,
signed-in administrator approves it
(`POST /api/extractions/corrections/{id}/decision`, `approve` or `reject`; a
rejection needs a reason). The proposer cannot decide their own correction,
a correction is decided once, and one proposal per field can be pending. The
extracted value is never overwritten; the extraction view keeps the extracted,
proposed and approved values apart. Shipment IDs cannot be corrected. Running an
extraction is admin only. Corrections made before approvals existed (schema < 4)
are marked **legacy**: they never apply and cannot be approved; propose them again.

**What an approved correction changes.** Only an approved correction reaches an
audit, and only for the fields a calculation reads: BOL `piece_count` and
`expected_delivery_date`, POD `delivered_pieces` and `delivery_date`, INVOICE
`invoice_total`. They replace the document reader's value for that shipment in the
quantity, delivery and invoice audits (and so in the financial exposure and the
assistant). Charges by label, carrier, route and completeness still use the
original readers; other approved fields are recorded but consumed by nothing.
A correction applies only while its document is unchanged. Unresolved values (missing,
conflicting, unparseable) stay unresolved; the audit still says NEEDS REVIEW. See
`docs/CORRECTION_AWARE_AUDIT.md`.

**Audit inputs and history.** Every recorded run stores, per shipment, the inputs
its calculations consumed (`GET /api/history/runs/{id}/inputs`): the value, whether
it came from the document reader or an approved correction, the correction,
extraction and extractor version, the document, the page (null when unknown),
validation, warnings and the calculation version. Approving a correction makes the
next read recalculate, and records a targeted run for that shipment that
*supersedes* its earlier runs (kept, with their inputs); other shipments are not
touched. If recording fails the approval stands, `GET /api/extractions/staleness`
lists the shipment, and `POST /api/extractions/recompute` records it (safe to repeat, or
to call from two places at once: the check and the write are one transaction, so a
recalculation is recorded once). Runs from
before inputs were recorded have none; they are not reconstructed. The assistant
adds a note, and marks the citation `value_origin: approved_correction` with an
unknown page, when a result uses an approved correction.

Results can be stored in `extractions.sqlite3` (one row per file hash and
extractor version; reprocessing is a no-op). The extractor version is a schema
number plus a fingerprint of the extraction source files, so any change to the
parsing stores new results instead of reusing old ones. Stored results contain
typed values, states, pages, labels and reason codes; the raw text snippets are
dropped.

If the extraction store is damaged or from a newer version, the audit still answers from
the documents but says so: each shipment's inputs carry `corrections_unavailable`, a
recorded run notes `correction_store_unavailable`, `GET /api/extractions/staleness` reports
`corrections_available: false` and the assistant adds a note. It never looks like "no
correction was ever approved". Row ids outside 1 to 2147483647 are refused with 422.

Stored extractions cannot be updated or deleted; corrections and decisions are
append-only (SQLite triggers) and hash-chained, and every stored result has a
hash. `GET /api/extractions/integrity` (administrator) recomputes them and also
reports an approval made by the proposer. This detects changes made with ordinary
SQL; it is not tamper-proof against someone who can write the database file (they
can drop triggers, recompute hashes, remove the newest rows or replace the file).
Rows written before the hashes existed are reported as unchained legacy rows.

**Reviewer interface.** The React app has a reviewer interface for all of this; it only
calls the endpoints above and never decides anything itself (the server is the authority):

- *Shipment workspace, Extractions section* (`ExtractionPanel`): per document, every field with its
  typed value, source file, page (or "page not recorded"), validation, warnings and
  needs-review flag; missing, conflicting, unreadable, unparseable and unsupported values are named
  in words, never drawn as 0; the original, proposed, approved, rejected and legacy values are shown
  apart. Raw document text is never displayed. Reviewers and administrators can propose a correction
  (`CorrectionDialog`: a required reason, the correct `based_on`, a 409 asks the user to reload the
  latest state before submitting again, and no double submission). Shipment ID corrections are not
  offered. Administrators can extract a document that has no stored extraction.
- *Status* (`RecomputeStatus`): a warning when the correction store cannot be read ("Corrections
  cannot be verified right now"), whether the latest recorded run lacks approved corrections, a
  retry that records the run (shown as successful only when the server says so), and an
  administrator's integrity check.
- *Correction approvals* (administrators; the entry is hidden from reviewers and the page refuses
  them): the pending queue with shipment, document, field, original and proposed values, reason,
  proposer, version, and Approve / Reject (a rejection needs a reason). A proposer's own approve and
  reject buttons are disabled with the reason, because the server refuses them too. The outcome
  of each decision, including a failed run recording with its retry, comes from the server's reply.
- *Audit History, Recorded runs*: calculation version, "Superseded by run #N" / "Supersedes run #M"
  links, a stale marker, and an inline inspector of each run's recorded inputs (value, source,
  correction, document, page) and the result it recorded. A run recorded before inputs were
  recorded says so; nothing is reconstructed.

The interface mirrors one server fact for explanation only: which calculation reads an approved
field (`consumerOf`, the same five fields as `audit_inputs.TARGETS`); the server decides. On a phone the
field table scrolls inside its card.

Limitations: no OCR; tabular (column) layouts and line items printed as
table rows are not read; labels outside the known list are not recognized;
the measured accuracy (see the evaluation) is on synthetic and
project-authored PDFs only and is not evidence of accuracy on real carrier
documents.

### HTTP API (`src/api`) and React interface (`frontend/`)

```powershell
python src/api/serve.py                      # API on http://127.0.0.1:8000
cd frontend; npm install; npm run dev        # UI on http://localhost:5173 (proxies /api)
npm run build                                # production build -> frontend/dist
python src/api/serve.py                      # now also serves the build at /
npm test                                     # frontend tests (Vitest)
```

| Variable | Meaning |
|---|---|
| `LOGIDOC_API_HOST` / `LOGIDOC_API_PORT` | Listen address (default `127.0.0.1:8000`; a non-loopback host needs `required` mode) |
| `LOGIDOC_CORS_ORIGINS` | Comma-separated origins allowed to call the API (`*` is refused: cookies are used) |
| `LOGIDOC_COOKIE_SECURE` | `1` to mark the session cookie Secure (implied by `LOGIDOC_ENV=production`) |
| `LOGIDOC_FRONTEND_DIR` | The React build to serve (default `frontend/dist`) |
| `LOGIDOC_DATA_DIR` | Documents and databases (default `data/`); the demo runbook points it at an empty folder |
| `LOGIDOC_CHROMA_DIR` | The vector index (default `chroma_db/`) |
| `LOGIDOC_AUTH_DB`, `LOGIDOC_REVIEW_DB`, `LOGIDOC_HISTORY_DB`, `LOGIDOC_INGESTION_DB`, `LOGIDOC_EXTRACTION_DB` | Single database locations (each wins over `LOGIDOC_DATA_DIR`) |
| `LOGIDOC_OLLAMA_CONNECT_TIMEOUT`, `LOGIDOC_EMBED_TIMEOUT`, `LOGIDOC_LLM_TIMEOUT` | Seconds one Ollama call may take (5, 60, 120) |

`.env.example` lists every variable with its default. The app reads the
process environment only; nothing loads a `.env` file.

Security of the API: HttpOnly + SameSite=Strict session cookie; every change
needs the `X-LogiDoc-Client` header (CSRF); CORS limited to the configured
origins; no route takes a file system path (documents are chosen by name from
the server's own listing); request bodies validated; uploads size-capped;
errors never carry a stack trace or a path; the settings route lists a fixed
set of harmless values. In production the API serves the build, and in
development Vite proxies `/api`, so the page and the API share one origin.

Endpoints, all under `/api`: `session`, `auth/login`, `auth/logout`,
`overview`, `shipments`, `shipments/{id}`, `documents`, `documents/file`,
`completeness`, `exposure`, `exceptions` (and `/{key}`, `/{key}/actions`,
`/{key}/history`, `/activity`), `history/runs`, `history/shipments/{id}`,
`history/compare`, `trends`, `trends/export.csv`, `reports/...`,
`ingestion/register`, `ingestion/errors`, `ingestion/upload`,
`ingestion/{id}/retry`, `ingestion/{id}/resolve`, `documents/reprocess`,
`documents/replace`, `documents/remove`, `chat`, `settings`, `admin/users`,
`admin/events`, `admin/verify-history`, `health`, `ready`.

`GET /api/health` only says the server answers. `GET /api/ready` checks what a
demo or a deployment needs: the document folder can be listed, the data
folders are writable, the extraction store is absent or current, and Ollama
answers with both models installed. It returns `ready` or `degraded` (200;
degraded means documents can be read but a model or the store is missing) or
`not_ready` (503), with status words only, never paths or error text.

React screens: Overview, Shipment Explorer (and detail), Document
Intelligence (documents, ingestion, ask), Freight Audit, Exception Center,
Audit History (trends, shipment history, runs), Document Completeness,
Reports, Settings. Language-model answers are labelled "Generated" and kept
apart from the verified findings. The interface only displays what the API
returns; it never calculates an audit.

### Databases

| File (default in `data/`) | Schema | Content |
|---|---|---|
| `audit_reviews.sqlite3` | v2 (migrates v1 in one transaction) | `exception_reviews` (state, assignee, evidence references, approval), `review_events` (append-only, hash-chained) |
| `audit_history.sqlite3` | v3 (migrates v1, v2) | append-only runs, snapshots (with carrier, currency), findings, and per run the consumed `run_inputs`, `run_input_sets` and `run_supersessions` |
| `auth.sqlite3` | v1 | `users` (password hashes), `sessions` (token hashes), `auth_events` |
| `ingestion_register.sqlite3` | v1 | `ingestion_files`, append-only `ingestion_events` and `ingestion_actions`; metadata only, never file contents |
| `extractions.sqlite3` | v5 (an older file is never upgraded by opening it: it reads as "migration pending" until `python scripts/migrate_extraction_store.py` makes a verified backup and migrates in one transaction; `--dry-run` first) | `extractions` (typed fields per file hash and extractor version, no raw text, payload hash; no update/delete), append-only hash-chained `field_corrections` (proposals) and `correction_decisions` (one approval or rejection each), append-only `extraction_aliases` (every file name a content was seen under) |

All databases are local runtime data and are git-ignored, as are
`data/quarantine/` and `data/archive/`. A database with a newer schema than
the code is refused, not guessed at.

### Deploying

1. Python 3.10 (3.12 is also tested), `pip install -r requirements.txt`, Ollama running with both models.
2. `npm ci && npm run build` in `frontend/`.
3. Set `LOGIDOC_ENV=production`, `LOGIDOC_AUTH_MODE=required`,
   `LOGIDOC_CORS_ORIGINS` (only if the UI is served from another origin) and
   create the first administrator with `scripts/manage_users.py`.
4. Run `python src/api/serve.py` behind a TLS-terminating reverse proxy (nginx,
   Caddy, IIS). Do not expose it without TLS. Streamlit (`app.py`) can run the
   same way with `LOGIDOC_AUTH_MODE=required`, but it has its own server: do
   not publish it without the same proxy.
5. One process, one writer: do not run two ingestion processes on one folder.
   Back up `data/` (databases, raw PDFs, archive) and `chroma_db/`.

## Setup (Windows, PowerShell)

Requires Python 3.10 and [Ollama](https://ollama.com/download). The same
pinned requirements also install and pass the test suite on Python 3.12.

```powershell
cd D:\LogiDoc-RAG
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt   # app + pytest

ollama pull nomic-embed-text
ollama pull nemotron-3-nano:4b
```

Model names are set in `config.py` (`EMBEDDING_MODEL`, `LLM_MODEL`).

## Run the app

```powershell
.\venv\Scripts\Activate.ps1
python -m streamlit run app.py
```

Use `python -m streamlit` so the venv's Streamlit (with `chromadb`) is
used. Upload PDFs in the sidebar and click **Process Documents**.

The React interface is served by `python src/api/serve.py` (see "HTTP API"
above). Run one of the two writers at a time on one data folder: do not
upload or change documents in Streamlit while the API server runs.

For a presentation, follow [docs/DEMO_RUNBOOK.md](docs/DEMO_RUNBOOK.md): it
runs the API on an empty demo folder (`LOGIDOC_DATA_DIR`,
`LOGIDOC_CHROMA_DIR`), so your own documents are never touched.

### Smoke test

```powershell
.\venv\Scripts\python.exe scripts\smoke_test.py
```

Starts the real server on a temporary folder and a free port, then checks
health, readiness, the React build, the upload of the six sample PDFs, the
audit result of both sample shipments, an extraction, a cited assistant answer
and the PDF report, and stops the server. Exit `0` = everything passed (a
check that needs Ollama is a warning when Ollama is not running), `1` = a
check failed, `2` = the server could not be started. It never reads or writes
`data/` or `chroma_db/`.

## Tests

All tests use temporary folders and indexes; `data/raw` and `chroma_db`
are never touched (checked automatically after every session).

```powershell
python -m pytest -q                   # everything; Ollama tests skip with
                                      # the reason if Ollama is unavailable
python -m pytest -q -m "not ollama"   # offline tests only
python -m pytest -q -m ollama         # only the tests needing Ollama
```

To prove the offline tests never call Ollama, point the client at a dead
address: `$env:OLLAMA_HOST = "http://127.0.0.1:9"; python -m pytest -q`.

Three tests need Ollama (real embeddings in ingestion, the full rebuild
and the upload workflow); everything else, including all RAG scoping,
grounding and calculation tests, runs offline with deterministic stand-in
embeddings and a fake language model. Results on 2026-10-26: 1723 passed
with Ollama on Python 3.10, and 1723 passed on Python 3.12 (853 on 2026-10-21;
574 before the production-readiness work); with Ollama unreachable the
same three tests skip. Frontend: `cd frontend; npm test` (199 tests),
`npm run typecheck`, `npm run build`.

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.10 and 3.12 with
Ollama unreachable, `pip check`, the deterministic evaluation, and the
frontend typecheck, tests, build and `npm audit`.

## Real-document extraction evaluation

The extraction scores above come from synthetic, project-authored documents. A separate,
reproducible harness measures the extractor on **real documents we have permission to process**,
against labels a person wrote and a second person verified without seeing the extractor's output:
`src/real_eval.py`, `scripts/evaluate_real_documents.py` (`template`, `validate`, `coverage`,
`freeze`, `evaluate`) and `docs/real_world_evaluation/` (the labelling protocol, example files and the
known gaps).

**Status: no real-world result exists.** No permitted real documents have been supplied, so
`evaluate` reports `NOT MEASURED` and lists the data still required (documents per type, layout
families, multi-page, table, credit, foreign-currency, conflicting, image-only, encrypted and
malformed examples). Documents, manifest, labels and reports live in `eval_real/`, which is git-ignored;
the tool refuses a directory inside a repository that does not ignore it. Reports name documents by
id only and show values as shapes. Every metric carries its denominator and a Wilson 95% interval;
fewer than 10 labelled cases is "not measured". The held-out split can be scored only after `freeze`, and
the report calls a held-out score independent only if the labels, manifest and extractor are unchanged
since the freeze. `tests/test_known_extraction_defects.py` is an executable register of defects found with
synthetic probes (none observed on real documents).

## Assistant evaluation suite

A repeatable, **deterministic** evaluation of the AI Logistics Assistant, the
extraction and the RAG retrieval. It needs no network, no Ollama and no
hosted model, and it never touches `data/raw`, `chroma_db` or any real
database: the corpus is built in a temporary folder from the fixture PDFs and
synthetic PDFs.

```powershell
python scripts/evaluate_assistant.py                      # everything, human-readable
python scripts/evaluate_assistant.py --json reports/eval.json     # also write JSON (refuses to overwrite without --force)
python scripts/evaluate_assistant.py --no-rag             # skip the slower RAG part (its thresholds show "skipped")
python scripts/evaluate_assistant.py --min routing_intent_accuracy=0.9
python -m pytest tests/test_assistant_eval.py             # tests of the suite itself
```

Exit code `0` = every threshold passed, `1` = a threshold or a mandatory
safety check failed, `2` = malformed dataset, bad option or refused overwrite.
`reports/` and `eval_report*.json` are git-ignored; no baseline is tracked.

**Datasets** (`tests/eval/`, versioned, scope stated in their first line):
`assistant_cases.jsonl` (labelled questions: expected intent, response status,
facts, citations, forbidden text, safety flag, ambiguity flag),
`extraction_labels.json` (documents to build and expected field values) and the
existing `questions.jsonl` (RAG, 40 questions). They are written by the
project for the project: a score says the system does what the labels say, not
that it is accurate on real documents.

**What is measured, and how**

| Section | Metrics |
|---|---|
| Assistant routing | intent accuracy, per category, confusion matrix, misrouted case ids. The routing metric covers the question-to-intent step only. |
| Assistant answers | status accuracy; supported questions answered; unsupported ones not answered; false and missed answers; facts and finding values compared field by field (no whole-sentence match); arithmetic mismatches never described as overbilling; generated text flagged |
| Citations | every cited file exists, its type is right, its page is valid **or unknown** (never invented), expected sources are cited, no other shipment is cited |
| Security / reliability | unauthenticated and forged principals, cross-shipment tool calls, invalid tool names and arguments, citation leakage, prompt injection (question and document text), model timeout / empty / missing output, duplicate requests, mixed currencies, missing values |
| Extraction | field accuracy, precision, recall, missing-field detection (a label can say a field is genuinely absent), source filename/page attribution, document type and status (parsed, partial, image-only, corrupt), duplicates |
| RAG (offline lexical embeddings) | recall@5, precision@5, hit@1, citation precision, abstention accuracy, cross-shipment leakage; questions that need the language model are not measured offline and are excluded |

Rules: a metric is `n/a` (never 0) when no label supports it; cases marked
`ambiguous` are reported separately with their observed outcome and never
counted as passed or failed; cases marked `safety` and the leakage and
citation checks are **mandatory**, and a missing mandatory metric also fails.
Fields the extractor does not output (shipper, consignee, invoice number,
currency, tax, field confidence) and OCR are listed as not measured; reconciliation needs a BOL, an
invoice and a POD, so incomplete shipments have no field values to score.
Other thresholds (`--min NAME=VALUE`) have defaults in
`assistant_eval.DEFAULT_THRESHOLDS`; they are conventions chosen for this
project, not industry targets.

**Dataset sets and comparability.** `assistant_cases.jsonl` (v1.0, 54 cases) is
frozen: its labels never change, so it stays comparable with every earlier
run. `assistant_cases_v2.jsonl` (additions) and `assistant_cases_v3.jsonl`
(held-out) are reported separately (`by_set` in the JSON, one line per set in
the text report). A set stops being held-out once the classifier is changed in
response to its failures; the dataset's own `_meta.scope` says how it was
labelled. Cases flagged `ambiguous` are never scored; `safety` cases are
mandatory.

**Intent classifier** (`src/intent_classifier.py`): deterministic and offline.
It normalizes and stems the question, scores weighted cues per intent
(phrases are matched on whole words, never as substrings), and returns one
intent, `none` (unsupported topic, an action request such as "pay", "send" or
"delete" because the assistant is read-only, or no signal) or `ambiguous` (two
requests in one question, or tied signals), for which the assistant asks which
one is meant instead of guessing. It only chooses a tool family: shipment
scope, authorization and argument validation are unchanged and run first.

The live-model mode is not implemented: model-based evaluation would be
non-deterministic and billable, so it is kept out of this command. Use
`python scripts/evaluate.py --ollama` for the existing live RAG evaluation.

## Evaluation

`tests/eval/questions.jsonl` holds 40 questions about the two sample
shipments (24 factual, 8 reasoning, 4 unanswerable, 4 cross-shipment
traps), each with the expected answer, source page and whether the system
should abstain.

```powershell
python scripts/evaluate.py            # offline: lexical embeddings, no LLM
python scripts/evaluate.py --ollama   # real embedding and language models
```

Offline mode uses deterministic lexical embeddings and no language
model: it measures retrieval, scoping, calculations and deterministic
answers, and reports the 13 questions that need the model as "not
measured" instead of scoring them. `--ollama` uses `nomic-embed-text`
and `nemotron-3-nano:4b` and measures all 40.

Results measured on 2026-10-15 (this machine, 40 questions, final code):

| Metric | Offline | Ollama | Target |
|---|---|---|---|
| Cross-shipment leakage | 0.00 (n=39) | 0.00 (n=39) | 0 |
| Retrieval recall@5 | 1.00 (n=32) | 1.00 (n=32) | ≥ 0.90 |
| Citation precision | 1.00 (29 citations) | 1.00 (38 citations) | ≥ 0.90 |
| Abstention accuracy | 1.00 (n=27) | 1.00 (n=40) | ≥ 0.75 |
| Answer match | 1.00 (n=23) | 1.00 (n=32) | — |
| Retrieval latency, mean / p95 | 5 / 7 ms | 68 / 354 ms | — |
| Generation latency, mean / p95 | not measured | 3.4 / 11.4 s (n=13) | — |

All eight reasoning questions (R01–R08) are answered by the
calculations, so their results do not depend on the model. The other
13 questions do: in another Ollama run of the same code that day, the
Ollama server failed on three of them ("CUDA error", "model runner has
unexpectedly stopped") and the model missed a fourth (F10), giving
answer match 0.88 and abstention accuracy 0.90 (all targets still
passed). Results that use the model can vary between runs.

Recall@5 is not informative here: each shipment has only three
single-chunk documents, so a correctly scoped search always returns all
of them. The question set is small, synthetic and was written from the
two sample shipments; these figures do not show how the system performs
on real documents.

## Sample shipments

| | SHJ-2026-001 | SHJ-2026-002 |
|---|---|---|
| Pieces (BOL / POD) | 48 / 45 → **SHORTAGE** of 3 | 60 / 60 → **MATCH** |
| Delivery (expected / actual) | Oct 3 / Oct 5, 2026 → **DELAYED** 2 days | Oct 9 / Oct 8, 2026 → **EARLY** |
| Invoice total | $1,550.00 (line items add up to $1,430.00) | $2,875.50 (line items match) |
| Shipment status | **EXCEPTION** (3 exceptions: quantity, delivery, invoice) | **CLEAR** (0 exceptions) |

The PDFs are in `tests/fixtures/`; SHJ-2026-001 is also the sample in
`data/raw`.

## Limitations

- **Grounding is lexical, not semantic.** A language-model answer counts
  as supported when its dates, amounts, identifiers or names appear in a
  chunk. It does not check arithmetic or whether the answer fits the
  question: a wrong calculation that reuses document values could pass.
  The calculations listed above are therefore done in Python; any other
  calculation, including a calculation question worded in a way the
  router does not recognise, is left to the model.
- **Audit Analytics** covers the PDFs in the document folder, like the
  Reconciliation tab (not the chat index), and shows the current audit
  only (trends are in Audit History / Historical Trends). In local
  development mode the quick review form takes a typed reviewer name, which
  is not verified.
  The analytics CSVs cover the filtered shipments and exceptions only;
  there is no analytics-level PDF. Reconciling many shipments reads every
  PDF once and may take a while.
- **Audit Operations.** History exists only for audits that someone
  recorded: there is no automatic or scheduled recording, and the history database is
  local. The
  completeness register reads text only (no OCR), identifies a document
  by its content (type and `Shipment Number`), and cannot tell which
  shipment an unreadable file belongs to: for that reason a single
  unusable file puts every shipment at `NEEDS REVIEW` (it is deliberately
  cautious, even when the file belongs to no shipment), and it clears only
  when the file is replaced or removed. A readable document with no
  shipment ID is listed and excluded but does not by itself change a
  result. Duplicate detection is byte-identical files only. Financial figures are as stated in the
  documents (no currency or tax handling) and say nothing about what is
  owed. The optional model commentary depends on the local 4B model and
  is not checked beyond the listing of figures that are not in the audit.
  A finding's history summary can contain the file names and values of a
  conflict message (for example the two piece counts and their files),
  never the document text.
- **Calculations and deterministic answers** depend on the field labels
  used by the sample documents (e.g. `Number of Pieces Delivered`,
  `Expected Delivery Date`) and are triggered by the question's wording;
  documents with other labels fall back to the model or to an
  abstention. Units are not checked (pieces vs pallets). An unknown
  invoice charge written as a whole number with no `$`, no cents and no
  charge word such as fee or charge in its label (e.g. `Inside Delivery:
  50`) is not recognised as an amount. Other questions go to a small local model (4B), which can
  be slow on first use and is not always consistent.
- **Evaluation** uses 40 synthetic questions about two sample shipments;
  it is not evidence of production accuracy.
- **No OCR:** scanned PDFs without a text layer cannot be read.
- **Shipment identity** comes from the `Shipment Number` printed in each
  document. Documents without one are never attributed to a shipment.
  Identifiers are compared without case, spaces or separators
  (`shipment_identity.identifier_key`), so `SHJ-2026-001` and `SHJ2026001`
  are one shipment. Two identifiers that share that key but split it at
  different places (`AB-12-3` and `AB-1-23`) may be different shipments:
  their documents are never used together. The audit reports the shipment
  as not reconciled with the reason, a question about it abstains, and the
  extraction cross-check reports `shipment_id_collision`
  (`shipment_identity.identifier_collisions`). Forms that only leave out or
  add separators (`SHJ2026001`, `shj 2026 001`) are not collisions. The key
  itself is unchanged, so stored data keeps its keys; correcting the printed
  ID and uploading the document again resolves a collision.
- **Single machine, single writer.** Authentication is local accounts only
  (no SSO or MFA, and it needs HTTPS in front); no locking across
  processes, backups or monitoring; ChromaDB caches indexes
  per process, so do not ingest from another process while the app runs.
- **Full rebuild** (`python src/vector_store.py`) deletes and recreates all
  vectors and is not used by the app.
- Text extraction quality limits answers (e.g. the "Special Instructions"
  line of the sample BOLs does not extract cleanly).

A production version would need semantic answer verification, an
evaluation set built from real documents, access control, audit logging,
backups, and monitoring.
