"""
HTTP API: webhooks, scheduled jobs and TMS/ERP integrations (administrators:
integrations.manage). Every change also passes the CSRF header check, and the
services check the permission again (src/webhooks.py, src/scheduler.py,
src/integration_framework.py).

Webhooks
    GET   /api/integrations/webhooks/settings           event types, signature scheme, why delivery is off
    GET   /api/integrations/webhooks                    endpoints with their delivery statistics
    POST  /api/integrations/webhooks                    register (the secret is in this answer only)
    GET   /api/integrations/webhooks/{id}               endpoint, statistics, change history
    PATCH /api/integrations/webhooks/{id}               name, URL, event types, enabled
    POST  /api/integrations/webhooks/{id}/rotate-secret  a new secret (this answer only); the old one signs
                                                        beside it for the grace period
    POST  /api/integrations/webhooks/{id}/archive       deactivate for good (history kept)
    POST  /api/integrations/webhooks/{id}/test          a test event, attempted now
    GET   /api/integrations/deliveries                  deliveries (filter by endpoint and status)
    GET   /api/integrations/deliveries/{id}             one delivery: masked payload, every attempt
    POST  /api/integrations/deliveries/{id}/retry       retry a failed delivery now

Scheduled jobs
    GET   /api/integrations/jobs                        the scheduler's own health and every job
    PATCH /api/integrations/jobs/{name}                 enable, disable, interval
    POST  /api/integrations/jobs/{name}/run             run now (202; 409 while it runs)
    POST  /api/integrations/jobs/{name}/retry           run again after a failure (202)
    GET   /api/integrations/jobs/{name}/runs            execution history
    GET   /api/integrations/job-runs/{id}               one run

TMS/ERP integrations
    GET   /api/integrations/connections                 integrations and the providers offered
    POST  /api/integrations/connections                 configure one
    GET   /api/integrations/connections/{id}            status, cursor, counts, change history
    PATCH /api/integrations/connections/{id}            name, enabled, settings, credential variable, interval
    POST  /api/integrations/connections/{id}/test       connection test
    POST  /api/integrations/connections/{id}/sync       sync now (202; 409 while one runs)
    GET   /api/integrations/connections/{id}/runs       sync history
    GET   /api/integrations/sync-runs/{id}              one sync with its rejected records
    GET   /api/integrations/connections/{id}/records    imported records with provenance
    GET   /api/integrations/connections/{id}/records/{record}   one record, its versions and corrections
    POST  /api/integrations/connections/{id}/records/{record}/corrections   correct a field (never overwritten)
    POST  /api/integrations/connections/{id}/sandbox/changes    sandbox only: simulate partner changes

Nothing here returns a signing secret (other than once, on creation and
rotation), a credential, a path or a host name.
"""

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import authz
import integration_framework as framework
import scheduler as scheduler_module
import webhooks
from api import schemas
from api.core import ApiState, get_state, require
from integration_store import StoreVersionError

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

Manage = Depends(require(authz.INTEGRATIONS_MANAGE))

CONFLICT = {409: {"model": schemas.ErrorResponse, "description": "Running already, or not possible in this state"}}

SECRET_NOTE = ("Store this secret in the receiving system now: it is not shown again. Verify every delivery's "
               "LogiDoc-Signature with it, and refuse an event id you have already processed.")


class EndpointBody(BaseModel):

    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2000)
    event_types: list[str] = Field(min_length=1, max_length=20)
    enabled: bool = True


class EndpointChanges(BaseModel):

    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    event_types: list[str] | None = Field(default=None, min_length=1, max_length=20)
    enabled: bool | None = None


class JobChanges(BaseModel):

    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=scheduler_module.MIN_INTERVAL, le=scheduler_module.MAX_INTERVAL)


class IntegrationBody(BaseModel):

    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=50)
    settings: dict[str, Any] = Field(default_factory=dict)
    credential_env: str | None = Field(default=None, max_length=80)
    sync_interval_seconds: int = Field(default=3600, ge=framework.MIN_SYNC_INTERVAL, le=framework.MAX_SYNC_INTERVAL)
    enabled: bool = True


