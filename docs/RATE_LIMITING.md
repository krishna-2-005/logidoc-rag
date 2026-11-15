# Rate limiting

The API limits its expensive and sensitive operations itself, and the nginx example adds limits at the proxy in
front of it. Code: `src/api/rate_limit.py`. Tests: `tests/test_rate_limit.py`.

## 1. The application's limits

Each rule is a token bucket per caller: up to *limit* requests at once, then one more every *period ÷ limit*
seconds. Paths are matched after `/api/v1/...` is resolved to `/api/...`, so both forms share one budget.

| Rule | Operations | Limit | Per |
|---|---|---|---|
| `sign_in` | `POST /api/auth/login` | 10 **failed** attempts a minute | client address |
| `assistant` | `POST /api/chat`, `POST /api/assistant` | 20 a minute | session |
| `documents` | upload, retry, replace, reprocess, remove | 30 a minute | session |
| `audit_jobs` | extraction run and recompute, recording a run, controlled re-audit, backups and their verification, history verification, integration sync, test and job runs, webhook test | 30 a minute | session |
| `exports` | report CSV and PDF downloads: `/api/reporting/{report}.csv` and `.pdf`, `/api/reports/...`, `/api/trends/export.csv` | 60 a minute | session |
| `accounts` | creating and changing user accounts | 20 a minute | session |

Everything else, in particular every screen's reads and the report previews, is not limited.

- **Sign-in counts failures only** (any answer of 400 or more). Many people signing in from one office address
  are never slowed down; a client guessing passwords is, on top of the per-account lockout (5 failures, then
  15 minutes).
- **Per session** means per signed-in session (a SHA-256 digest of its cookie, never the cookie itself). A
  request without a session counts against its client address, and is refused with 401 anyway.

## 2. A refused request

`429 Too Many Requests`, with:

- `Retry-After`: whole seconds until the next request would pass;
- a JSON body `{"detail": "Too many report downloads: try again in 20 s."}`;
- the usual security headers and request ID. The request is never run.

Each refusal is logged as a warning naming the rule (never the address or user) and counted in
`logidoc_rate_limited_total{rule}` ([OBSERVABILITY_AND_MONITORING.md](OBSERVABILITY_AND_MONITORING.md)).

## 3. The client address and forwarded headers

The application never reads `X-Forwarded-For` itself. The address it limits is the one the server resolves:

- **Without a proxy:** the address of the connection.
- **Behind a proxy:** uvicorn's proxy-header handling takes `X-Forwarded-For` only from the addresses in
  `LOGIDOC_FORWARDED_ALLOW_IPS` (`127.0.0.1` by default), and from the right: the last address a trusted proxy
  added, not one the client wrote.
- **`LOGIDOC_FORWARDED_ALLOW_IPS=*` is refused in production** and warned about otherwise: with `*` the server
  takes the left-most address, which the client chooses.

The proxies in `deploy/` replace the header with the address they see: nginx with
`proxy_set_header X-Forwarded-For $remote_addr;`, Caddy by default.

## 4. Limits at the proxy

`deploy/nginx/logidoc.conf` adds, per client address:

- `logidoc_sign_in`: 30 sign-in requests a minute (burst 30), successful or not;
- `logidoc_api`: 20 API requests a second (burst 60, as one screen makes several requests at once).

Both answer 429 (`limit_req_status`). The `limit_req_zone` lines belong to nginx's `http` context: the file is
included there (`sites-enabled/` or `conf.d/`). Caddy has no rate limiting without a plugin; the application's
limits apply either way.

## 5. Limits and memory

- **Bounded:** at most 10,000 buckets are kept, the least recently used dropped first. A dropped bucket starts
  full again.
- **Per process:** the service unit runs one process (`deploy/logidoc-api.service`). With several, each applies
  the limits on its own, and the proxy's limits apply to all of them together.
- **Restart:** the buckets start full again.

## 6. Changing a limit

The rules are code (`RULES` in `src/api/rate_limit.py`), reviewed like any other change: a rule names its
methods, a path pattern, the limit and period, and whether it counts per session or per client address, and
every request or failures only. `tests/test_rate_limit.py` lists which operation each rule covers.
