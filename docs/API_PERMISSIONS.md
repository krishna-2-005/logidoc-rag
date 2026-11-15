# API permissions

Who may call each operation of the API. Generated from the routes by `scripts/api_permissions.py`;
`tests/test_api_authorization.py` fails when this file and the code disagree, and calls every operation
without a session, as a reviewer and as an administrator. Do not edit by hand.

## Rules

- **Every path is also served as `/api/v1/...`**, the same route with the same permission and the same
  CSRF rule.
- **Without a session:** 401, apart from the operations marked *Anyone*.
- **Without the permission:** 403, before the request body is read or any store is opened.
- **Every POST and PATCH needs the header `X-LogiDoc-Client: web`** (CSRF): 403 without it.
- **Local mode** (`LOGIDOC_AUTH_MODE=local`, for development) runs every request as the local
  administrator, so the server starts in it only on a loopback address, and production refuses it.

## Roles

| Permission | Administrator | Auditor / reviewer |
|---|---|---|
| `admin.maintenance` | yes | no |
| `admin.settings` | yes | no |
| `admin.users` | yes | no |
| `carriers.manage` | yes | no |
| `exceptions.approve` | yes | no |
| `exceptions.assign_any` | yes | no |
| `exceptions.assign_self` | yes | yes |
| `exceptions.dismiss` | yes | no |
| `exceptions.reopen` | yes | yes |
| `exceptions.update` | yes | yes |
| `exceptions.view` | yes | yes |
| `exceptions.waive` | yes | no |
| `extraction.approve` | yes | no |
| `extraction.correct` | yes | yes |
| `extraction.run` | yes | no |
| `extraction.view` | yes | yes |
| `history.record` | yes | no |
| `history.view` | yes | yes |
| `ingestion.view` | yes | yes |
| `ingestion.write` | yes | no |
| `integrations.manage` | yes | no |
| `reports.download` | yes | yes |

## Operations

126 operations: 5 open to anyone, 60 for administrators and reviewers, 61 for administrators only.

### admin

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/admin/alerts` | Administrators only | `admin.maintenance` |  |
| GET | `/api/admin/backups` | Administrators only | `admin.maintenance` |  |
| POST | `/api/admin/backups` | Administrators only | `admin.maintenance` |  |
| GET | `/api/admin/backups/checks` | Administrators only | `admin.maintenance` |  |
| POST | `/api/admin/backups/{backup_id}/verify` | Administrators only | `admin.maintenance` |  |
| GET | `/api/admin/events` | Administrators only | `admin.users` |  |
| GET | `/api/admin/metrics` | Administrators only | `admin.maintenance` |  |
| GET | `/api/admin/users` | Administrators only | `admin.users` |  |
| POST | `/api/admin/users` | Administrators only | `admin.users` |  |
| PATCH | `/api/admin/users/{username}` | Administrators only | `admin.users` |  |
| POST | `/api/admin/verify-history` | Administrators only | `admin.maintenance` |  |

### assistant

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| POST | `/api/assistant` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/assistant/tools` | Administrators and reviewers | `exceptions.view` |  |

### auth

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| POST | `/api/auth/login` | Anyone | none | Sign-in. Failed attempts lock the account (5 failures, 15 minutes) and are rate limited. |
| POST | `/api/auth/logout` | Anyone | none | Ends the caller's own session, if any. |

### carrier-registry

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/carrier-registry` | Administrators and reviewers | `exceptions.view` |  |
| POST | `/api/carrier-registry` | Administrators only | `carriers.manage` |  |
| GET | `/api/carrier-registry/carriers` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/carrier-registry/carriers/{carrier_id}` | Administrators and reviewers | `exceptions.view` | The carrier's change history is included only with `carriers.manage`. |
| GET | `/api/carrier-registry/carriers/{carrier_id}/events` | Administrators only | `carriers.manage` |  |
| GET | `/api/carrier-registry/document-names` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/carrier-registry/events` | Administrators only | `carriers.manage` |  |
| GET | `/api/carrier-registry/links` | Administrators and reviewers | `exceptions.view` |  |
| POST | `/api/carrier-registry/links` | Administrators only | `carriers.manage` |  |
| POST | `/api/carrier-registry/links/{request_id}/decision` | Administrators only | `carriers.manage` |  |
| PATCH | `/api/carrier-registry/{carrier_id}` | Administrators only | `carriers.manage` |  |

### carriers

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/carriers` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/carriers/{carrier_key}` | Administrators and reviewers | `exceptions.view` |  |

### chat

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| POST | `/api/chat` | Administrators and reviewers | `exceptions.view` |  |

### completeness

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/completeness` | Administrators and reviewers | `exceptions.view` |  |

