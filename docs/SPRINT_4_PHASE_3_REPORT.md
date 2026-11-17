# Sprint 4, phase 3 report

Performance and scalability: the frontend bundle, request efficiency, the document text cache and database
queries.

Date: 2026-11-17. Branch `feature/production-readiness`, starting from `f13dfbf`. Plan:
[SPRINT_4_PLAN.md](SPRINT_4_PLAN.md).

## 0. Preflight

- **CI for `f13dfbf`:** green on all five jobs, in both the push and the pull-request runs.
- **Branch:** local and remote at `f13dfbf`, with a clean working tree.
- **Disk:** C: 7.2 GB and D: 3.4 GB free at the start.
- **Baselines:** taken on synthetic data in temporary folders. The real API and Streamlit app were not started.

**How the figures were measured.** Timings on this laptop swing by up to 8 times with memory pressure. So every
before and after below comes from running the committed code (`f13dfbf`, extracted to a scratch folder) and the
new code back to back with the same script. The ratios are the reliable part; the absolute times are this
machine's.

## 1. Task 1, the frontend bundle

**Before:** one JavaScript chunk, loaded in full before the sign-in page.

**Now:**

- every page is loaded when its route is first opened (`React.lazy` in `frontend/src/App.tsx`);
- the charting library is in its own chunk, fetched only by pages that draw charts;
- the sign-in page and the application shell stay in the first load.

| | Before | After |
|---|---|---|
| First load (what `index.html` names) | 1,168.94 kB, 327.51 kB gzip | 351.0 kB, 114.6 kB gzip (65% less) |
| Largest chunk | 1,168.94 kB | 388.2 kB (charts, 111.5 kB gzip) |
| Chunks | 1 | 71 |
| All JavaScript | 1,168.9 kB | 1,200.7 kB |

The first screen after sign-in (Overview) also draws charts, so it fetches the charts chunk: about 235 kB gzip
in total, still 28% less than before.

**Navigation, loading and errors:**

- **Shell first.** The shell (navigation, search, status) renders at once; the page area shows the usual loading
  skeleton while a page's code arrives (`RouteBoundary`).
- **A page whose code cannot be fetched** (offline, or the server was updated since the tab was opened) shows an
  error with "Try again", which reloads the application. Before, any page error blanked the screen.
- **Deep links and the redirect of unknown addresses** work as before.

**Budget:** `npm run size` (`frontend/scripts/check-bundle.mjs`, no new dependency) fails when the first load
exceeds 150 kB gzip or any chunk 500 kB. CI runs it after the build.

**Tests:** `frontend/src/test/lazy_routes.test.tsx` (new, 5 tests) covers:

- the shell and a lazy page;
- navigation between lazy pages;
- a deep link and the redirect;
- the loading state;
- the load-failure state with its retry.

## 2. Task 2, request efficiency

**What the profile showed.** A warm read opens one database connection, so connections were not the waste. The
time went to `stat` calls: every cached value re-fingerprinted the whole document folder, 4 to 5 times per
request, each time with a directory listing and two `stat` calls per file.

**Changes:**

- **One `stat` per file.** The fingerprint lists the folder once and stats each PDF once. Its value is
  identical (tested).
- **One lookup per read request.** Inside a GET request the audit state key is computed once
  (`audit_service.request_scope`). Safeguards:
  - it is never kept beyond the request, so the next request always sees a change;
  - a change made through the service clears it;
  - POST and PATCH requests do not use it at all.

Nothing is cached across requests or users, so no authorization boundary and no stale snapshot is involved.

| Warm request, median | 100 shipments (two runs each) | 1,000 shipments (one run) | Folder listings per request |
|---|---|---|---|
| Overview | 91, 78 → 39, 29 ms | 4,711 → 1,173 ms | 4 → 1 |
| Shipment list | 38, 36 → 17, 17 ms | 2,140 → 576 ms | 2 → 1 |
| Shipment detail | 104, 91 → 27, 27 ms | 5,602 → 1,300 ms | 5 → 1 |
| Exceptions | 94, 85 → 71, 32 ms | 5,037 → 1,617 ms | 4 → 1 |
| Carriers | 89, 113 → 36, 33 ms | 4,822 → 1,379 ms | 4 → 1 |
| Risk overview | 92, 131 → 46, 34 ms | 4,966 → 1,456 ms | 4 → 1 |
| Risk table | 88, 86 → 37, 37 ms | 4,909 → 1,339 ms | 4 → 1 |
| Report preview | 122, 130 → 37, 45 ms | 4,941 → 1,436 ms | 4 → 1 |
| Report CSV | 125, 122 → 41, 35 ms | 4,923 → 1,546 ms | 4 → 1 |

