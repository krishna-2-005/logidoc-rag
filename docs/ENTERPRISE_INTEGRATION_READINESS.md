# Enterprise integration readiness

Date: 2026-11-08. Branch `feature/production-readiness`, based on `306eff2` (CI passed on it). **Nothing is
committed or pushed.**

This report records what was built and measured to prepare LogiDoc-RAG for integration with logistics
systems. **It does not claim production readiness**: section 8 lists what is still missing.

---

## 1. API contracts (Priority 1)

### What changed

`src/api/schemas.py` holds Pydantic response models for the integration-facing endpoints:

| Endpoint | Model |
|---|---|
| `GET /api/carriers` | `CarrierIntelligenceResponse` |
| `GET /api/carriers/{key}` | `CarrierDetailResponse` |
| `GET /api/shipments/{id}/freight-audit` (**new**) | `FreightAuditResponse` / `FreightAuditResult` |
| `GET /api/history/shipments/{id}` | `list[TimelineEntry]` |
| `GET /api/exceptions` | `ExceptionList` |
| `GET /api/exceptions/{key}/history` | `list[ReviewEvent]` |
| `POST /api/exceptions/{key}/actions` | `ActionResult` |
| `GET /api/reports/carriers.csv` | declared `text/csv`; columns `carrier_intelligence.CSV_COLUMNS` |
| `GET /api/integrations/audit-runs` (**new**, section 6) | `AuditRunFeed` |
| `/api/carrier-registry/...` (**new**, section 3) | `CarrierRegistryResponse`, `RegisteredCarrierRecord`, `LinkRequest`, `RegistryEvent` |

### Conventions

