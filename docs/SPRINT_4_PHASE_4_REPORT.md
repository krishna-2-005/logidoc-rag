# Sprint 4, phase 4 report

Release readiness: a staging rehearsal, the runbook and configuration reference, the document text cache's
security, release versioning with its checks, and the acceptance checklist.

Date: 2026-11-20. Branch `feature/production-readiness`, starting from `b9a2893`. Plan:
[SPRINT_4_PLAN.md](SPRINT_4_PLAN.md).

**Outcome:** release candidate `1.0.0-rc.1` passes everything that can be verified on this machine. It is **not
ready for production**: the items that need a real server, a real browser or an outside party are NOT VERIFIED
([RELEASE_ACCEPTANCE_CHECKLIST.md](RELEASE_ACCEPTANCE_CHECKLIST.md)).

## 0. Preflight

- **CI for `b9a2893`:** green on all five jobs, in both the push and the pull-request runs.
- **Branch:** local and remote at `b9a2893`, with a clean working tree.
- **Disk:** C: 3.4 GB and D: 3.3 GB free.
- **The 2026-11-15 backup:** 23 of 23 files match the manifest, and the manifest its own digest (read-only).
- **Real data:** the 34 files of `data/` and `chroma_db/` unchanged (`sha256sum -c`, exit 0).
- **Decision applied:** the document text cache stays on disk at `data/cache/page_text.cache`, with the safeguards
  of section 4.

The application was never started against the real databases.

## 1. Task 1, the staging rehearsal

`scripts/staging_rehearsal.py` (new) runs the real server in the production profile on an isolated folder of
synthetic data. [STAGING_REHEARSAL.md](STAGING_REHEARSAL.md)

**Environment used:** the development laptop (Windows, Python 3.12.7); no real staging server was available.

| Real | Simulated (reported as SIMULATED) |
|---|---|
| `python src/api/serve.py` with `LOGIDOC_ENV=production` | The HTTPS proxy: its headers only, no TLS |
| The built React application, SQLite, ChromaDB, the text cache | The model service: a stand-in for Ollama |
| `manage_users.py`, `manage_backups.py` | Webhook delivery, scheduled job runs, the TMS |

**Isolation:** the calling shell's `LOGIDOC_*` settings are dropped, the installation's folders are refused as a
work folder, and accounts and the signing key are generated per run. The documents are four synthetic PDFs.

**Result of the final run** (fresh folder, final code and frontend build): **63 passed, 0 failed, 4 simulated.**

| Step | Passed | Simulated |
|---|---|---|
| 1. Configuration and secrets | 4 | |
| 2. Startup and shutdown | 3 | |
| 3. Frontend and API | 5 | |
| 4. Sign-in and roles | 8 | |
| 5. Synthetic document ingestion | 4 | 1 (embeddings) |
| 6. Freight audit and exceptions | 5 | |
| 7. Correction approval and controlled re-audit | 4 | |
| 8. Risk and reporting | 3 | |
| 9. Webhooks and scheduler | 4 | 3 (delivery, job runs, TMS) |
| 10. Monitoring and health | 8 | |
| 11. Backup, verification and isolated restore | 10 | |
| 12. Migration guard on old-schema databases | 5 | |

**Three runs were made, each in its own folder under `D:\LogiDoc-staging`, all kept:**

1. The first failed 5 checks. All five were wrong expectations in the new script, not application faults: a
   field name, a required request body, the scheduler being idle by design, a rejected file counted as
   "unsupported", and a Windows exit code.
2. After correcting the script: 63 passed.
3. With the final code and build: 63 passed.

**Not verified by the rehearsal:** TLS and certificates, the proxy configurations as deployed, Linux and systemd,
Ollama with real models, a delivered webhook or alert, scheduled backups over days, load, a real browser, a
penetration test.

## 2. Task 2, operational documentation

| Document | What it covers |
|---|---|
| [RELEASE_RUNBOOK.md](RELEASE_RUNBOOK.md) (new) | What runs where; the checks before a deployment; install and update; the migration preflight; start, stop and exit codes; backups; monitoring, alerts and logs; scheduler ownership, webhooks and the TMS limitation; checks after a deployment; troubleshooting; incidents; a failed deployment; rollback; versions and promotion |
| [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md) (new) | Supported Python and Node.js versions; the profiles and what production requires; every setting with its default; the secrets, how to generate and where to keep them |
| [STAGING_REHEARSAL.md](STAGING_REHEARSAL.md) (new) | The rehearsal: what is real and simulated, each step, the result, what remains unverified |
| [DOCUMENT_TEXT_CACHE.md](DOCUMENT_TEXT_CACHE.md) (new) | Section 4 below |

