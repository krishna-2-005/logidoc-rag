"""
LogiDoc-RAG financial exposure

Summarises what the invoice audit found in money terms. The invoice
figures come from the reconciliation results (invoice line items,
Total Invoice Amount, difference, status) as they are; this module
only adds them up and groups them.

Each invoice is one of:

    verified      status MATCH: the line items add up to the total
    mismatch      status MISMATCH: a confirmed arithmetic mismatch
    unverifiable  status NEEDS REVIEW (a line item or the total is
                  missing, unreadable or conflicting), or the shipment
                  could not be reconciled

Definitions (METRICS):

    stated invoice total     sum of the Total Invoice Amounts that were
                             read; an invoice without a readable total
                             is not counted as 0, it is reported as not
                             covered
    confirmed mismatches     invoices with status MISMATCH
    signed difference        Total Invoice Amount - sum of line items,
                             per invoice and summed (net); positive
                             means the stated total is above its line
                             items
    absolute variance        sum of |signed difference| over the
                             mismatches: how much arithmetic disagrees,
                             in either direction
    amount under review      sum of the stated totals of unverifiable
                             invoices whose total was read: the size of
                             the invoices that could not be checked, not
                             a loss or an estimate of one

An arithmetic mismatch only shows that the invoice's own figures do not
add up. It does not prove overbilling and is not a recoverable or
payable amount; nothing here claims one. There are no currency
conversions: the documents carry amounts with a "$" and no currency
code, so amounts are shown as stated. If an invoice result ever carries
a "currency", amounts of different currencies are never added together.
"""

from audit_analytics import cell, write_csv
from audit_export import NOT_AVAILABLE
from shipment_identity import identifier_key


VERIFIED = "verified"
MISMATCH = "mismatch"
UNVERIFIABLE = "unverifiable"

CLASS_LABELS = {
    VERIFIED: "Verified (adds up)",
    MISMATCH: "Confirmed arithmetic mismatch",
    UNVERIFIABLE: "Unverifiable (needs review)",
}

METRICS = {
    "stated_total": (
        "Sum of the Total Invoice Amounts that were read. An invoice "
        "without a readable total is not counted as 0; the card says how "
        "many invoices it covers."
    ),
    "mismatches": "Invoices whose line items do not add up to the stated total (status MISMATCH).",
    "net_difference": (
        "Sum of (Total Invoice Amount - line items) over the mismatches. "
        "Positive: the stated totals are above their line items; negative: below."
    ),
    "absolute_variance": "Sum of |Total Invoice Amount - line items| over the mismatches.",
    "unverifiable": (
        "Invoices that could not be verified: a line item or the total is "
        "missing, unreadable or conflicting, or the shipment could not be reconciled."
    ),
    "amount_under_review": (
        "Sum of the stated totals of the unverifiable invoices whose total "
        "was read. It is the size of what could not be checked, not a loss "
        "or a recoverable amount."
    ),
}

NOTE = (
    "A mismatch is an arithmetic finding about the invoice itself. It does "
    "not prove overbilling and is not a recoverable or payable amount."
)

COLUMNS = (
    "shipment_id", "carrier", "invoice_status", "classification",
    "stated_total", "line_item_sum", "signed_difference",
    "absolute_difference", "amount_under_review", "currency",
    "discrepancy_categories", "note",
)


def known(value):

    return value is not None and not isinstance(value, bool) and isinstance(value, (int, float))


# ============================================================
# ONE ROW PER SHIPMENT
# ============================================================

def exposure_rows(snapshot):
    """
    One row per identified shipment from an
    audit_analytics.build_snapshot() snapshot. Amounts the audit does
    not have stay None; they are never turned into 0.
    """

    rows = []

    for shipment in snapshot["shipments"]:

        result = shipment["result"]
        success = bool(result.get("success"))
        invoice = (result.get("invoice") or {}) if success else {}

        status = invoice.get("status") if success else None

        classification = (
            VERIFIED if status == "MATCH"
            else MISMATCH if status == "MISMATCH"
            else UNVERIFIABLE
        )

        total = invoice.get("total") if known(invoice.get("total")) else None
        difference = invoice.get("difference") if known(invoice.get("difference")) else None

        if classification == MISMATCH and difference is None:
            # A mismatch always has its difference; if not, do not count
            # it as an amount
            classification = UNVERIFIABLE

        rows.append({
            "shipment_id": shipment["shipment_id"],
            "shipment_key": shipment["shipment_key"],
            "carrier": (result.get("carrier") or {}).get("bol") if success else None,
            "invoice_status": status if success else "NOT RECONCILED",
            "classification": classification,
            "stated_total": total,
            "line_item_sum": invoice.get("line_item_sum") if known(invoice.get("line_item_sum")) else None,
            # Known for an invoice that adds up (0.00) and for a mismatch;
            # unknown for an unverifiable one
            "signed_difference": difference if classification != UNVERIFIABLE else None,
            "absolute_difference": abs(difference) if classification != UNVERIFIABLE else None,
            "amount_under_review": total if classification == UNVERIFIABLE else None,
            "currency": invoice.get("currency"),
            "discrepancy_categories": sorted(
                {str(item.get("category")) for item in (result.get("discrepancies") or [])}
            ) if success else [],
        })

    return rows


