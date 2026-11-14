"""
HTTP API: the controlled re-audit of a shipment, and its comparison with the TMS
shipment record that names it (docs/CORRECTION_AWARE_AUDIT.md, "Controlled
re-audit"; docs/TMS_SHIPMENT_AUDIT.md).

A re-audit is previewed first (GET): what recording it would change against the
shipment's latest recorded run, with a token naming that exact state. Recording
it (POST) needs the token: when the audit changed since the preview, nothing is
recorded (409). A TMS comparison is read by reviewers; deciding an open one needs
an administrator, a reason, and the values the decision was made on (409 when
they changed since they were shown). The service layer enforces every rule; these
routes only map its errors to HTTP.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import authz
import tms_audit
from api import schemas
from api.core import ApiState, get_state, require
from audit_service import ReAuditInvalid, ReAuditRefused

router = APIRouter(prefix="/api")

ViewAudit = Depends(require(authz.EXCEPTIONS_VIEW))
ViewHistory = Depends(require(authz.HISTORY_VIEW))
Record = Depends(require(authz.HISTORY_RECORD))
Decide = Depends(require(authz.EXTRACTION_APPROVE))

CONFLICT = {409: {"model": schemas.ErrorResponse, "description": "The state changed since it was shown, or nothing to do"}}
NOT_FOUND = {404: {"model": schemas.ErrorResponse, "description": "No such shipment"}}


class ReAuditBody(BaseModel):

    token: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=500)


class TmsResolutionBody(BaseModel):

    field: str = Field(min_length=1, max_length=40)
    choice: str = Field(pattern="^(document|tms)$")
    reason: str = Field(min_length=1, max_length=1000)
    record_id: int = Field(ge=1, le=2**31 - 1)
    # The values the decision was made on, exactly as the comparison showed them (null: none)
    tms_value: str | int | float | None = None
    document_value: str | int | float | None = None


def denied(error):

    return HTTPException(status_code=403, detail=str(error))


# ------------------------------------------------------------
# Controlled re-audit
# ------------------------------------------------------------

@router.get("/shipments/{shipment_id}/re-audit", response_model=schemas.ReAuditPreview, responses={**schemas.ERRORS, **NOT_FOUND})
def re_audit_preview(shipment_id: str, principal=ViewHistory, state: ApiState = Depends(get_state)):
    """What a re-audit of this shipment would record, against its latest recorded run."""

    try:
        return state.service.re_audit_preview(shipment_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such shipment.") from None


@router.post("/shipments/{shipment_id}/re-audit", status_code=201, response_model=schemas.ReAuditRecorded,
             responses={**schemas.ERRORS, **NOT_FOUND, **CONFLICT})
def re_audit(shipment_id: str, body: ReAuditBody, principal=Record, state: ApiState = Depends(get_state)):
    """
    Records the previewed re-audit of this one shipment (its token), with the
    reason. 409 when nothing changed or the audit changed since the preview.
    """

    try:
        return state.service.re_audit(principal, shipment_id, body.token, body.reason)
    except authz.PermissionDenied as error:
        raise denied(error) from None
    except LookupError:
        raise HTTPException(status_code=404, detail="No such shipment.") from None
    except ReAuditRefused as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ReAuditInvalid as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.get("/re-audits", response_model=schemas.ReAuditQueue, responses=schemas.ERRORS)
def re_audits(principal=ViewHistory, state: ApiState = Depends(get_state)):
    """The shipments waiting for a controlled re-audit (a line-item correction or a chosen TMS value not recorded yet)."""

    return {"re_audits": state.service.re_audits()}


# ------------------------------------------------------------
# TMS comparison
# ------------------------------------------------------------

def shipment_or_404(state, shipment_id):

    shipment = state.service.shipment(shipment_id)

    if shipment is None:
        raise HTTPException(status_code=404, detail="No such shipment.")

    return shipment


def unavailable_of(state):

    return state.service.snapshot().get("tms_unavailable") or []


@router.get("/shipments/{shipment_id}/tms", response_model=schemas.TmsShipmentView, responses={**schemas.ERRORS, **NOT_FOUND})
def shipment_tms(shipment_id: str, principal=ViewAudit, state: ApiState = Depends(get_state)):
    """The TMS record that names this shipment, how each field compares, and every decision made on them."""

    shipment = shipment_or_404(state, shipment_id)
    view = shipment.get("tms") or {}

    return {
        "shipment_id": shipment["shipment_id"], "shipment_key": shipment["shipment_key"],
        "configured": state.locations.integration_path().is_file(),
        "match": view.get("match"), "matches": view.get("matches") or [], "ambiguous": bool(view.get("ambiguous")),
        "comparisons": view.get("comparisons") or [], "open_conflicts": view.get("open_conflicts") or 0,
        "applied_resolution_ids": view.get("applied_resolution_ids") or [],
        "history": tms_audit.resolutions_of(state.service.tms_source, shipment["shipment_key"]),
        "unavailable": unavailable_of(state),
    }


@router.post("/shipments/{shipment_id}/tms/resolutions", status_code=201, response_model=schemas.TmsResolutionResult,
             responses={**schemas.ERRORS, **NOT_FOUND, **CONFLICT})
def resolve_tms(shipment_id: str, body: TmsResolutionBody, principal=Decide, state: ApiState = Depends(get_state)):
    """
    Decides one open comparison: keep the documents' value, or (for a field a
    calculation reads) use the TMS value. A reason is required. 409 when the
    values changed since they were shown, an approved correction protects the
    field, or the TMS value was corrected by hand in LogiDoc.
    """

    shipment = shipment_or_404(state, shipment_id)
    source = state.service.tms_source

    if source is None:
        raise HTTPException(status_code=409, detail="No TMS integration is configured.")

    try:
        stored = source.resolve(principal, shipment, body.field, body.choice, body.reason, body.record_id,
                                body.tms_value, body.document_value)
    except authz.PermissionDenied as error:
        raise denied(error) from None
    except tms_audit.TmsRefused as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except tms_audit.TmsInvalid as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    state.service.invalidate()
    consumed = body.field in tms_audit.CONSUMED

    return {"resolution": {key: value for key, value in stored.items() if key != "shipment_id"},
            "shipment_id": shipment["shipment_id"],
            "re_audit": "required" if consumed and body.choice == "tms" else "not_needed"}


@router.get("/tms/conflicts", response_model=schemas.TmsConflictQueue, responses=schemas.ERRORS)
def tms_conflicts(principal=ViewAudit, state: ApiState = Depends(get_state)):
    """Every open TMS comparison of every shipment."""

    snapshot = state.service.snapshot()

    return {"conflicts": tms_audit.conflicts(snapshot), "unavailable": snapshot.get("tms_unavailable") or []}
