# Release candidate validation plan

How to close the remaining release-readiness gaps of `1.0.0-rc.1`, without adding product features.

Status: **draft for the owner's review.** Nothing in this plan has been implemented, deployed, committed or pushed.

Prepared on 2026-11-22 from `feature/production-readiness` at `5869d0a`.

## 0. Where things stand

| Fact | Value |
|---|---|
| CI for `5869d0a` | Green on all five jobs, in both the push and the pull-request runs |
| Branch | Local equals remote; working tree clean |
| Acceptance checklist | 17 PASS, 1 FAIL (performance targets), 11 NOT VERIFIED |
| Real data | 34 files unchanged since 2026-11-15 (`sha256sum -c`); reviews at schema 2, history at schema 3 |
| Real-data backup | The 2026-11-15 backup still matches its manifest, 23 of 23 files. It is the only one, on the same machine as the data |
| Free disk | C: 3.6 GB, D: 3.3 GB |

Every cost and effort figure below is an **estimate to be confirmed** by a quote or a price list: none was looked
up for this plan. Effort is in focused working days.

## 1. Remaining blockers, in priority order

| # | Blocker | Workstream | Why it blocks |
|---|---|---|---|
| B1 | No deployment behind real TLS and a real proxy has ever run | A | Sessions, cookies, forwarded addresses and rate limits depend on the proxy; only its headers were simulated |
| B2 | Nobody has used the built application in a real browser | B | 281 frontend tests run in a simulated browser; a broken click path would not be seen |
| B3 | No threat model, no independent security test | C | The authorization tests prove what was thought of, not what was missed |
| B4 | The data folder's permissions on a host are unverified, the text cache included | A, C | Document text sits in three places on disk; on Windows nothing restricts them by default |
| B5 | The real databases are not upgraded, and the only backup is on the same machine | E | The upgrade is forward-only; a disk failure today loses the data and its backup together |
| B6 | Performance fails the plan's targets, and load was never tested | D | 0.6 to 1.6 s per request at 1,000 shipments, on a laptop, with one user |
| B7 | Extraction accuracy on real documents is unmeasured | none | No permitted real documents exist. Not closable by this plan: it stays a stated limitation |

**Recommended order:** E's backup copy first (half an hour, removes a real risk today), then A, then B and C on
the staging environment, D alongside, and E's migration last, when everything else has passed.

## 2. Workstream A: a real staging environment

### What to stand up

One small Linux virtual machine, separate from the laptop and from any production host, with synthetic data only.

| Item | Requirement | Estimated cost |
|---|---|---|
| Virtual machine | Ubuntu LTS, 4 vCPU, 8 GB RAM, 50 GB disk. Ollama with the two models needs about 4 GB of RAM; no GPU is needed for a functional test, answers will be slow | 20 to 40 USD a month at a mainstream provider; keep it 2 to 4 weeks |
| Host name | A subdomain such as `staging.<your domain>` with one DNS A record | Free if you own a domain; otherwise 10 to 15 USD a year |
| TLS certificate | Let's Encrypt through Caddy (`deploy/Caddyfile`), automatic | Free |
| Disk encryption | The provider's volume encryption, or LUKS | Usually free |
| Webhook receiver | A second, throwaway HTTPS endpoint you control, to receive a real delivery | Free (a few lines on the same provider, or a request-inspection service; use no real data) |
| Alert receiver | Any HTTPS endpoint that accepts a POST | Free |

**Total for staging: roughly 20 to 60 USD,** if the machine is deleted after the validation.

### Credentials and configuration needed

- A provider account, and SSH access for one administrator: key-based only, password login off.
- A dedicated system user `logidoc` with no shell login, as in `deploy/logidoc-api.service`.
- `/etc/logidoc/logidoc.env` with mode `600`, from `deploy/logidoc.production.env.example`, with a newly generated
  `LOGIDOC_WEBHOOK_SECRET_KEY`. **No secret from the laptop or from production is reused.**
- Three staging accounts (two administrators, one reviewer) with new passwords.
- Firewall: 80 and 443 open; 22 limited to your address; 8000 and 11434 closed.

### Deployment steps (not to be run before approval)

