# Configuration reference

Every setting LogiDoc-RAG reads, what it does, and what production requires. The application reads settings from
the process environment only; it never loads a `.env` file. Names and defaults are also in `.env.example`, and a
production starting point is `deploy/logidoc.production.env.example`.

`python src/api/serve.py --check` validates the settings without opening or starting anything, and the server
refuses to start on any error. Messages name the setting, never its value.

## 1. Supported versions

| Component | Supported | Notes |
|---|---|---|
| Python | 3.10 and 3.12 | Both run the full test suite in CI. Dependencies are pinned in `requirements.txt` |
| Node.js | 22 | To build the web application only (`npm ci`, `npm run build`); not needed on the server |
| SQLite | the one bundled with Python | Default journal mode; WAL is not enabled |
| ChromaDB | pinned in `requirements.txt` | The vector index, in `LOGIDOC_CHROMA_DIR` |
| Ollama | a local instance | Models `nomic-embed-text` (embeddings) and `nemotron-3-nano:4b` (answers), set in `config.py` |
| Operating system | Windows (developed and tested in CI), Linux (deployment examples) | The frontend job runs on Linux in CI |

## 2. Profiles

`LOGIDOC_ENV` selects one: `development` (default), `test`, or `production`.

**Production** falls back to no development default. It requires:

- `LOGIDOC_AUTH_MODE=required`;
- `LOGIDOC_CORS_ORIGINS` with HTTPS origins only;
- `LOGIDOC_TRUSTED_HOSTS` naming the host names served (no catch-all);
- `LOGIDOC_DATA_DIR`, `LOGIDOC_CHROMA_DIR` and `LOGIDOC_BACKUP_DIR` as absolute, separate folders;
- `LOGIDOC_BACKUP_SCHEDULE` (it may be `off`, with a warning).

It also refuses:

- a value left unchanged from an example file (`example.com`, `change-me`, `<...>`);
- `LOGIDOC_COOKIE_SECURE=0`;
- `LOGIDOC_FORWARDED_ALLOW_IPS=*`;
- the development-only webhook switches;
- the TMS sandbox.

## 3. Settings

"Required" means required in production.

### Server

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_ENV` | `development` | The profile |
| `LOGIDOC_API_HOST` | `127.0.0.1` | Listen address. Keep it on loopback, behind the HTTPS proxy |
| `LOGIDOC_API_PORT` | `8000` | Listen port |
| `LOGIDOC_AUTH_MODE` | `local` | `local`: no sign-in, loopback only. `required`: accounts. **Required: `required`** |
| `LOGIDOC_CORS_ORIGINS` | the Vite dev origins | Exact origins; `*` is refused. **Required, HTTPS** |
| `LOGIDOC_TRUSTED_HOSTS` | any | Host names answered. **Required** |
| `LOGIDOC_COOKIE_SECURE` | off | Always on in production |
| `LOGIDOC_FRONTEND_DIR` | `frontend/dist` | The built web application served at `/` |
| `LOGIDOC_FORWARDED_ALLOW_IPS` | `127.0.0.1` | The proxy whose `X-Forwarded-*` headers are trusted. Never `*` |
| `LOGIDOC_HTTPS_REDIRECT` | `0` | Redirect plain HTTP (the proxy usually does it) |
| `LOGIDOC_HSTS_SECONDS` | `0` (production: 31536000) | `Strict-Transport-Security` |
| `LOGIDOC_MAX_REQUEST_KB` | `1024` | Largest request body other than an upload |
| `LOGIDOC_MAX_UPLOAD_FILES` | `20` | Files in one upload |
| `LOGIDOC_MAX_UPLOAD_MB` | `25` | Size of one PDF |
| `LOGIDOC_BACKGROUND_JOBS` | `1` | `0`: no backup schedule, monitor, detection recording or scheduler in this server |

### Data locations

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_DATA_DIR` | `data` | Documents (`raw/`), databases, `archive/`, `quarantine/`, `cache/`. **Required, absolute** |
| `LOGIDOC_CHROMA_DIR` | `chroma_db` | The vector index. **Required, absolute, not the data folder** |
| `LOGIDOC_BACKUP_DIR` | `backups` | Backups; another disk than the data. **Required, absolute** |
| `LOGIDOC_REVIEW_DB`, `LOGIDOC_HISTORY_DB`, `LOGIDOC_AUTH_DB`, `LOGIDOC_INGESTION_DB`, `LOGIDOC_EXTRACTION_DB`, `LOGIDOC_CARRIER_DB`, `LOGIDOC_INTEGRATION_DB` | in the data folder | One database moved on its own. Leave unset: a restore can replace live data in one step only when every store is in its default place |

What each file holds: [README.md](../README.md), "Databases". The document text cache:
[DOCUMENT_TEXT_CACHE.md](DOCUMENT_TEXT_CACHE.md).

