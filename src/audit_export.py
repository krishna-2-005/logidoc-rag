"""
LogiDoc-RAG freight audit CSV export

Turns the result of reconciliation.reconcile_shipment() for one
shipment into a CSV file. Nothing is recalculated: every value comes
from the reconciliation result as it is.

Each row has a record_type:

    AUDIT SUMMARY  the shipment's final status
    AUDIT          one audit (Quantity, Delivery, Invoice, Carrier,
                   Origin, Destination): expected and actual values,
                   difference and status
    LINE ITEM      one invoice line item and its amount
    DISCREPANCY    a discrepancy the reconciliation reported
    REVIEW         a value that needs review, and why
    EVIDENCE       a document field an audit read: source file, page,
                   document type, shipment ID and the field text

Values the result does not have are written as NOT AVAILABLE, never as
0. Line items keep the invoice audit's status, so an amount read from
an invoice that needs review is not shown as verified. Amounts are
plain numbers with two decimals; differences keep their sign:

    Quantity  difference = BOL pieces - POD pieces
    Delivery  difference = actual - expected delivery date, in days
    Invoice   difference = Total Invoice Amount - sum of line items

Only one shipment is ever exported: the result must be a successful
reconciliation of the selected shipment, and every evidence row must
belong to it.

The file is UTF-8 with a byte-order mark, so spreadsheet programs show
characters such as "—" correctly. Any cell, in any column, that a
spreadsheet could run as a formula (starting with =, +, -, @ after
leading spaces or control characters, or with a tab or carriage
return) is prefixed with "'"; plain numbers such as -30.00 are not.
"""

import csv
import io
import re

from shipment_identity import identifier_key


NOT_AVAILABLE = "NOT AVAILABLE"

COLUMNS = (
    "record_type",
    "shipment_id",
    "category",
    "field",
    "status",
    "expected",
    "actual",
    "difference",
    "difference_basis",
    "invoice_total",
    "line_item_sum",
    "final_status",
    "description",
    "reason",
    "source",
    "page",
    "document_type",
    "evidence_shipment_id",
    "evidence_text",
)

# Characters that make a spreadsheet read a cell as a formula, also in
# their full-width forms
FORMULA_CHARACTERS = "=+-@＝＋－＠"

# A plain number, such as a negative difference: never a formula
PLAIN_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


class ExportError(ValueError):
    """
    The result cannot be exported (failed, or not one shipment).
    """


def number(value, decimals=None):
    """
    A number as text; NOT AVAILABLE when the result has no value.
    """

    if value is None or isinstance(value, bool):
        return NOT_AVAILABLE

    if not isinstance(value, (int, float)):
        return str(value)

    if decimals is None:
        return str(value)

    return f"{value:.{decimals}f}"


def money(value):

    return number(value, decimals=2)


def text(value):

    return NOT_AVAILABLE if value is None else str(value)


def is_formula_like(value):
    """
    Whether a spreadsheet could run value as a formula: it starts with
    a tab or carriage return, or, after leading spaces and control
    characters, with = + - @ (or a full-width form of them). A plain
    number such as "-30.00" is not.
    """

    if PLAIN_NUMBER.fullmatch(value):
        return False

    if value[:1] in ("\t", "\r"):
        return True

    start = value.lstrip("".join(chr(code) for code in range(33)) + "\x7f 　﻿")

    return start[:1] != "" and start[0] in FORMULA_CHARACTERS


def safe_cell(value):
    """
    Every cell, in every column: text that a spreadsheet could run as
    a formula starts with "'". Plain numbers are left as they are.
    """

    value = "" if value is None else str(value)

    return "'" + value if is_formula_like(value) else value


def check_exportable(result, selected_shipment=None):
    """
    The result's shipment ID when it can be exported; otherwise
    ExportError with the reason.
    """

    if not isinstance(result, dict) or not result.get("success"):
        raise ExportError("Reconciliation did not succeed; there is nothing to export.")

    shipment_id = result.get("shipment_id")
    shipment_key = identifier_key(shipment_id)

    if not shipment_key:
        raise ExportError("The reconciliation result has no shipment ID.")

    if selected_shipment is not None and identifier_key(selected_shipment) != shipment_key:
        raise ExportError(
            f"The reconciliation result is for {shipment_id}, not the "
            f"selected shipment {selected_shipment}."
        )

    for category in ("quantity", "delivery", "invoice"):

        for item in (result.get(category) or {}).get("evidence") or []:

            if identifier_key(item.get("shipment_id")) != shipment_key:
                raise ExportError(
                    f"The {category} evidence includes a document of "
                    f"another shipment ({item.get('shipment_id')}); "
                    "shipments are never mixed in one export."
                )

    return shipment_id