### documents

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/documents` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/documents/file` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/documents/pipeline` | Administrators and reviewers | `exceptions.view` |  |
| POST | `/api/documents/remove` | Administrators only | `ingestion.write` |  |
| POST | `/api/documents/replace` | Administrators only | `ingestion.write` |  |
| POST | `/api/documents/reprocess` | Administrators only | `ingestion.write` |  |

### exceptions

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/exceptions` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/exceptions/activity` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/exceptions/{key}` | Administrators and reviewers | `exceptions.view` |  |
| POST | `/api/exceptions/{key}/actions` | Administrators and reviewers | `exceptions.view` | Then the action's own permission: start, note, resolve, dispute and assign `exceptions.update`; assigning to oneself `exceptions.assign_self`, to someone else `exceptions.assign_any`; dismiss `exceptions.dismiss`; waive `exceptions.waive`; reopen `exceptions.reopen`; approve and reject `exceptions.approve`. A reviewer can work an exception, not dismiss, waive or approve one. |
| GET | `/api/exceptions/{key}/history` | Administrators and reviewers | `exceptions.view` |  |

### exposure

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/exposure` | Administrators and reviewers | `exceptions.view` |  |

### extractions

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/extractions` | Administrators and reviewers | `extraction.view` |  |
| POST | `/api/extractions/corrections/{correction_id}/decision` | Administrators only | `extraction.approve` |  |
| GET | `/api/extractions/integrity` | Administrators only | `admin.maintenance` |  |
| GET | `/api/extractions/pending` | Administrators and reviewers | `extraction.view` |  |
| POST | `/api/extractions/recompute` | Administrators only | `history.record` |  |
| POST | `/api/extractions/run` | Administrators only | `extraction.run` |  |
| GET | `/api/extractions/staleness` | Administrators and reviewers | `history.view` |  |
| GET | `/api/extractions/{extraction_id}` | Administrators and reviewers | `extraction.view` |  |
| POST | `/api/extractions/{extraction_id}/corrections` | Administrators and reviewers | `extraction.correct` |  |
| POST | `/api/extractions/{extraction_id}/line-items/{index}/corrections` | Administrators and reviewers | `extraction.correct` |  |

### health

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/health` | Anyone | none | Liveness: answers that the process runs, opens nothing. |

### history

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/history/compare` | Administrators and reviewers | `history.view` |  |
| GET | `/api/history/runs` | Administrators and reviewers | `history.view` |  |
| POST | `/api/history/runs` | Administrators only | `history.record` |  |
| GET | `/api/history/runs/{run_id}/inputs` | Administrators and reviewers | `history.view` |  |
| GET | `/api/history/shipments/{shipment_id}` | Administrators and reviewers | `history.view` |  |

