# Production deployment

How to run LogiDoc-RAG for real users: the shape of the deployment, the
settings, HTTPS, and the steps to stage and go live. Nothing here has been
deployed to a public server yet. The configuration was checked by the
automated tests (`tests/test_production_config.py`), not by a live
installation.

## The shape of it

```
browser --HTTPS--> reverse proxy (Caddy or nginx, ports 80/443)
                        |  HTTP on 127.0.0.1:8000, X-Forwarded-Proto / -For / X-Request-ID
                        v
                   LogiDoc-RAG API (python src/api/serve.py, one process)
                        |-- serves the React build (frontend/dist) at /
                        |-- SQLite stores, PDFs (LOGIDOC_DATA_DIR)
                        |-- ChromaDB, embedded (LOGIDOC_CHROMA_DIR)
                        |-- backups (LOGIDOC_BACKUP_DIR, another disk)
                        '-- Ollama on 127.0.0.1:11434 (never published)
```

This is the existing architecture with HTTPS in front. There are no new
services, no containers and no cloud dependency. Four points shape it:

- **One API process.** The ingestion lock that keeps uploads, backups and
  document changes apart works inside one process. Do not run several uvicorn
  workers or several servers on one data folder. A second server on the same
  folder refuses to start, because the first holds the data-folder lock.
- **TLS ends at the reverse proxy.** The API listens on 127.0.0.1 only and
  trusts `X-Forwarded-*` only from `LOGIDOC_FORWARDED_ALLOW_IPS` (127.0.0.1).
- **Streamlit stays local.** It is the secondary interface for local use.
  It is not exposed by this setup, and it must not write to the production
  data while the API runs.
- **Ollama is optional.** When it is down, the server reports `degraded`.
  Audits, documents, history, reports and backups keep working; uploads and
  generated answers return a clear 503 or 504.

## Environment profiles

`LOGIDOC_ENV` selects `development` (the default), `test` or `production`
(`src/environment.py`). The server validates the environment before it reads
anything else, and does not start on any error. Messages name the setting,
never its value.

| | development | test | production |
|---|---|---|---|
| Sign-in | optional (`local` mode, loopback only) | optional | `LOGIDOC_AUTH_MODE=required` |
| Cookies | Secure if `LOGIDOC_COOKIE_SECURE=1` | same | always Secure; turning it off is refused |
| CORS origins | Vite dev server by default | same | required; HTTPS only; `*` refused |
| Trusted hosts | any | any | `LOGIDOC_TRUSTED_HOSTS` required; no catch-all |
| Data, index, backup folders | defaults in the project | same | required, absolute; backups outside the data |
| Backup schedule | off by default | never runs | `LOGIDOC_BACKUP_SCHEDULE` required (`off` is a warning) |
| Background jobs (schedule, monitor) | on under `serve.py` | never | on |
| Logs | text | text | JSON lines |
| `/api/docs`, `/api/openapi.json` | shown | shown | hidden (published file: `docs/api/openapi-v1.json`) |
| HSTS | off | off | one year on HTTPS responses |
| Example values (example.com, change-me, `<...>`) | allowed | allowed | refused |

Malformed values are refused in every profile: a port that is not a number,
a negative timeout, an unknown schedule. `.env.example` lists every setting
the application reads, and a test fails if one is missing from it.
`deploy/logidoc.production.env.example` is a production template holding
placeholders only.

The application has no signing key or API secret of its own:

- **Sessions** are random tokens held in an HttpOnly, SameSite=Strict
  cookie. Only their hashes are stored.
- **Passwords** are hashed in the user store and managed with
  `scripts/manage_users.py`.
- **The alert webhook URL** is the one secret setting. It is never logged,
  returned or written to a backup, and in production it must use HTTPS.

## Security settings