1. Create the machine, the user, the folders (`/var/lib/logidoc/data`, `/var/lib/logidoc/index`,
   `/var/backups/logidoc` on a second volume if available), owned by `logidoc`, mode `700`.
2. Check out `5869d0a`, create the virtual environment, `pip install -r requirements.txt`; build the frontend.
3. Install Ollama, pull both models, bind it to loopback.
4. Install the service unit and Caddy with the repository's files; `serve.py --check`; start.
5. Run the acceptance steps below.

### Acceptance criteria and evidence

| Gate | Criterion | Evidence to keep |
|---|---|---|
| A1 TLS | The site answers on HTTPS with a valid certificate; HTTP redirects; HSTS is sent; no mixed content | `curl -sI` output for both schemes; a TLS scan grade from a public scanner |
| A2 Proxy | `X-Forwarded-For` is replaced, not appended; the API sees the real client address; `/api/admin/metrics` is not reachable from outside | A request with a forged header, and the logged client address |
| A3 Production profile | `serve.py --check` passes; the server refuses to start with one unsafe setting changed | Command output |
| A4 Permissions | Data, index and cache are `700`/`600` and owned by `logidoc`; another local user cannot read them | `ls -l` and a failed read as another user |
| A5 Workflow | The staging rehearsal's steps 3 to 8, done through the real host name | The rehearsal's checks re-run against the host (see note), or the browser tests of workstream B |
| A6 Monitoring and logs | `/api/ready` is `ready` with real Ollama; logs are JSON in journald; one alert is received | The readiness answer; a log excerpt; the alert as received |
| A7 Backup and restore | A scheduled backup runs unattended for 3 nights; one is restored into a new folder and matches | `manage_backups.py list`; the restore report |
| A8 Migration guard | An old-schema data folder: exit code 4 without a backup, upgrade with one | The service's log and exit status |
| A9 Webhooks | One endpoint registered; one `exception.created` delivered and its signature verified by the receiver; a forced failure retried | The delivery page; the receiver's record |
| A10 Scheduler | With an endpoint configured, the jobs run on time for 24 hours with no overdue alert | The Scheduled Jobs page |

Note on A5: `scripts/staging_rehearsal.py` starts its own server on loopback. Pointing its checks at an existing
host is a small script change (a `--base-url` option that skips steps 1, 2, 11 and 12). That is test tooling, not
a product feature, and is included in the effort below.

**Effort:** 2 to 3 days, including the script option and three nights of waiting for A7.

## 3. Workstream B: browser acceptance

### Approach

**Recommended: Playwright, run in CI only.** One small test project in `frontend/e2e/`, using Chromium alone. The
browser is downloaded on the CI runner, never on the laptop, so it costs no local disk. It runs against the real
server that `scripts/staging_rehearsal.py` already knows how to start, on synthetic data with the stand-in model
service.

| Option | Disk on the laptop | Maintenance | Verdict |
|---|---|---|---|
| Playwright in CI only (Chromium) | None. About 300 to 400 MB on the runner, plus one dev dependency | One file per flow, written with roles and labels the frontend tests already use | **Recommended** |
| Playwright on the laptop as well | 300 to 400 MB | Same | Only with your approval, and after a restart for disk |
| Manual script, no automation | None | Repeated by hand for every candidate | The fallback, and the first pass for rc.1 either way |

Either way, **one manual pass by a person is still required for rc.1**: automation proves the paths, a person
notices what looks wrong.

### Test cases

Each is one browser flow, on synthetic data. "Admin A" and "Admin B" are two administrators.

