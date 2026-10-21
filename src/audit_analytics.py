"""
LogiDoc-RAG freight audit analytics

Audits every identified shipment with the existing reconciliation
(reconciliation.reconcile_shipment) and summarises the results: KPIs,
a shipment audit register, an exception register and CSV exports.
Nothing is recalculated here: quantities, dates and invoice figures
come from the reconciliation results as they are; this module only
counts and adds them up.

Documents are read once (chunker.create_all_chunks: the PDFs in the
raw folder, as the Reconciliation tab reads them) and every shipment
is reconciled from that one reading. Shipment IDs are compared with
identifier_key (case, spaces and dashes do not matter). Documents
without a shipment ID are never attributed to a shipment; they are
counted and listed separately.

Metric definitions (METRICS):

    identified shipments   distinct shipment IDs in the documents
    reconciled             identified shipments whose BOL, invoice and
                           POD could be reconciled
    with discrepancies     reconciled, final status DISCREPANCIES DETECTED
    needs manual review    not reconciled, or with at least one value
                           that could not be verified (review item)
    no discrepancies       reconciled, final status NO DISCREPANCIES
                           (which also means nothing needs review)
    total invoice amount   sum of the stated invoice totals that were
                           read; shipments without one are not counted
                           as 0 but reported as not covered
    invoice variance       sum of |total - line items| where both are
                           known; net (signed) sum given as well
    % with discrepancies   with discrepancies / reconciled

Exception severity (the rules of Shipment Intelligence):

    HIGH    quantity shortage, delivery delay, invoice mismatch, and a
            shipment that could not be reconciled at all
    MEDIUM  quantity overage, carrier or route inconsistency, and a
            value that needs review
"""

import csv
import hashlib
import io
import json
from pathlib import Path

from audit_export import NOT_AVAILABLE, ExportError, check_exportable, safe_cell
from shipment_identity import identifier_key


DISCREPANCIES = "DISCREPANCIES DETECTED"
NEEDS_REVIEW = "NEEDS REVIEW"
NO_DISCREPANCIES = "NO DISCREPANCIES"
NOT_RECONCILED = "NOT RECONCILED"

HIGH = "HIGH"
MEDIUM = "MEDIUM"

DISCREPANCY = "Discrepancy"
REVIEW = "Needs review"

METRICS = {
    "identified": "Distinct shipment IDs found in the documents (documents without an ID are not counted).",
    "reconciled": "Identified shipments whose BOL, invoice and POD could be reconciled.",
    "with_discrepancies": "Reconciled shipments with final status DISCREPANCIES DETECTED.",
    "needs_review": "Shipments that could not be reconciled, or have at least one value that could not be verified.",
    "no_discrepancies": "Reconciled shipments with final status NO DISCREPANCIES (nothing to review either).",
    "invoice_total": "Sum of the stated invoice totals that were read; shipments without a readable total are not counted as 0.",
    "invoice_variance": "Sum of |Total Invoice Amount - sum of line items| where both are known.",
    "discrepancy_rate": "Shipments with discrepancies / reconciled shipments.",
}


# ============================================================
# SNAPSHOT: every shipment reconciled from one reading
# ============================================================