### Logs

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_LOG_FORMAT` | `text` (production: `json`) | One JSON object per line in production |
| `LOGIDOC_LOG_LEVEL` | `INFO` | |
| `LOGIDOC_LOG_FILE` | none | Also write to this file: rotated at 10 MB, 5 files kept (about 60 MB at most) |

Without `LOGIDOC_LOG_FILE` the log goes to the process output only (journald under systemd).

### Backups

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_BACKUP_SCHEDULE` | `off` | `off`, `daily@HH:MM` (UTC) or `every:<N>h`. **Required** |
| `LOGIDOC_BACKUP_KEEP_DAILY` / `_WEEKLY` / `_MONTHLY` | `7` / `4` / `6` | Retention of scheduled backups. Manual ones are never removed |
| `LOGIDOC_BACKUP_MIN_FREE_MB` | `1024` | Free space that must remain, or a backup does not start |
| `LOGIDOC_BACKUP_RETRY_MINUTES` | `60` | A failed scheduled backup is tried again after this |
| `LOGIDOC_BACKUP_VERIFY_HOURS` | `24` | Every complete backup is checked again; `0` = off |
| `LOGIDOC_BACKUP_DRILL_HOURS` | `168` | The newest backup is restored into an isolated folder and checked; `0` = off |
| `LOGIDOC_BACKUP_DRILL_DIR` | the temp folder | Where restore drills restore |

### Monitoring and alerts

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_MONITOR_INTERVAL` | `300` | Seconds between the monitor's checks |
| `LOGIDOC_ALERT_WEBHOOK_URL` | none | HTTPS URL that receives alerts. **A secret** |
| `LOGIDOC_ALERT_DISK_WARNING_MB` / `_CRITICAL_MB` | `2048` / `512` | Free-space thresholds |
| `LOGIDOC_ALERT_WINDOW_SECONDS` | `300` | The window the next three look at |
| `LOGIDOC_ALERT_5XX_COUNT` | `5` | Server errors in the window |
| `LOGIDOC_ALERT_DB_ERROR_COUNT` | `1` | Store errors in the window |
| `LOGIDOC_ALERT_LATENCY_MS`, `LOGIDOC_ALERT_MIN_REQUESTS` | `5000`, `20` | 95th percentile, with at least that many requests |
| `LOGIDOC_ALERT_OLLAMA_FAILURES` | `3` | Failed model-service checks in a row |
| `LOGIDOC_ALERT_BACKUP_MAX_AGE_HOURS` | `26` | Age of the newest good backup |
| `LOGIDOC_ALERT_COOLDOWN_SECONDS` | `3600` | Before the same alert is sent again |

### Webhooks and integrations

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_WEBHOOK_SECRET_KEY` | none | At least 32 random characters. **A secret.** Without it no endpoint can be registered |
| `LOGIDOC_WEBHOOK_TIMEOUT_SECONDS` | `10` | One delivery attempt |
| `LOGIDOC_WEBHOOK_MAX_ATTEMPTS` | `8` | Attempts before a delivery fails |
| `LOGIDOC_WEBHOOK_ROTATION_GRACE_HOURS` | `24` | The previous secret still signs beside the new one |
| `LOGIDOC_WEBHOOK_ALLOW_HTTP`, `LOGIDOC_WEBHOOK_ALLOW_PRIVATE` | off | Development only: refused in production |
| `LOGIDOC_INTEGRATION_<NAME>` | none | The credential of the integration that names it. **A secret**, read when used, never stored |
| `LOGIDOC_INTEGRATION_SANDBOX_DIR`, `LOGIDOC_INTEGRATION_SANDBOX_TOKEN` | none | The synthetic TMS sandbox. Refused in production |

### Policy

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_REQUIRE_APPROVAL` | off | Resolutions and dismissals wait for an administrator |
| `LOGIDOC_MIN_NOTE_LENGTH` | `10` | Shortest resolution or dismissal note |
| `LOGIDOC_FREIGHT_TOLERANCES` | exact matching | Freight audit tolerances as JSON |
| `LOGIDOC_RISK_MODEL` | the built-in model | Risk weights and thresholds as JSON, with its own version |

### Model service

| Setting | Default | Meaning |
|---|---|---|
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Read by the ollama library. Never publish this port |
| `LOGIDOC_OLLAMA_CONNECT_TIMEOUT` | `5` | Seconds |
| `LOGIDOC_EMBED_TIMEOUT` | `60` | Seconds for one embedding |
| `LOGIDOC_LLM_TIMEOUT` | `120` | Seconds for one generated answer |

### Development only

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_API_URL` | `http://127.0.0.1:8000` | Where the Vite development server proxies `/api` |

## 4. Secrets

| Secret | How to generate | Where to keep it |
|---|---|---|
| `LOGIDOC_WEBHOOK_SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` | The server's environment file only |
| `LOGIDOC_ALERT_WEBHOOK_URL` | given by the alert receiver | The same |
| `LOGIDOC_INTEGRATION_<NAME>` | given by the integrated system | The same |
| Account passwords | chosen by each person (12 characters at least); created with `scripts/manage_users.py`, which asks for them or reads `--password-env` | Nowhere: only a salted scrypt hash is stored |

**Rules:**

- **One environment file,** readable by the service account only: `/etc/logidoc/logidoc.env` with mode `600` on
  Linux; an ACL for the service account and administrators only on Windows.
- **Never** in git, in a command-line argument, in a backup, in a ticket or in a log. The application never logs
  a setting's value.
- **Backups do not contain the environment file.** Keep a copy of the secrets in the organisation's secret store:
  losing the webhook key means rotating every endpoint's secret.
- **Rotate** the webhook key by rotating each endpoint (the previous secret keeps signing for the grace period);
  reset a password with `scripts/manage_users.py reset-password`, which ends that account's sessions.

## 5. Changing a setting

Edit the environment file, run `python src/api/serve.py --check` with it, then restart the server
([RELEASE_RUNBOOK.md](RELEASE_RUNBOOK.md), "Start and stop"). No setting is read while the server runs, and none
can be changed from the web application.