Requests are 2 to 4 times faster at both sizes.

**Tests:** `tests/test_request_efficiency.py` (new, 5 tests):

- the fingerprint equals the former definition;
- each of 8 read routes lists the folder exactly once;
- a document added or removed between two requests is seen;
- the state key is kept for one scope only, and cleared by a change;
- a POST is never memoised.

## 3. Task 3, the document text cache

**Before:** the only cache was in memory, for the freight audit extraction. Every restart parsed every PDF, and
every recalculation parsed every PDF again for the reconciliation. A cold start spent 75% of its time parsing.

**Now:** `src/page_text_cache.py` (new) keeps the text PyMuPDF reads from each page.

| Aspect | Behaviour |
|---|---|
| Identity | The SHA-256 of the file bytes plus the PyMuPDF version. Never a name, a size or a time |
| Replaced document | Other bytes, another hash: always parsed afresh |
| Same content under another name | A hit |
| Library upgrade | Every entry is a miss, and the old ones are removed |
| What is stored | Exactly what the parser returned, as JSON. Classification, identifiers, chunks and fields are still computed on every audit by the same code, so results and provenance are identical (tested against a run with no cache) |
| Removed document | Its entry is removed at the next audit |
| Corrupted document | Reported as unreadable as before, and never stored |
| Damaged, locked or uncreatable cache | Ignored: the documents are parsed, the audit works |
| Where | `data/cache/page_text.cache`, git-ignored, not part of a backup (it is rebuilt) |

| Audit of N synthetic shipments (4 PDFs each) | 100: before → after | 1,000: before → after |
|---|---|---|
| First start (nothing cached) | 9.95 → 9.47 s | 176 → 153 s |
| Restart, nothing changed | 1.85 → 0.59 s | 121 → 62 s (8,000 page reads from the cache, none parsed) |
| Recalculation after one shipment changed | 1.11 → 0.40 s | 85 → 48 s |
| Same audit result after the restart | yes | yes |

**Tests:** `tests/test_page_text_cache.py` (new, 12 tests) covers:

- hits and misses, and a restart;
- identity by content;
- loader and extractor output with and without the cache;
- a parser version change;
- a replaced document (only it is parsed, and the audit changes);
- a removed document;
- a damaged cache file, a cache that cannot be created, and an undecodable entry;
- a corrupted document, then its repair;
- no cache at all.

**Decision to confirm.** The cache holds document text on disk, beside the documents and the search index (which
already holds it). The application stores (`*.sqlite3`) still hold none: an existing test guards that, and it
caught my first version, which had named the cache like a store. If you would rather not keep parsed text on
disk, the cache can be turned into a memory-only one by passing no path.

## 4. Task 4, database queries

**Measured first**, on an isolated synthetic integration store (200,000 deliveries, 200,000 attempts, 100,000 sync
runs over 180 days) and a history of 12,000 observations.

| Query (7-day window) | As is | With the planned indexes (`created_at`, `started_at`) |
|---|---|---|
| Webhook endpoint health | 612 ms | 607 ms (index not used) |
| Webhook monitoring, deliveries | 374 ms | 573 ms (slower) |
| Webhook monitoring, attempts | 241 ms | 617 ms (slower) |
| Stuck deliveries | 28 ms (already uses the status index) | 29 ms |
| Sync monitoring | 95 ms | 93 ms (index not used) |
| History: every observation (trends) | 145 ms, joined by primary key | — |
| History: one shipment timeline | 1.5 ms | — |

These timings were taken while the machine was slow; the same queries took 18 to 113 ms in an earlier run.

**Result: no index is added and no schema changes.** The two indexes the plan proposed speed up none of these
queries and slow two of them. The history trend reads every observation by design, through primary keys.