The runbook states explicitly that restoring a pre-upgrade backup discards every record created after it, and
lists what that means. No credential, user path or document content is in any of these documents.

## 3. Task 4, versioning, changelog and CI gates

- **Version:** `1.0.0-rc.1`, in one place (`src/release.py`), as Semantic Versioning with `-rc.N` for a candidate.
  The frontend's `package.json` already carried `1.0.0`; there was no other convention and no tag.
- **`CHANGELOG.md` (new):** the first entry describes everything on the branch, with an "Upgrading" section and the
  known limitations.
- **How the running application reports it:**
  - `GET /api/settings` (`"release"`, signed-in users only), shown on the *Settings* page;
  - the start-up log line;
  - `python src/api/serve.py --version`, which reads no setting.

  `/api/health` is unchanged and public, and says nothing about the version.
- **`scripts/check_release.py` (new), run in CI** after the Python tests. It fails when:
  - the version is malformed;
  - the changelog's newest entry is not that version;
  - the web application's version differs;
  - the published API contract or permission inventory is out of date;
  - a release document is missing;
  - the cache, databases, documents, index, backups or `.env` would not be git-ignored.
- **Promotion** is documented in the runbook (section 14): candidate, rehearsal, checklist, then the same commit
  re-versioned, tagged and merged by the repository owner.

Every existing CI check is kept: Python 3.10 and 3.12, the frontend typecheck, tests, build and size budget, the
dependency audits, the whitespace and authorship checks, the evaluation guard. No tag, GitHub release or merge
was made.

## 4. Task 3, the document text cache's security

**Review:**

