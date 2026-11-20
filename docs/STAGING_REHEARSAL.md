# Staging rehearsal

A repeatable rehearsal of a production deployment, on an isolated folder of synthetic data. It runs the real
server in the production profile and walks the whole workflow, then a backup, a restore and an upgrade of an
older installation.

```
python scripts/staging_rehearsal.py --work <new folder>
```

**Before running it:** build the web application (`npm run build` in `frontend/`); the rehearsal checks that it is
served. The work folder must not exist, and must be outside the project. Exit status 0 means no step failed.

## 1. What it is, and is not

**No real staging server was available.** The rehearsal ran locally, on the development laptop. It proves the
application's own behaviour in the production profile; it does not prove a server, a network or a proxy.

| Real in the rehearsal | Simulated, and reported as SIMULATED |
|---|---|
| `python src/api/serve.py` with `LOGIDOC_ENV=production`: startup validation, sign-in, Secure cookies, trusted host, CSRF, rate limits | **The HTTPS proxy:** requests go to the loopback port with the `Host`, `X-Forwarded-Proto` and `X-Forwarded-For` headers a proxy sends. TLS, certificates and the proxy's limits are not exercised |
| The built React application, served by the API | **The model service:** a stand-in for Ollama with deterministic embeddings and a fixed answer. No model is loaded |
| SQLite databases, the ChromaDB index, the document text cache | **Webhook delivery:** configuration and the refusal of unsafe destinations are checked; nothing is delivered |
| `scripts/manage_users.py`, `scripts/manage_backups.py` | **Scheduled job runs:** the scheduler is idle until an endpoint or integration exists |
| The migration guard, on databases of an older schema | **The TMS:** no vendor exists, and the sandbox adapter is refused in production |

**Isolation:** every `LOGIDOC_*` and `OLLAMA_*` setting of the calling shell is dropped; the installation's own
data folder, index and backup folder are refused as a work folder; accounts and the signing key are generated for
the run and never printed. The documents are four synthetic PDFs of one shipment (SHJ-2026-101).

## 2. Steps

| Step | What is checked |
|---|---|
| 1. Configuration | The production settings validate; unsafe ones (`local` sign-in, `*` as trusted proxy) stop the start, each named; the release is printed; accounts are created from the command line with passwords from the environment |
| 2. Startup and shutdown | The server starts; a second server on the same data is refused (exit code 3); it stops cleanly on an operator's interrupt and releases its port |
| 3. Frontend and API | The React build and its script are served with a Content-Security-Policy; `/api/health` answers with `API-Version: 1`; an unknown host name is refused (400); the interactive API documentation is off |
| 4. Sign-in and roles | No data without a session (401); a wrong password is refused; two administrators and a reviewer sign in; the cookie is Secure, HttpOnly and SameSite=Strict; the reviewer is refused administrator operations (403); a change without the CSRF header is refused; the release is shown to a signed-in user |
| 5. Ingestion | A reviewer may not upload; four PDFs are stored, classified (BOL, POD, INVOICE, RATE_CONFIRMATION) and indexed; a file that is not a PDF is reported and not stored; the document text cache is built |
| 6. Freight audit and exceptions | The shipment is reconciled; the fuel surcharge is found above its contract; the audit is recorded; a reviewer assigns, starts and resolves an exception with evidence but may not dismiss it; its history verifies |
| 7. Correction and re-audit | A correction is refused to its own author and approved by a second administrator; the controlled re-audit is previewed and recorded; the audit reads the approved amount; the first audit is kept |
| 8. Risk and reporting | The risk score equals the sum of its factors; the report agrees with it; CSV and PDF download for a reviewer |
| 9. Webhooks and scheduler | Delivery is configured (signing key, HTTPS only); loopback, private and plain-HTTP destinations are refused; the scheduler reports its state; a reviewer cannot manage integrations |
| 10. Monitoring | Readiness is `ready` and names no path; metrics are for administrators only; alerts are readable; request IDs are carried; the log file is JSON lines with no password, signing key or document text |
| 11. Backup and restore | A backup from the running application; `manage_backups.py` create, verify and restore into a new folder; a restore over the live data is refused; the text cache is in no backup; a second server on the restored copy signs the same accounts in and gives the same risk, report and history |
| 12. Migration guard | An installation with a version 2 review database and a version 3 history: without a backup the server exits with code 4 and changes nothing; after `manage_backups.py create` it upgrades at startup and serves |

## 3. Result for release 1.0.0-rc.1

Run on 2026-11-20: Windows, Python 3.12.7, release `1.0.0-rc.1`, in a new folder under `D:\LogiDoc-staging`.

| Step | Passed | Failed | Simulated |
|---|---|---|---|
| 1. Configuration | 4 | 0 | 0 |
| 2. Startup and shutdown | 3 | 0 | 0 |
| 3. Frontend and API | 5 | 0 | 0 |
| 4. Sign-in and roles | 8 | 0 | 0 |
| 5. Ingestion | 4 | 0 | 1 (embeddings) |
| 6. Freight audit and exceptions | 5 | 0 | 0 |
| 7. Correction and re-audit | 4 | 0 | 0 |
| 8. Risk and reporting | 3 | 0 | 0 |
| 9. Webhooks and scheduler | 4 | 0 | 3 (delivery, job runs, TMS) |
| 10. Monitoring | 8 | 0 | 0 |
| 11. Backup and restore | 10 | 0 | 0 |
| 12. Migration guard | 5 | 0 | 0 |
| **Total** | **63** | **0** | **4** |

A first run of the script failed 5 checks. All five were wrong expectations in the script (a field name, a
required request body, the scheduler being idle by design, a rejected file counted as "unsupported" rather than
"failed", and the exit code of an interrupted process on Windows), not faults of the application; they were
corrected and the rehearsal run again on a fresh folder. Both folders are kept.

The JSON report (`staging-rehearsal-report.json` in the work folder) lists every check with its detail.

## 4. What remains unverified

These need a real staging server and are not covered by the rehearsal:

- TLS: a certificate, its renewal, HSTS in a browser, the HTTP-to-HTTPS redirect;
- the proxy itself: `deploy/Caddyfile` or `deploy/nginx/logidoc.conf` as deployed, its rate limits, upload size
  limit and timeouts;
- the systemd unit on Linux (the rehearsal ran on Windows), and file permissions of a service account;
- Ollama with the real models: embedding quality, answer time, memory;
- a webhook delivered to a real receiver over the internet, and an alert received;
- scheduled backups over days, retention, the restore drill on the real disk;
- load: more than one user at a time, and real document volumes;
- a real browser session (the rehearsal speaks HTTP; the frontend tests run in a simulated browser);
- a penetration test.

Each is carried in [RELEASE_ACCEPTANCE_CHECKLIST.md](RELEASE_ACCEPTANCE_CHECKLIST.md) as NOT VERIFIED.