Consequences:

- the integration store stays at version 3 and the history at version 6;
- nothing for the migration guard to do, and no historical record touched;
- WAL mode was not enabled.

One correction along the way: my first measurement ran `ANALYZE`, which the application never does, and showed a
slow plan for the stuck-deliveries check that real stores do not have. The figures above are without it.

## 5. Validation

| Check | Result |
|---|---|
| New Python tests (3.12): page text cache 12, request efficiency 5 | passed, exit 0 |
| **Full suite (3.12)** | **2,490 passed, 1 failed** (25 min 25 s). The failure was the guard described in section 3 (document text found in a `.sqlite3` file: the cache). After renaming the cache file, that test file and the two new ones: 50 passed, exit 0. The full suite was not run again |
| Affected files (3.10): the two new files, extraction end to end, the phase 2 end-to-end, migration guard, read-only requests, freight audit, API, analytics cache, unreadable documents, line-item corrections | 232 passed, exit 0 |
| Frontend typecheck | exit 0 |
| Frontend tests | 14 files, 279 tests passed, exit 0 |
| Frontend production build and `npm run size` | exit 0; first load 114.6 kB gzip |
| Deterministic evaluation guard (3.12 and 3.10) | `RESULT: PASS`, exit 0 both |
| OpenAPI and permission inventory | `--check` up to date: no change |
| Audit immutability, migration guard | their tests pass in the runs above (end-to-end, migration guard) |
| Real data | the 34 files unchanged (`sha256sum -c`, exit 0); no cache folder was created in `data/` |
| The 2026-11-15 backup | 23 of 23 files still match the manifest (read-only check) |
| `git diff --check`, attribution | clean |

## 6. Changed files

- **Frontend:**
  - new: `frontend/src/components/common/RouteBoundary.tsx`, `frontend/src/test/lazy_routes.test.tsx`,
    `frontend/scripts/check-bundle.mjs`
  - changed: `frontend/src/App.tsx`, `frontend/src/components/layout/AppShell.tsx`, `frontend/package.json`,
    `frontend/vite.config.ts`
- **Backend:**
  - new: `src/page_text_cache.py`
  - changed: `src/audit_service.py` (request scope, page cache), `src/audit_analytics.py` (fingerprint),
    `src/api/app.py` (scope for GET), `src/document_loader.py` and `src/document_extraction.py` (optional cache)
- **Tests:**
  - new: `tests/test_page_text_cache.py`, `tests/test_request_efficiency.py`
  - changed: `tests/test_extraction_e2e.py` (a comment)
- **Other:**
  - `.github/workflows/ci.yml`: the size budget
  - `.gitignore`: `data/cache/`
  - `README.md`, `docs/SPRINT_4_PLAN.md`, `docs/PERFORMANCE_BASELINE.md`, this report

**Not touched:** `data/`, `chroma_db/`, the PDFs, the backups, the evaluation files, `main`, any other project.
No temporary folder was deleted.

## 7. Known limitations

- **The plan absolute targets are not met on this machine.** At 1,000 shipments a warm request takes 0.6 to
  1.6 s (target 300 ms to 1 s). What remains is mostly one `stat` per PDF per request (4,000 files). Trusting the
  directory listing for sizes and times would remove it, but those can lag on Windows, so it was not done.
- **A restart at 1,000 shipments still takes about a minute.** Every file is read to compute its hash, and the
  reconciliation and field extraction are recomputed. Recomputing only changed shipments (T3.5) is the next step,
  and a larger redesign.
- **The cache serves the API audit only.** The Streamlit app still parses on every recalculation.
- **Timings are noisy here.** Each comparison was made back to back, but the 1,000-shipment figures are single
  runs.
- **The Overview still loads the charts chunk** right after sign-in.

## 8. Disk space

- **Start:** C: 7.2 GB and D: 3.4 GB free.
- **End:** C: 0.55 GB and D: 3.4 GB free. The page file on C: grew during the full suite; a restart returns it.

## 9. Commit and push

Two commits on `f13dfbf`, staged by exact paths, by the repository owner account. Nothing was merged into
`main`, and phase 4 was not started.