- **Optional fields are explicit** (`X | None`). A value the audit could not determine is null, never 0.
- **Types:** counts are integers; amounts are decimal numbers, with their currency beside them (null or "Not
  stated" when the documents state none; nothing is converted). Dates are ISO 8601.
- **Fixed value sets:** review statuses and freight-audit outcomes are restricted to their documented values.
- **Errors** are always `{"detail": "<message>"}`, including request validation errors (the application already
  normalised these). 401, 403, 404, 409 and 422 are declared in the OpenAPI document with an `ErrorResponse`
  model.

### Backward compatibility, and how it is enforced

The response shapes were **captured from the running API before the models were written**, and the frontend's
use of each field was checked first.

- **No field can be dropped or altered.** Every typed route uses `response_model_exclude_unset=True`, so it
  returns exactly the fields its source set.
- **Compatibility test:** `tests/test_api_contracts.py::test_every_typed_endpoint_returns_what_it_returned_before`
  compares each endpoint's JSON with the raw value of its route function, on data that fills every optional part
  (recorded history, an approved correction, a disputed exception, a contracted shipment).
- **The test was shown to work:** removing one field from a model made it fail, naming the endpoint.
- **No frontend change was needed** for the contracts.
- **Two additions were deliberately backward compatible:**
  - Carrier cards gained `registered` (the master data, or null).
  - The carrier CSV gained `scac`, `mc_number` and `dot_number` **at the end**, so every earlier column keeps its
    position.

## 2. Versioning and the published contract (Priority 2)

- **`/api/v1/...` is the same API as `/api/...`.** A small ASGI middleware (`ApiVersions`, `src/api/app.py`)
  rewrites the prefix before CORS, CSRF and routing, so the permissions and CSRF rule are identical.
  - Every `/api` response carries `API-Version: 1`, also exposed through CORS.
  - An unknown version (`/api/v2/...`) is 404.
- **Policy:**
  - Integrations pin `/api/v1`; the web application keeps using `/api`.
  - Additive changes (new optional fields, new endpoints, new CSV columns at the end) stay in v1.
  - Removing or renaming a field, changing a type or the meaning of a value, or reordering CSV columns is
    published as `/api/v2` alongside v1, never by changing v1.
- **Published contract:** `docs/api/openapi-v1.json` (65 paths, 78 schemas), written by
  `scripts/export_openapi.py`.
  - The script builds the application on a temporary folder, so it never opens the installation's data.
  - `python scripts/export_openapi.py --check` and the test `test_the_published_contract_is_up_to_date` fail when
    the code and the published file disagree.
- **Integration contracts for future systems,** documented here rather than faked:
  - **TMS:** pulls shipment audit results (`/api/v1/shipments/{id}/freight-audit`, `/api/v1/history/...`) and
    the change feed (section 6).
  - **ERP or accounts payable:** consumes the feed and the exception list (financial impact, recommended action,
    review state including disputed and waived) to hold, short-pay or release invoices.
  - **Carrier platforms:** carrier master data, with the identifiers validated (section 3).
  - **Document management:** documents keep arriving through the existing upload endpoint
    (`POST /api/ingestion/upload`), with its validation (section 5). No connector, credential or customer system
    was invented.

## 3. Carrier master data (Priority 3)

`src/carrier_registry.py` holds the store `carrier_registry.sqlite3`; its path is `LOGIDOC_CARRIER_DB`, or
beside the document folder. The API is in `src/api/routes_carriers.py`.

- **Fields:** legal name, trading name, SCAC, MC number, USDOT number, and contact name, email and phone.
- **Validation (refused, never cleaned into something else):**
  - SCAC: 2–4 letters, stored in upper case.
  - MC and USDOT numbers: 1–8 digits; `MC-`, `MC`, `USDOT` and `DOT` prefixes are accepted, and the number is
    stored as digits.
  - Email and phone formats are checked.
- **Duplicates:**
  - One identifier, one carrier: a second carrier with the same SCAC, MC or USDOT number is refused (409),
    naming the carrier that has it.
  - A carrier whose legal or trading name equals an existing one's (ignoring case, spaces and punctuation) is
    refused as a possible duplicate, unless the request says it is a different company (`confirm_distinct`).
- **Manual merge approval:**
  - A carrier name printed on the documents is attached to a registered carrier only by a **proposal, with a
    reason, from one administrator and approval by a different administrator**. The proposer is refused (403).
  - That is the only way two document names become one carrier. Similar names are never merged.
  - Moving or unlinking a name needs a new approved request.
- **Identity conflicts:** a name already linked elsewhere stays where it is until a move is approved, and a
  second pending request for the same name is refused (409).
- **History preserved:** no shipment is stored in the registry. Shipments are associated when the figures are
  read, through the names their documents print, so recorded audit history is never rewritten (tested).
  Unlinking restores the earlier grouping.
- **Audit trail:** `carrier_events` is append-only (database triggers), recording who made each change, when,
  and the values before and after. Only administrators can read it.
- **Permissions:** reading needs `exceptions.view`. Every change needs the new administrator-only
  `carriers.manage`. Reading never creates the database.
- **In Carrier Intelligence:**
  - Names linked to one registered carrier are one carrier, shown under its legal name with its identifiers, and
    a shipment whose documents print two linked names is no longer "conflicting".
  - The carrier detail shows the identifiers and contact, and the CSV export includes the identifiers.
- **Not built:** no screen for *managing* the registry. Registering carriers and deciding links is done through
  the API; the page shows the result.

## 4. Performance and scalability (Priority 4)

`scripts/benchmark_scale.py` runs on **synthetic data only** (every shipment ID starts with `BENCH-`; no
customer document is read). The in-memory sizes use the audit's own structures; the end-to-end run reads
generated PDFs.

- **Machine:** Windows 11, 16 GB RAM, Python 3.12.7.
- **What the figures are:** time and peak Python allocation (tracemalloc) of one call.
- **Load:** the machine was carrying other work, so treat the numbers as orders of magnitude.

### After the changes (100 / 1,000 / 10,000 shipments; 82 / 859 / 8,271 exceptions)

| Operation | 100 | 1,000 | 10,000 | 10,000 before |
|---|---|---|---|---|
| Carrier aggregation | 0.021 s | 0.158 s | **1.721 s** | 6.625 s |
| Carrier detail, first page of 50 | 0.009 s | 0.091 s | **0.841 s** | 6.054 s |
| Exception aggregation | 0.004 s | 0.042 s | 0.468 s | 0.445 s |
| Exception explanations | 0.002 s | 0.027 s | 0.294 s | 0.289 s |
| Freight audit engine, per shipment | 1.13 ms | 1.01 ms | 1.14 ms | same |
| Review store sync, first / unchanged | 0.016 / 0.004 s | 0.128 / 0.038 s | 1.137 / 0.385 s | same |
| Review queue query, all / search | 0.003 / 0.001 s | 0.035 / 0.001 s | 0.308 / 0.005 s | same |
| Record an audit run | 0.079 s | 0.816 s | **8.244 s** | 13.166 s |
| Record an unchanged audit run | 0.049 s | 0.499 s | **4.958 s** | 15.049 s |
| Peak memory, carrier aggregation | 0.2 MiB | 1.4 MiB | 13.8 MiB | — |

### Bottlenecks found, and what was changed (only where measured)

1. **Carrier aggregation and carrier detail were quadratic.** Each shipment scanned every exception. Exceptions
   are now grouped once per shipment, and the detail computes only its carrier's shipments. At 10,000 shipments:
   6.6 s to 1.7 s, and 6.1 s to 0.84 s.
2. **Recording an audit run was quadratic** (profiled at 10,000):
   - 5.7 s went on rescanning every exception for each shipment; they are now grouped once.
   - 4.5 s went on SQLite scanning `run_input_sets` and `run_shipments`, which had no index starting with the
     shipment. Audit History schema **version 5** adds two indexes. The migration adds the indexes only and
     touches no row; a test verifies a version 4 database keeps every row and that queries use the new index.
   - Result: 13.2 s to 8.2 s, and an unchanged run 15.0 s to 5.0 s.
3. **Reading PDFs is the largest cost, and it is linear.**
   - A cold audit snapshot takes **about 50 ms per PDF**: 19.9 s for 100 shipments (400 PDFs), against 0.055 s
     when cached.
   - Projected for 10,000 shipments (40,000 PDFs): about 33 minutes cold. That is a projection from the measured
     rate, not a measurement.
   - After one new upload the whole audit was recomputed, re-reading every PDF: 3.16 s for 204 PDFs. Profiling:
     47% was the freight audit's extraction, 43% reading the text for the reconciliation.
   - **Change:** the freight-audit extraction is cached per file content (name, SHA-256, extractor version), and
     the cache keeps only current files. A changed file has a new hash, so it is re-extracted. After one upload:
     **3.16 s to 1.09 s** (−65%).
   - **Cost:** about 5.9 KiB of memory per file, so roughly 230 MiB at 40,000 files (projected).
   - The text reading for the reconciliation is not cached yet (see 8).
4. **The carrier endpoint's first call after a change** also scans every PDF for the completeness register (5.7 s
   for 400 PDFs). It is cached afterwards; this was not changed.

