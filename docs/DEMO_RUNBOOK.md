# LogiDoc-RAG demo runbook

A short, safe path through the application for a live presentation. Everything here uses the two
synthetic sample shipments in `tests/fixtures/` and a separate demo folder, so no real or private
document is involved and the project's own `data/` and `chroma_db/` are not touched.

## 1. Before the presentation (10 minutes)

1. **Ollama.** Start the Ollama app (tray icon), then in a terminal:
   ```powershell
   ollama list
   ```
   Both models must be listed: `nomic-embed-text` and `nemotron-3-nano:4b` (names from `config.py`).
   Do not pull models on stage: a download needs internet and takes minutes.
2. **Free memory.** Close unneeded browser tabs and applications. With about 1 GB of free RAM the
   language model has crashed once in testing; uploads and the audit are not affected.
3. **Frontend build** (once, or after frontend changes):
   ```powershell
   cd D:\LogiDoc-RAG\frontend
   npm run build          # run `npm ci` first only if node_modules is missing
   ```
4. **Smoke test** (about 10 seconds; it starts the real server on temporary folders, uploads the six
   sample PDFs and checks the audit, the assistant, an extraction and the PDF report, then stops):
   ```powershell
   cd D:\LogiDoc-RAG
   .\venv\Scripts\python.exe scripts\smoke_test.py
   ```
   Expected last line: `RESULT: PASS: 11 passed, 0 warning(s), 0 failed`. If Ollama is not usable the
   upload and readiness lines say `WARN` and everything else still passes; see section 5.

## 2. Start the demo server

A fresh demo folder in the Windows temp folder keeps the demo separate from `data/`:

```powershell
cd D:\LogiDoc-RAG
$env:LOGIDOC_DATA_DIR = "$env:TEMP\logidoc-demo\data"
$env:LOGIDOC_CHROMA_DIR = "$env:TEMP\logidoc-demo\chroma"
New-Item -ItemType Directory -Force "$env:LOGIDOC_DATA_DIR\raw" | Out-Null
.\venv\Scripts\python.exe src\api\serve.py
```

Open **http://127.0.0.1:8000** (the server also serves the React build). The server prints its address
and a one-writer reminder; leave that window open. Stop it with Ctrl+C (on Windows the exit code after a
clean stop is 3; that is normal).

These lines were run as written on 2026-10-26 (with the demo folder redirected to a scratch location):
the server started on port 8000, printed the two lines above, answered `/api/ready` with `ready`, and
served the interface at `/`. The smoke test runs the same `serve.py` with the same variables. To start
again from an empty demo, stop the server and delete `%TEMP%\logidoc-demo`.

