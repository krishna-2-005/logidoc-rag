# Release acceptance checklist

Release candidate `1.0.0-rc.1`, assessed on 2026-11-20 on branch `feature/production-readiness`.

**Verdict: not ready for production.** Everything that can be verified by automation on this machine passes. What
needs a real server, real documents or an outside party has not been verified, and several of those items block
a production release (section 4).

## 1. How to read this

| Status | Meaning |
|---|---|
| **PASS** | Verified, with the evidence named |
| **FAIL** | Verified and not acceptable |
| **NOT VERIFIED** | Not tested: no claim is made |

| Kind | Meaning |
|---|---|
| Automated | A test, a script or a CI job checks it on every change |
| Rehearsed | Checked by `scripts/staging_rehearsal.py` on synthetic data, in the production profile, with a simulated proxy and model service ([STAGING_REHEARSAL.md](STAGING_REHEARSAL.md)) |
| Manual | A person must do it on a real environment |

## 2. Checklist

| # | Area | Status | Kind | Evidence, or what is missing |
|---|---|---|---|---|
| 1 | CI and automated tests | **PASS** | Automated | CI is green on all five jobs for `b9a2893`, the commit this phase started from. Locally for this candidate: affected Python tests on 3.12 and 3.10, frontend typecheck, 281 tests, build and size budget. The run for the commit that adds this file is reported in [SPRINT_4_PHASE_4_REPORT.md](SPRINT_4_PHASE_4_REPORT.md) and must be green before promotion |
| 2 | End-to-end synthetic workflow | **PASS** | Automated, rehearsed | `tests/test_end_to_end_workflow.py`: one shipment from upload to report in 15 steps. The rehearsal repeats it against the real server process |
| 3 | Security and authorization | **PASS** for the application | Automated, rehearsed | Every one of 126 operations called without a session, as a reviewer and as an administrator; CSRF on every write; rate limits; production settings validated at startup; Secure, HttpOnly, SameSite=Strict cookie and trusted host in the rehearsal |
| 3a | Penetration test | **NOT VERIFIED** | Manual | None has been done. No threat model is written |
| 3b | TLS and the reverse proxy | **NOT VERIFIED** | Manual | No certificate, domain or proxy has been deployed. The proxy was simulated by headers |
| 4 | Document-text cache security | **PASS** for the application | Automated | `tests/test_page_cache_security.py`: never logged, never in a backup or a restore, replaced and removed text gone from the file, owner-only permissions on POSIX, safe clear and rebuild. `check_release.py`: git-ignored |
| 4a | Cache file permissions on the production host | **NOT VERIFIED** | Manual | The NTFS access control list, or the POSIX owner and mode, must be set and checked on the real data folder ([DOCUMENT_TEXT_CACHE.md](DOCUMENT_TEXT_CACHE.md), section 2). Disk encryption is recommended, not checked |
| 5 | Backup and restore | **PASS** | Automated, rehearsed | A restored copy reproduces audit, corrections, history, reviews, risk and reports exactly (test and rehearsal). The real data's first backup (2026-11-15) still matches its manifest, 23 of 23 files |
| 5a | Scheduled backups over time | **NOT VERIFIED** | Manual | The schedule, retention and weekly restore drill have not run for days on a real disk |
| 6 | Database migration safety | **PASS** | Automated, rehearsed | The upgrades were rehearsed on copies of the real data's backup (reviews 2 → 3, history 3 → 6): every row kept. The migration guard refuses an upgrade without a fresh verified backup, in tests and in the rehearsal |
| 6a | The real databases' upgrade | **NOT VERIFIED** | Manual | Not run: the real databases are still at reviews 2 and history 3, on purpose. It is forward-only |
| 7 | Staging deployment | **PASS** as a local rehearsal | Rehearsed | 63 checks passed, 0 failed, 4 simulated, in the production profile on synthetic data |
| 7a | A real staging server | **NOT VERIFIED** | Manual | None was available. Linux, systemd, the proxy configurations and a service account were not exercised |
| 8 | API functionality | **PASS** | Automated | The published OpenAPI contract matches the code; typed contracts are tested |
| 8a | Frontend functionality | **PASS** in a simulated browser | Automated | 281 component and page tests; the built application is served in the rehearsal |
| 8b | A real browser | **NOT VERIFIED** | Manual | No real-browser test exists (deferred). Nobody has clicked through the built application for this candidate |
| 9 | Monitoring and logging | **PASS** for the application | Automated, rehearsed | Health, readiness, metrics, alerts and request IDs; the production log is JSON lines without passwords, the signing key or document text |
| 9a | Alert delivery and log retention on a host | **NOT VERIFIED** | Manual | No alert has been received by a real receiver; no log shipping or retention is set up |
| 10 | Webhooks | **PASS** for configuration and delivery logic | Automated | Signing, retries, idempotency and recovery are tested with a stand-in receiver; unsafe destinations are refused in the rehearsal |
| 10a | Webhook delivery to a real receiver | **NOT VERIFIED** | Manual | Nothing has been delivered over the internet |
| 10b | Scheduled jobs | **PASS** for the scheduler's logic | Automated | Leases, overlap protection and recovery are tested. In the rehearsal the scheduler was idle, as designed without an endpoint |
| 10c | TMS and ERP | **NOT VERIFIED** | Manual | Only a synthetic sandbox adapter exists, and it is refused in production. No real vendor is connected |
| 11 | Reporting and exports | **PASS** | Automated, rehearsed | Report totals per currency, CSV formula-injection protection, plain PDFs; downloads in the rehearsal |
| 12 | Performance baselines | **PASS** as measured baselines | Automated (bundle), measured (the rest) | First load 114.6 kB gzip, enforced in CI. Request and restart figures at 100 and 1,000 synthetic shipments in [PERFORMANCE_BASELINE.md](PERFORMANCE_BASELINE.md) |
| 12a | Performance targets and load | **FAIL** against the plan's targets; load **NOT VERIFIED** | Measured | At 1,000 shipments a warm request takes 0.6 to 1.6 s on the development laptop (target 300 ms to 1 s). No test with several users at once, and none on server hardware |
| 13 | Dependency security | **PASS** | Automated | `pip-audit` and `npm audit` run in CI on every push and weekly; no known vulnerability on `b9a2893` |
| 14 | Documentation completeness | **PASS** | Automated for existence | Runbook, configuration reference, staging rehearsal, cache guide, migration, backup, deployment, permissions and dependency policy exist (`check_release.py`). Nobody other than the author has followed the runbook |
| 15 | Extraction accuracy on real documents | **NOT VERIFIED** | Manual | No permitted real documents: accuracy is measured on synthetic and sample PDFs only. No OCR |
| 16 | Known limitations | **PASS** as documented | Manual | Listed in the changelog and section 3 |