def audit_rows(result, selected_shipment=None):
    """
    The CSV rows (dicts keyed by COLUMNS) of one shipment's
    reconciliation result.
    """

    shipment_id = check_exportable(result, selected_shipment)
    final_status = result.get("final_status")

    rows = []

    def add(record_type, **values):
        row = {column: "" for column in COLUMNS}
        row.update(record_type=record_type, shipment_id=shipment_id, **values)
        rows.append(row)

    add(
        "AUDIT SUMMARY",
        category="Shipment",
        field="final status",
        status=text(final_status),
        final_status=text(final_status),
    )

    quantity = result.get("quantity") or {}

    add(
        "AUDIT",
        category="Quantity",
        field="pieces (BOL expected, POD delivered)",
        status=text(quantity.get("status")),
        expected=number(quantity.get("bol")),
        actual=number(quantity.get("pod")),
        difference=number(quantity.get("difference")),
        difference_basis="BOL - POD",
        final_status=text(final_status),
        reason=quantity.get("reason") or "",
    )

    delivery = result.get("delivery") or {}

    add(
        "AUDIT",
        category="Delivery",
        field="delivery date",
        status=text(delivery.get("status")),
        expected=text(delivery.get("expected")),
        actual=text(delivery.get("actual")),
        difference=number(delivery.get("days_late")),
        difference_basis="actual - expected (days)",
        final_status=text(final_status),
        reason=delivery.get("reason") or "",
    )

    invoice = result.get("invoice") or {}

    add(
        "AUDIT",
        category="Invoice",
        field="line items vs Total Invoice Amount",
        status=text(invoice.get("status")),
        expected=money(invoice.get("line_item_sum")),
        actual=money(invoice.get("total")),
        difference=money(invoice.get("difference")),
        difference_basis="Total Invoice Amount - sum of line items",
        invoice_total=money(invoice.get("total")),
        line_item_sum=money(invoice.get("line_item_sum")),
        final_status=text(final_status),
        description=invoice.get("breakdown") or "",
        reason=invoice.get("reason") or "",
    )

    for label, amount in invoice.get("line_items") or []:

        add(
            "LINE ITEM",
            category="Invoice",
            field=label,
            status=NOT_AVAILABLE if amount is None else text(invoice.get("status")),
            actual=money(amount),
            invoice_total=money(invoice.get("total")),
            final_status=text(final_status),
        )

    carrier = result.get("carrier") or {}
    route = result.get("route") or {}

    for category, field, expected, actual, status in (
        ("Carrier", "carrier (BOL vs Invoice)", carrier.get("bol"),
         carrier.get("invoice"), carrier.get("bol_invoice_status")),
        ("Carrier", "carrier (BOL vs POD)", carrier.get("bol"),
         carrier.get("pod"), carrier.get("bol_pod_status")),
        ("Origin", "origin (BOL vs Invoice)", route.get("origin"),
         route.get("invoice_origin"), route.get("origin_status")),
        ("Destination", "destination (BOL vs Invoice)", route.get("destination"),
         route.get("invoice_destination"), route.get("destination_status")),
    ):

        if not (carrier if category == "Carrier" else route):
            continue

        add(
            "AUDIT",
            category=category,
            field=field,
            status=text(status),
            expected=text(expected),
            actual=text(actual),
            final_status=text(final_status),
        )

    for item in result.get("discrepancies") or []:

        difference = item.get("difference", item.get("days_late"))

        add(
            "DISCREPANCY",
            category=text(item.get("category")),
            status="DISCREPANCY",
            difference=(
                money(difference) if item.get("category") == "Invoice"
                else "" if difference is None
                else number(difference)
            ),
            final_status=text(final_status),
            description=item.get("description") or "",
        )

    for item in result.get("review_items") or []:

        add(
            "REVIEW",
            category=text(item.get("category")),
            status="NEEDS REVIEW",
            final_status=text(final_status),
            reason=item.get("reason") or "",
        )

    for category in ("Quantity", "Delivery", "Invoice"):

        for item in (result.get(category.lower()) or {}).get("evidence") or []:

            add(
                "EVIDENCE",
                category=category,
                source=text(item.get("source")),
                page=text(item.get("page")),
                document_type=text(item.get("document_type")),
                evidence_shipment_id=text(item.get("shipment_id")),
                evidence_text=text(item.get("text")),
            )

    return rows


def audit_csv(result, selected_shipment=None):
    """
    The CSV file as UTF-8 bytes (with a byte-order mark).
    """

    rows = audit_rows(result, selected_shipment)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\r\n")

    writer.writeheader()

    for row in rows:
        writer.writerow({column: safe_cell(row[column]) for column in COLUMNS})

    return buffer.getvalue().encode("utf-8-sig")


def export_filename(result):

    shipment_id = str(result.get("shipment_id") or "shipment")

    return "freight_audit_" + re.sub(r"[^A-Za-z0-9_-]+", "_", shipment_id) + ".csv"
