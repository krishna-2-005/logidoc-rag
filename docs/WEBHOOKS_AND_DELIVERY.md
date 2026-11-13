# Webhooks and delivery

LogiDoc-RAG can notify external systems (an ERP, a TMS, an accounts payable system) when something is committed:
a freight audit recorded, an exception created or changing status, carrier master data changed. This document is
for the operators who configure webhooks and the integrators who build the receiving side.

Code: `src/webhooks.py` (service), `src/url_safety.py` (destination checks), `src/integration_store.py`
(storage), `src/api/routes_integrations.py` (API). Screens: Integrations, then Webhooks (administrators).

---

## 1. Events

| Type | When | Data |
|---|---|---|
| `freight_audit.completed` | A recorded audit run wrote a shipment's new snapshot with a freight audit | run id and label, shipment id and key, snapshot id, outcome, carrier, currency, invoice total, discrepancy and review counts, final status |
| `freight_audit.discrepancy_detected` | The same, when the outcome is `DISCREPANCY` | as above |
| `exception.created` | The audit detected a new exception (recorded as open) | exception key, shipment, category, status, detected at |
| `exception.status_changed` | A review status changed (a person's action, or the audit reopening it) | exception key, shipment, category, from and to, the action, actor and role, changed at |
| `carrier.created` | A carrier was registered | carrier id, master data, actor |
| `carrier.updated` | A carrier's master data changed | carrier id, each changed field before and after, actor |
| `carrier_name_link.approved` | A second administrator approved a name link | request id, document name, carrier id, previous carrier id, approved by and at |

Free text (review notes, link reasons) is not sent: it stays inside the application. A shipment whose run found it
unchanged produces no new freight audit event; it was reported when its snapshot was first written.

### Only committed changes

Events are never raised from inside an operation. The collector reads three append-only logs that already exist,
after a stored cursor:

- `audit_runs`: the audit-run feed.
- `review_events`: the exception review log.
- `carrier_events`: the carrier registry log.

A reader sees only committed rows, so a change that fails or is rolled back can never become an event (tested by
holding an uncommitted insert open while the collector runs).

### Never twice

- The event id is derived from (source, source row, type): `evt_` and 32 hex characters.
- The outbox (`webhook_events`) holds each (source, source row, type) once, and each (event, endpoint) has
  one delivery.
- The cursor moves in the same transaction that stores the events, so a crash cannot skip a change. Re-reading
  the log changes nothing (tested by moving a cursor back).
- `webhook_events` is append-only.

### Starting point

Collection starts when the first endpoint is registered: the cursors are set to the logs' current ends. History
from before is not replayed.

## 2. What a delivery looks like

```
POST <endpoint URL>
Content-Type: application/json
User-Agent: LogiDoc-Webhooks/1
LogiDoc-Event-Id: evt_41e06349072b9aa5ddf0bffc870acdbd
Idempotency-Key: evt_41e06349072b9aa5ddf0bffc870acdbd
LogiDoc-Event-Type: carrier.created
LogiDoc-Delivery-Id: 17
LogiDoc-Delivery-Attempt: 1
LogiDoc-Timestamp: 1794316800
LogiDoc-Signature: v1=5f2c...e9

{"api_version":"1","created_at":"...","data":{...},"id":"evt_...","occurred_at":"...","type":"carrier.created"}
```

### Verifying a delivery (the receiver's job)

1. Read `LogiDoc-Timestamp`, `LogiDoc-Event-Id` and `LogiDoc-Signature`; refuse the request if one is missing.
2. Refuse a timestamp more than 300 seconds from your clock (a delayed or replayed request).
3. Compute `hex(HMAC-SHA256(secret, "<timestamp>.<event id>.<raw body bytes>"))` and compare it, in constant
   time, with each `v1=` value of the header. During a secret rotation there are two.
4. **Refuse an event id you have already processed** and answer 2xx: delivery is *at least once*. A retry, or a
   delivery interrupted by a server restart, can arrive again. Keep processed ids in your own database.
   - `Idempotency-Key` carries the same event id, for receivers and gateways that deduplicate on that header.
   - Every attempt of a delivery sends the same event id, `Idempotency-Key` and body bytes (rebuilt from the
     stored event). Only `LogiDoc-Delivery-Attempt`, the timestamp and therefore the signature change, so verify
     each attempt on its own.
5. Answer 2xx within 10 seconds, then do the work. Any 2xx is success.

`webhooks.verify_signature()` and `webhooks.ReplayGuard` are a reference implementation of steps 1 to 4. The test
`test_end_to_end_over_real_http_with_the_receivers_own_check` runs them in a real HTTP receiver.

## 3. Retries

| Answer | What happens |
|---|---|
| 2xx | Delivered |
| 408, 425, 429, 5xx, timeout, connection, DNS or TLS failure | Retried with back-off |
| 3xx | Failed at once: redirects are never followed |
| Other 4xx | Failed at once: the receiver refused the request itself; fix the receiver, then retry by hand |
| Destination refused by the safety checks | Failed at once |

- **Back-off:** the n-th attempt waits 30 s × 2^(n−1), at most 6 hours, times a random factor from 0.5 to 1
  (jitter, so many deliveries do not retry at the same moment).
- **Retry-After:** a 429 or 503 carrying `Retry-After` is honoured, up to the same maximum.
- **Giving up:** after `LOGIDOC_WEBHOOK_MAX_ATTEMPTS` attempts (8 by default) the delivery fails, with "Gave up
  after n attempts" in its last attempt.
- **Manual retry:** an administrator retries a failed (or retrying) delivery from the delivery list or its detail
  page. The retry starts a new cycle of attempts and is recorded (who, when).
- **Interrupted attempts:** an attempt interrupted by a server stop is found when its lease expires. It is
  recorded as `interrupted` and sent again.
- **Disabled endpoint:** new events create no delivery for it, and waiting deliveries are held until it is
  enabled again.
- **Archived endpoint:** archiving is final. Waiting deliveries are cancelled and the history stays readable.
- **Test events:** a `webhook.test` delivery is attempted once, immediately, and never queued for retries. Test
  deliveries are not counted in the endpoint's statistics.

## 4. Security

### Signing secrets are never stored

- A secret is `whsec_` + base64url(HMAC-SHA256(`LOGIDOC_WEBHOOK_SECRET_KEY`, endpoint id, version, random
  salt)). The store keeps only the salt and the version.
- A copy of the integration store (a backup, for example) therefore cannot sign anything.
- The secret is shown once, when the endpoint is created or its secret rotated, and never in any list, detail,
  log or history.
- **Rotation:** a new secret is issued, and the previous one keeps signing beside it for
  `LOGIDOC_WEBHOOK_ROTATION_GRACE_HOURS` (24), so the receiver can switch without missing an event.
- **Without the key** (or with one under 32 characters), no endpoint can be registered, nothing is delivered, and
  the screen and `GET /api/integrations/webhooks/settings` say why.
- **Losing the key** invalidates every secret: rotate every endpoint after setting a new key.

### Destinations (SSRF protection, `src/url_safety.py`)

A webhook request is made by the server, from inside its network, so the destination is checked twice: when the
URL is saved, and again after a fresh DNS lookup before every attempt.

- **Scheme and credentials:**
  - Only `https://` is accepted, with no user name or password in the URL and no fragment.
  - `http://` only with `LOGIDOC_WEBHOOK_ALLOW_HTTP=1`, which is refused in production.
- **Host names** that point at the server or an internal network are refused before any lookup: `localhost`,
  `*.localhost`, `*.local`, `*.internal`, `*.home.arpa`, `*.lan`, single-label names, the cloud metadata names,
  and names that look like encoded addresses (`2130706433`, `0x7f.1`).
- **Addresses:** every address the name resolves to must be public and global. One refused address refuses the
  whole name. Refused:
  - loopback, private (RFC 1918, ULA) and link-local (169.254.0.0/16, which holds metadata services)
  - carrier-grade NAT, multicast, reserved and unspecified
  - IPv6 forms that embed an IPv4 address (mapped, NAT64, 6to4, Teredo)
- **Ports:** 443, or 1024 to 65535.
- **Pinning:** the connection goes to the address that was checked, with the host name kept for SNI and the
  certificate check. A second DNS answer (rebinding) cannot change it.
- **Limits:** redirects are not followed. Each attempt has a 10 second timeout (`LOGIDOC_WEBHOOK_TIMEOUT_SECONDS`).
  The receiver's answer is read up to 64 KB and discarded. A payload over 256 KB is not sent.
- **Development:** `LOGIDOC_WEBHOOK_ALLOW_PRIVATE=1` lifts the address rules outside production, for a receiver
  on the same machine. Production refuses to start with it (`src/environment.py`).

### Logs and screens

- Log lines name the delivery and endpoint ids and the error category, never the URL, a secret or the payload.
- The URL is shown with its query values masked (`?token=[redacted]`), including in the change history.
- The payload preview masks contact email, phone and any key that looks like a secret, token, password,
  authorization or credential. The receiver still gets the full payload.

### Permissions

Every webhook route needs `integrations.manage` (administrators) and, for a change, the `X-LogiDoc-Client`
header. The service checks the permission again itself.

## 5. API

| Method and path | Answer |
|---|---|
| `GET /api/integrations/webhooks/settings` | `WebhookConfiguration`: event types, signature scheme, limits, why delivery is off |
| `GET /api/integrations/webhooks?include_archived=` | `WebhookEndpointList`, each with seven-day statistics |
| `POST /api/integrations/webhooks` | 201 `WebhookSecretIssued` (the secret, this once); 409 without a signing key; 422 refused destination |
| `GET /api/integrations/webhooks/{id}` | `WebhookEndpointDetail` with the change history |
| `PATCH /api/integrations/webhooks/{id}` | name, url, event_types, enabled |
| `POST /api/integrations/webhooks/{id}/rotate-secret` | `WebhookSecretIssued` |
| `POST /api/integrations/webhooks/{id}/archive` | the endpoint, archived |
| `POST /api/integrations/webhooks/{id}/test` | `WebhookDeliveryDetail` of the test delivery |
| `GET /api/integrations/deliveries?endpoint_id=&status=&event_type=&from=&to=&offset=&limit=` | `DeliveryPage`; `event_type` repeatable, `from`/`to` are UTC days (inclusive) of creation, 422 when `from` is after `to` |
| `GET /api/integrations/deliveries/{id}` | `WebhookDeliveryDetail`: masked payload, every attempt |
| `POST /api/integrations/deliveries/{id}/retry` | `WebhookDeliveryDetail` after the new attempt |
| `GET /api/integrations/monitoring/webhooks?days=&endpoint_id=&event_type=` | `WebhookMonitoring` for the last 1 to 90 days (default 14) |

Deleting is done by archiving: no route removes an endpoint or its history.

### Monitoring

The Webhooks page's **Monitoring** and **Delivery history** tabs read only the stored deliveries and attempts. Test
deliveries are left out of the monitoring. Over the chosen period (deliveries created in it, UTC days):

- totals: deliveries, succeeded, failed, waiting (pending, retrying or sending), cancelled, attempts, and retry
  attempts (any attempt after a delivery's first);
- success rate = succeeded / (succeeded + failed). Waiting deliveries are not in the denominator, and with none
  finished the rate is `null` (n/a), never 0 % or 100 %;
- per endpoint: its counts and its last success and failure in the period, endpoints with failures first;
- per event type, per attempt error category, and per day: outcomes, attempts, and the attempts' measured duration
  as median and 95th percentile (nearest rank). A day without an attempt has no latency, not 0 ms.

## 6. Storage

`integrations.sqlite3` (`LOGIDOC_INTEGRATION_DB`, default beside the other stores) holds the following tables:

| Table | Holds |
|---|---|
| `webhook_endpoints` | endpoints (salt and version only) |
| `endpoint_changes` | append-only: who changed what |
| `webhook_events` | append-only: the outbox |
| `webhook_deliveries` | one per event and endpoint: status, attempts, next attempt, last answer, lease, manual retries |
| `delivery_attempts` | append-only: every attempt with its duration, HTTP status, outcome, category and short detail |
| `source_cursors` | the collector's position in each log |

The store is created when the first endpoint is registered. Reading never creates it. It is included in the
backups (`src/backup.py`).

## 7. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `LOGIDOC_WEBHOOK_SECRET_KEY` | none | At least 32 random characters. Secrets are derived from it. Keep it out of git and out of backups |
| `LOGIDOC_WEBHOOK_TIMEOUT_SECONDS` | 10 | One attempt (1 to 60) |
| `LOGIDOC_WEBHOOK_MAX_ATTEMPTS` | 8 | Attempts before a delivery fails (1 to 30) |
| `LOGIDOC_WEBHOOK_ROTATION_GRACE_HOURS` | 24 | How long the previous secret still signs (0 to 720) |
| `LOGIDOC_WEBHOOK_ALLOW_HTTP` | off | Development only: `http://` destinations |
| `LOGIDOC_WEBHOOK_ALLOW_PRIVATE` | off | Development only: loopback and private destinations |

## 8. Limitations

- Events are collected by the scheduler (`docs/SCHEDULED_SYNCHRONISATION.md`), so they leave within about 30
  seconds (activity) or a minute (audit runs) of the commit, not instantly.
- `exception.created` is recorded when the exception queue is read (the review store records newly detected
  exceptions then), as before this sprint.
- Delivery runs in one server process, one delivery at a time: a slow receiver delays the others by up to the
  timeout.
- No receiver-side SDK is shipped; the verification steps above and `verify_signature()` are the reference.
- Tested against fake receivers and a real local HTTP receiver. No external partner endpoint was used.
