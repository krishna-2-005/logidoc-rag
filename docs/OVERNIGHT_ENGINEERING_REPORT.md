# Overnight engineering report (2026-10-26)

Work done the evening before the faculty presentation on 2026-10-27, on branch
`feature/production-readiness`. The work was left uncommitted overnight for review, then committed and
pushed on 2026-10-27 as nine commits on this branch.

## 1. Executive summary

- **The demo path is verified end to end.** A new smoke test (`scripts/smoke_test.py`) starts the real
  server on a temporary folder and checks health, readiness, the interface, upload, audit, extraction, a
  cited assistant answer and the PDF report: 11 of 11 passed in 5.8 s. The runbook's own start commands
  were also run word for word: the server started, answered `ready` and served the interface.
- **A readiness check now exists.** `GET /api/ready` reports whether the document folder, the data
  folders, the extraction store and the Ollama models are usable (`ready` / `degraded` / `not_ready`).
  Before, `/api/health` said `ok` even with Ollama stopped.
- **Two real gaps are closed.** First, `streamlit-pdf` was missing from `requirements.txt`, so a clean
  install broke the Streamlit "View Original PDF" viewer. Second, the data and index folders could not
  be moved, so a demo or test could not run without touching `data/`.
- **CI is defined.** `.github/workflows/ci.yml` covers Python 3.10 and 3.12 on Windows, the frontend
  build and tests, the assistant evaluation, and a commit-identity check. Its first run on GitHub is
  started by the push of these commits.
- **The dashboard was redesigned with real data only.** The Overview now has 12 panels: KPI rows, a
  ring chart, stacked and grouped bar charts, a signed-difference chart and a ranked "needs attention"
  table. The Exception Center has 3 summary charts. Every number comes from an existing API response. On
  the live server, 13 of 13 chart totals matched the API.
- **Results:**
  - Python suite: 1723 passed on 3.10 and on 3.12, exit 0 both times.
  - Frontend: 199 of 199 tests passed; typecheck and production build exit 0.
  - `pip check`: clean.
  - `npm audit --omit=dev --audit-level=high`: 0 vulnerabilities.
  - Deterministic assistant evaluation (137 cases): PASS.
  - Live data (`data/`, `chroma_db/`, `eval_real/`, `reports/`, `tests/eval/`): byte-for-byte as at the
    start of the work.

## 2. Git state

