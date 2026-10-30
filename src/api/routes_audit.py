"""
HTTP API: the audit data (overview, shipments, documents, financial
exposure) and the report downloads.

Every figure comes from the existing audit modules through
audit_service; this file only selects, filters and shapes it. Nothing is
recalculated, and nothing a client sends can change an audit result.
"""

import hashlib
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response

import audit_analytics as analytics
import audit_explanations as explanations
import authz
import financial_exposure as exposure
import processing_trace
from api.core import ApiState, get_state, require
from audit_export import ExportError, audit_csv, export_filename
from audit_pdf_export import audit_pdf, pdf_filename
from document_completeness import STATUS_TEXT, filter_shipments, register_csv as completeness_csv
from ingestion_register import IngestionRegister
from shipment_identity import identifier_key


router = APIRouter(prefix="/api")

ViewAudit = Depends(require(authz.EXCEPTIONS_VIEW))
Download = Depends(require(authz.REPORTS_DOWNLOAD))


def attachment(content, filename, media_type):

    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


def review_counts(exceptions):

    counts = Counter(item["review_status"] for item in exceptions)

    return {status: counts.get(status, 0) for status in ("open", "in_review", "resolved", "dismissed")}


# ------------------------------------------------------------
# Overview
# ------------------------------------------------------------

@router.get("/overview")
def overview(principal=ViewAudit, state: ApiState = Depends(get_state)):
    """
    KPIs, status and category distributions, financial exposure, document
    readiness and the review queue: the dashboard.
    """

    service = state.service

    if not service.has_documents():
        return {"empty": True, "message": "No PDFs in the document folder yet. Upload documents in Ingestion."}

    snapshot = service.snapshot()
    rows = analytics.register_rows(snapshot)
    found = service.exceptions()

    exposure_rows = exposure.exposure_rows(snapshot)
    completeness = service.completeness()

    return {
        "empty": False,
        "kpis": analytics.kpis(rows),
        "metric_definitions": analytics.METRICS,
        "status_counts": dict(Counter(row["final_status"] for row in rows)),
        "category_counts": dict(Counter(
            category for row in rows for category in row["discrepancy_categories"]
        )),
        "carriers": [
            {"carrier": name, **figures}
            for name, figures in _carrier_figures(rows).items()
        ],
        "exposure": exposure.summary(exposure_rows),
        "exposure_note": exposure.NOTE,
        "completeness": {
            "ready": sum(item["readiness"] == "ready" for item in completeness["shipments"]),
            "incomplete": sum(item["readiness"] == "incomplete" for item in completeness["shipments"]),
            "unusable_files": len(completeness["unusable"]),
            "unidentified_files": len(completeness["unidentified"]),
        },
        "reviews": review_counts(found),
        "open_ingestion_errors": len(service.offered_problems()),
        "unreadable_files": snapshot["unreadable_files"],
        "unidentified_sources": snapshot["unidentified_sources"],
    }


def _carrier_figures(rows):

    groups = {}

    for row in rows:
        groups.setdefault(row["carrier"] or "Not stated", []).append(row)

    return {
        name: {
            "shipments": len(members),
            "with_discrepancies": sum(item["final_status"] == analytics.DISCREPANCIES for item in members),
        }
        for name, members in sorted(groups.items())
    }


# ------------------------------------------------------------
# Shipments
# ------------------------------------------------------------

@router.get("/shipments")
def shipments(
    principal=ViewAudit, state: ApiState = Depends(get_state),
    search: str = Query("", max_length=100),
    carrier: list[str] = Query(default=[]), status: list[str] = Query(default=[]),
    category: list[str] = Query(default=[]),
    invoice_mismatch: bool = False, delivery_delayed: bool = False, needs_review: bool = False,
):

    service = state.service

    if not service.has_documents():
        return {"shipments": [], "total": 0, "carriers": [], "statuses": [], "categories": []}

    rows = service.rows()

    found = analytics.filter_rows(
        rows, search=search, carriers=tuple(carrier), final_statuses=tuple(status),
        categories=tuple(category), invoice_mismatch=invoice_mismatch,
        delivery_delayed=delivery_delayed, needs_review=needs_review,
    )

    return {
        "shipments": found,
        "total": len(rows),
        "carriers": sorted({row["carrier"] or "NOT AVAILABLE" for row in rows}),
        "statuses": sorted({row["final_status"] for row in rows}),
        "categories": sorted({name for row in rows for name in row["discrepancy_categories"]}),
    }