## 5. Security and access control (Priority 5)

What was reviewed, and what tested it at the backend:

- **Authentication and roles** (unchanged): local accounts with sessions, or local single-user mode. Every new
  endpoint requires sign-in (401 tested, including through `/api/v1`).
- **Permissions:** checked on the server, never only hidden in the screen.
  - A reviewer cannot waive, dismiss, record history, approve a correction, or change carrier master data (403,
    tested by direct API calls).
  - A reviewer cannot read the registry's event log.
- **Two-person rules:**
  - A correction's proposer cannot approve it.
  - The administrator who waived an exception cannot approve the waiver (when approval is switched on).
  - A carrier-link proposer cannot decide the link.
  - All tested.
- **CSRF:** every change needs the `X-LogiDoc-Client` header, including the new registry endpoints (403
  without it, tested), whatever the version prefix.
- **Uploads** (reviewed, unchanged and covered by the existing ingestion tests): the size limit
  (`LOGIDOC_MAX_UPLOAD_MB`, default 25), a PDF-signature check, at most 20 files per request, and safe stored
  names.
- **CSV exports:** cells are written through `audit_export.safe_cell`. A value a spreadsheet could run as a
  formula (starting with `=`, `+`, `-`, `@`, their full-width forms, a tab or a carriage return; plain numbers
  excepted) gets an apostrophe in front (tested on the carrier export with a `=HYPERLINK(...)` carrier name). A
  missing value is "NOT AVAILABLE", never 0.
- **Sensitive information:**
  - Unexpected errors return a generic message (the server log has the cause).
  - The new endpoints' errors name no server path or database file (tested).
  - The readiness check reports status words only.
- **Audit-history integrity:** recorded runs, snapshots, findings, freight-audit records, review events and
  carrier events are append-only (database triggers refuse UPDATE and DELETE; tested). The review events also
  form a hash chain.

## 6. Integration architecture (Priority 6)

Built only where it fits the current architecture:

- **Outbound change feed:** `GET /api/v1/integrations/audit-runs?after=<run id>&limit=` (needs `history.view`).
  - It returns the recorded audit runs after the cursor, oldest first. Each run lists every shipment it checked,
    with its status, carrier, invoice total, freight-audit outcome, and whether it `changed` since its previous
    snapshot.
  - `next_after` and `has_more` page through the runs.
  - The feed is built on append-only history, so reading a cursor again returns the same runs: a consumer can
    retry safely and process at least once.
  - This is the foundation for ERP and TMS synchronisation, and for webhooks: a dispatcher can poll the feed and
    push each run.