| | |
|---|---|
| Branch | `feature/production-readiness` |
| Starting commit | `d6f9d4c` (same content as `main` after the merge of PR #10); clean tree, no stashes |
| Overnight state | `HEAD` still `d6f9d4c`; 10 tracked files modified and 11 new files (including this report), left uncommitted for review |
| Committed | 2026-10-27: nine commits on top of `d6f9d4c`, each file staged by name, pushed to `origin/feature/production-readiness` |

No reset, clean, stash, amend, rebase, force-push or broad staging was used.

## 3. Files changed and why

**Backend and configuration**

| File | Change | Why |
|---|---|---|
| `requirements.txt` | `+ streamlit-pdf==2.1.0` | `st.pdf` (the Streamlit app's "View Original PDF", `app.py` lines 554 and 902) needs this package; it was only installed by chance in `venv/`. A clean install from the pins failed. |
| `config.py` | `LOGIDOC_DATA_DIR`, `LOGIDOC_CHROMA_DIR` (defaults unchanged) | Lets the demo and the smoke test run on an empty folder without touching `data/` or `chroma_db/`. A single-database override such as `LOGIDOC_REVIEW_DB` still wins. |
| `src/model_client.py` | `installed_models()`, `has_model()` | Reads Ollama's model list with a 2 s limit and without loading a model, for the readiness check. |
| `src/readiness.py` (new) | Readiness checks | Document folder listable, data folders writable, extraction store absent or current, Ollama reachable with both models. Status words only, never paths or error text. |
| `src/api/app.py` | `GET /api/ready` | 200 for `ready` or `degraded`, 503 for `not_ready`. |
| `src/api/serve.py` | Docstring and one startup line | Documents the new variables and the health/ready difference, and prints the one-writer rule (the API and Streamlit must not write to the same folder at once). |
| `scripts/smoke_test.py` (new) | Pre-demo smoke test | See section 10. |
| `.env.example` (new) | Every variable with its default, placeholders only | The app reads the process environment; nothing loads a `.env` file. |
| `.github/workflows/ci.yml` (new) | CI | See section 9. |

**Frontend**

| File | Change | Why |
|---|---|---|
| `frontend/src/components/common/Charts.tsx` | `Donut`, `Columns` (grouped, stacked, horizontal, signed), a shared tooltip | Chart building blocks on `recharts`, which was already a dependency. Every bar chart also renders its numbers as a screen-reader table; the ring lists its values as text. |
| `frontend/src/lib/insights.ts` (new) | Pure functions that arrange API results into chart rows | They count and arrange; they never recalculate an audit. Missing values are reported as missing, never drawn as zero. |
| `frontend/src/pages/Overview.tsx` | New dashboard layout and charts | See the UI/UX section. |
| `frontend/src/pages/Exceptions.tsx` | Summary by category, severity and review state; a Severity column | See the UI/UX section. |
| `frontend/src/pages/Shipments.tsx` | Reads `?status=` and `?category=` from the link; the shipment ID no longer wraps | The Overview's chart links open a pre-filtered list. At 1366 px, IDs had broken over three lines. |

**Tests and documents**

| File | Content |
|---|---|
| `tests/test_dependency_completeness.py` (new, 3 tests) | Every imported third-party package is pinned; indirectly loaded packages (`streamlit-pdf`) are pinned and installed; `st.pdf` works. |
| `tests/test_readiness.py` (new, 15 tests) | Every status, missing models by role, tag matching, folder failures, an unreadable store, crash shielding, no leaked details, the route, a real model list over a local HTTP server, a silent socket, a closed port. |
| `tests/test_config_locations.py` (new, 4 tests) | Defaults, overrides, database override precedence, and the smoke test run with the model service unreachable. |
| `frontend/src/test/dashboard.test.tsx` (new, 15 tests) | Chart data functions, chart contents, links and navigation, partial and empty data, loading and error states, the Exception Center summary, filtered links. |
| `README.md` | Python 3.12 tested; `/api/ready`; the new variables; extraction store v5 and its explicit migration command (it said v4); the smoke test and runbook; the shipment-ID collision limitation; current test counts. |
| `docs/DEMO_RUNBOOK.md` (new) | The presentation runbook. |
| `docs/OVERNIGHT_ENGINEERING_REPORT.md` (new) | This report. |

## 4. Bugs fixed and their regression tests

| Defect | Evidence | Fix | Regression test |
|---|---|---|---|
| A clean install could not show PDFs in the Streamlit app | In a clean `venv312`, `st.pdf` raised *"requires the streamlit-pdf component"*; `venv/` worked only because the package happened to be installed | Pinned `streamlit-pdf==2.1.0`. The resolver, with all pins as constraints, adds only this package; `pip check` is clean on 3.10 and 3.12. | `tests/test_dependency_completeness.py`. A copy of the requirements with the pin removed is caught. |
| No way to run the app on a separate folder | `DATA_DIR` and `CHROMA_DIR` were fixed paths | Environment overrides with unchanged defaults | `tests/test_config_locations.py` |
| `/api/health` said `ok` with Ollama stopped or a model missing | Observed | `/api/ready` | `tests/test_readiness.py`. Two deliberate code mutations were caught. |
| Dashboard: ring-chart labels cut off ("Discrepan…"); count axes ran to 4 for a maximum of 2; shipment IDs wrapped over three lines in Shipment Explorer at 1366 px | Screenshots | Labels wrap and counts keep their width; small integer axes get one tick per whole number; IDs do not wrap and the route wraps instead | Verified visually at 1366×768 and 1920×1080. Layout cannot be measured in the unit-test environment, so there is no automated test. |

Found but **not fixed** (documented instead; see section 11):
- The Assistant page routes *"What are the payment terms on the invoice?"* to the invoice-arithmetic tool.
- Shipment-ID separator collisions.

## 5. Tests run

| Check | Result | Exit | Duration |
|---|---|---|---|
| Baseline full suite, Python 3.10, clean `d6f9d4c` | 1701 passed | 0 | 289 s |
| **Final full suite, Python 3.10** (`venv`, working tree) | **1723 passed**, 1 warning | **0** | 293.6 s |
| Full suite, Python 3.12 (`venv312`), mid-sprint tree | 1719 passed | 0 | 300 s |
| **Final full suite, Python 3.12.7** (`venv312`, working tree; `pip check` clean) | **1723 passed**, 1 warning | **0** | 276.2 s |
| New Python tests (included above) | 22 (3 + 15 + 4) | | |
| `scripts/evaluate_assistant.py`, model service unreachable (as in CI) | 137 cases (127 scored, 10 ambiguous); every threshold passed; cross-shipment leakage 0 | 0 | |
| `scripts/smoke_test.py`, real Ollama, final tree | 11 passed, 0 warnings, 0 failed | 0 | 5.8 s |
| Smoke test with the model service unreachable (inside the suite) | passes: uploads report WARN and the PDFs are copied instead | 0 | |
| `pip check` (3.10) | No broken requirements | 0 | |
| `git diff --check` | clean (new files checked separately: clean) | 0 | |

The one warning is a third-party deprecation notice (`StarletteDeprecationWarning` from
`fastapi.testclient`). The baseline run shows the same warning.

**Mutation checks.** These confirm that the new tests fail when the behaviour breaks. Each file was
restored byte-identical afterwards.
- Readiness: 2 mutations, both caught.
- Dependency pin: removing it is caught.
- Dashboard: dropping the mixed-currency guard is caught, and so is drawing an unreadable invoice as zero.

**Data safety.** All tests and the smoke test run on temporary folders. A fingerprint (name, size and
modification time of every file) of `data/`, `chroma_db/`, `eval_real/`, `reports/` and `tests/eval/` was
68 files, `1466239a63f21d5d`, at the start and again at the end. `git status` shows no change under
`tests/eval`, `reports`, `eval_real`, `data` or `chroma_db`.

## 6. Frontend

| Check | Result |
|---|---|
| `npm run typecheck` | exit 0 |
| `npx vitest run` | 199 passed (7 files): the 184 existing tests plus 15 new; exit 0 |
| `npm run build` | exit 0. Main bundle 932.64 kB (gzip 272.99 kB), up from 873.35 kB (gzip 260.05 kB) at the start, because more of `recharts` is used. Vite now prints its "chunk larger than 900 kB" advisory. No package was added. |
| `npm audit --omit=dev --audit-level=high` | 0 vulnerabilities |

## 7. Security and dependency checks

**Performed tonight:**
- `pip check` on 3.10, and on 3.12 within the final 3.12 run.
- `npm audit` on production dependencies: 0.
- A scan of all 20 changed and new files for credential patterns (GitHub, cloud and API-key formats,
  private-key headers): 0 matches.
- `.env.git` is ignored by `.env.*` and is not tracked. It was not opened.
- A scan of every tracked file, and of every commit's author, committer and message on all branches, for
  tool or co-author attribution: 0. Commit e-mails are only the owner's address and GitHub's web-merge
  address.
- `/api/ready` returns status words only; a test checks that no path, host or model name leaks.

**Not checked tonight:**
- Python vulnerability database scan: the last one was in the earlier phases, which led to the certifi
  and aiohttp updates already merged.
- Container or OS scanning: there is no container.
- TLS and reverse-proxy configuration.
- Authentication in `required` mode on a non-loopback host: unchanged and covered by the existing tests.

## 8. Python 3.12

- Tested: the full suite passes on 3.12.7 (`venv312`) with the same pins, and the CI matrix includes 3.12.
- **The migration is not complete.** The default runtime is still Python 3.10 (`venv/`), which reached end
  of life on 2026-10-01. Switching the default needs:
  - a decision;
  - a fresh environment;
  - the Streamlit 1.66 / Pillow 13 upgrade, which is still blocked by two review-form test failures
    found earlier;
  - the deprecated `use_container_width` calls replaced in 17 places.

## 9. Readiness check and CI

- **Readiness: implemented and verified.** 15 tests pass. On the live server it answered `ready` with
  real Ollama, and the smoke test shows `degraded` with Ollama unreachable.
- **CI: implemented, partially verified.** The workflow runs:
  - Python 3.10 and 3.12 on `windows-latest`: install, `pip check`, the full suite with the model
    service pointed at a dead address, and the deterministic evaluation.
  - The frontend on Ubuntu with Node 22: `npm ci`, typecheck, tests, build and `npm audit`.
  - Hygiene: `git diff --check` on pull requests, and a check that every commit carries only the
    owner's identity with no co-author or "generated with" lines.

  The YAML parses, the action versions exist, and each step's command passes locally. **The workflow
  had not run on GitHub when this report was written.** Its first run, started by the push of these
  commits, is its first real test.

  The commit-message rule matches the words *generated with* and *generated by* anywhere, so a harmless
  message such as "show the report generated by the audit" also fails it. Word commit messages around
  it.

## 10. Demo commands and rehearsal

The full sequence is in [`docs/DEMO_RUNBOOK.md`](DEMO_RUNBOOK.md). The start commands:

```powershell
cd D:\LogiDoc-RAG
.\venv\Scripts\python.exe scripts\smoke_test.py            # before the presentation; expect 11 passed
$env:LOGIDOC_DATA_DIR = "$env:TEMP\logidoc-demo\data"
$env:LOGIDOC_CHROMA_DIR = "$env:TEMP\logidoc-demo\chroma"
New-Item -ItemType Directory -Force "$env:LOGIDOC_DATA_DIR\raw" | Out-Null
.\venv\Scripts\python.exe src\api\serve.py                  # open http://127.0.0.1:8000
Invoke-RestMethod http://127.0.0.1:8000/api/ready | ConvertTo-Json -Depth 4   # second terminal
```

**Rehearsal status.** The whole demonstration ran against the real server, on temporary folders, with
real Ollama:
- Upload of the 6 sample PDFs: indexed in 1.3 s. A repeated upload is reported as a duplicate.
- SHJ-2026-001: *DISCREPANCIES DETECTED*, with three exceptions (quantity shortage, delivery delay,
  invoice mismatch). SHJ-2026-002: *NO DISCREPANCIES*.
- Assistant: deterministic answers with citations.
- Shipment Ask panel: a generated answer, "Net 30", in 6.9 s.
- Exception review: changes only the review state (3 open became 2 open and 1 in review; the audit
  figures were identical before and after).
- PDF report: downloaded.

The runbook's start and readiness lines were run word for word: port 8000, `ready`, interface served at
`/`.

## 11. Remaining limitations and blocked items

- **Assistant routing.** On the Assistant page, *"What are the payment terms on the invoice?"* is
  answered by the invoice-arithmetic tool, because the word *invoice* routes it there. The router was
  not changed: the frozen evaluation baseline would have to be updated deliberately. The runbook says
  to ask such questions in the shipment's Ask panel, where the answer is correct.
- **Shipment-ID collisions.** `identifier_key` ignores separators, so `AB-12-3` and `AB-1-23` both
  become `AB123` and their documents would be audited as one shipment (confirmed tonight). The sample
  data has no such pair. This is now documented in the README; the behaviour is unchanged.
- **Single writer.** The ingestion lock works within one process only. The API and Streamlit must not
  write to the same folder at once. This is now printed at server start and stated in the README and
  runbook. There is no lock across processes.
- **Real-document accuracy is not measured.** No permitted real documents exist (0 supplied), so
  `scripts/evaluate_real_documents.py evaluate` reports NOT MEASURED. All accuracy figures come from
  synthetic, project-authored documents.
- **CI** gets its first GitHub run with this push (section 9).
- **Python 3.12** is not yet the default runtime (section 8).
- **Bundle size** passed Vite's 900 kB advisory (section 6). It is served locally, so it does not affect
  the demo.
- **Deployment.** There is no container or service definition and no verified deployment. Production
  use remains blocked (the earlier phase G result is unchanged). Local single-user use is ready.
- **Blocked tonight:** none of the planned items stayed blocked.

## 12. Recommended next steps after the presentation

1. **Check the first CI run** on the pushed commits and fix whatever it shows.
2. **Writer safety across processes.** Add an OS-level lock file in the data folder, held by whichever
   process ingests (API or Streamlit). A second writer then gets a clear refusal instead of racing. Test
   it with two real processes.
3. **Shipment-ID collisions.** When two different printed identifiers produce the same key, report a
   collision on both shipments instead of merging them. This needs regression tests for the collision,
   for the legitimate variants (`SHJ-2026-001` and `shj 2026 001` stay one shipment) and for the
   evaluation baseline. It is a business-rule change, so it needs a documented decision.
4. **Real-document evaluation.** Collect documents you have permission to use (the harness asks for 20
   per type), label them with a second-person check, freeze the split, then run
   `scripts/evaluate_real_documents.py`. Do not claim accuracy before that.
5. **Assistant routing.** Fix the payment-terms case together with a deliberate, reviewed update of the
   evaluation baseline.
6. **Python 3.12 as default**, after the Streamlit/Pillow investigation.
7. **Split the frontend bundle** by route (lazy-loaded pages).
8. **Deployment definition**: a service or container and a reverse proxy with TLS, then a verified
   deployment.

## 13. Implemented, tested, partially tested, proposed

| Item | State |
|---|---|
| `streamlit-pdf` pin | Implemented and tested (3.10 and 3.12) |
| Data and index folder overrides | Implemented and tested |
| `/api/ready` | Implemented and tested (unit, route, live) |
| Smoke test | Implemented and tested (with and without Ollama) |
| One-writer startup message and docs | Implemented. A printed message, so not tested beyond being seen at startup. |
| `.env.example` | Implemented (documentation) |
| Demo runbook | Implemented. Rehearsed against the real server; start lines run verbatim. |
| Overview dashboard and Exception Center charts | Implemented and tested: unit tests, a live totals check (13/13), screenshots at two sizes |
| Shipment Explorer link filters | Implemented and tested |
| CI workflow | Implemented and partially tested: commands verified locally; first GitHub run starts with the push |
| Python 3.12 | Tested; not adopted as the default |
| Cross-process writer lock | Proposed only |
| Shipment-ID collision safeguard | Proposed only (limitation documented) |
| Real-document evaluation | Harness exists; measurement not possible (0 documents) |
| Assistant routing fix | Proposed only (limitation documented) |

---

## UI/UX: dashboard and data visualisation

### Screens improved

| Screen | Change |
|---|---|
| **Overview** | Rebuilt as a dashboard, top to bottom: page heading with Refresh; KPI row; audit outcomes and check breakdown; a ranked "needs attention" table; invoice comparison and differences; categories and review queue; carriers and documents by type; financial exposure; document readiness. A chart whose data fails to load shows its own error and Retry inside its card; the rest of the page stays. |
| **Exception Center** | Summary cards above the list: by category (each bar is a filter button), by severity and by review state; a Severity column in the table. |
| **Shipment Explorer** | Opens pre-filtered from a chart link; long IDs stay on one line. |
| Shipment detail, Freight Audit, Document Intelligence, Assistant, Audit History, Reports | Reviewed in screenshots, not changed. They already had evidence panels, badges and empty states, and changing them the night before would add risk without a clear gain. |

### Charts, their data and interactivity

Every figure comes from an existing API response. No endpoint was added for the charts, and no value is
invented or calculated in the browser beyond counting and arranging.

| Chart | Type | Data source | Interactive |
|---|---|---|---|
| Key figures (shipments, with discrepancies, needs review, invoice variance) | KPI cards (existing) | `/api/overview` `kpis` | Tooltip definitions |
| Audit outcomes | Ring with total and list | `/api/overview` `status_counts` | Each result opens Shipment Explorer filtered by it |
| Where discrepancies come from | Horizontal stacked bars | `/api/shipments`: `quantity_status`, `delivery_status` and `invoice_status` of every shipment, counted as passed, discrepancy or could not be verified. An unknown or missing status is never counted as passed. | Hover for exact counts |
| Shipments needing attention | Ranked table | `/api/shipments` (`discrepancy_count`, `needs_review`, `invoice_difference`), sorted most discrepancies first, then largest difference | Links to the shipment and to its Freight Audit evidence |
| Invoice totals against their line items | Grouped bars | `/api/exposure` rows: `stated_total`, `line_item_sum` | Hover. An invoice with an unreadable amount is left out and counted in a note; more than one currency shows a message instead of a shared axis. |
| Invoice differences | Signed bars, labelled | `/api/exposure` rows: `signed_difference` (red above, amber below, blue when they add up) | Hover |
| Discrepancy categories | Bar list (existing, now linked) | `/api/overview` `category_counts` | Each category opens Shipment Explorer filtered by it |
| Exception review queue | Proportion bar (existing) | `/api/overview` `reviews` | Link to the queue |
| Carriers | Bar list (existing) | `/api/overview` `carriers` | |
| Documents by type | Stacked bars | `/api/documents` files: `document_type` and extraction `status` (text read / some pages without text / not usable) | Hover; link to the documents |
| Financial exposure, document readiness | KPI cards (existing) | `/api/overview` `exposure`, `completeness` | |
| Exceptions by category | Bar list of buttons | `/api/exceptions` list | Selecting a category filters the list (`aria-pressed`) |
| Exceptions by severity | Proportion bar | `/api/exceptions` `severity` (HIGH, MEDIUM, LOW) | |
| Exceptions by review state | Proportion bar | `/api/exceptions` `status` (all four states, including zeros) | |

Colours are the same everywhere: red for a discrepancy, blue for passed or clean, amber for warning or in
review, grey for not verified. Colour is never the only signal. Each chart has a legend or labels, and
each value is either written next to it or shown in a tooltip. Every bar chart also renders a
screen-reader table with its exact numbers; the ring and the proportion bars list their values as text.
Money is formatted with its currency symbol and never mixed with counts on one axis. Every chart has its
own empty state ("No shipment to check", "No documents stored", "Several currencies", and so on); there
is no sample or placeholder data.

### Requested visualisations that the data does not support

- **Trend over time.** Trends need recorded audit runs. The demo data has none (`/api/history/runs`
  returns `[]`; `/api/trends` says *"No audit history has been recorded yet"*). The existing Audit History
  screen shows trends once runs are recorded, so no trend chart was put on the Overview.
- **Expected against invoiced amount.** There is no expected or contracted amount: the samples have no
  rate confirmation, and the extractor outputs no rate-confirmation amount. The invoice chart therefore
  compares the stated total with its own line items, and says so.
- **Processing funnel.** The ingestion register keeps each file's current state (queued, processing,
  indexed, partial, failed, unsupported), not how many files passed each stage, so a funnel would
  misrepresent it. "Documents by type" shows the outcome instead.
- **Model confidence or accuracy.** There is no validated measure, so there is no chart.

### Tests, build and visual checks

- **Frontend:**
  - 15 new tests:
    - data functions
    - each chart's numbers
    - links and navigation
    - a failed chart that leaves the rest of the page in place
    - partial data (left-out invoice, empty shipment and document lists)
    - mixed currencies
    - the loading state
    - the Exception Center summary and filter
    - pre-filtered Shipment Explorer
  - 199 of 199 frontend tests pass; typecheck and build exit 0.
- **Browser:** headless Chrome over the DevTools protocol, against the real API on a demo folder holding
  the six sample PDFs.
  - Screenshots of the Overview, Exception Center and Shipment Explorer (also pre-filtered) at
    **1366×768** and **1920×1080**.
  - **0 console errors and 0 failed requests** on every page captured.
  - The defects found this way (cut-off labels, axis range, wrapping IDs) were fixed and re-checked.
- **Totals against the API:** a script read each chart's numbers from the live page and compared them
  with the API responses fetched in the same page. **13 of 13 agreed**:
  - ring = `status_counts`, and its total = identified shipments
  - each check row = the shipment count
  - invoice bars and differences = exposure rows
  - absolute differences = overview variance; stated totals = KPI
  - documents chart = files listed
  - attention table, review queue and severity summary = their sources
- **Not tested:** a dark theme (none was introduced), touch devices, screens narrower than 1366 px
  beyond the existing responsive layout, and a screen reader in use (the data tables and labels are in
  place, but no screen reader was run).

### Suggested dashboard sequence for the presentation

1. **Overview, empty:** the page explains itself before any upload.
2. **Upload the six sample PDFs** (Document Intelligence), then return to the Overview. The figures:
   2 shipments, 1 with discrepancies, $120.00 variance.
3. **Audit outcomes ring:** click *Discrepancies detected*. Shipment Explorer opens already filtered.
   Back.
4. **Where discrepancies come from:** each of the three checks failed for one shipment.
5. **Shipments needing attention:** *Open audit* on SHJ-2026-001 to show the evidence behind each
   finding.
6. **Invoice charts:** $1,550.00 stated against $1,430.00 in line items. Say clearly that it is an
   arithmetic mismatch, not proof of overbilling.
7. **Exception Center:** the three summary cards; click a category to filter; start a review on one
   exception. The review-state bar moves; the audit result does not.
8. **Reports:** download the shipment's PDF report.