| Question | Finding |
|---|---|
| Who can read it? | It was created with default permissions. **Fixed:** owner-only on POSIX (folder `0700`, file `0600`). On Windows it inherits the data folder's access control list, documented with `icacls` commands |
| Is it in git? | No: `data/cache/` is ignored, and `check_release.py` now fails if it were not |
| Is it in a backup or a restore? | No (tested): a backup lists no cache file, and a restored installation has none |
| Is it in deployment artifacts or diagnostic bundles? | The data folder is outside the release folder in production. The application has no diagnostic bundle; the document says what to leave out of a manual one |
| Is its text ever logged? | No (tested): the one warning names an error type only, never text or a path |
| Do removed or replaced documents leave entries? | No: they are removed at the next audit. **Hardened:** deleted text is now overwritten in the file (`secure_delete`), tested by searching the file's bytes |
| Can it be cleared safely? | **New:** `scripts/manage_page_cache.py status` and `clear --confirm`. Clearing needs the server stopped (it takes the data folder's lock) and removes the cache only |
| What if it is damaged or unavailable? | Unchanged: it is ignored and the documents are parsed (tested) |
| Retention and encryption | Documented: retention follows the documents; disk encryption of the data volume is recommended, not done by the application |

Extraction results and audit behaviour are unchanged: the cache tests still compare an audit with and without it.
No real cache exists, and none was created or deleted.

**`tests/test_page_cache_security.py` (new, 10 tests, 1 skipped on Windows):**

- no log line holds the text;
- no backup or restore holds the cache;
- replaced and removed text is gone from the file;
- entries never accumulate over six replacements;
- owner-only permissions (POSIX: skipped on Windows, runs nowhere in CI's Python jobs, which are on Windows);
- `describe` gives counts, never text;
- `status` prints no text or path;
- `clear` needs `--confirm` and a stopped server, removes only the cache, and the next audit rebuilds it.

## 5. Task 5, the acceptance checklist

[RELEASE_ACCEPTANCE_CHECKLIST.md](RELEASE_ACCEPTANCE_CHECKLIST.md) (new) gives PASS, FAIL or NOT VERIFIED for 29
items in 16 areas, and says for each whether it is automated, rehearsed or manual.

- **PASS (17):** CI and tests; the end-to-end workflow; application security and authorization; the cache's
  application-level security; backup and restore; migration safety; the local staging rehearsal; API and
  the frontend in a simulated browser; monitoring and logging; webhook and scheduler logic; reporting; performance
  baselines; dependency security; documentation; known limitations.
- **FAIL (1):** the plan's performance targets at 1,000 shipments (0.6 to 1.6 s against 300 ms to 1 s), already
  reported in phase 3.
- **NOT VERIFIED (11):** a penetration test; TLS and the proxy; cache permissions on the host; scheduled backups
  over time; the real databases' upgrade; a real staging server; a real browser; alert delivery and log
  retention; webhook delivery to a real receiver; TMS and ERP; real-document accuracy. Load is also unverified.

**Verdict: not ready for production.** The checklist ends with what blocks a release, in order.

## 6. Validation

| Check | Result |
|---|---|
| New Python tests (3.12): release 6, cache security 10 | 15 passed, 1 skipped (POSIX permissions), exit 0 |
| Affected files (3.12): the new files, page text cache, API, production configuration, authorization, extraction end to end, the end-to-end workflow, migration guard, backup verification, readiness, request efficiency, API contracts | 283 passed, 2 failed, 1 skipped. Both failures were in the new `tests/test_release.py`: the checklist was not written yet, and an assertion matched an unrelated Windows variable. After both were fixed, that file and the cache security file: 15 passed, exit 0 |
| Affected files (3.10): release, cache security, page text cache, API, production configuration, the end-to-end workflow, migration guard, extraction end to end, readiness | 216 passed, 1 skipped, exit 0 |
| **Full Python suites** | **not run locally:** with 3.3 GB free the page file growth of a full run was too risky. CI runs both full suites on the push |
| Frontend typecheck | exit 0 |
| Frontend tests | 15 files, 281 tests passed, exit 0 |
| Frontend build and size budget | exit 0; first load 114.6 kB gzip |
| Staging rehearsal (final) | `RESULT: PASS`, 63 passed, 0 failed, 4 simulated, exit 0 |
| `scripts/check_release.py` | `RESULT: PASS`, exit 0 |
| OpenAPI and permission inventory | `--check` up to date: no change |
| Deterministic evaluation guard (3.12 and 3.10) | `RESULT: PASS`, exit 0 both |
| Audit immutability, correction and re-audit, migration guard | their tests pass in the runs above, and in the rehearsal |
| No sensitive data leakage | the rehearsal's log has no password, signing key or document text; cache text is never logged (tests) |
| Real data | the 34 files unchanged (`sha256sum -c`, exit 0); no cache folder in `data/`; nothing upgraded |
| The 2026-11-15 backup | 23 of 23 files match the manifest |
| `git diff --check`, attribution | clean |

## 7. Changed files

- **New, source and scripts:** `src/release.py`, `scripts/staging_rehearsal.py`, `scripts/check_release.py`,
  `scripts/manage_page_cache.py`
- **Changed, source:**
  - `src/page_text_cache.py`: owner-only permissions, `secure_delete`, `describe`
  - `src/api/routes_ingestion.py`: `release` in the settings answer
  - `src/api/serve.py`: `--version`, the release in the start-up line
- **Frontend:** `frontend/src/pages/Settings.tsx` and `frontend/src/lib/types.ts` (the release row);
  `frontend/src/test/settings_release.test.tsx` (new)
- **Tests, new:** `tests/test_release.py`, `tests/test_page_cache_security.py`
- **CI:** `.github/workflows/ci.yml` (the release readiness step)
- **Documentation, new:** `CHANGELOG.md`, `docs/RELEASE_RUNBOOK.md`, `docs/CONFIGURATION_REFERENCE.md`,
  `docs/STAGING_REHEARSAL.md`, `docs/DOCUMENT_TEXT_CACHE.md`, `docs/RELEASE_ACCEPTANCE_CHECKLIST.md`, this report
- **Documentation, changed:** `docs/SPRINT_4_PLAN.md` (status), `README.md`

**Not committed, kept:** the three rehearsal folders under `D:\LogiDoc-staging`, the backups, restored copies and
earlier rehearsal folders on D:, and the temporary folders. Nothing was deleted.

**Not touched:** `data/`, `chroma_db/`, the PDFs, the verified backup, the evaluation outputs, `main`, any other
project.

## 8. Remaining release blockers and limitations

**Blockers, in order** (from the checklist):

1. A real staging deployment behind TLS and a proxy.
2. Protection of the data folder on the host, the text cache included.
3. The real databases' upgrade, after a fresh verified backup and a rehearsal on its restore.
4. A person using the built application in a real browser.
5. A threat model and a penetration test.
6. A decision on the measured performance.
7. Real-document accuracy, if real documents become available.

**Limitations of this phase's work:**

- The rehearsal ran on Windows only; the Linux deployment files are untested.
- The POSIX permission test runs on no CI job (the Python jobs are on Windows).
- The rehearsal is not part of CI: it takes about three minutes and starts real servers. It is a step of the
  runbook.
- The full Python suites were not run locally in this phase.

## 9. Disk space

- **Start:** C: 3.4 GB and D: 3.3 GB free.
- **End:** C: 3.3 GB and D: 3.3 GB free. The three rehearsal folders take 9 MB.

## 10. Commit and push

Two commits on `b9a2893`, staged by exact paths, by the repository owner account: the code, scripts, tests and
CI step; then the documentation. Nothing was merged into `main`, no tag or release was created, and Sprint 5 was
not started.