**Check readiness** in a second terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/ready | ConvertTo-Json -Depth 4
```

- `ready`: every check passed.
- `degraded`: the folders are fine, but the model service or the extraction store is not. The audit,
  documents, history and reports work; uploads and generated answers need Ollama.
- `not_ready` (HTTP 503): the document folder or the data folder is not usable (for example the `raw`
  folder was not created).

`/api/health` only says that the server answers.

## 3. The demonstration

Expected results below are what the rehearsal produced with these exact files.

1. **Overview** starts empty.
2. **Documents → Ingestion:** upload the six PDFs from
   `tests\fixtures\shipment_SHJ-2026-001\` and `tests\fixtures\shipment_SHJ-2026-002\`
   (BOL, INVOICE and POD of each). Result: **6 indexed** (about 1 to 2 seconds).
3. **Duplicate detection:** upload `BOL_SHJ-2026-001.pdf` again. Result: duplicate, *"Same content as
   existing file BOL_SHJ-2026-001.pdf. Nothing was added."*
4. **Overview:** 2 shipments identified and reconciled, 1 with discrepancies, discrepancy rate 50%,
   invoice total $4,425.50, invoice variance $120.00. Then walk down the dashboard (every chart is drawn
   from the API's own figures; hover a bar for the exact value):
   - **Audit outcomes** ring: 2 shipments, 1 *Discrepancies detected*, 1 *No discrepancies*. Click
     *Discrepancies detected*: Shipment Explorer opens filtered to SHJ-2026-001. Go back.
   - **Where discrepancies come from:** for each check (quantity, delivery, invoice) 1 shipment failed and
     1 passed.
   - **Shipments needing attention:** SHJ-2026-001 with its three failed checks, 3 discrepancies, $120.00
     invoice difference, and an *Open audit* link to the evidence.
   - **Invoice totals against their line items:** $1,550.00 against $1,430.00 for SHJ-2026-001,
     $2,875.50 against $2,875.50 for SHJ-2026-002; **Invoice differences:** one red $120.00 bar.
     Say it plainly: an arithmetic mismatch, not proof of overbilling.
   - **Discrepancy categories** (each name opens the filtered shipment list), the **exception review
     queue** (3 open), **carriers**, and **documents by type** (2 BOL, 2 INVOICE, 2 POD, all text read).
5. **Shipments → SHJ-2026-001:** *DISCREPANCIES DETECTED* with three exceptions: **Quantity shortage**,
   **Delivery delay**, **Invoice mismatch**, each with its evidence documents. SHJ-2026-002 shows
   *NO DISCREPANCIES*.
6. **Assistant** (deterministic answers computed from the documents, with citations, not generated):
   | Shipment | Question | Answer |
   |---|---|---|
   | SHJ-2026-001 | How many pieces were delivered? | BOL: 48 pieces; POD delivered: 45. 3 piece(s) short. |
   | SHJ-2026-001 | Was the shipment delivered on time? | Expected October 3, 2026; delivered October 5, 2026: 2 day(s) late. |
   | SHJ-2026-001 | Does the invoice match? | The stated total $1,550.00 differs from its line items $1,430.00 by $120.00. This is an arithmetic mismatch; it does not show overbilling. |
   | SHJ-2026-002 | Does the invoice match? | The stated total $2,875.50 equals the sum of its line items. |
7. **Generated answer** (needs Ollama): on the **SHJ-2026-001 shipment page**, in the Ask panel, ask
   *"What are the payment terms on the invoice?"* Answer: *"The payment terms on the invoice are Net 30."*,
   with its evidence, labelled as generated (about 7 seconds; the first question after starting Ollama can
   take longer while the model loads).
8. **Exceptions:** the summary shows 3 exceptions by category (Delivery delay, Invoice mismatch,
   Quantity shortage; click one to filter the list), by severity (3 high) and by review state (3 open).
   Open one exception and set a review state with a note (the history of every change is kept); the
   review-state bar changes, the audit result does not.
9. **Reports:** download the SHJ-2026-001 PDF report (three pages).

What needs Ollama: uploads (embedding) and the Ask panel's generated answers. Everything else (the
audit, the Assistant's deterministic answers, structured extraction, exceptions, history, reports) is
computed in Python from the PDFs.

## 4. Known limitation to avoid on stage

On the **Assistant** page, *"What are the payment terms on the invoice?"* is answered with the invoice
arithmetic check, because the word *invoice* routes it to that tool. Ask document questions like this one
in a shipment's **Ask panel** (step 7) instead. The router is unchanged until the evaluation baseline can
be updated deliberately.

## 5. If something goes wrong

| What you see | What to do |
|---|---|
| `The embedding service is unavailable (it could not be reached).` on an upload; later files *"Not tried: the model service failed on an earlier file"* | Start Ollama, wait for `ollama list` to work, then **Documents → Errors → Retry** on each file (copies are kept). |
| `... did not answer within 60 s (timed out).` | Ollama is overloaded or stuck: wait, close other apps, then Retry. |
| `The embedding model (...) is not installed on the model service.` | The model is missing (`ollama list`); pulling it needs internet, so use the backup below. |
| Readiness `degraded` with `model_service` not ok | Same as above; the deterministic parts of the demo still work. |
| Readiness `not_ready` | Create the folder: `New-Item -ItemType Directory -Force "$env:LOGIDOC_DATA_DIR\raw"`. |
| Port 8000 already in use | `$env:LOGIDOC_API_PORT = "8001"` and start again; open http://127.0.0.1:8001. |
| The page shows only the API, no interface | Run `npm run build` in `frontend\` (smoke test line *React build served at /*). |

**Backup if Ollama is unavailable:** put the sample PDFs into the demo folder directly instead of
uploading them:

```powershell
Copy-Item D:\LogiDoc-RAG\tests\fixtures\shipment_SHJ-2026-00*\*.pdf "$env:LOGIDOC_DATA_DIR\raw\"
```

Steps 4 to 6, 8 and 9 then work exactly as above (the smoke test checks this path with the model service
switched off). Files copied this way are not indexed, so the Ask panel (step 7) has nothing to search.

## 6. Data and privacy

- Use only the files in `tests\fixtures\`: they are synthetic, made for this project.
- The demo folder (`%TEMP%\logidoc-demo`) is separate from `data\` and `chroma_db\`, which keep whatever
  was there before.
- Run one writer at a time: do not upload from the Streamlit app (`.\venv\Scripts\python.exe -m streamlit
  run app.py`) while the API server runs on the same data folder.