# ============================================================
# SUMMARY, GROUPS AND FILTERS
# ============================================================

def total_of(values):

    values = [value for value in values if value is not None]

    return round(sum(values), 2) if values else None


def summary(rows):
    """
    The financial KPIs of the rows given (METRICS). A figure that
    cannot be calculated is None, never 0. Amounts of different
    currencies are never added: with more than one currency the money
    figures are None and mixed_currency is True.
    """

    currencies = {row["currency"] for row in rows if row["currency"]}
    mixed = len(currencies) > 1

    mismatches = [row for row in rows if row["classification"] == MISMATCH]
    unverifiable = [row for row in rows if row["classification"] == UNVERIFIABLE]
    with_total = [row for row in rows if row["stated_total"] is not None]

    def money(values):
        return None if mixed else total_of(values)

    return {
        "invoices": len(rows),
        "verified": sum(row["classification"] == VERIFIED for row in rows),
        "mismatches": len(mismatches),
        "unverifiable": len(unverifiable),
        "totals_read": len(with_total),
        "totals_missing": len(rows) - len(with_total),
        "stated_total": money(row["stated_total"] for row in with_total),
        "net_difference": money(row["signed_difference"] for row in mismatches),
        # With at least one mismatch these are known amounts, possibly 0.00
        "total_above_line_items": None if mixed or not mismatches else round(
            sum(max(row["signed_difference"], 0) for row in mismatches), 2
        ),
        "total_below_line_items": None if mixed or not mismatches else round(
            sum(max(-row["signed_difference"], 0) for row in mismatches), 2
        ),
        "absolute_variance": money(row["absolute_difference"] for row in mismatches),
        "amount_under_review": money(row["amount_under_review"] for row in unverifiable),
        "unverifiable_without_total": sum(row["amount_under_review"] is None for row in unverifiable),
        "mixed_currency": mixed,
        "currencies": sorted(currencies),
    }


def by_carrier(rows):
    """
    The figures per carrier (a shipment without a carrier is listed
    as NOT AVAILABLE).
    """

    groups = {}

    for row in rows:
        groups.setdefault(row["carrier"] or NOT_AVAILABLE, []).append(row)

    result = []

    for carrier in sorted(groups):

        figures = summary(groups[carrier])

        result.append({
            "carrier": carrier,
            "invoices": figures["invoices"],
            "mismatches": figures["mismatches"],
            "unverifiable": figures["unverifiable"],
            "stated_total": figures["stated_total"],
            "net_difference": figures["net_difference"],
            "absolute_variance": figures["absolute_variance"],
            "amount_under_review": figures["amount_under_review"],
        })

    return result


def by_category(rows):
    """
    Shipments per discrepancy category. Only the Invoice category has
    an amount (its absolute variance); the others are counts: the
    documents give them no monetary value.
    """

    categories = sorted({category for row in rows for category in row["discrepancy_categories"]})

    result = []

    for category in categories:

        members = [row for row in rows if category in row["discrepancy_categories"]]

        result.append({
            "category": category,
            "shipments": len(members),
            "absolute_variance": (
                total_of(row["absolute_difference"] for row in members) if category == "Invoice" else None
            ),
            "amount_basis": (
                "absolute invoice difference" if category == "Invoice"
                else "no monetary value in the documents"
            ),
        })

    return result


def filter_rows(rows, search="", carriers=(), classifications=(), categories=()):
    """
    The rows matching every filter given; an empty filter matches
    everything. search matches the shipment ID regardless of case,
    spaces and dashes.
    """

    search_key = identifier_key(search) if search and search.strip() else None

    def matches(row):

        if search_key and search_key not in row["shipment_key"]:
            return False

        if carriers and (row["carrier"] or NOT_AVAILABLE) not in carriers:
            return False

        if classifications and row["classification"] not in classifications:
            return False

        if categories and not set(categories) & set(row["discrepancy_categories"]):
            return False

        return True

    return [row for row in rows if matches(row)]


def rows_csv(rows):
    """
    The rows given (already filtered) as CSV, UTF-8 with a byte-order
    mark, every cell formula-protected, missing amounts as NOT
    AVAILABLE.
    """

    return write_csv(COLUMNS, [
        [
            cell(row["shipment_id"]), cell(row["carrier"]), cell(row["invoice_status"]),
            CLASS_LABELS[row["classification"]],
            cell(row["stated_total"], money=True), cell(row["line_item_sum"], money=True),
            cell(row["signed_difference"], money=True), cell(row["absolute_difference"], money=True),
            cell(row["amount_under_review"], money=True), cell(row["currency"]),
            "; ".join(row["discrepancy_categories"]),
            NOTE if row["classification"] == MISMATCH else "",
        ]
        for row in rows
    ])