| # | Flow | Passes when |
|---|---|---|
| T1 | Sign in, sign out | A wrong password shows an error; a right one opens the Overview; after sign-out, a data page returns to sign-in and the browser's back button shows no data |
| T2 | Session expiry | A deactivated account is sent to sign-in at its next action |
| T3 | Reviewer's limits | The reviewer sees no administration, upload or integration controls, and a direct address to an administrator page shows no data |
| T4 | Upload | Admin A uploads the four synthetic PDFs; each gets a status; the shipment appears in the Shipment Explorer |
| T5 | Unusable upload | A file that is not a PDF is listed as rejected, with its reason |
| T6 | Freight audit | The shipment's page shows the fuel surcharge discrepancy with its evidence |
| T7 | Exception lifecycle | The reviewer assigns, starts and resolves an exception with a note and evidence; "dismiss" is not offered; the history shows each step |
| T8 | Correction | Admin A proposes the invoice line; the approve control is not usable by Admin A; Admin B approves |
| T9 | Controlled re-audit | The preview shows what changes; confirming needs a reason; afterwards both audits are in the history |
| T10 | Risk | The Shipment Risk page lists the shipment; its explanation's factors add up to the score shown |
| T11 | Reports | The freight audit summary previews with totals; filters apply; a filter the report cannot take is refused with a message |
| T12 | Downloads | CSV and PDF download with content, and the CSV's row matches the preview |
| T13 | Navigation | Every menu entry opens its page; a deep link opens directly; an unknown address returns to the Overview; the browser's back and forward work |
| T14 | Error states | With the API stopped, a page shows its error with "Try again"; a page whose code cannot load shows the reload message |
| T15 | Rate limit | A refused download shows the server's message, not a blank page |

**Evidence:** the Playwright report with a trace for each flow, attached to the CI run; and the manual pass as a
dated, signed copy of this table.

**Acceptance:** all 15 pass on Chromium in CI, and once by hand in a second browser (Firefox or Edge) on staging.

**Effort:** 3 to 4 days for the automated flows and the CI job; half a day for the manual pass.

## 4. Workstream C: security acceptance

### C1. Threat model (to be written, 1 day)

A short document, `docs/THREAT_MODEL.md`, with this structure:

| Section | Content |
|---|---|
| Assets | Document text (PDFs, index, text cache); audit results and history; review decisions; accounts and sessions; the webhook signing key; backups |
| Actors | An anonymous internet user; a signed-in reviewer; an administrator; someone with a stolen session; a local user of the host; a malicious document; a compromised webhook receiver or TMS |
| Entry points | The 126 API operations; uploaded PDFs; the web application; outbound webhooks and integration calls; the command-line scripts; the files on disk |
| Trust boundaries | Internet to proxy; proxy to application; application to disk; application to Ollama; application to outbound destinations |
| Threats, by boundary | Using STRIDE: for each, the existing control, the test that proves it, and the residual risk |
| Out of scope | Physical access; the operating system's own hardening; the provider |

Threats already known to need a line each: a malicious PDF (parser exploit, oversized file, embedded script); a
prompt injection in document text reaching the assistant; an administrator rewriting the history file (it is
append-only, not tamper-evident); document text at rest in three places; a single administrator account being
compromised.

### C2. Security testing checklist (half a day to write, 1 to 2 days to run on staging)

| Area | Check | Today |
|---|---|---|
| Authentication | Lockout, session expiry, cookie flags, no session fixation, sign-out invalidates server-side | Tested, except fixation |
| Authorization | Every operation as each role | Tested for all 126 |
| Object-level access | A reviewer reading another user's objects by identifier | **Not reviewed:** both roles see all shipments by design; confirm that is intended |
| CSRF and CORS | Writes need the header; only listed origins | Tested |
| Input | Path traversal in document names, oversized bodies, malformed PDFs, SQL in filters | Mostly tested; to be repeated through the proxy |
| Output | CSV formula injection, PDF content, no stack traces or paths in errors | Tested |
| Outbound | Webhook and integration destinations: private, loopback, redirects, DNS rebinding | Tested with a stand-in resolver; to be repeated with real DNS |
| Rate limits | Each rule, and the proxy's | Tested in the application; the proxy's untested |
| Headers | CSP, HSTS, frame, content-type, referrer | Tested in the application; to be checked through the proxy |
| Secrets | None in logs, errors, backups, git | Tested; git history to be scanned once with a secret scanner |
| Dependencies | Known vulnerabilities | In CI |
| Disk | Permissions of data, index, cache, backups, environment file | **Unverified on a host** |

### C3. Authorization and data-exposure review (1 day)

