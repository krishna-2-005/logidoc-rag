# TMS and ERP integration framework

A provider-neutral way to read shipments and carriers from an external system, hand it LogiDoc's recorded audit
runs, and keep every imported record with its source and history. It includes one working provider, a sandbox
TMS on synthetic data. **No real vendor is integrated.** This document is for the engineers who will add a real
provider and the operators who run integrations.

Code:

- `src/integration_framework.py`: contracts, errors, provider interface, record store, sync engine, service.
- `src/sandbox_tms.py`: the sandbox provider.
- `src/api/routes_integrations.py`: the API.

Screen: Integrations, then TMS / ERP (administrators).

---

## 1. Layers

| Layer | Where | Holds |
|---|---|---|
| Contracts | `ShipmentRecord`, `CarrierRecord`, `Page`, `ConnectionResult` | Typed, provider-neutral records |
| Provider | a `Provider` subclass | Authentication, transport and the partner's own format. Never reads or writes LogiDoc's stores |
| Errors | `IntegrationError` categories | `authentication_failed`, `rate_limited`, `timeout`, `unavailable`, `invalid_response`, `not_supported`, `configuration`. `normalize()` maps any other failure onto them |
| Sync state | integration store (`integrations`, `sync_runs`, `sync_rejections`, `integration_changes`) | Settings, credential variable name, cursor, status, runs, rejected records, configuration history |
| Imported records | `ExternalRecordStore` | Records with provenance, every version, manual corrections |

Nothing imported is written into LogiDoc's documents, audits, reviews or carrier registry. Imported records are
reference data in their own store. The freight audit engine contains no vendor logic.

## 2. How a sync runs

1. **Lease.** The integration's lease is claimed: one sync of an integration at a time, scheduled or requested
   (409 otherwise).
2. **Pages.** For each entity the provider supports (carriers, then shipments), pages are fetched from the
   stored cursor.
   - A rate limit, timeout or unavailability is retried 3 times with back-off, honouring the partner's
     `Retry-After` up to a minute.
   - An authentication failure stops the run and marks the connection `auth_failed`.
3. **Validation.** Each item is validated and mapped to its contract. A refused item is a **rejection** (entity,
   external id, reason), never a guess:
   - an unknown status, or a date that is not YYYY-MM-DD
   - a delivery before its pickup
   - a charge without a currency, or a negative or non-numeric amount
   - a timestamp without a time zone
   - an identifier the carrier registry's own rules refuse
