# Release runbook

How to install, start, upgrade, watch, recover and roll back LogiDoc-RAG. It is the one place to start from; each
section links to the document with the detail.

Release: see `python src/api/serve.py --version`. Settings: [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md).
What has and has not been verified for this release: [RELEASE_ACCEPTANCE_CHECKLIST.md](RELEASE_ACCEPTANCE_CHECKLIST.md).

## 1. What runs where

```
browser --HTTPS--> reverse proxy (Caddy or nginx, 80/443, TLS ends here)
                      | HTTP on 127.0.0.1:8000
                      v
                   one LogiDoc-RAG process (python src/api/serve.py)
                      |-- the web application (frontend/dist)
                      |-- SQLite databases, documents, cache   in LOGIDOC_DATA_DIR
                      |-- the vector index                     in LOGIDOC_CHROMA_DIR
                      |-- backups                              in LOGIDOC_BACKUP_DIR (another disk)
                      `-- Ollama on 127.0.0.1:11434 (never published)
```

- **One server process per data folder.** A second one is refused (exit code 3). The Streamlit app (`app.py`) is a
  development tool: do not run it on the same data folder as the server.
- **Requirements:** Python 3.10 or 3.12; Node.js 22 to build the web application; Ollama with both models.
- **TLS and the proxy:** [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md), with `deploy/Caddyfile`,
  `deploy/nginx/logidoc.conf` and `deploy/logidoc-api.service`. The proxy must set `X-Forwarded-Proto` and
  replace `X-Forwarded-For` ([RATE_LIMITING.md](RATE_LIMITING.md)).

## 2. Before every deployment

1. **CI is green** for the commit to deploy, on all jobs.
2. **`python scripts/check_release.py`** passes: version, changelog, contracts, documents.
3. **The staging rehearsal passes** for that commit:
   `python scripts/staging_rehearsal.py --work <new folder>` ([STAGING_REHEARSAL.md](STAGING_REHEARSAL.md)).
4. **Read the changelog entry**, in particular "Upgrading".
5. **The settings validate:** `python src/api/serve.py --check` with the production environment file.

## 3. Install or update the code

1. Check out the release commit into a new release folder. Never deploy the data folder with the code.
2. `python -m venv venv`, then `venv/bin/python -m pip install -r requirements.txt`.
3. In `frontend/`: `npm ci`, `npm run build`, `npm run size`.
4. First installation only: create the data, index and backup folders, owned by the service account and closed
   to others ([DOCUMENT_TEXT_CACHE.md](DOCUMENT_TEXT_CACHE.md), section 2, gives the commands), and create the
   accounts: `python scripts/manage_users.py create <name> --role admin`.

## 4. Migration preflight (every upgrade)

The review and history databases upgrade themselves, forward only, and **an upgrade cannot be undone**. The
server therefore upgrades them only when it starts, and only if a fresh, verified backup holds their current
contents ([MIGRATION_REHEARSAL.md](MIGRATION_REHEARSAL.md), section 6).

1. Stop the server (section 5).
2. `python scripts/manage_backups.py create --label "before <version>"`. It verifies what it wrote.
3. Recommended: rehearse on a copy.
   `python scripts/manage_backups.py restore <backup id> --target <new folder>`, then
   `python scripts/rehearse_migration.py --source <new folder>/data --chroma <new folder>/chroma_db --work <another new folder>`.
   Every check must pass.
4. Start the new version. If an upgrade is due it runs once, before anything is served, and the log says so.
   Without a qualifying backup the server exits with code 4 and prints these steps.
5. Keep that backup until the upgraded installation has been checked (section 9).

The extraction store has its own explicit command: `python scripts/migrate_extraction_store.py --dry-run`, then
without `--dry-run` ([MIGRATION_STATUS_DESIGN.md](MIGRATION_STATUS_DESIGN.md)).

## 5. Start and stop

| | Linux (systemd) | By hand (any system) |
|---|---|---|
| Start | `systemctl start logidoc-api` | `python src/api/serve.py` with the environment set |
| Stop | `systemctl stop logidoc-api` | Ctrl+C (Ctrl+Break on Windows): requests in flight finish, the lock is released |
| Status | `systemctl status logidoc-api` | `GET /api/health` |

**Exit codes of `serve.py`:**

| Code | Meaning |
|---|---|
| 0 | Stopped normally |
| 2 | A setting is invalid: each is named |
| 3 | Another server, backup or restore is using this data folder |
| 4 | A database upgrade is due and no fresh verified backup exists: nothing was changed |

**After a start, check:** `GET /api/health` answers `{"status": "ok"}`; `GET /api/ready` answers `ready`; sign in;
*Settings* shows the release.

## 6. Backups

- **Scheduled:** `LOGIDOC_BACKUP_SCHEDULE`, made by the running server.
- **By hand with the server stopped:** `python scripts/manage_backups.py create`.
- **From the application:** *Settings* (administrators), or `POST /api/admin/backups`.

Every backup is verified when made, verified again every `LOGIDOC_BACKUP_VERIFY_HOURS`, and the newest is restored
into an isolated folder every `LOGIDOC_BACKUP_DRILL_HOURS`.

**Commands:**

```
python scripts/manage_backups.py list
python scripts/manage_backups.py verify <backup id>
python scripts/manage_backups.py restore <backup id> --target <new folder>
```

A backup holds the documents, every database and the index. It does not hold the environment file (the secrets)
or the document text cache. Detail: [PRODUCTION_BACKUP_AND_RECOVERY.md](PRODUCTION_BACKUP_AND_RECOVERY.md),
[AUTOMATED_BACKUP_OPERATIONS.md](AUTOMATED_BACKUP_OPERATIONS.md).

## 7. Monitoring, alerts and logs

| What | Where |
|---|---|
| Liveness | `GET /api/health` (for the proxy's health check) |
| Readiness | `GET /api/ready`: `ready`, `degraded` (a model or the index is missing; documents can still be read) or `not_ready` (503) |
| Metrics | `GET /api/admin/metrics` (administrators; keep it off the public proxy) |
| Alerts | `GET /api/admin/alerts`, and `LOGIDOC_ALERT_WEBHOOK_URL` when set: disk space, server errors, store errors, latency, the model service, backup age, late jobs |
| Logs | The process output (journald under systemd), and `LOGIDOC_LOG_FILE` when set: JSON lines with a request ID, rotated at 10 MB with 5 files kept |
| Proxy logs | The proxy's own access log (`deploy/Caddyfile` writes JSON to `/var/log/caddy/`; nginx to its usual log). Rotate them with the proxy's or the system's log rotation |

Logs hold no password, token, setting value or document text. Keep application logs as long as your audit policy
asks; the review and audit histories are in the databases, not in the logs. Detail:
[OBSERVABILITY_AND_MONITORING.md](OBSERVABILITY_AND_MONITORING.md).

## 8. Scheduler, webhooks and integrations

- **Scheduler ownership.** The scheduler runs inside the one server process (`LOGIDOC_BACKGROUND_JOBS=1`), holds a
  lease so that a second process cannot run the same job, and starts working only once a webhook endpoint or an
  integration is configured. Administrators see and change its jobs under *Scheduled Jobs*
  ([SCHEDULED_SYNCHRONISATION.md](SCHEDULED_SYNCHRONISATION.md)).
- **Background recording.** The same process records newly detected exceptions every 30 seconds
  ([EXCEPTION_RECORDING.md](EXCEPTION_RECORDING.md)). With background jobs off, they are recorded at the first
  change to an exception.
- **Webhooks.** Set `LOGIDOC_WEBHOOK_SECRET_KEY`, then register endpoints under *Webhooks*. Only public HTTPS
  destinations are accepted in production. A failed delivery is retried with back-off (8 attempts by default);
  a delivery that failed for good is retried by hand from its page, and interrupted deliveries return to the
  queue by themselves ([WEBHOOKS_AND_DELIVERY.md](WEBHOOKS_AND_DELIVERY.md)).
- **TMS and ERP.** The framework is in place, but **only a synthetic sandbox adapter exists, and it is refused in
  production**. No real TMS vendor is connected: connecting one is new development, not configuration
  ([TMS_ERP_INTEGRATION_FRAMEWORK.md](TMS_ERP_INTEGRATION_FRAMEWORK.md)).

## 9. After a deployment

1. `GET /api/ready` is `ready`.
2. Sign in as an administrator and as a reviewer.
3. Open a known shipment: its audit, its history, its report as PDF.
4. *Settings* shows the release deployed, and the newest backup is recent (`manage_backups.py list`, or the
   backup section for administrators).
5. `POST /api/admin/verify-history` answers `ok` (the review history's hash chain).
6. No new alert.

## 10. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Exit code 2 at start | A setting is invalid or still an example value | Fix the settings named; `serve.py --check` |
| Exit code 3 at start | Another process holds the data folder | Stop it; a backup or restore from the command line also holds the lock |
| Exit code 4 at start | An upgrade is due without a fresh verified backup | Section 4 |
| `not_ready` (503) | A folder or a database is unusable | `/api/ready` names the failing check; check permissions and disk space |
| `degraded` | Ollama is not answering or a model is missing; or the index cannot be read | Start Ollama, `ollama pull` the model; uploads and the assistant wait, audits still work |
| 429 answers | A rate limit | `Retry-After` says when; see [RATE_LIMITING.md](RATE_LIMITING.md) |
| Sign-in refused for a correct password | The account is locked after 5 failures | It unlocks after 15 minutes, or `manage_users.py reset-password` |
| "Review database unavailable" | The data folder is not writable | Permissions of the data folder |
| Slow first request after a start | The audit is computed, and the text cache may be rebuilding | Expected once; `manage_page_cache.py status` |
| A webhook endpoint keeps failing | The receiver | Its page shows each attempt and error; retry after it is fixed |

## 11. Incidents

1. **Contain.** If data may be exposed or damaged, stop the server; the proxy then answers 502.
2. **Preserve.** Copy the logs. Make no change to the data folder. If the server can run, make a backup; if not,
   copy the data folder as it is.
3. **Assess.** `/api/ready`, the alerts, `POST /api/admin/verify-history`, `GET /api/extractions/integrity`,
   `manage_backups.py verify` on the newest backups, and the account events (`manage_users.py events`).
4. **Recover.** Section 12 or 13.
5. **Rotate** any secret that may have leaked (the webhook key through endpoint rotation, account passwords, the
   alert URL), and deactivate any account in doubt (`manage_users.py deactivate`).
6. **Record** what happened, when, what was restored and what was lost.

## 12. A failed deployment

The new version does not start, or is wrong after starting.

- **No database was upgraded** (the upgrade never ran, or this release has none): stop the server, point the
  service back at the previous release folder, start. The data needs nothing.
- **A database was upgraded:** the previous release cannot open it. Roll back as in section 13.

How to tell: the log of the failed start says whether an upgrade ran, and `manage_backups.py` shows the
"before <version>" backup.

## 13. Rollback, and what it costs

Upgrades are irreversible in place: each store refuses a database newer than the code that opens it. Rolling back
across an upgrade means restoring the backup made before it.

1. Stop the server.
2. `python scripts/manage_backups.py restore <backup id> --target <new folder>`, into a new folder: never over the
   live data.
3. Check the restore report, then put the restored data in place: either point `LOGIDOC_DATA_DIR` and
   `LOGIDOC_CHROMA_DIR` at the new folder, or use `manage_backups.py activate` (it asks for the backup ID twice
   and keeps the replaced data aside).
4. Start the previous release.

**Restoring a pre-upgrade backup discards every record created after that backup:** documents uploaded, audits
recorded, exception reviews and their history, corrections and approvals, TMS decisions, accounts and password
changes, and webhook deliveries. They exist only in the data that the restore replaces. Before restoring, copy
the current data folder aside, so that what is discarded can still be read, and tell the people affected which
period they must redo.

There is no partial rollback: the databases refer to each other and to the documents, so they are restored
together.

## 14. Versions and promotion

- **The version** is set in one place, `src/release.py`, as `MAJOR.MINOR.PATCH`, or `MAJOR.MINOR.PATCH-rc.N` for a
  release candidate. The web application carries the same `MAJOR.MINOR.PATCH`.
  - MAJOR: a change that is not backward compatible for integrators or operators.
  - MINOR: new behaviour, or any database schema upgrade.
  - PATCH: fixes only.
- **A change of version** updates `src/release.py`, `frontend/package.json` and a new entry at the top of
  `CHANGELOG.md`, in one commit. `scripts/check_release.py` (run in CI) fails when they disagree.
- **Promotion:**
  1. A release candidate (`-rc.N`) is cut on the release branch when CI is green.
  2. It goes through the staging rehearsal and the acceptance checklist.
  3. A candidate with a failed item is fixed and becomes `-rc.N+1`.
  4. When every item that must pass does, the same commit is re-versioned without `-rc`, tagged `v<version>` and
     merged into `main`.
- **Tags, GitHub releases and merges into `main` are made by the repository owner only,** after approval. None
  exists for this release candidate.
- **The API contract version** (`/api/v1`) is separate: it changes only with a change that is not backward
  compatible, published as `/api/v2` beside it.
