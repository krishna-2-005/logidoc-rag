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

## Setup (Windows, PowerShell)

Requires Python 3.10 and [Ollama](https://ollama.com/download).

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
embeddings and a fake language model. Results on 2026-10-21: 574 passed
with Ollama; 571 passed and 3 skipped with Ollama unreachable.

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
  only: it draws no trend charts. The review database is local,
  single-user and has no authentication: the reviewer name is typed in.
  The analytics CSVs cover the filtered shipments and exceptions only;
  there is no analytics-level PDF. Reconciling many shipments reads every
  PDF once and may take a while.
- **Audit Operations.** History exists only for audits that someone
  recorded: there is no automatic or scheduled recording, no cross-shipment
  trend view, and the history database is local and single-user. The
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
- **Single machine, single writer.** No authentication, multi-user
  locking across processes, backups or monitoring; ChromaDB caches indexes
  per process, so do not ingest from another process while the app runs.
- **Full rebuild** (`python src/vector_store.py`) deletes and recreates all
  vectors and is not used by the app.
- Text extraction quality limits answers (e.g. the "Special Instructions"
  line of the sample BOLs does not extract cleanly).

A production version would need semantic answer verification, an
evaluation set built from real documents, access control, audit logging,
backups, and monitoring.