### ingestion

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/ingestion/errors` | Administrators and reviewers | `ingestion.view` |  |
| GET | `/api/ingestion/register` | Administrators and reviewers | `ingestion.view` |  |
| POST | `/api/ingestion/upload` | Administrators only | `ingestion.write` |  |
| POST | `/api/ingestion/{file_id}/resolve` | Administrators only | `ingestion.write` |  |
| POST | `/api/ingestion/{file_id}/retry` | Administrators only | `ingestion.write` |  |

### integrations

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/integrations/audit-runs` | Administrators and reviewers | `history.view` |  |
| GET | `/api/integrations/connections` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/connections` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/connections/{integration_id}` | Administrators only | `integrations.manage` |  |
| PATCH | `/api/integrations/connections/{integration_id}` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/connections/{integration_id}/records` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/connections/{integration_id}/records/{record_id}` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/connections/{integration_id}/records/{record_id}/corrections` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/connections/{integration_id}/runs` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/connections/{integration_id}/sandbox/changes` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/connections/{integration_id}/sync` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/connections/{integration_id}/test` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/deliveries` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/deliveries/{delivery_id}` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/deliveries/{delivery_id}/retry` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/job-runs/{run_id}` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/jobs` | Administrators only | `integrations.manage` |  |
| PATCH | `/api/integrations/jobs/{name}` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/jobs/{name}/retry` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/jobs/{name}/run` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/jobs/{name}/runs` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/monitoring/connections` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/monitoring/webhooks` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/sync-runs/{run_id}` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/webhooks` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/webhooks` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/webhooks/settings` | Administrators only | `integrations.manage` |  |
| GET | `/api/integrations/webhooks/{endpoint_id}` | Administrators only | `integrations.manage` |  |
| PATCH | `/api/integrations/webhooks/{endpoint_id}` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/webhooks/{endpoint_id}/archive` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/webhooks/{endpoint_id}/rotate-secret` | Administrators only | `integrations.manage` |  |
| POST | `/api/integrations/webhooks/{endpoint_id}/test` | Administrators only | `integrations.manage` |  |

### overview

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/overview` | Administrators and reviewers | `exceptions.view` |  |

### re-audits

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/re-audits` | Administrators and reviewers | `history.view` |  |

### ready

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/ready` | Anyone | none | Readiness: status words only, never a path, host or model name. |

### reporting

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/reporting/catalog` | Administrators and reviewers | `reports.download` |  |
| GET | `/api/reporting/{report}` | Administrators and reviewers | `reports.download` | The corrections report also needs `history.view`. |
| GET | `/api/reporting/{report}.csv` | Administrators and reviewers | `reports.download` | The corrections report also needs `history.view`. |
| GET | `/api/reporting/{report}.pdf` | Administrators and reviewers | `reports.download` | The corrections report also needs `history.view`. |

### reports

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/reports/carriers.csv` | Administrators and reviewers | `reports.download` |  |
| GET | `/api/reports/completeness.csv` | Administrators and reviewers | `reports.download` |  |
| GET | `/api/reports/exceptions.csv` | Administrators and reviewers | `reports.download` |  |
| GET | `/api/reports/exposure.csv` | Administrators and reviewers | `reports.download` |  |
| GET | `/api/reports/register.csv` | Administrators and reviewers | `reports.download` |  |
| GET | `/api/reports/shipments/{shipment_id}.csv` | Administrators and reviewers | `reports.download` |  |
| GET | `/api/reports/shipments/{shipment_id}.pdf` | Administrators and reviewers | `reports.download` |  |

### risk

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/risk/model` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/risk/overview` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/risk/shipments` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/risk/shipments/{shipment_id}` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/risk/trends` | Administrators and reviewers | `exceptions.view` |  |

### session

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/session` | Anyone | none | Says whether the caller is signed in, and as whom; no data. |

### settings

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/settings` | Administrators and reviewers | sign-in | Any signed-in user: harmless values only (limits and policy switches). |

### shipments

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/shipments` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/shipments/{shipment_id}` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/shipments/{shipment_id}/freight-audit` | Administrators and reviewers | `exceptions.view` |  |
| GET | `/api/shipments/{shipment_id}/re-audit` | Administrators and reviewers | `history.view` |  |
| POST | `/api/shipments/{shipment_id}/re-audit` | Administrators only | `history.record` |  |
| GET | `/api/shipments/{shipment_id}/tms` | Administrators and reviewers | `exceptions.view` |  |
| POST | `/api/shipments/{shipment_id}/tms/resolutions` | Administrators only | `extraction.approve` | One administrator decides a TMS conflict, with a mandatory reason, recorded in the audit history. |

### tms

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/tms/conflicts` | Administrators and reviewers | `exceptions.view` |  |

### trends

| Method | Path | Who | Permission | Notes |
|---|---|---|---|---|
| GET | `/api/trends` | Administrators and reviewers | `history.view` |  |
| GET | `/api/trends/export.csv` | Administrators and reviewers | `reports.download` |  |