| Concern | Setting or behaviour |
|---|---|
| Sign-in and roles | `required` mode; administrator and reviewer permissions are checked on the server for every route (`src/authz.py`) |
| CSRF | every state-changing request needs `X-LogiDoc-Client: web`; a cross-origin page cannot send it past the CORS rule |
| CORS | exact origins only (`LOGIDOC_CORS_ORIGINS`); credentials allowed only for them |
| Host header | `LOGIDOC_TRUSTED_HOSTS`, plus `localhost` and `127.0.0.1` for probes on the machine; anything else gets 400 |
| Request size | JSON and other bodies: `LOGIDOC_MAX_REQUEST_KB` (1024). Uploads: `LOGIDOC_MAX_UPLOAD_FILES` x `LOGIDOC_MAX_UPLOAD_MB` + 1 MB (501 MB by default), and each file `LOGIDOC_MAX_UPLOAD_MB`. Both the declared length and the bytes received are checked (413) |
| Error responses | always `{"detail": "..."}`; an unexpected error is `Something went wrong on the server.` with no stack trace, path or exception text; every response carries `X-Request-ID` for support |
| Headers | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a Content-Security-Policy on the web application, `Cache-Control: no-store` on the API, HSTS on HTTPS |
| Logs | credentials, URL paths, file folders and e-mail addresses are removed from every line; request bodies, document text and prompts are never logged ([OBSERVABILITY_AND_MONITORING.md](OBSERVABILITY_AND_MONITORING.md)) |
| Admin operations | `/api/admin/backups`, `/metrics` and `/alerts` need the `admin.maintenance` permission; restores are command-line only |

## HTTPS and certificates

Use one of the two reverse proxies in `deploy/`. Neither file holds a
certificate or key, and none is committed.

**Caddy** (`deploy/Caddyfile`) is the simplest choice. When it first starts,
Caddy obtains a certificate from Let's Encrypt, renews it on its own about 30
days before expiry, and redirects HTTP to HTTPS. Requirements:

- the host name resolves to the server
- ports 80 and 443 reach it
- the Caddy service account can write its data folder

Replace `logidoc.example.com`, then run `caddy validate --config Caddyfile`
and start the `caddy` service.

**nginx with certbot** (`deploy/nginx/logidoc.conf`):

1. Install nginx and certbot from the distribution.
2. Serve the HTTP challenge folder, then obtain the certificate:
   `sudo certbot certonly --webroot -w /var/www/certbot -d <host>`.
3. Install the configuration, replace `logidoc.example.com`, then run
   `sudo nginx -t && sudo systemctl reload nginx`.
4. Renewal: certbot's timer renews automatically. Add
   `--deploy-hook "systemctl reload nginx"` so nginx picks up the new
   certificate, and check it with `sudo certbot renew --dry-run`.

Both configurations:

- proxy to `127.0.0.1:8000`
- set `X-Forwarded-Proto` and `X-Forwarded-For` and pass a request ID
- allow 501 MB bodies, the application's whole-upload limit
- allow 180 s for a response, so a generated answer (`LOGIDOC_LLM_TIMEOUT`,
  120 s) fits
- answer 404 for `/api/admin/metrics` from outside
- redirect HTTP to HTTPS (Caddy itself; nginx with a `return 301`)

The tests check each of these against the application's settings.

`LOGIDOC_HTTPS_REDIRECT=1` adds the same redirect inside the application,
except for `/api/health` and `/api/ready`. It is off by default because the
proxy already redirects. If you turn it on, keep `LOGIDOC_FORWARDED_ALLOW_IPS`
set to the proxy's address, or every request will look like plain HTTP and
be redirected.

No certificate is generated by this project, and none was issued during
development: that needs a real domain and a public server.

## Deploying on Linux (systemd)

1. Create a service account and folders, with backups on another disk:
   ```
   sudo useradd --system --home /opt/logidoc-rag logidoc
   sudo mkdir -p /var/lib/logidoc/data /var/lib/logidoc/chroma_db /var/backups/logidoc /var/log/logidoc /etc/logidoc
   sudo chown -R logidoc:logidoc /var/lib/logidoc /var/backups/logidoc /var/log/logidoc
   sudo chmod 750 /var/lib/logidoc /var/backups/logidoc
   ```
2. Install the code at the commit you are deploying, with Python 3.12:
   ```
   sudo -u logidoc git clone <repository> /opt/logidoc-rag && cd /opt/logidoc-rag && sudo -u logidoc git checkout <commit>
   sudo -u logidoc python3.12 -m venv venv && sudo -u logidoc venv/bin/pip install -r requirements.txt
   venv/bin/pip check
   ```
3. Build the web application: `cd frontend && npm ci && npm run build`. The
   server serves `frontend/dist`.
4. Install Ollama and its two models (`nomic-embed-text`,
   `nemotron-3-nano:4b`), listening on 127.0.0.1 only.
5. Copy `deploy/logidoc.production.env.example` to `/etc/logidoc/logidoc.env`
   with mode 640, owned by `root:logidoc`, and replace every example value.
6. Check the configuration without starting anything:
   ```
   sudo -u logidoc bash -c 'set -a; . /etc/logidoc/logidoc.env; venv/bin/python src/api/serve.py --check'
   ```
   It must print `Configuration valid for production.` Read any warnings.