class IntegrationChanges(BaseModel):

    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    settings: dict[str, Any] | None = None
    credential_env: str | None = Field(default=None, max_length=80)
    sync_interval_seconds: int | None = Field(default=None, ge=framework.MIN_SYNC_INTERVAL, le=framework.MAX_SYNC_INTERVAL)


class RecordCorrectionBody(BaseModel):

    field: str = Field(min_length=1, max_length=50)
    value: Any = None
    reason: str = Field(min_length=10, max_length=500)
    clear: bool = False


class SandboxChangesBody(BaseModel):

    count: int = Field(default=3, ge=1, le=20)


def refusal(error):

    if isinstance(error, authz.PermissionDenied):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error).strip("'\""))
    if isinstance(error, (scheduler_module.JobBusy, framework.SyncBusy, webhooks.DeliveryDisabled)):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, StoreVersionError):
        return HTTPException(status_code=503, detail=str(error))

    return HTTPException(status_code=422, detail=str(error))


REFUSED = (authz.PermissionDenied, LookupError, ValueError, scheduler_module.JobBusy, framework.SyncBusy, StoreVersionError)


def runtime(state):

    try:
        return state.integrations
    except StoreVersionError as error:
        raise refusal(error) from None


# ------------------------------------------------------------
# Webhooks
# ------------------------------------------------------------

@router.get("/webhooks/settings", response_model=schemas.WebhookConfiguration, responses=schemas.ERRORS)
def webhook_settings(principal=Manage, state: ApiState = Depends(get_state)):

    settings = runtime(state).webhook_settings

    return {
        "delivery_enabled": settings.delivery_enabled, "problems": settings.problems(),
        "event_types": [{"type": name, "description": text} for name, text in webhooks.EVENT_TYPES.items()],
        "https_only": not settings.policy.allow_http, "private_destinations_allowed": settings.policy.allow_private,
        "timeout_seconds": settings.timeout_seconds, "max_attempts": settings.max_attempts,
        "rotation_grace_hours": settings.rotation_grace_hours,
        "signature": {"signature_header": webhooks.SIGNATURE_HEADER, "timestamp_header": webhooks.TIMESTAMP_HEADER,
                      "event_id_header": webhooks.EVENT_HEADER, "scheme": "v1=hex(HMAC-SHA256(secret, timestamp + '.' + event id + '.' + body))",
                      "tolerance_seconds": webhooks.TOLERANCE_SECONDS},
    }


@router.get("/webhooks", response_model=schemas.WebhookEndpointList, responses=schemas.ERRORS)
def endpoint_list(principal=Manage, state: ApiState = Depends(get_state), include_archived: bool = False):

    return {"endpoints": runtime(state).webhooks.endpoints(include_archived)}


@router.post("/webhooks", status_code=201, response_model=schemas.WebhookSecretIssued,
             responses={**schemas.ERRORS, **CONFLICT})