4. **Duplicates and changes.**
   - The same item twice in one page: kept once, the later change winning; counted as a duplicate.
   - An item an earlier page of the same run brought with the same content: a duplicate.
   - Content equal to what is stored: unchanged (a change of the partner's timestamp alone is not a change).
   - Different content: a new version (updated).
5. **Manual corrections win.** A field an administrator corrected is never overwritten. The received value is kept
   in the record's version and counted as a conflict. Clearing the correction brings the partner's value back.
6. **Cursor.** The cursor is saved after each page, after that page's records. A crash re-reads at most one page,
   and re-reading changes nothing (tested).
7. **Audit runs.** Providers that accept them get LogiDoc's recorded audit runs (the audit-run feed) after their
   own cursor. Re-sending is safe: the sandbox stores each run id once.
8. **Bounded runs.** At most 20 pages per run. More waiting: the next sync comes after a minute instead of the
   interval.
9. **Outcome.**
   - `succeeded`
   - `partial`: some records were rejected, or the run failed after some pages
   - `failed`
   - `interrupted`: found after a stop

   A failure backs off from 60 s, doubling, within the interval.

## 3. Provenance

Every imported record keeps:

- the integration, the provider and the partner's external id
- the partner's change time (`source_updated_at`)
- the first and last sync runs that brought it
- a content hash
- `received`: exactly what was received, mapped
- `data`: received, with the corrections in force applied
- every version: created, updated (with any corrected field kept), correction, correction removed

Versions and corrections are append-only.

## 4. The sandbox TMS

A simulated external TMS for building and testing integrations. It is not a real vendor's API and claims no
compatibility with one.

- **Isolation.**
  - Offered only when `LOGIDOC_INTEGRATION_SANDBOX_DIR` names an absolute folder apart from the data, index and
    backup folders, and never in production. Startup validation and the service both refuse otherwise.
  - The simulated system (`sandbox_tms.sqlite3`) and everything a sandbox integration imports
    (`sandbox_imports.sqlite3`) live in that folder only.
  - The integration store keeps only the configuration, runs and rejection reasons.
  - Every identifier starts with `SBX-` and every name with "Sandbox".
- **Data.** 6 carriers and 40 shipments, generated the same way every time. Four are deliberately malformed (an
  invalid SCAC, an unknown status, a delivery before its pickup, a charge without a currency), so a first sync
  imports 42 records and rejects 4.
- **Paging and increments.** Items are ordered by (updated_at, id), and the cursor is the last item's pair. "Simulate
  partner changes" moves shipments forward with a new updated_at, so the next sync finds exactly those.
- **Authentication.** The credential must be the public sandbox value `sandbox-token`, read from the variable the
  integration names (`LOGIDOC_INTEGRATION_SANDBOX_TOKEN` by default).
- **Simulations** (settings):
  - `simulate_auth_failure`
  - `rate_limit_every`: every Nth call answers "rate limited, retry after 1 s"
  - `timeout_every`
  - `duplicate_records`
  - `unavailable`
  - `page_size`

## 5. Credentials

- An integration names the **environment variable** holding its credential, never the value. The name must be
  `LOGIDOC_INTEGRATION_<NAME>`, so an integration cannot read `PATH` or another application's secret.
- The value is read when the system is called and is never stored, logged or returned. The API returns only
  `credential_configured` (set or not).
- Settings are validated per provider. An unknown setting (such as `api_key`) is refused, so a secret cannot be
  smuggled into the stored settings.

## 6. API

All routes need `integrations.manage`, and the CSRF header for changes.

| Method and path | Answer |
|---|---|
| `GET /api/integrations/connections` | `IntegrationList`: integrations, and the providers with why one is unavailable |
| `POST /api/integrations/connections` | 201 `Integration` |
| `GET /api/integrations/connections/{id}` | `IntegrationDetail` with the configuration history |
| `PATCH /api/integrations/connections/{id}` | name, enabled, settings, credential_env, sync_interval_seconds |
| `POST /api/integrations/connections/{id}/test` | `ConnectionTest` (ok, category, detail, latency) |
| `POST /api/integrations/connections/{id}/sync` | 202 `SyncStarted`; 409 while a sync runs |
| `GET /api/integrations/connections/{id}/runs` | `SyncRunPage` |
| `GET /api/integrations/sync-runs/{id}` | `SyncRunDetail` with the rejected records |
| `GET /api/integrations/connections/{id}/records?entity=&search=&offset=&limit=` | `RecordPage` |
| `GET /api/integrations/connections/{id}/records/{record}` | `ImportedRecordDetail`: provenance, versions, corrections |
| `POST /api/integrations/connections/{id}/records/{record}/corrections` | `{"field", "value", "reason", "clear"}` → the record |
| `POST /api/integrations/connections/{id}/sandbox/changes` | sandbox only: `{"changed": [ids]}` |

## 7. Adding a real TMS or ERP provider

Do not claim a vendor is integrated until each step below is done against the vendor's own sandbox.

1. **Documentation and access.** Obtain the vendor's API documentation, a sandbox account and test credentials.
   Do not build from guesses about an undocumented API.
2. **A `Provider` subclass** (one module, such as `src/provider_<vendor>.py`):
   - `key`, `label`, `description`, `entities`, `accepts_audit_runs`, and `sandbox = False`.
   - `default_credential_env`, for example `LOGIDOC_INTEGRATION_ACME_TOKEN`.
   - `check_settings()`: accept only documented, non-secret options (base URL, account id, page size). Refuse
     anything else.
   - `test_connection()`: a cheap authenticated call.
   - `fetch(entity, cursor, page_size) -> Page`: one page, and the next cursor in the vendor's own terms
     (timestamp, sequence or token).
   - `map(entity, raw)`: the vendor's item to `ShipmentRecord` or `CarrierRecord`; raise `ValueError` with the
     reason to reject an item.
   - `send_audit_runs(runs)`, if the vendor takes them.
   - Raise the `IntegrationError` categories (`RateLimited` with `retry_after`, `AuthenticationFailed`, and so
     on). Never return a partial page silently.
3. **Transport.**
   - HTTPS only, with timeouts, no credentials in URLs or logs, and redirects off.
   - If the vendor URL is configurable, check it with `url_safety.check_url`.
   - Do not add a large client library when the standard library will do.
4. **Register** it in `integration_framework.registered_providers()`. Its records are stored in the integration
   store (not in the sandbox folder).
5. **Tests** against recorded vendor responses (no network in the test suite):
   - paging and incremental sync
   - authentication failure, rate limits and timeouts
   - duplicates and changed records
   - rejections, and corrections that are kept
6. **Vendor sandbox.** Run it against the vendor's sandbox, record the results in a report, then enable it for an
   administrator.
7. **Documentation.** Document the vendor's settings in `.env.example` and this file.

## 8. Limitations

- Only the sandbox provider exists. No real TMS or ERP was connected or tested.
- Imported records are reference data. They are not yet matched to the shipments LogiDoc reads from documents,
  and they do not feed the freight audit. Doing so is a design decision for a later sprint.
- One sync at a time per integration, all integrations one after another in the scheduler's thread.
- The sandbox's failure counters are shared by every sandbox integration (they live in the sandbox's own
  database).
