"""
HTTP API: the exception workflow, the audit history and the trends.

The routes validate the request and hand it to the same services the
Streamlit app uses: ExceptionWorkflow (roles, required notes and evidence,
transitions, approval; every change records the signed-in account),
audit_history.record_run_as (administrators only) and trend_analytics.
Resolving or dismissing an exception never touches the audit result.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

import audit_history
import authz
import trend_analytics as trends
from api.core import ApiState, get_principal, get_state, require
from api.routes_audit import attachment
from exception_workflow import ExceptionWorkflow
from review_store import STATUSES, ReviewError


router = APIRouter(prefix="/api")

ViewAudit = Depends(require(authz.EXCEPTIONS_VIEW))

ACTIONS = ("assign", "start", "note", "resolve", "dismiss", "reopen", "approve", "reject")


class ActionBody(BaseModel):

    action: Literal["assign", "start", "note", "resolve", "dismiss", "reopen", "approve", "reject"]
    note: str | None = Field(default=None, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    assignee: str | None = Field(default=None, max_length=64)

    @field_validator("evidence")
    @classmethod
    def evidence_items(cls, items):

        if any(len(item) > 300 for item in items):
            raise ValueError("An evidence reference is too long (300 characters at most).")

        return items


class RecordBody(BaseModel):

    label: str | None = Field(default=None, max_length=100)


def workflow_of(state):

    assignable = None

    if state.mode == "required":
        assignable = lambda: [user["username"] for user in state.users().users() if user["active"]]  # noqa: E731

    return ExceptionWorkflow(state.service.review_store(), assignable)


def find(state, key):
    """
    The detected exception with a key, with its review state.
    """

    found = next((item for item in state.service.exceptions() if item["exception_key"] == key), None)

    if found is None:
        raise HTTPException(status_code=404, detail="The audit does not detect this exception (any more).")

    return found


# ------------------------------------------------------------
# Exceptions
# ------------------------------------------------------------

@router.get("/exceptions")
def exceptions(
    principal=Depends(get_principal), state: ApiState = Depends(get_state),
    search: str = Query("", max_length=100), assignee: str | None = Query(None, max_length=64),
    unassigned: bool = False, status: list[str] = Query(default=[]), category: list[str] = Query(default=[]),
    min_age_days: int | None = Query(None, ge=0, le=100000), max_age_days: int | None = Query(None, ge=0, le=100000),
    pending_approval: bool = False, include_absent: bool = False,
):
    """
    The exception queue with the filters of the screen. Reading it records
    newly detected exceptions as open, like the dashboard.
    """

    if not state.service.has_documents():
        return {"exceptions": [], "assignees": [], "categories": [], "statuses": list(STATUSES), "total": 0}

    try:
        authz.require(principal, authz.EXCEPTIONS_VIEW)
    except authz.PermissionDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from None

    unknown = set(status) - set(STATUSES)

    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown state: {sorted(unknown)}.")

    detected = {item["exception_key"]: item for item in state.service.exceptions()}
    flow = workflow_of(state)

    rows = flow.queue(
        principal, search=search, assignee=assignee, unassigned=unassigned, statuses=tuple(status),
        categories=tuple(category), min_age_days=min_age_days, max_age_days=max_age_days,
        pending_approval=pending_approval, include_absent=include_absent,
    )

    store = state.service.review_store()

    return {
        "exceptions": [
            {
                **row,
                "detected": row["exception_key"] in detected,
                "description": detected.get(row["exception_key"], {}).get("description"),
                "severity": detected.get(row["exception_key"], {}).get("severity"),
                "kind": detected.get(row["exception_key"], {}).get("kind"),
                "allowed_actions": flow.allowed_actions(principal, row) if row["exception_key"] in detected else [],
            }
            for row in rows
        ],
        "total": len(rows),
        "assignees": store.assignees(),
        "categories": sorted({row["category"] for row in store.states().values()}),
        "statuses": list(STATUSES),
    }


@router.get("/exceptions/activity")
def activity(principal=ViewAudit, state: ApiState = Depends(get_state)):

    return workflow_of(state).activity(principal, 200)


@router.get("/exceptions/{key:path}/history")
def exception_history(key: str, principal=ViewAudit, state: ApiState = Depends(get_state)):

    return workflow_of(state).history(principal, key)


@router.get("/exceptions/{key:path}")
def exception_detail(key: str, principal=ViewAudit, state: ApiState = Depends(get_state)):

    item = find(state, key)
    flow = workflow_of(state)
    row = state.service.review_store().states()[key]

    return {
        **item, "review": row, "allowed_actions": flow.allowed_actions(principal, row),
        "history": flow.history(principal, key),
    }


@router.post("/exceptions/{key:path}/actions")
def exception_action(
    key: str, body: ActionBody, principal=Depends(get_principal), state: ApiState = Depends(get_state),
):
    """
    One workflow step on a detected exception. 403 when the role does not
    allow it, 422 when a rule is broken (a missing note, no evidence, an
    impossible transition, a stale audit). Nothing is written on a refusal.
    """

    item = find(state, key)
    flow = workflow_of(state)

    try:

        if body.action == "assign":
            row = flow.assign(principal, item, body.assignee, body.note)
        elif body.action == "start":
            row = flow.start(principal, item, body.assignee, body.note)
        elif body.action == "note":
            row = flow.add_note(principal, item, body.note, body.evidence)
        elif body.action == "resolve":
            row = flow.resolve(principal, item, body.note, body.evidence)
        elif body.action == "dismiss":
            row = flow.dismiss(principal, item, body.note, body.evidence)
        elif body.action == "reopen":
            row = flow.reopen(principal, item, body.note)
        elif body.action == "approve":
            row = flow.approve(principal, item, body.note)
        else:
            row = flow.reject(principal, item, body.note)

    except authz.PermissionDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from None
    except ReviewError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    return {"review": row, "allowed_actions": flow.allowed_actions(principal, row)}


# ------------------------------------------------------------
# Audit history
# ------------------------------------------------------------

@router.get("/history/runs")
def runs(principal=Depends(require(authz.HISTORY_VIEW)), state: ApiState = Depends(get_state)):

    if not state.locations.history_db.is_file():
        return []

    return state.service.history_store().runs(100)


@router.post("/history/runs", status_code=201)
def record_run(
    body: RecordBody, principal=Depends(require(authz.HISTORY_RECORD)), state: ApiState = Depends(get_state),
):
    """
    Records an audit run of every shipment (administrators). A shipment
    whose result and documents did not change gets no new snapshot.
    """

    from audit_analytics import build_exceptions

    snapshot = state.service.snapshot()

    # The permission was checked before anything was opened; record_run_as
    # checks it again, so the service stays safe when called elsewhere
    return audit_history.record_run_as(
        principal, state.service.history_store(), snapshot, build_exceptions(snapshot),
        label=body.label or "All shipments",
    )


@router.get("/history/shipments/{shipment_id}")
def shipment_history(shipment_id: str, principal=Depends(require(authz.HISTORY_VIEW)), state: ApiState = Depends(get_state)):
    """
    A shipment's recorded snapshots, newest first, each with what changed
    since the one before.
    """

    shipment = state.service.shipment(shipment_id)

    if shipment is None or not state.locations.history_db.is_file():
        return []

    return state.service.history_store().timeline(shipment["shipment_key"])


@router.get("/history/compare")
def compare(
    shipment: str = Query(max_length=100), first: int = Query(ge=1), second: int = Query(ge=1),
    principal=Depends(require(authz.HISTORY_VIEW)), state: ApiState = Depends(get_state),
):

    found = state.service.shipment(shipment)

    if found is None or not state.locations.history_db.is_file():
        raise HTTPException(status_code=404, detail="No recorded history for this shipment.")

    result = trends.compare_snapshots(
        state.service.history_store().snapshots(found["shipment_key"]), first, second
    )

    if result is None:
        raise HTTPException(status_code=404, detail="One of the snapshots is not a snapshot of this shipment.")

    return result


# ------------------------------------------------------------
# Trends
# ------------------------------------------------------------

def parse_day(text, name):

    try:
        return trends.day(text)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be a date like 2026-03-31.") from None


def trend_result(state, period, carrier, status, category, start, end, min_sample):

    if period not in trends.PERIODS:
        raise HTTPException(status_code=422, detail=f"period must be one of {list(trends.PERIODS)}.")

    history = state.service.history_store() if state.locations.history_db.is_file() else None
    observations = history.observations() if history else []

    return observations, trends.analyze(
        observations, period=period, carriers=tuple(carrier), statuses=tuple(status), categories=tuple(category),
        start=parse_day(start, "from"), end=parse_day(end, "to"), min_sample=min_sample,
    )


@router.get("/trends")
def trend_data(
    principal=Depends(require(authz.HISTORY_VIEW)), state: ApiState = Depends(get_state),
    period: str = "month", carrier: list[str] = Query(default=[]), status: list[str] = Query(default=[]),
    category: list[str] = Query(default=[]), start: str = Query("", alias="from", max_length=10),
    end: str = Query("", alias="to", max_length=10), min_sample: int = Query(trends.MIN_SAMPLE, ge=1, le=1000),
):
    """
    Trends over the recorded history only (never the current documents),
    with sample sizes, intervals and the metric definitions.
    """

    observations, result = trend_result(state, period, carrier, status, category, start, end, min_sample)

    options = trends.filter_options(observations)

    return {
        **result,
        "definitions": trends.METRICS,
        "options": {
            **options,
            "first": options["first"].isoformat() if options["first"] else None,
            "last": options["last"].isoformat() if options["last"] else None,
        },
    }


@router.get("/trends/export.csv")
def trend_export(
    principal=Depends(require(authz.REPORTS_DOWNLOAD)), state: ApiState = Depends(get_state),
    period: str = "month", carrier: list[str] = Query(default=[]), status: list[str] = Query(default=[]),
    category: list[str] = Query(default=[]), start: str = Query("", alias="from", max_length=10),
    end: str = Query("", alias="to", max_length=10), min_sample: int = Query(trends.MIN_SAMPLE, ge=1, le=1000),
):

    _, result = trend_result(state, period, carrier, status, category, start, end, min_sample)

    return attachment(trends.analytics_csv(result), "audit_trends.csv", "text/csv; charset=utf-8")
