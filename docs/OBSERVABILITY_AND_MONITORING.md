# Observability and monitoring

What LogiDoc-RAG records about itself: logs, request IDs, metrics, health
checks and alerts. Everything runs inside the API process with the Python
standard library. There is no paid service and no extra dependency.

Code: `src/observability.py` (logs, request context), `src/redaction.py`,
`src/metrics.py`, `src/alerts.py`, `src/operations.py` (the monitor),
`src/readiness.py`. Tests: `tests/test_observability.py`,
`tests/test_backup_automation.py` (alerts), `tests/test_readiness.py`.

## Logs

`python src/api/serve.py` sends every logger, uvicorn's included, through one
handler:

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_LOG_FORMAT` | `json` in production, `text` otherwise | one JSON object per line, or readable text |
| `LOGIDOC_LOG_LEVEL` | `INFO` | `DEBUG` to `CRITICAL` |
| `LOGIDOC_LOG_FILE` | none | also write to this file, rotated at 10 MB with 5 kept |

Logs go to stderr, so systemd's journal (or a Windows service wrapper) keeps
them.

A JSON line:

```json
{"timestamp": "2026-11-09T14:03:12.481Z", "level": "INFO", "component": "logidoc.access",
 "message": "GET /api/shipments/{shipment_id} 200", "request_id": "3f9c2a...", "correlation_id": "3f9c2a...",
 "endpoint": "/api/shipments/{shipment_id}", "method": "GET", "status": 200, "duration_ms": 41.7, "outcome": "success"}
```

| Field | Present | Meaning |
|---|---|---|
| `timestamp` | always | UTC, milliseconds |
| `level` | always | `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `component` | always | the logger: `logidoc.access`, `logidoc.api`, `logidoc.pipeline` (uploads), `logidoc.models` (Ollama), `logidoc.operations` (backups and the monitor), `logidoc.alerts`, `logidoc.server` |
| `request_id`, `correlation_id` | during a request | see the next section |
| `endpoint`, `method`, `status`, `duration_ms`, `outcome` | request lines | the route template (never the raw path with its IDs), and success, client_error or server_error |
| `operation`, `backup_id` | backups | |
| `error_category` | failures | a fixed word: `authentication`, `permission`, `validation`, `not_found`, `conflict`, `payload_too_large`, `database`, `model_service`, `configuration`, `unavailable`, `timeout`, `internal` |
| `error_type`, `traceback` | unexpected errors | the exception class, and the traceback with folders removed |
| `alert`, `severity` | alerts | |

### What is never logged

- **Never written at all:** request and response bodies, query strings,
  document text, extracted fields, prompts and generated answers. Requests
  are logged by route template, method, status and duration only.
- **Removed from every message, field and traceback** by `redaction.redact()`
  before it is written:
  - credentials written as `password=`, `token:`, `api_key=`, `cookie=`,
    `Authorization:` and similar
  - Bearer tokens
  - `user:password@` in URLs
  - everything after the host in a URL (a webhook's path is often its secret)
  - the folders of Windows, UNC and POSIX file paths: the file name is kept,
    as `<path>/audit_history.sqlite3`
  - e-mail addresses
- **Cut to length:** text longer than 2,000 characters (8,000 for a
  traceback), so a document that reached an exception message cannot fill
  the log.

The client IP address is not logged. The reverse proxy's own access log has
it, if you need it.

## Request IDs

Every response carries `X-Request-ID` and `X-Correlation-ID`, including errors
and the 500 for an unexpected error.

- **`X-Request-ID`** is the caller's own value when it is valid (letters,
  digits and `. _ : -`, up to 64 characters); otherwise a new 32-character ID.
  The nginx configuration passes nginx's `$request_id` and Caddy passes a
  UUID, so the proxy log and the application log share one ID.
- **`X-Correlation-ID`** is the caller's own value, otherwise the request ID.
  An ERP or TMS calling the API can send its own (for example
  `erp:order-991`) to follow one business operation through both systems.

Both appear on every log line written while the request is served: the
access line, errors, model calls, the upload pipeline. When a user reports a
problem, the request ID shown in the browser's developer tools finds every
line about it. An unexpected error is logged with its redacted traceback and
the request ID, while the user sees only `Something went wrong on the
server.`

## Metrics

Kept in memory since the process started, for real events only. A metric
that nothing recorded is absent, never zero-filled or estimated. Read them
as an administrator:

```
GET /api/admin/metrics                      JSON snapshot
GET /api/admin/metrics?format=prometheus    Prometheus text format (0.0.4)
```

| Metric | Type | Labels | Recorded when |
|---|---|---|---|
| `logidoc_http_requests_total` | counter | method, route, status (`2xx`...) | every API request |
| `logidoc_http_request_duration_seconds` | histogram | route | every API request |
| `logidoc_http_errors_total` | counter | category | every 4xx/5xx |
| `logidoc_database_errors_total` | counter | | a store error or a SQLite failure reaches a request |
| `logidoc_ingestion_files_total` | counter | outcome (indexed, partial, failed, duplicate, ...) | each uploaded file finishes |
| `logidoc_pdf_processing_seconds` | histogram | | each uploaded file finishes |
| `logidoc_llm_request_duration_seconds` | histogram | kind (embedding, language model), outcome (success or the error class) | every Ollama call |
| `logidoc_ollama_available` | gauge | | `/api/ready` and each monitor check |
| `logidoc_freight_audit_duration_seconds` | histogram | | each freight audit over all shipments |
| `logidoc_backup_runs_total` | counter | outcome | each backup attempt |
| `logidoc_backup_duration_seconds` | histogram | | each backup attempt |
| `logidoc_backup_last_success_timestamp_seconds` | gauge | | a backup succeeds |
| `logidoc_disk_free_bytes` | gauge | volume (data, index, backups) | each monitor check |
| `logidoc_readiness` | gauge | status | each `/api/ready` |
| `logidoc_alerts_total` | counter | rule, severity | an alert is sent |

