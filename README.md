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
  and invoice and report shortages, delays and exceptions.

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

## Evaluation

`tests/eval/questions.jsonl` holds 40 questions about the two sample
shipments (24 factual, 8 reasoning, 4 unanswerable, 4 cross-shipment
traps), each with the expected answer, source page and whether the system
should abstain.

```powershell
python scripts/evaluate.py            # offline: lexical embeddings, no LLM
python scripts/evaluate.py --ollama   # real embedding and language models
```

Offline mode cannot measure questions that need the language model; it
reports them as "not measured" instead of scoring them. Results measured
on 2026-10-14 (this machine):

| Metric | Offline | Ollama | Target |
|---|---|---|---|
| Cross-shipment leakage | 0.00 (n=39) | 0.00 (n=39) | 0 |
| Retrieval recall@5 | 1.00 (n=32) | 1.00 (n=32) | ≥ 0.90 |
| Citation precision | 1.00 (21 citations) | 1.00 (36 citations) | ≥ 0.90 |
| Abstention accuracy | 1.00 (n=22) | 0.97 (n=40) | ≥ 0.75 |
| Answer match | 1.00 (n=18) | 0.94 (n=32) | — |

Recall@5 is not informative here: each shipment has only three
single-chunk documents, so a correctly scoped search always returns all
of them. Language-model results vary between runs.

## Sample shipments

| | SHJ-2026-001 | SHJ-2026-002 |
|---|---|---|
| Pieces (BOL / POD) | 48 / 45 → **SHORTAGE** of 3 | 60 / 60 → **MATCH** |
| Delivery (expected / actual) | Oct 3 / Oct 5, 2026 → **DELAYED** 2 days | Oct 9 / Oct 8, 2026 → **EARLY** |
| Invoice total | $1,550.00 (line items add up to $1,430.00) | $2,875.50 (line items match) |
| Shipment status | **EXCEPTION** (2 exceptions) | **CLEAR** (0 exceptions) |

The PDFs are in `tests/fixtures/`; SHJ-2026-001 is also the sample in
`data/raw`.

## Limitations

- **Grounding is lexical, not semantic.** An answer counts as supported
  when its dates, amounts, identifiers or names appear in a chunk. Correct
  answers built only from computed values (e.g. "3 pieces missing") can be
  rejected; a wrong answer that reuses document values could pass.
- **Deterministic answers** cover a fixed set of fields and are triggered
  by keywords; other questions go to a small local model (4B), which can
  be slow on first use and is not always consistent.
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