@router.get("/shipments/{shipment_id}")
def shipment_detail(shipment_id: str, principal=ViewAudit, state: ApiState = Depends(get_state)):
    """
    One shipment: the reconciliation result (quantity, delivery, invoice,
    carrier, route, discrepancies, review items with their evidence), its
    exceptions with review state, the deterministic explanations and its
    documents. Language-model commentary is never part of this response.
    """

    service = state.service
    shipment = service.shipment(shipment_id)

    if shipment is None:
        raise HTTPException(status_code=404, detail="No such shipment.")

    key = shipment["shipment_key"]
    found = [item for item in service.exceptions() if item["shipment_key"] == key]

    return {
        "shipment_id": shipment["shipment_id"],
        "shipment_key": key,
        "result": shipment["result"],
        "exceptions": found,
        "explanations": explanations.explain_all(service.snapshot(), [
            item for item in analytics.build_exceptions(service.snapshot()) if item["shipment_key"] == key
        ]),
        "documents": [item for item in service.scan() if item["shipment_key"] == key],
        "history": history_of(state, key),
        # The invoice matched against the rate confirmation and BOL/POD, charge by charge
        "freight_audit": shipment.get("freight_audit"),
    }


def history_of(state, key):
    """
    The recorded snapshots of a shipment, or [] when nothing was ever
    recorded (the history file is not created by reading it).
    """

    if not state.locations.history_db.is_file():
        return []

    return [
        {"snapshot": entry["snapshot"], "changes": entry["changes"]}
        for entry in state.service.history_store().timeline(key)
    ]


# ------------------------------------------------------------
# Documents
# ------------------------------------------------------------

@router.get("/documents")
def documents(principal=ViewAudit, state: ApiState = Depends(get_state)):
    """
    Every stored PDF with what extraction found (status, type, shipment,
    pages), its ingestion record, and the completeness register.
    """

    service = state.service
    files = service.scan()

    records = {}

    if state.locations.register_db.is_file():

        for record in IngestionRegister(state.locations.register_db).files(limit=1000):
            records.setdefault(record["filename"], record)

    register = service.completeness()

    references = evidence_references(service.snapshot())

    return {
        "files": [
            {
                **item,
                "status_text": STATUS_TEXT[item["status"]],
                "sha256": item["sha256"][:12],
                "references": references.get(item["filename"], []),
                "ingestion": (
                    {key: records[item["filename"]][key] for key in ("status", "code", "message", "attempts", "updated_at")}
                    if item["filename"] in records else None
                ),
            }
            for item in files
        ],
        "completeness": register,
    }


def evidence_references(snapshot):
    """
    Where each document is cited: {file name: [{shipment_id, audit, page,
    document_type, text}]}, grouped from the evidence the reconciliation
    already holds for the quantity, delivery and invoice checks. Nothing is
    read or calculated again, and an item is listed only under the shipment
    it was found for.
    """

    found = {}

    for shipment in snapshot["shipments"]:

        result = shipment["result"]

        if not result.get("success"):
            continue

        for audit in ("quantity", "delivery", "invoice"):

            for item in (result.get(audit) or {}).get("evidence") or []:

                if not item.get("source") or identifier_key(item.get("shipment_id")) != shipment["shipment_key"]:
                    continue

                found.setdefault(item["source"], []).append({
                    "shipment_id": shipment["shipment_id"], "audit": audit, "page": item.get("page"),
                    "document_type": item.get("document_type"), "text": item.get("text"),
                })

    return found