def endpoint_create(body: EndpointBody, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        endpoint, secret = runtime(state).webhooks.create_endpoint(principal, body.name, body.url, body.event_types, body.enabled)
    except REFUSED as error:
        raise refusal(error) from None

    return {"endpoint": endpoint, "secret": secret, "note": SECRET_NOTE}


@router.get("/webhooks/{endpoint_id}", response_model=schemas.WebhookEndpointDetail, responses=schemas.ERRORS)
def endpoint_detail(endpoint_id: int, principal=Manage, state: ApiState = Depends(get_state)):

    service = runtime(state).webhooks

    try:
        return {**service.endpoint(endpoint_id), "changes": service.changes(endpoint_id)}
    except REFUSED as error:
        raise refusal(error) from None


@router.patch("/webhooks/{endpoint_id}", response_model=schemas.WebhookEndpoint, responses=schemas.ERRORS)
def endpoint_change(endpoint_id: int, body: EndpointChanges, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        return runtime(state).webhooks.update_endpoint(principal, endpoint_id, **body.model_dump(exclude_unset=True))
    except REFUSED as error:
        raise refusal(error) from None


@router.post("/webhooks/{endpoint_id}/rotate-secret", response_model=schemas.WebhookSecretIssued,
             responses={**schemas.ERRORS, **CONFLICT})
def endpoint_rotate(endpoint_id: int, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        endpoint, secret = runtime(state).webhooks.rotate_secret(principal, endpoint_id)
    except REFUSED as error:
        raise refusal(error) from None

    return {"endpoint": endpoint, "secret": secret, "note": SECRET_NOTE}


@router.post("/webhooks/{endpoint_id}/archive", response_model=schemas.WebhookEndpoint, responses=schemas.ERRORS)
def endpoint_archive(endpoint_id: int, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        return runtime(state).webhooks.archive_endpoint(principal, endpoint_id)
    except REFUSED as error:
        raise refusal(error) from None


@router.post("/webhooks/{endpoint_id}/test", response_model=schemas.WebhookDeliveryDetail,
             responses={**schemas.ERRORS, **CONFLICT})
def endpoint_test(endpoint_id: int, principal=Manage, state: ApiState = Depends(get_state)):
    """Sends a webhook.test event to this endpoint now (one attempt, never retried by itself)."""

    try:
        result = runtime(state).webhooks.send_test(principal, endpoint_id)
    except REFUSED as error:
        raise refusal(error) from None

    if result is None:
        raise HTTPException(status_code=409, detail="The test delivery is being sent already.")

    return result


@router.get("/deliveries", response_model=schemas.DeliveryPage, responses=schemas.ERRORS)
def delivery_list(principal=Manage, state: ApiState = Depends(get_state), endpoint_id: int | None = Query(None, ge=1),
                  status: list[Literal["pending", "sending", "retrying", "succeeded", "failed", "cancelled"]] = Query(default=[]),
                  offset: int = Query(0, ge=0, le=1000000), limit: int = Query(50, ge=1, le=200),
                  event_type: list[str] = Query(default=[], max_length=20),
                  start: date | None = Query(None, alias="from"), end: date | None = Query(None, alias="to")):
    """Deliveries, newest first; filters: endpoint, status, event type, and the day created (from, to: YYYY-MM-DD, UTC)."""

    if start and end and start > end:
        raise HTTPException(status_code=422, detail="from is after to.")

    page = runtime(state).webhooks.deliveries(endpoint_id, tuple(status), offset, limit, tuple(event_type),
                                              start.isoformat() if start else None, end.isoformat() if end else None)

    return {**page, "offset": offset, "limit": limit}


# ------------------------------------------------------------
# Monitoring (recorded deliveries and sync runs only)
# ------------------------------------------------------------

@router.get("/monitoring/webhooks", response_model=schemas.WebhookMonitoring, responses=schemas.ERRORS)
def webhook_monitoring(principal=Manage, state: ApiState = Depends(get_state), days: int = Query(14, ge=1, le=90),
                       endpoint_id: int | None = Query(None, ge=1), event_type: str | None = Query(None, max_length=60)):
    """Delivery outcomes, attempts, retries, failing endpoints, error categories and daily latency over the window."""

    return runtime(state).webhooks.monitoring(days, endpoint_id, event_type)


@router.get("/monitoring/connections", response_model=schemas.IntegrationDashboard, responses=schemas.ERRORS)
def integration_monitoring(principal=Manage, state: ApiState = Depends(get_state), days: int = Query(30, ge=1, le=90)):
    """Each integration's health and the sync outcomes, records and error categories per day over the window."""

    return runtime(state).integrations.dashboard(days)


@router.get("/deliveries/{delivery_id}", response_model=schemas.WebhookDeliveryDetail, responses=schemas.ERRORS)
def delivery_detail(delivery_id: int, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        return runtime(state).webhooks.delivery(delivery_id)
    except REFUSED as error:
        raise refusal(error) from None


@router.post("/deliveries/{delivery_id}/retry", response_model=schemas.WebhookDeliveryDetail,
             responses={**schemas.ERRORS, **CONFLICT})
def delivery_retry(delivery_id: int, principal=Manage, state: ApiState = Depends(get_state)):
    """A failed (or retrying) delivery, attempted again now with a new cycle of retries."""

    try:
        return runtime(state).webhooks.retry_delivery(principal, delivery_id)
    except REFUSED as error:
        raise refusal(error) from None


# ------------------------------------------------------------
# Scheduled jobs
# ------------------------------------------------------------

@router.get("/jobs", response_model=schemas.SchedulerStatus, responses=schemas.ERRORS)
def job_status(principal=Manage, state: ApiState = Depends(get_state)):

    return runtime(state).status()


@router.patch("/jobs/{name}", response_model=schemas.ScheduledJob, responses=schemas.ERRORS)
def job_change(name: str, body: JobChanges, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        return runtime(state).scheduler.set_job(principal, name, body.enabled, body.interval_seconds)
    except REFUSED as error:
        raise refusal(error) from None


@router.post("/jobs/{name}/run", status_code=202, response_model=schemas.JobStarted, responses={**schemas.ERRORS, **CONFLICT})
def job_run(name: str, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        run_id = runtime(state).scheduler.trigger(principal, name)
    except REFUSED as error:
        raise refusal(error) from None

    return {"run_id": run_id, "job": name, "trigger": "manual"}


@router.post("/jobs/{name}/retry", status_code=202, response_model=schemas.JobStarted, responses={**schemas.ERRORS, **CONFLICT})
def job_retry(name: str, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        run_id = runtime(state).scheduler.trigger(principal, name, retry=True)
    except REFUSED as error:
        raise refusal(error) from None

    return {"run_id": run_id, "job": name, "trigger": "retry"}


@router.get("/jobs/{name}/runs", response_model=schemas.JobRunPage, responses=schemas.ERRORS)
def job_runs(name: str, principal=Manage, state: ApiState = Depends(get_state),
             status: list[Literal["running", "succeeded", "failed", "interrupted"]] = Query(default=[]),
             offset: int = Query(0, ge=0, le=1000000), limit: int = Query(50, ge=1, le=200)):

    scheduler = runtime(state).scheduler

    if name not in scheduler.specs:
        raise HTTPException(status_code=404, detail="No such job.")

    return {**scheduler.runs(name, tuple(status), offset, limit), "offset": offset, "limit": limit}


@router.get("/job-runs/{run_id}", response_model=schemas.JobRun, responses=schemas.ERRORS)
def job_run_detail(run_id: int, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        return runtime(state).scheduler.run(run_id)
    except REFUSED as error:
        raise refusal(error) from None


# ------------------------------------------------------------
# TMS / ERP integrations
# ------------------------------------------------------------

@router.get("/connections", response_model=schemas.IntegrationList, responses=schemas.ERRORS)
def connection_list(principal=Manage, state: ApiState = Depends(get_state)):

    service = runtime(state).integrations

    return {"integrations": service.integrations(), "providers": service.provider_list()}


@router.post("/connections", status_code=201, response_model=schemas.Integration, responses=schemas.ERRORS)
def connection_create(body: IntegrationBody, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        return runtime(state).integrations.create(principal, body.name, body.provider, body.settings, body.credential_env,
                                                  body.sync_interval_seconds, body.enabled)
    except REFUSED as error:
        raise refusal(error) from None


@router.get("/connections/{integration_id}", response_model=schemas.IntegrationDetail, responses=schemas.ERRORS)
def connection_detail(integration_id: int, principal=Manage, state: ApiState = Depends(get_state)):

    service = runtime(state).integrations

    try:
        return {**service.integration(integration_id), "changes": service.changes(integration_id)}
    except REFUSED as error:
        raise refusal(error) from None


@router.patch("/connections/{integration_id}", response_model=schemas.Integration, responses=schemas.ERRORS)
def connection_change(integration_id: int, body: IntegrationChanges, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        return runtime(state).integrations.update(principal, integration_id, **body.model_dump(exclude_unset=True))
    except REFUSED as error:
        raise refusal(error) from None


@router.post("/connections/{integration_id}/test", response_model=schemas.ConnectionTest, responses=schemas.ERRORS)
def connection_test(integration_id: int, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        result = runtime(state).integrations.test_connection(principal, integration_id)
    except REFUSED as error:
        raise refusal(error) from None

    return {"ok": result.ok, "category": result.category, "detail": result.detail, "latency_ms": result.latency_ms}


@router.post("/connections/{integration_id}/sync", status_code=202, response_model=schemas.SyncStarted,
             responses={**schemas.ERRORS, **CONFLICT})
def connection_sync(integration_id: int, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        return {"run_id": runtime(state).integrations.sync_now(principal, integration_id)}
    except REFUSED as error:
        raise refusal(error) from None


@router.get("/connections/{integration_id}/runs", response_model=schemas.SyncRunPage, responses=schemas.ERRORS)
def connection_runs(integration_id: int, principal=Manage, state: ApiState = Depends(get_state),
                    offset: int = Query(0, ge=0, le=1000000), limit: int = Query(50, ge=1, le=200)):

    service = runtime(state).integrations

    try:
        service.integration(integration_id)
    except REFUSED as error:
        raise refusal(error) from None

    return {**service.runs(integration_id, offset, limit), "offset": offset, "limit": limit}


@router.get("/sync-runs/{run_id}", response_model=schemas.SyncRunDetail, responses=schemas.ERRORS)
def sync_run_detail(run_id: int, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        return runtime(state).integrations.run(run_id)
    except REFUSED as error:
        raise refusal(error) from None


@router.get("/connections/{integration_id}/records", response_model=schemas.RecordPage, responses=schemas.ERRORS)
def record_list(integration_id: int, principal=Manage, state: ApiState = Depends(get_state),
                entity: Literal["shipments", "carriers"] | None = None, search: str = Query("", max_length=100),
                offset: int = Query(0, ge=0, le=1000000), limit: int = Query(50, ge=1, le=200)):

    try:
        page = runtime(state).integrations.records(integration_id, entity, search.strip(), offset, limit)
    except REFUSED as error:
        raise refusal(error) from None

    return {**page, "offset": offset, "limit": limit}


@router.get("/connections/{integration_id}/records/{record_id}", response_model=schemas.ImportedRecordDetail,
            responses=schemas.ERRORS)
def record_detail(integration_id: int, record_id: int, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        return runtime(state).integrations.record(integration_id, record_id)
    except REFUSED as error:
        raise refusal(error) from None


@router.post("/connections/{integration_id}/records/{record_id}/corrections", response_model=schemas.ImportedRecordDetail,
             responses=schemas.ERRORS)
def record_correct(integration_id: int, record_id: int, body: RecordCorrectionBody, principal=Manage,
                   state: ApiState = Depends(get_state)):
    """Corrects one imported field by hand (with a reason). Later imports never overwrite it; clear removes it."""

    try:
        return runtime(state).integrations.correct(principal, integration_id, record_id, body.field, body.value, body.reason,
                                                   body.clear)
    except REFUSED as error:
        raise refusal(error) from None


@router.post("/connections/{integration_id}/sandbox/changes", response_model=schemas.SandboxChanges, responses=schemas.ERRORS)
def sandbox_changes(integration_id: int, body: SandboxChangesBody, principal=Manage, state: ApiState = Depends(get_state)):
    """Sandbox only: the simulated TMS moves some shipments forward, so the next sync finds changed records."""

    try:
        return runtime(state).integrations.simulate_changes(principal, integration_id, body.count)
    except REFUSED as error:
        raise refusal(error) from None