Labels are fixed words and route templates. They never hold a raw path, a
shipment ID, a file name or a user, so the number of series stays small.
Counters restart from zero when the process restarts; `uptime_seconds` in the
JSON says how long they cover.

To scrape with Prometheus, collect from the machine itself. The reverse
proxies block `/api/admin/metrics` from outside, and the endpoint needs an
administrator's session. A scraper on the same machine can sign in, or read
the JSON snapshot with a session cookie. A token-based scrape is not built.

## Health checks

See [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md#health-and-readiness).
In short:

- **`/api/health`** means alive.
- **`/api/ready`** means ready, degraded or not ready. It has separate checks
  for the documents, the data folder, the core databases, the extraction
  store, the vector index, the model service and the free disk space.
- **Optional AI.** Ollama being offline makes the server `degraded`, never
  `not_ready`.

## Alerts

The monitor runs in the server process every `LOGIDOC_MONITOR_INTERVAL`
seconds (300 by default). It measures the free space on the data, index and
backup volumes, asks Ollama for its model list, reads the recent requests
from the metrics and reads the backup status. Then it applies these rules:

| Alert | Severity | Fires when | Settings (default) |
|---|---|---|---|
| `backup_failed` | critical | any backup attempt fails (raised at once, not by the monitor); cleared by the next successful backup | |
| `backup_stale` | warning | a schedule is on and the last successful backup is older than the limit, or there is none | `LOGIDOC_ALERT_BACKUP_MAX_AGE_HOURS` (26) |
| `disk_low` | warning, critical | the lowest free space under the warning or critical limit | `LOGIDOC_ALERT_DISK_WARNING_MB` (2048), `LOGIDOC_ALERT_DISK_CRITICAL_MB` (512) |
| `api_failures` | critical | this many 5xx responses in the window | `LOGIDOC_ALERT_5XX_COUNT` (5), `LOGIDOC_ALERT_WINDOW_SECONDS` (300) |
| `database_errors` | critical | this many store errors in the window | `LOGIDOC_ALERT_DB_ERROR_COUNT` (1) |
| `ai_service_down` | warning | the model service failed this many checks in a row | `LOGIDOC_ALERT_OLLAMA_FAILURES` (3) |
| `latency_high` | warning | the 95th percentile request time over the limit, with enough requests | `LOGIDOC_ALERT_LATENCY_MS` (5000), `LOGIDOC_ALERT_MIN_REQUESTS` (20) |

How alerts behave:

- **First firing.** An alert is sent when it first fires, and again when it
  becomes more severe.
- **Repeats.** While it lasts, it is sent again only every
  `LOGIDOC_ALERT_COOLDOWN_SECONDS` (3600).
- **Clearing.** When its rule clears, a `resolved` event is sent.
- **Viewing.** `GET /api/admin/alerts` (administrators) lists the active
  alerts and the last 100 events.
- **Restarts.** Alerts are kept in memory, so a restart clears them; the
  monitor raises any condition that still holds again at its next check.

Messages are written for the person who receives them. They say what still
works, for example "the audit, documents and reports keep working", and
never include a path, a host or a secret.

### Where alerts go

- **The log, always.** Every alert is a line from the `logidoc.alerts`
  component: `WARNING` for warnings, `CRITICAL` for critical ones. Anything
  that watches the logs (journald, a log shipper) can act on it.
- **A webhook, optional.** Set `LOGIDOC_ALERT_WEBHOOK_URL` (HTTPS in
  production). Each alert event is POSTed as
  `{"source": "logidoc-rag", "alert": {"key", "severity", "message", "state", "first_seen", "last_seen"}}`
  with a 5-second timeout and no retry loop. A failure is logged without the
  URL.

### Adding another channel

A notifier is any object with a `send(alert)` method, added to
`AlertManager.notifiers` (`src/alerts.py`, built in
`AlertManager.from_settings`).

- **E-mail.** A notifier using the standard library's `smtplib` with
  STARTTLS. Read the server, account and password from new `LOGIDOC_ALERT_SMTP_*`
  settings, treat the password as a secret, and add the names to
  `environment.py` and `.env.example`. Not built: it needs an SMTP account.
- **Chat (Slack, Teams).** Their incoming webhooks take a JSON POST, so the
  existing webhook notifier works with a small adapter for their message
  format.
- **External monitoring** (Prometheus with Alertmanager, Grafana, Zabbix,
  Uptime Kuma). Probe `/api/health` and `/api/ready` from outside; for
  metrics, scrape `/api/admin/metrics?format=prometheus` on the machine.
  These tools can then take over the alert rules.

## Limits

- **Single process.** Metrics and alerts are per process and reset on
  restart, which fits the single-process design. Long-term history needs an
  external collector (Prometheus or a log store).
- **No dashboard page.** The metrics are not shown in the web application;
  read them through the endpoints or a collector.
- **Latency starts at the application.** The latency alert measures from the
  moment the application receives a request, so time spent in the reverse
  proxy or on the network is not included.
