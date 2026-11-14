"""
Invoice line-item corrections (docs/CORRECTION_AWARE_AUDIT.md, "Line-item corrections").

A reviewer can correct the amount of one charge of an invoice: the charge the
extractor read at a position of the invoice's line items. It is an ordinary
correction of the extraction store (a proposal, approved or rejected by a
different administrator, append-only and hash-chained), whose field name is the
line's position ("line_item:0" is the first line read) and whose value is

    {"description": the line's description (not corrected),
     "amount": the corrected amount,
     "of": {"description", "amount"} the line as the extractor read it}

"of" pins the correction to that line: it is applied only while the line at that
position of the same file content still reads so (a new extractor version that
reads the lines differently makes it not applicable, and it says so; it is never
moved onto another line).

Who reads an approved one

    freight audit     the corrected amount replaces the line's amount (charge
                      classification, contract comparison, line total)
    invoice audit     when the line's description is one of the labels the
                      reconciliation reads (Base Freight, Fuel Surcharge,
                      Detention Charge, Other Accessorial Charges), the
                      corrected amount replaces that label's printed amount

Recording the effect is a controlled re-audit (audit_service.re_audit_preview /
re_audit): approving a line-item correction never records a run by itself.

This module is deliberately apart from document_extraction: that module's source
is part of the extractor version, and correcting a charge does not change how a
document is read.
"""

from document_extraction import INVOICE, LineItem, money, parse_money
from shipment_calculations import INVOICE_LINE_ITEMS

PREFIX = "line_item:"

# The reconciliation's label for a charge description -> the audit field it reads
TARGETS = {label.casefold(): name for name, label in INVOICE_LINE_ITEMS}
TARGETS_BY_FIELD = tuple((name, label.casefold()) for name, label in INVOICE_LINE_ITEMS)
FIELDS = tuple(name for name, _ in INVOICE_LINE_ITEMS)


def field_name(index):
    """The correction field name of the line item at this position."""

    return f"{PREFIX}{int(index)}"


def index_of(name):
    """The position a line-item correction names, or None for any other field."""

    if not isinstance(name, str) or not name.startswith(PREFIX):
        return None

    tail = name[len(PREFIX):]

    return int(tail) if tail.isdigit() else None


def is_line_item(document_type, name):

    return document_type == INVOICE and index_of(name) is not None


def words(text):

    return " ".join(str(text or "").split())


def same_line(item, read):
    """Whether a line item (LineItem or dict) still reads as `read` ({description, amount}) did."""

    description = item.description if isinstance(item, LineItem) else item.get("description")
    amount = item.amount if isinstance(item, LineItem) else item.get("amount")

    if not read or amount is None or read.get("amount") is None:
        return False

    return words(description).casefold() == words(read.get("description")).casefold() and money(amount) == money(read["amount"])


def coerce(line, text):
    """
    The corrected line a reviewer's amount means ({"description", "amount",
    "of"}). The amount is parsed like any money value (a credit is negative).
    ValueError for an empty or invalid amount.
    """

    lines = [part.strip() for part in (text or "").splitlines() if part.strip()]

    if not lines:
        raise ValueError("a corrected amount cannot be empty")

    amount, _, _ = parse_money(lines[0])

    if amount is None:
        raise ValueError("not a money amount")

    return {"description": line["description"], "amount": round(float(amount), 2),
            "of": {"description": line["description"], "amount": line["amount"]}}


def target_of(correction):
    """The reconciliation's audit field an approved line-item correction stands for, or None."""

    if not is_line_item(correction.get("document_type"), correction.get("field_name")):
        return None

    return TARGETS.get(words((correction.get("value") or {}).get("description")).casefold())


def line_value(value):
    """{description, amount} of a stored corrected value (without "of")."""

    value = value or {}

    return {"description": value.get("description"), "amount": value.get("amount")}