- **Typed pull endpoints** (section 1) under `/api/v1`, with the published contract.
- **Carrier data ingestion:** the registry's API is the entry point for carrier master data from a carrier
  platform, with the same validation and two-person linking.
- **Designed, not built** (they need decisions this codebase cannot make alone):
  - **Webhooks:** a dispatcher that posts each new feed run to configured URLs, with HMAC-signed bodies,
    retries with back-off and a delivery log. It needs the URL and secret storage and an operator to own them.
  - **Scheduled synchronisation:** the same poller run on a schedule.
  - **Inbound TMS shipment orders and ERP payment status:** these need the partner's actual API; none was
    invented.
  - **n8n:** not introduced. It could later sit on the feed and the typed endpoints.

## 7. Test results

All figures come from the final working tree.

| Check | Result |
|---|---|
| Full Python suite, Python 3.12.7 | **1,968 passed, 0 failed**, 3 skipped (6 min 7 s). The skips are the three tests that need Ollama, which was not running |
| New tests | 69: `test_api_contracts.py` 17, `test_carrier_registry.py` 31, `test_integration_feed.py` 14, `test_scale_safeguards.py` 7. Plus 1 in the frontend |
| Frontend type check (`tsc --noEmit`) | clean |
| Frontend tests (`vitest run`) | **9 files, 228 passed, 0 failed** |
| Production build (`vite build`) | succeeds. Main chunk 988.12 kB (gzip 286.58 kB), against 987.50 kB before. Vite's 900 kB advisory is unchanged |
| `pip check` | clean on Python 3.12 and 3.10 |
| Deterministic evaluation (Python 3.10) | **PASS, identical line for line to the verified baseline** |
| `scripts/export_openapi.py --check` | the published contract is up to date |
| `git diff --check` | clean |
| Real `data/` databases | byte-for-byte unchanged (SHA-256); no registry file was created there |

**Checks that the tests check the right thing:**

- The compatibility test was run against a model with one field removed, and it failed as it should.
- The race and quadratic fixes were measured before and after (section 4).

**Not run:** the Ollama-dependent tests (Ollama was stopped), and the 10,000-shipment end-to-end run on real
PDFs (projected only, from the measured rate per PDF).

## 8. Remaining limitations and integration gaps

- **Not production-ready.** There is no verified deployment, container definition, TLS proxy, backup routine or
  monitoring, and authentication is local accounts only (no SSO or MFA).
- **Scale:**
  - A cold audit of 10,000 shipments is projected at about 33 minutes, because every PDF is read.
  - After any change the whole audit is recomputed; the reconciliation's text reading is not cached per file.
  - Recording a run of 10,000 shipments takes about 8 s.
  - Everything runs in one process, and SQLite allows one writer.
  - There is no lock across processes (the API and Streamlit must not write together).
- **Carrier master data:**
  - There is no management screen (API only).
  - Identifiers are validated for format, not checked against FMCSA or NMFTA.
  - No carrier numbers are extracted from documents.
- **API:**
  - Only the endpoints listed in section 1 have models; the rest still return plain dictionaries.
  - Pagination is offset-based on carrier shipments and cursor-based on the feed; other lists are bounded but
    not paged.
- **No real-document accuracy measurement:** every figure comes from synthetic documents.

## 9. Files

**Safe to commit**, all tested together:

- **Modified (11):**
  - `config.py`
  - `src/api/app.py`
  - `src/api/routes_audit.py`
  - `src/api/routes_workflow.py`
  - `src/audit_history.py`
  - `src/audit_service.py`
  - `src/authz.py`
  - `src/carrier_intelligence.py`
  - `frontend/src/lib/types.ts`
  - `frontend/src/pages/CarrierIntelligence.tsx`
  - `frontend/src/test/carriers.test.tsx`
- **New (11):**
  - `src/api/schemas.py`
  - `src/api/routes_carriers.py`
  - `src/carrier_registry.py`
  - `scripts/benchmark_scale.py`
  - `scripts/export_openapi.py`
  - `docs/api/openapi-v1.json`
  - `docs/ENTERPRISE_INTEGRATION_READINESS.md`
  - `tests/test_api_contracts.py`
  - `tests/test_carrier_registry.py`
  - `tests/test_integration_feed.py`
  - `tests/test_scale_safeguards.py`

**Not part of the repository:** `data/`, `chroma_db/`, and the temporary benchmark and test folders under
`D:\logidoc_tmp` (synthetic only; they can be deleted).

**What happens on the next server start:**

- **Audit History** moves to schema version 5: two indexes are added and no row changes.
- **The carrier registry** is created only when an administrator first registers a carrier.