def source_fingerprint(raw_dir):
    """
    The PDFs of the raw folder as (name, size, modified): changes
    whenever a document is added, removed or replaced, so cached
    analytics are recomputed.
    """

    raw_dir = Path(raw_dir)

    if not raw_dir.exists():
        return ()

    return tuple(
        (path.name, path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(raw_dir.glob("*.pdf"))
    )


def shipment_fingerprint(chunks):
    """
    A hash of what a shipment was audited from: each chunk's source
    file, page, position and a hash of its text (the text itself is not
    kept). It changes when a document of the shipment is added,
    removed or replaced, and does not depend on file times.
    """

    parts = sorted(
        (
            str((chunk.get("metadata") or {}).get("source")),
            str((chunk.get("metadata") or {}).get("page")),
            str((chunk.get("metadata") or {}).get("chunk")),
            hashlib.sha256(str(chunk.get("text") or "").encode("utf-8")).hexdigest(),
        )
        for chunk in chunks
    )

    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()


def build_snapshot(chunks, reconcile, unreadable_files=None):
    """
    Every identified shipment reconciled from the same chunks.

    reconcile: reconciliation.reconcile_shipment (passed in so the
    caller decides which documents are read).

    unreadable_files: the files that gave no chunks (chunker.read_chunks
    problems). They belong to no shipment; every shipment is
    reconciled knowing about them (its result cannot be clean while they
    might hold one of its documents), and they are listed in the
    snapshot.

    Returns {"shipments": [{shipment_id, shipment_key, result,
    input_fingerprint}], "unidentified_sources": [...],
    "unreadable_files": [...]}; result is the reconciliation result, or
    {"success": False, "error": ...}.
    input_fingerprint identifies the documents the shipment was
    audited from (see shipment_fingerprint).
    """

    chunks = list(chunks or [])
    unreadable_files = list(unreadable_files or [])

    names = {}
    by_shipment = {}
    unidentified = set()

    for chunk in chunks:

        metadata = chunk.get("metadata") or {}
        shipment_id = metadata.get("shipment_id")
        key = identifier_key(shipment_id)

        if key:
            names.setdefault(key, set()).add(str(shipment_id).strip())
            by_shipment.setdefault(key, []).append(chunk)
        else:
            unidentified.add(str(metadata.get("source") or "unknown source"))

    shipments = []

    for key in sorted(names):

        # Written the same way in most documents: the shortest, then
        # alphabetical, for a stable name
        shipment_id = sorted(names[key], key=lambda name: (len(name), name))[0]

        try:
            if unreadable_files:
                result = reconcile(shipment_id, chunks=chunks, unreadable_files=unreadable_files)
            else:
                result = reconcile(shipment_id, chunks=chunks)
        except Exception as error:
            result = {"success": False, "error": f"Reconciliation failed: {error}"}

        if not isinstance(result, dict):
            result = {"success": False, "error": "No documents found."}

        if result.get("success"):

            # Never let another shipment's evidence through
            try:
                check_exportable(result, shipment_id)
            except ExportError as error:
                result = {"success": False, "error": str(error)}

        shipments.append({
            "shipment_id": shipment_id,
            "shipment_key": key,
            "result": result,
            "input_fingerprint": shipment_fingerprint(by_shipment[key]),
        })

    return {
        "shipments": shipments,
        "unidentified_sources": sorted(unidentified),
        "unreadable_files": unreadable_files,
    }


# ============================================================
# SHIPMENT AUDIT REGISTER
# ============================================================

def known(value):

    return value is not None and not isinstance(value, bool) and isinstance(value, (int, float))


def register_rows(snapshot):
    """
    One row per identified shipment. Values the reconciliation does
    not have stay None (shown as NOT AVAILABLE); a shipment that could
    not be reconciled has final status NOT RECONCILED.
    """

    rows = []

    for shipment in snapshot["shipments"]:

        result = shipment["result"]
        success = bool(result.get("success"))

        carrier = (result.get("carrier") or {}).get("bol") if success else None
        route = (result.get("route") or {}) if success else {}
        quantity = (result.get("quantity") or {}) if success else {}
        delivery = (result.get("delivery") or {}) if success else {}
        invoice = (result.get("invoice") or {}) if success else {}
        discrepancies = (result.get("discrepancies") or []) if success else []
        review_items = (result.get("review_items") or []) if success else []

        rows.append({
            "shipment_id": shipment["shipment_id"],
            "shipment_key": shipment["shipment_key"],
            "reconciled": success,
            "error": None if success else result.get("error") or "Reconciliation failed.",
            "carrier": carrier,
            "origin": route.get("origin"),
            "destination": route.get("destination"),
            "quantity_status": quantity.get("status"),
            "delivery_status": delivery.get("status"),
            "invoice_status": invoice.get("status"),
            "final_status": result.get("final_status") if success else NOT_RECONCILED,
            "invoice_total": invoice.get("total") if known(invoice.get("total")) else None,
            "invoice_difference": invoice.get("difference") if known(invoice.get("difference")) else None,
            "discrepancy_count": len(discrepancies),
            "review_count": len(review_items),
            "discrepancy_categories": sorted({str(item.get("category")) for item in discrepancies}),
            "needs_review": (not success) or bool(review_items),
        })

    return rows


def kpis(rows):
    """
    The dashboard metrics (definitions in METRICS). A metric that
    cannot be calculated is None, never 0.
    """

    reconciled = [row for row in rows if row["reconciled"]]
    totals = [row["invoice_total"] for row in reconciled if row["invoice_total"] is not None]
    differences = [row["invoice_difference"] for row in reconciled if row["invoice_difference"] is not None]
    with_discrepancies = sum(row["final_status"] == DISCREPANCIES for row in reconciled)

    return {
        "identified": len(rows),
        "reconciled": len(reconciled),
        "with_discrepancies": with_discrepancies,
        "needs_review": sum(row["needs_review"] for row in rows),
        "no_discrepancies": sum(
            row["final_status"] == NO_DISCREPANCIES and not row["review_count"]
            for row in reconciled
        ),
        "invoice_total": round(sum(totals), 2) if totals else None,
        "invoice_total_covered": len(totals),
        "invoice_variance": round(sum(abs(value) for value in differences), 2) if differences else None,
        "invoice_variance_net": round(sum(differences), 2) if differences else None,
        "invoice_variance_covered": len(differences),
        "discrepancy_rate": (
            round(100 * with_discrepancies / len(reconciled), 1) if reconciled else None
        ),
    }


def filter_rows(
    rows,
    search="",
    carriers=(),
    final_statuses=(),
    categories=(),
    invoice_mismatch=False,
    delivery_delayed=False,
    needs_review=False,
):
    """
    The rows matching every filter given. search matches the shipment
    ID regardless of case, spaces and dashes; an empty filter matches
    everything. A shipment without a carrier is listed as NOT
    AVAILABLE in the carrier filter.
    """

    search_key = identifier_key(search) if search and search.strip() else None

    def matches(row):

        if search_key and search_key not in row["shipment_key"]:
            return False

        if carriers and (row["carrier"] or NOT_AVAILABLE) not in carriers:
            return False

        if final_statuses and row["final_status"] not in final_statuses:
            return False

        if categories and not set(categories) & set(row["discrepancy_categories"]):
            return False

        if invoice_mismatch and row["invoice_status"] != "MISMATCH":
            return False

        if delivery_delayed and row["delivery_status"] != "DELAYED":
            return False

        if needs_review and not row["needs_review"]:
            return False

        return True

    return [row for row in rows if matches(row)]


# ============================================================
# EXCEPTION REGISTER
# ============================================================

def evidence_references(items):

    return [
        f"{item.get('source') or NOT_AVAILABLE} p.{item.get('page') if item.get('page') is not None else '?'} "
        f"({item.get('document_type') or NOT_AVAILABLE}): {item.get('text') or ''}".strip()
        for item in items or []
    ]


def signature(exception):
    """
    What was audited, so a review can tell when it changed.
    """

    payload = json.dumps(
        [exception["category"], exception["description"], exception["difference"]],
        sort_keys=True, default=str,
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def discrepancy_text(result, category, fallback):

    for item in result.get("discrepancies") or []:
        if item.get("category") == category:
            return item.get("description") or fallback

    return fallback


def build_exceptions(snapshot):
    """
    The exceptions of every shipment: detected discrepancies (kind
    DISCREPANCY) and values that could not be verified, including a
    shipment that could not be reconciled (kind REVIEW).
    """

    exceptions = []

    def add(shipment, code, kind, category, severity, description,
            difference=None, unit=None, evidence=()):

        exception = {
            "exception_key": f"{shipment['shipment_key']}|{code}",
            "shipment_id": shipment["shipment_id"],
            "shipment_key": shipment["shipment_key"],
            "kind": kind,
            "category": category,
            "severity": severity,
            "description": description,
            "difference": difference,
            "unit": unit,
            "evidence": evidence_references(evidence),
        }

        exception["signature"] = signature(exception)
        exceptions.append(exception)

    for shipment in snapshot["shipments"]:

        result = shipment["result"]

        if not result.get("success"):
            add(shipment, "reconciliation", REVIEW, "Reconciliation incomplete", HIGH,
                result.get("error") or "Reconciliation failed.")
            continue

        quantity = result.get("quantity") or {}
        delivery = result.get("delivery") or {}
        invoice = result.get("invoice") or {}
        carrier = result.get("carrier") or {}
        route = result.get("route") or {}

        if quantity.get("status") == "MISMATCH" and known(quantity.get("difference")):

            shortage = quantity["difference"] > 0

            add(shipment, "quantity", DISCREPANCY,
                "Quantity shortage" if shortage else "Quantity overage",
                HIGH if shortage else MEDIUM,
                discrepancy_text(result, "Quantity", "BOL and POD piece counts differ."),
                quantity["difference"], "pieces (BOL - POD)", quantity.get("evidence"))

        if delivery.get("status") == "DELAYED":

            add(shipment, "delivery", DISCREPANCY, "Delivery delay", HIGH,
                discrepancy_text(result, "Delivery Date", "Delivered after the expected date."),
                delivery.get("days_late"), "days late", delivery.get("evidence"))

        if invoice.get("status") == "MISMATCH":

            add(shipment, "invoice", DISCREPANCY, "Invoice mismatch", HIGH,
                discrepancy_text(result, "Invoice", "Invoice line items do not add up to the total."),
                invoice.get("difference"), "USD (total - line items)", invoice.get("evidence"))

        for code, status, label in (
            ("carrier-invoice", carrier.get("bol_invoice_status"), "Carrier differs between BOL and Invoice."),
            ("carrier-pod", carrier.get("bol_pod_status"), "Carrier differs between BOL and POD."),
        ):
            if status == "MISMATCH":
                add(shipment, code, DISCREPANCY, "Carrier inconsistency", MEDIUM, label)

        for code, status, label in (
            ("origin", route.get("origin_status"), "Origin differs between BOL and Invoice."),
            ("destination", route.get("destination_status"), "Destination differs between BOL and Invoice."),
        ):
            if status == "MISMATCH":
                add(shipment, code, DISCREPANCY, "Route inconsistency", MEDIUM, label)

        audits = {"Quantity": quantity, "Delivery": delivery, "Invoice": invoice}

        for item in result.get("review_items") or []:

            category = str(item.get("category"))

            add(shipment, f"review-{category.lower()}", REVIEW, f"{category} needs review", MEDIUM,
                str(item.get("reason") or "The value could not be verified."),
                evidence=(audits.get(category) or {}).get("evidence"))

    return exceptions


def with_reviews(exceptions, reviews):
    """
    The exceptions with their review state (from
    review_store.ReviewStore.sync); an exception without a stored
    state is open.
    """

    merged = []

    for exception in exceptions:

        review = reviews.get(exception["exception_key"]) or {}

        merged.append(dict(
            exception,
            review_status=review.get("status") or "open",
            reviewer=review.get("reviewer"),
            review_note=review.get("note"),
            needs_recheck=bool(review.get("needs_recheck")),
            review_updated=review.get("updated_at"),
        ))

    return merged


# ============================================================
# CSV EXPORTS
# ============================================================

REGISTER_COLUMNS = (
    "shipment_id", "carrier", "origin", "destination", "quantity_status",
    "delivery_status", "invoice_status", "final_status", "invoice_total",
    "invoice_difference", "discrepancy_count", "review_count",
    "discrepancy_categories", "needs_review", "error",
)

EXCEPTION_COLUMNS = (
    "shipment_id", "kind", "category", "severity", "description",
    "difference", "difference_unit", "review_status", "needs_recheck",
    "reviewer", "review_note", "review_updated", "evidence",
)


def cell(value, money=False):
    """
    A CSV value: NOT AVAILABLE for a missing value, never 0.
    """

    if value is None:
        return NOT_AVAILABLE

    if isinstance(value, bool):
        return "yes" if value else "no"

    if money and known(value):
        return f"{value:.2f}"

    if isinstance(value, (list, tuple)):
        return "; ".join(str(item) for item in value)

    return str(value)


def write_csv(columns, rows):

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    writer.writerow(columns)

    for row in rows:
        writer.writerow([safe_cell(value) for value in row])

    return buffer.getvalue().encode("utf-8-sig")


def register_csv(rows):
    """
    The shipment register rows given (already filtered) as CSV,
    UTF-8 with a byte-order mark, every cell formula-protected.
    """

    return write_csv(REGISTER_COLUMNS, [
        [
            cell(row["shipment_id"]), cell(row["carrier"]), cell(row["origin"]),
            cell(row["destination"]), cell(row["quantity_status"]),
            cell(row["delivery_status"]), cell(row["invoice_status"]),
            cell(row["final_status"]), cell(row["invoice_total"], money=True),
            cell(row["invoice_difference"], money=True), str(row["discrepancy_count"]),
            str(row["review_count"]), "; ".join(row["discrepancy_categories"]),
            cell(row["needs_review"]), row["error"] or "",
        ]
        for row in rows
    ])


def exceptions_csv(exceptions):
    """
    The exceptions given (with their review state) as CSV.
    """

    return write_csv(EXCEPTION_COLUMNS, [
        [
            cell(item["shipment_id"]), item["kind"], item["category"], item["severity"],
            item["description"],
            cell(item["difference"], money=item.get("unit", "") and item["unit"].startswith("USD")),
            item.get("unit") or "", item.get("review_status") or "open",
            cell(bool(item.get("needs_recheck"))), item.get("reviewer") or "",
            item.get("review_note") or "", item.get("review_updated") or "",
            " | ".join(item["evidence"]),
        ]
        for item in exceptions
    ])
