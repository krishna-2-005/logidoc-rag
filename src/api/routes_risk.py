"""
HTTP API: shipment risk analytics (docs/SHIPMENT_RISK_ANALYTICS.md).

Every score is computed here from what the audit already holds (the current
audit, its exceptions with their review state, the document completeness
register; for trends, the recorded audit history) under the versioned risk model
(risk_analytics). Nothing is stored and nothing a client sends changes a score.
Reviewers and administrators read it (exceptions.view). Served under /api and
/api/v1.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

import authz
import carrier_intelligence as carriers_module
import risk_analytics as risk
from api import schemas
from api.core import ApiState, get_state, require
from api.routes_audit import carrier_inputs
from api.routes_carriers import resolver_of
from shipment_identity import identifier_key

router = APIRouter(prefix="/api/risk")

View = Depends(require(authz.EXCEPTIONS_VIEW))
NOT_FOUND = {404: {"model": schemas.ErrorResponse, "description": "No such shipment"}}
UNUSABLE = {503: {"model": schemas.ErrorResponse, "description": "The configured risk model cannot be used"}}


def model_or_503():

    try:
        return risk.load_model()
    except risk.ModelError as error:
        raise HTTPException(status_code=503, detail=f"The risk model cannot be used: {error}") from None


def scored(state):
    """(scored rows of every shipment, the exceptions with their review state, the model)."""

    model = model_or_503()
    shipments, exceptions, completeness_rows = carrier_inputs(state)
    facts = carriers_module.shipment_rows(shipments, exceptions, completeness_rows, resolver_of(state))

    return risk.assess(shipments, exceptions, completeness_rows, facts, model), exceptions, model


def period(start, end):

    if start and end and start > end:
        raise HTTPException(status_code=422, detail="The start date is after the end date.")

    return start, end


def selected(rows, search, category, carrier, start, end, min_score=None, max_score=None, partial=None):

    start, end = period(start, end)

    if min_score is not None and max_score is not None and min_score > max_score:
        raise HTTPException(status_code=422, detail="min_score is above max_score.")

    return [row for row in rows if risk.matches(row, search=search.strip(), categories=tuple(category), carrier=carrier,
                                                 min_score=min_score, max_score=max_score, partial=partial,
                                                 start=start, end=end)]


Search = Query("", max_length=100)
Categories = Query(default=[])
Carrier = Query(None, max_length=200)


@router.get("/model", response_model=schemas.RiskModelView, responses={**schemas.ERRORS, **UNUSABLE})
def risk_model(principal=View):
    """The risk model in force: its version, digest, thresholds and every signal's weights."""

    return risk.model_view(model_or_503())


@router.get("/overview", response_model=schemas.RiskOverview, responses={**schemas.ERRORS, **UNUSABLE})
def risk_overview(principal=View, state: ApiState = Depends(get_state), search: str = Search,
                  category: list[risk.CategoryName] = Categories, carrier: str | None = Carrier,
                  start: date | None = None, end: date | None = None):
    """The dashboard's figures over the selected shipments (the same filters as the table)."""

    rows, exceptions, model = scored(state)

    return risk.overview(selected(rows, search, category, carrier, start, end), exceptions, model)


@router.get("/shipments", response_model=schemas.RiskShipmentPage, responses={**schemas.ERRORS, **UNUSABLE})
def risk_shipments(principal=View, state: ApiState = Depends(get_state), search: str = Search,
                   category: list[risk.CategoryName] = Categories, carrier: str | None = Carrier,
                   start: date | None = None, end: date | None = None,
                   min_score: int | None = Query(None, ge=0, le=100), max_score: int | None = Query(None, ge=0, le=100),
                   partial: bool | None = None, sort: risk.SortName = "score", order: risk.OrderName = "desc",
                   offset: int = Query(0, ge=0, le=1_000_000), limit: int = Query(50, ge=1, le=200)):
    """A page of scored shipments, highest score first by default."""

    rows, _, model = scored(state)
    found = selected(rows, search, category, carrier, start, end, min_score, max_score, partial)
    found.sort(key=risk.SORTS[sort], reverse=order == "desc")

    return {"rows": [{**{key: row[key] for key in ("shipment_id", "shipment_key", "carrier_key", "carrier", "date", "final_status",
                                                    "freight_outcome", "open_exceptions", "score", "category", "partial")},
                      "top_factors": risk.top_factors(row)} for row in found[offset:offset + limit]],
            "total": len(found), "offset": offset, "limit": limit,
            "model": {"version": model["version"], "digest": risk.digest_of(model)}}


@router.get("/shipments/{shipment_id}", response_model=schemas.RiskShipment, response_model_exclude_unset=True,
            responses={**schemas.ERRORS, **NOT_FOUND, **UNUSABLE})
def risk_shipment(shipment_id: str, principal=View, state: ApiState = Depends(get_state)):
    """One shipment's score, every factor with its evidence, and what could not be assessed."""

    rows, _, model = scored(state)
    wanted = identifier_key(shipment_id)
    row = next((item for item in rows if item["shipment_key"] == wanted), None)

    if row is None:
        raise HTTPException(status_code=404, detail="No such shipment.")

    return {**row, "thresholds": dict(model["thresholds"])}


@router.get("/trends", response_model=schemas.RiskTrends, responses={**schemas.ERRORS, **UNUSABLE})
def risk_trends(principal=View, state: ApiState = Depends(get_state), period_name: risk.PeriodName = Query("month", alias="period")):
    """The risk of what recorded audit runs found, per month or ISO week (empty without recorded history)."""

    model = model_or_503()

    if not state.locations.history_db.is_file():
        return risk.trends([], {}, model, period_name)

    history = state.service.history_store()

    return risk.trends(history.observations(), history.freight_facts(), model, period_name)
