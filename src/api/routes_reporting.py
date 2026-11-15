"""
HTTP API: freight audit and carrier reporting (docs/REPORTING.md).

A report is previewed as JSON (a page of its rows, with every total computed over
all of them) and downloaded as CSV or PDF, all from the same build
(reporting.build): the preview and the files never disagree. Every route needs
reports.download; the corrections report also needs history.view (it names who
proposed, decided and recorded). A filter a report cannot take is refused (422).
Served under /api and /api/v1.
"""

from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

import authz
import carrier_intelligence as carriers_module
import reporting
import risk_analytics as risk
import tms_audit
from api import schemas
from api.core import ApiState, get_state, require
from api.routes_audit import attachment, carrier_inputs
from api.routes_carriers import resolver_of
from api.routes_risk import model_or_503
from extraction_store import StoreUnavailable

router = APIRouter(prefix="/api/reporting")

Download = Depends(require(authz.REPORTS_DOWNLOAD))

ReportName = Literal["freight-audit-summary", "discrepancies", "carrier-performance", "corrections"]
OutcomeName = Literal["MATCHED", "MATCHED_WITH_TOLERANCE", "DISCREPANCY", "INSUFFICIENT_EVIDENCE", "DUPLICATE", "PENDING_REVIEW",
                      "NOT_RUN"]
StatusName = Literal["open", "in_review", "disputed", "resolved", "dismissed", "waived"]

FILE_ERRORS = {**schemas.ERRORS, 503: {"model": schemas.ErrorResponse, "description": "The configured risk model cannot be used"}}


def filters_of(start: date | None = None, end: date | None = None, carrier: str | None = Query(None, max_length=200),
               shipment: str | None = Query(None, max_length=100), outcome: list[OutcomeName] = Query(default=[]),
               exception_status: list[StatusName] = Query(default=[]),
               risk_category: list[risk.CategoryName] = Query(default=[])):
    """The report filters from the query (each list may repeat)."""

    return reporting.Filters(start, end, (carrier or "").strip() or None, " ".join((shipment or "").split()) or None,
                             tuple(outcome), tuple(exception_status), tuple(risk_category))


Filtered = Depends(filters_of)


def allowed(principal, report):

    if report == "corrections" and not principal.can(authz.HISTORY_VIEW):
        raise HTTPException(status_code=403, detail="The corrections report needs the history.view permission.")


def inputs_of(state, report):
    """What the reports read, once per request."""

    model = model_or_503()
    shipments, exceptions, completeness_rows = carrier_inputs(state)
    facts = carriers_module.shipment_rows(shipments, exceptions, completeness_rows, resolver_of(state))
    inputs = reporting.Inputs(shipments, exceptions, completeness_rows, facts,
                              risk.assess(shipments, exceptions, completeness_rows, facts, model), model,
                              datetime.now(timezone.utc))

    if report == "corrections":
        try:
            store = state.service.extraction_store()
            inputs.corrections = store.all_corrections() if store is not None else []
        except StoreUnavailable:
            inputs.corrections = None

        inputs.re_audits = state.service.history_store().re_audits() if state.locations.history_db.is_file() else None
        source = state.service.tms_source

        if source is not None and state.locations.integration_path().is_file():
            inputs.tms_resolutions = [item for items in source.load().resolutions.values() for item in items]

    return inputs


def carriers_of(inputs):

    return {row["carrier_key"]: row["carrier"] for row in inputs.facts if row["carrier_key"]}


def built(state, principal, report, filters):

    allowed(principal, report)
    inputs = inputs_of(state, report)

    try:
        return reporting.build(report, inputs, filters, carriers_of(inputs)), inputs
    except reporting.ReportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.get("/catalog", response_model=schemas.ReportCatalog, responses=FILE_ERRORS)
def report_catalog(principal=Download, state: ApiState = Depends(get_state)):
    """The reports, the filters each takes, and the values the filters offer."""

    inputs = inputs_of(state, "freight-audit-summary")
    carriers = sorted(carriers_of(inputs).items(), key=lambda item: str(item[1]).casefold())

    return {
        "reports": reporting.catalog(),
        "options": {
            "carriers": [{"key": key, "name": name} for key, name in carriers if key != carriers_module.CONFLICTING],
            "outcomes": list(reporting.OUTCOMES), "statuses": list(reporting.REVIEW_STATUSES), "categories": list(risk.CATEGORIES),
        },
        "can_view_history": principal.can(authz.HISTORY_VIEW),
    }


@router.get("/{report}.csv", responses=FILE_ERRORS)
def report_csv(report: ReportName, filters=Filtered, principal=Download, state: ApiState = Depends(get_state)):
    """The whole report as CSV (header block, table, totals; every cell protected against formula injection)."""

    result, inputs = built(state, principal, report, filters)

    return attachment(reporting.to_csv(result), reporting.filename(report, inputs.generated_at, "csv"), "text/csv; charset=utf-8")


@router.get("/{report}.pdf", responses=FILE_ERRORS)
def report_pdf(report: ReportName, filters=Filtered, principal=Download, state: ApiState = Depends(get_state)):
    """The whole report as PDF (plain text, standard fonts, nothing embedded or linked)."""

    result, inputs = built(state, principal, report, filters)

    return attachment(reporting.to_pdf(result), reporting.filename(report, inputs.generated_at, "pdf"), "application/pdf")


@router.get("/{report}", response_model=schemas.ReportPreview, responses=FILE_ERRORS)
def report_preview(report: ReportName, filters=Filtered, principal=Download, state: ApiState = Depends(get_state),
                   offset: int = Query(0, ge=0, le=1_000_000), limit: int = Query(50, ge=1, le=500)):
    """The report with a page of its rows; the totals cover every row."""

    result, _ = built(state, principal, report, filters)
    rows = result.pop("rows")
    result.pop("pdf_columns")

    return {**result, "rows": rows[offset:offset + limit], "total_rows": len(rows), "offset": offset, "limit": limit}