A table of every operation and export that returns document text, personal data or money, with who can read it.
It is the open item T2.6 of the Sprint 4 plan. Starting list, from the code:

- **Document text:** `GET /api/documents` (evidence passages), `GET /api/documents/file` (the PDF itself), shipment
  and exception detail (evidence), the assistant's answers and citations, the shipment PDF report.
- **Personal data:** account names in review history, corrections and reports; `GET /api/admin/users` and
  `/api/admin/events`.
- **Money:** every audit, exposure, risk and report answer.

The review answers, for each: is the role right, is anything returned that the screen does not need, and does it
reach a file that leaves the application.

### C4. Independent penetration test

| | |
|---|---|
| Scope | The staging host only: the web application and API as an anonymous user, a reviewer and an administrator. Grey-box: the tester gets the API contract, the permission inventory and three accounts |
| Not in scope | Any production system, real data, denial-of-service, the provider's infrastructure |
| Deliverable | A report with findings by severity, and a retest of the fixes |
| Who | A person or firm that did not write the code. For a low budget: an experienced independent tester for a fixed scope |
| Estimated cost | Widely variable: roughly 1,500 to 5,000 USD for an independent tester for 2 to 4 days; 8,000 USD and more for a firm. **Obtain two quotes** |
| Acceptance | No open critical or high finding; every medium finding fixed or accepted in writing by the owner |

If the budget does not allow it for rc.1, the documented alternative is an automated scan (OWASP ZAP baseline
against staging, free) plus the checklist of C2, with the checklist item left as NOT VERIFIED and the release
limited to a trusted internal network. That is a decision for the owner (D5).

### C5. Cache and document-storage permissions

On the staging host, then on the production host before go-live:

1. `ls -l` of the data folder, `raw/`, `cache/`, the index and the backup folder: owner `logidoc`, `700`/`600`.
2. As another local user: reading a PDF, the cache and a database all fail.
3. `manage_page_cache.py status` reports `0o600`.
4. Remove a document through the application, run one audit, and search the cache file's bytes for a string of
   that document: not found.