@router.get("/documents/file")
def document_file(name: str = Query(max_length=200), principal=ViewAudit, state: ApiState = Depends(get_state)):
    """
    The PDF stored under a file name. The name is looked up in the
    server's own listing of the document folder, never joined to a path.
    """

    found = next((path for path in sorted(state.locations.raw_dir.glob("*.pdf")) if path.name == name), None)

    if found is None:
        raise HTTPException(status_code=404, detail="No such document.")

    return FileResponse(found, media_type="application/pdf", filename=found.name, content_disposition_type="inline",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/documents/pipeline")
def document_pipeline(name: str = Query(max_length=200), principal=ViewAudit, state: ApiState = Depends(get_state)):
    """
    How one stored PDF was processed: checks, extracted text, document type and
    shipment, chunks, and the vectors stored for it (processing_trace, read-only).
    Found by name in the server's own listing, like /documents/file.
    """

    found = next((path for path in sorted(state.locations.raw_dir.glob("*.pdf")) if path.name == name), None)

    if found is None:
        raise HTTPException(status_code=404, detail="No such document.")

    sha256 = hashlib.sha256(found.read_bytes()).hexdigest()
    register_db = state.locations.register_db
    record = processing_trace.register_record(IngestionRegister(register_db), found.name, sha256) if register_db.is_file() else None

    return processing_trace.trace(found, state.locations.chroma_dir, record=record)


@router.get("/completeness")
def completeness(
    principal=ViewAudit, state: ApiState = Depends(get_state), search: str = Query("", max_length=100),
    incomplete_only: bool = False,
):

    register = state.service.completeness()

    return {
        **register,
        "shipments": filter_shipments(register["shipments"], search=search, incomplete_only=incomplete_only),
    }


# ------------------------------------------------------------
# Financial exposure
# ------------------------------------------------------------

@router.get("/exposure")
def financial_exposure(
    principal=ViewAudit, state: ApiState = Depends(get_state), search: str = Query("", max_length=100),
    carrier: list[str] = Query(default=[]), classification: list[str] = Query(default=[]),
):

    rows = exposure.filter_rows(
        state.service.exposure_rows(), search=search, carriers=tuple(carrier), classifications=tuple(classification)
    )

    return {
        "rows": rows, "summary": exposure.summary(rows), "by_carrier": exposure.by_carrier(rows),
        "by_category": exposure.by_category(rows), "metric_definitions": exposure.METRICS, "note": exposure.NOTE,
    }


# ------------------------------------------------------------
# Reports (the existing exports)
# ------------------------------------------------------------

def result_of(state, shipment_id):

    shipment = state.service.shipment(shipment_id)

    if shipment is None:
        raise HTTPException(status_code=404, detail="No such shipment.")

    if not shipment["result"].get("success"):
        raise HTTPException(status_code=409, detail="A report needs a successful reconciliation.")

    return shipment["result"], shipment["shipment_id"]


@router.get("/reports/shipments/{shipment_id}.csv")
def report_shipment_csv(shipment_id: str, principal=Download, state: ApiState = Depends(get_state)):

    result, name = result_of(state, shipment_id)

    try:
        return attachment(audit_csv(result, name), export_filename(result), "text/csv; charset=utf-8")
    except ExportError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


@router.get("/reports/shipments/{shipment_id}.pdf")
def report_shipment_pdf(shipment_id: str, principal=Download, state: ApiState = Depends(get_state)):

    result, name = result_of(state, shipment_id)

    try:
        return attachment(audit_pdf(result, name), pdf_filename(result), "application/pdf")
    except ExportError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


@router.get("/reports/register.csv")
def report_register(
    principal=Download, state: ApiState = Depends(get_state), search: str = Query("", max_length=100),
    carrier: list[str] = Query(default=[]), status: list[str] = Query(default=[]),
):

    rows = analytics.filter_rows(
        state.service.rows(), search=search, carriers=tuple(carrier), final_statuses=tuple(status)
    )

    return attachment(analytics.register_csv(rows), "shipment_register.csv", "text/csv; charset=utf-8")


@router.get("/reports/exceptions.csv")
def report_exceptions(principal=Download, state: ApiState = Depends(get_state)):

    return attachment(analytics.exceptions_csv(state.service.exceptions()), "exceptions.csv", "text/csv; charset=utf-8")


@router.get("/reports/exposure.csv")
def report_exposure(principal=Download, state: ApiState = Depends(get_state)):

    return attachment(exposure.rows_csv(state.service.exposure_rows()), "financial_exposure.csv", "text/csv; charset=utf-8")


@router.get("/reports/completeness.csv")
def report_completeness(principal=Download, state: ApiState = Depends(get_state)):

    return attachment(
        completeness_csv(state.service.completeness()["shipments"]), "document_completeness.csv", "text/csv; charset=utf-8"
    )