7. Create the first administrator:
   `sudo -u logidoc bash -c 'set -a; . /etc/logidoc/logidoc.env; venv/bin/python scripts/manage_users.py'`.
8. Install and start the service:
   ```
   sudo cp deploy/logidoc-api.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now logidoc-api
   ```
9. Install the reverse proxy (previous section).
10. Check `curl -s https://<host>/api/health`, then `curl -s https://<host>/api/ready`,
    then sign in, then trigger a backup from `POST /api/admin/backups` or
    `scripts/manage_backups.py`, then restore it into a scratch folder.

On Windows, run `python src\api\serve.py` with the same settings as a service,
for example with Task Scheduler ("At startup", run whether the user is signed
in or not), and Caddy for Windows in front. The settings, locks and backups
work the same.

## Health and readiness

| Endpoint | Meaning | Status codes |
|---|---|---|
| `GET /api/health` | the process answers (alive); touches nothing | 200 |
| `GET /api/ready` | can it do its work: `ready`, `degraded` or `not_ready`, with each check | 200, or 503 when not ready |

`/api/ready` reports these checks:

- `documents`: the document folder can be listed
- `data_writable`: new databases can be created
- `databases`: reviews, history, register, users and carriers open read-only
- `extraction_store`
- `vector_index`
- `model_service`: Ollama answers and holds both models
- `disk_space`: `ok`, `warning` or `critical`

`not_ready` means a folder is unusable or a core database cannot be opened.
An unreadable index, a missing model, an unusable extraction store or
critical disk space only make it `degraded`, so an offline AI service never
takes the application down. The answer holds status words only: no paths,
hosts or model names. Both endpoints need no sign-in, so a load balancer or
uptime monitor can use them.

## Staging procedure

Stage on a machine like production, with a copy of real data restored from a
backup, never the live folders:

1. Deploy the candidate commit following the steps above, with its own data,
   index and backup folders and its own host name
   (`logidoc-staging.<domain>`).
2. Restore the latest production backup into the staging data folder with
   `scripts/manage_backups.py restore <id> --target /var/lib/logidoc-staging`,
   and point `LOGIDOC_DATA_DIR` and `LOGIDOC_CHROMA_DIR` at it. Read
   `restore-report.json`: it must say `restored`.
3. Start the service. Check `/api/health`, then `/api/ready` (it must be
   `ready`, or `degraded` only for a reason you expect), and the logs (JSON,
   without errors).
4. Smoke test, signed in as an administrator and as a reviewer:
   - Overview and Shipment Explorer
   - a shipment's freight audit
   - the Exception Center (a reviewer cannot waive)
   - Carrier Intelligence and its CSV export
   - an upload of a test PDF
   - a question to the assistant
5. Make a backup from `POST /api/admin/backups`, and restore it into a scratch
   folder with the command line.
6. Watch `/api/admin/metrics` and `/api/admin/alerts` for an hour: no 5xx
   alerts, no latency alert.
7. Record the commit, the restore report and the results, then promote the
   same commit to production.

## Deployment checklist

- [ ] The commit to deploy has passed the full test suites (Python 3.12, the
      frontend and the build) and the evaluation.
- [ ] `/etc/logidoc/logidoc.env` exists with mode 640, every example value is
      replaced, and `serve.py --check` says the configuration is valid.
- [ ] `LOGIDOC_BACKUP_DIR` is on a different disk from `LOGIDOC_DATA_DIR`, and
      the backup folder is readable by the service account only.
- [ ] At least one administrator account exists; no default or shared
      passwords.
- [ ] The reverse proxy serves a valid certificate, redirects HTTP, and
      `curl -I http://<host>` returns 301 or 308.
- [ ] Only ports 80 and 443 are reachable from outside; 8000 and 11434 are
      not.
- [ ] `/api/ready` returns `ready`, and both Ollama models are present.
- [ ] A manual backup succeeded, and a restore of it into a scratch folder
      says `restored`.
- [ ] The backup schedule shows in `GET /api/admin/backups`, and after the
      first scheduled run `last_success` is set.
- [ ] Alerts reach a person: the webhook is configured, or the JSON logs are
      collected and watched.
- [ ] A backup copy leaves the machine (not automated by this project).
- [ ] Streamlit is not running against the production data.

## Not done in this sprint

These need approval or infrastructure that is not available here:

- a deployment to a real server
- a certificate issued for a real domain
- an external penetration test
- off-machine backup copies
- an external monitoring service

Docker images were deliberately not built: the development machine's disks
are nearly full, and the plain service setup above needs no image.