## 3. Known limitations of this release

- One server process and SQLite: one site, no horizontal scaling.
- No OCR: image-only PDFs are reported as needing it.
- Amounts are never converted or added across currencies.
- Until an exception is recorded it has no webhook and no history ([EXCEPTION_RECORDING.md](EXCEPTION_RECORDING.md)).
- A restart at 1,000 shipments takes about a minute; the Streamlit app does not use the text cache.
- Rate limits are per process and reset on restart.
- The audit history is append-only but not tamper-evident (deferred).
- Upgrades are forward-only: rolling back means restoring a backup and losing what came after it.

## 4. What blocks a production release

In order:

1. **A real staging deployment** behind TLS and a proxy (items 3b, 7a, 9a, 10a), following
   [RELEASE_RUNBOOK.md](RELEASE_RUNBOOK.md).
2. **Protection of the data folder on the host,** the text cache included (4a).
3. **The real databases' upgrade,** after a fresh verified backup and a rehearsal on its restore (6a).
4. **A person using the built application in a real browser** (8b).
5. **A security review:** a threat model and a penetration test (3a).
6. **A decision on performance** (12a): accept the measured figures for the expected volume, or do the further
   work first.
7. **Real-document accuracy** (15), if real documents become available.

Not blocking, by decision: a real TMS vendor (10c), real-browser automated tests, WAL mode, tamper-evident
history.

## 5. Sign-off

| Role | Name | Date | Decision |
|---|---|---|---|
| Repository owner | | | |
| Operations | | | |
| Security | | | |

No tag, GitHub release or merge into `main` exists for this candidate. They are made by the repository owner
after sign-off ([RELEASE_RUNBOOK.md](RELEASE_RUNBOOK.md), section 14).