5. The volume is encrypted (the provider's console, or `lsblk -f`).

On a Windows host instead: `icacls` on the data folder shows the service account, Administrators and SYSTEM
only, with inheritance from the drive removed.

**Evidence:** the command outputs, kept with the release record.

## 5. Workstream D: performance acceptance

### What has been measured (the development laptop, synthetic data, one user)

| Measure | 100 shipments | 1,000 shipments |
|---|---|---|
| Warm API request | 17 to 71 ms | 0.6 to 1.6 s |
| Summary report PDF | 0.9 s (when the machine was not under memory pressure) | not measured |
| First start, nothing cached | 9.5 s | 153 s |
| Restart, nothing changed | 0.6 s | 62 s |
| Recalculation after one changed shipment | 0.4 s | 48 s |
| Parsing one PDF | about 35 ms | the same |
| Upload with real embeddings | not measured | not measured |

These are a laptop's figures, and they varied by up to 8 times with memory pressure. They are a reference, not
an acceptance result.

### The existing criteria are kept

The Sprint 4 plan set these at 1,000 shipments, warm, 95th percentile. **They are not lowered here.** The laptop
fails them; whether a server meets them is unknown.

| Endpoint | Existing target | Laptop today |
|---|---|---|
| Overview and shipment list | 300 ms | 0.6 to 1.2 s: fails |
| Risk overview | 500 ms | 1.5 s: fails |
| Report preview | 1 s | 1.4 s: fails |
| Report CSV | 2 s | 1.5 s: passes |
| Report PDF | 5 s | not measured at 1,000 |

### Proposed thresholds where none existed (for approval, D6)

To be measured on the staging machine (4 vCPU, 8 GB), synthetic data, 95th percentile over 50 requests.

| Measure | 100 shipments | 1,000 shipments |
|---|---|---|
| Warm API read | 200 ms | the existing targets above |
| Report PDF | 3 s | 5 s (existing) |
| First start, nothing cached | 30 s | 5 minutes |
| Restart, nothing changed | 5 s | 60 s |
| Recalculation after one changed shipment | 5 s | 60 s |
| Upload of one PDF, with real embeddings | 5 s | 5 s |
| 5 users reading at once | no request above 2 times the single-user figure, no error | the same |

### What happens if the existing targets fail on staging

Three honest outcomes, for the owner to choose between (D7):

1. **Accept for a stated volume.** Release for up to, say, 300 shipments, where measurements pass, and state the
   limit in the release notes.
2. **Do the work first.** Recompute only changed shipments, and stop checking every file on every request (a
   watched folder state). This is the deferred redesign T3.5: 5 to 8 days, and a new release candidate.
3. **Change the targets,** openly, with the reason recorded. Not recommended without measurements from real use.

**Evidence:** `scripts/benchmark_scale.py` output from the staging machine at 100 and 1,000 shipments, three runs
each, with the machine's specification; and a small concurrent-read script's output.

**Effort:** 1 day to measure and write up; the concurrent-read script is half a day of test tooling.

## 6. Workstream E: production migration readiness

The real review database (schema 2 to 3) and audit history (3 to 6) upgrade forward only. These are the steps
before anyone authorizes it. Nothing here is run by this plan.

### E0. Now, independently of everything else: a second copy of the backup

The 2026-11-15 backup is on the same machine as the data. Copy the folder
`logidoc-20261115T142522Z-manual-d4e6ee` to a second medium (an external drive or a private cloud bucket) and
verify the copy against `manifest.sha256`. It changes no source data. **Recommended today** (D1).

### E1 to E8. The gate before the real migration

| Step | Action | Evidence |
|---|---|---|
| E1 Freeze | Announce a maintenance window; nobody uses the application; the server is stopped | The announcement; the server's stop in its log |
| E2 Fresh backup | `manage_backups.py create --label "before 1.0.0"`, with `LOGIDOC_BACKUP_DIR` set | The backup ID; its `verify` output |
| E3 Independent copy | Copy that backup to a second medium, on another disk or site; verify the copy's checksums there | The checksum verification on the copy |
| E4 Integrity | `PRAGMA integrity_check` and `foreign_key_check` on each database copy in the backup; the review history's hash chain | The outputs (the rehearsal script produces them) |
| E5 Isolated restore | `manage_backups.py restore` into a new folder; the restore report says `restored` | The restore report |
| E6 Rehearsal | `rehearse_migration.py` on that restore: every check passes, with row digests equal | `rehearsal-report.json` |
| E7 Go or no-go | The owner reads E2 to E6 and signs (D8) | A dated, written authorization naming the backup ID |
| E8 Migration | Start the new version with `LOGIDOC_BACKUP_DIR` pointing at the fresh backup. The guard checks the backup again, upgrades, then serves | The start-up log; schema versions 3 and 6; `/api/ready`; `verify-history` ok |

**No-go if any of:** the backup does not verify; the copy's checksums differ; any integrity check fails; the
rehearsal reports a changed or lost row; the guard refuses at start (it means the data changed after the
backup: go back to E2).

**Maintenance window:** the data is small (under 5 MB today). The upgrade itself takes seconds; allow 1 hour for
E1 to E8 with the checks, and choose a time when a second hour is available for a rollback.

**After E8:** the checks of the runbook's section 9; keep the "before 1.0.0" backup and its copy for at least
30 days.

### Rollback, and its limits

- Rolling back means restoring the "before 1.0.0" backup and starting the previous release. **Everything recorded
  after that backup is discarded.**
- So the decision to roll back should be made **before the application is reopened to users**. Once people have
  worked on the upgraded data, a rollback costs their work.
- The review upgrade also leaves `audit_reviews.before-v3.sqlite3` beside the database. It restores the reviews
  alone, with the same loss, and only if the history is restored with it.
- There is no downgrade path and none is planned.

**Effort:** half a day, in one sitting, by one person with the owner available for E7.

## 7. Effort and cost summary

| Workstream | Effort | Estimated cost | Depends on |
|---|---|---|---|
| E0 Backup copy | 0.5 hour | A drive or a few cents of storage | Nothing |
| A Staging | 2 to 3 days | 20 to 60 USD | D2, D3 |
| B Browser acceptance | 3.5 to 4.5 days | None (CI minutes) | A for the manual pass; D4 |
| C Security: threat model, checklist, exposure review, permissions | 3.5 to 4.5 days | None | A |
| C Penetration test | 2 to 4 days of a tester, plus 1 to 3 days of fixes | 1,500 to 8,000 USD or more | A; D5 |
| D Performance | 1.5 days | None | A |
| E Migration gate | 0.5 day | None | Everything else passed; D8 |
| **Total** | **about 11 to 14 days of work, plus the penetration test** | **about 20 to 60 USD, plus the penetration test** | |

Elapsed time is longer than the effort: three nights for scheduled backups, and the tester's availability.

## 8. Evidence required for each release gate

| Gate | Passes when | Evidence kept |
|---|---|---|
| G1 CI | All jobs green on the candidate commit | The run's link |
| G2 Staging | A1 to A10 pass | The outputs listed in section 2 |
| G3 Browser | T1 to T15 pass in CI and once by hand | The Playwright report; the signed manual table |
| G4 Security | Threat model written; checklist run; exposure review done; permissions verified; penetration test with no open critical or high finding (or D5's alternative, recorded) | The documents; the tester's report and retest |
| G5 Performance | The thresholds of section 5 measured on staging, and D7 decided | The benchmark outputs; the decision |
| G6 Migration | E2 to E7 complete | The backup ID, the copy's verification, the rehearsal report, the authorization |
| G7 Sign-off | The acceptance checklist has no FAIL and no NOT VERIFIED among the blocking items | The checklist, signed |

Only then: re-version to `1.0.0`, tag, merge. Each of those is the owner's action.

## 9. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The only backup is lost with the laptop's disk | Low, but possible any day | High: the real data is gone | E0 now |
| Staging behaves differently on Linux (paths, locks, signals) | Medium | Medium: fixes and a new candidate | A is first; the code was developed and tested on Windows only |
| Ollama is too slow on a small machine without a GPU | High | Low for acceptance: answers work but slowly | Measure and state it; a GPU is a production sizing decision |
| The browser tests are flaky | Medium | Low | Chromium only, role and label selectors, traces on failure, no fixed sleeps |
| The penetration test finds something structural | Low to medium | High: delay | The threat model and checklist first, so the cheap findings are already fixed |
| The targets fail on staging too | Medium | Medium | D7's three outcomes are prepared |
| The real migration is authorized on a stale backup | Low | High | The guard refuses a backup that does not hold the current data; E3 and E7 name the backup ID |
| A rollback is decided after users have worked | Low | High: their work is lost | Decide before reopening; the runbook says so |
| Staging data or secrets leak | Low | Low | Synthetic data only; staging-only secrets; delete the machine afterwards |
| Disk fills on the laptop again | High | Low | No browser runtime locally; no full suites without a restart |

## 10. Decisions for the owner

| # | Decision | Recommendation |
|---|---|---|
| D1 | Copy the 2026-11-15 backup to a second medium now | **Yes, today** |
| D2 | Rent a staging machine and use a subdomain (20 to 60 USD) | Yes |
| D3 | Which provider and which domain | Your choice; any provider with Ubuntu LTS and volume encryption |
| D4 | Browser tests: Playwright in CI only, with one new development dependency | Yes; no install on the laptop |
| D5 | Penetration test: commission one (two quotes), or release to a trusted internal network without it | Commission one before any internet-facing use |
| D6 | The new thresholds of section 5 | Approve, or adjust before measuring, not after |
| D7 | If the existing targets fail on staging: limit the volume, do the redesign, or change the targets | Limit the volume for 1.0.0; plan the redesign after |
| D8 | Go or no-go for the real migration | Only at gate G6, in writing |
| D9 | Object-level access: both roles see every shipment | Confirm it is intended, or it becomes a feature for a later release |
| D10 | Real-document accuracy stays unmeasured | Accept as a stated limitation, or provide permitted documents |

## 11. Out of scope

- New product features, including a real TMS vendor, OCR and currency conversion.
- Deploying to production, tagging, releasing or merging into `main`.
- Sprint 5.
- Any change to the real data, the existing backups, the evaluation outputs or other projects.
