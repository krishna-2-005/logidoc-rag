"""
LogiDoc-RAG carrier intelligence

The carriers of the shipments as the documents are now: delivery, money,
documentation, audit and exception figures per carrier, read from what the
application already holds. It never reads a document, never recalculates an
audit and never stores anything:

    shipments      AuditService.snapshot()["shipments"]: each shipment's
                   reconciliation result and freight audit (whose inputs list
                   every document's extracted fields, approved corrections
                   applied)
    exceptions     AuditService.exceptions(): the detected exceptions with
                   their review state and financial impact
    completeness   AuditService.completeness()["shipments"]: which documents
                   each shipment has

(trend_analytics is the other view: what recorded audit runs found, over
time. This module is the current state.)

Carrier identity

    A shipment's carrier is what its documents (BOL, POD, invoice, rate
    confirmation) name, compared by carrier_key: case and spacing ignored,
    nothing else. Names that only differ in punctuation or spacing inside
    words ("Swift Lines, Inc." and "Swift Lines Inc") are listed as possible
    duplicates for a person to decide; they are never merged. A shipment whose
    documents name different carriers is not attributed to either: it is
    listed with the names it gives. One without a carrier is "Not stated".

Every rate comes with its count and its denominator n (trend_analytics.
proportion, with the 95% Wilson interval); a rate whose n is 0 is None, never
0. A missing amount is never 0: amounts are added only where they were read,
and only within one currency (an unstated currency is its own group, "Not
stated"; nothing is converted). The definitions are in DEFINITIONS and are
returned with every result.

There is no composite score: the scorecard shows the figures it would be made
of, so nothing hides behind an arbitrary weighting.
"""

import re
from collections import Counter, defaultdict
from datetime import date
from statistics import mean

from audit_analytics import cell, write_csv
from trend_analytics import NOT_STATED, ON_TIME_STATUSES, carrier_key, proportion


DELAYED = "DELAYED"
CONFLICTING = "Conflicting"

OPEN_STATES = ("open", "in_review")
MATCHED_OUTCOMES = ("MATCHED", "MATCHED_WITH_TOLERANCE")

DEFINITIONS = {
    "carrier": (
        "The carrier the shipment's documents name, compared without case or extra spaces. Names that "
        "differ otherwise are different carriers; similar ones are listed for review, never merged."
    ),
    "shipment_date": (
        "The BOL pickup date; without one the POD delivery date; without one the invoice date. A shipment "
        "with none is undated: it is left out when a date range is chosen."
    ),
    "completed_shipments": "Shipments whose POD states a delivery date.",
    "on_time_rate": (
        "On time or early / deliveries measured. Measured: the BOL states a pickup date, and the audit "
        "compared the BOL's expected delivery date with the POD's delivery date (ON TIME, EARLY or "
        "DELAYED). A delivery the audit could not decide is not measured, never counted as on time."
    ),
    "average_delay_days": "The mean number of days late over the late deliveries only.",
    "invoiced_total": "The sum of the invoice totals that were read, per currency. An invoice whose total was not read is counted as not read, never as 0.",
    "contracted_total": "The sum of the rate confirmations' contracted amounts (agreed rate, plus fuel when stated) for invoices the freight audit compared with a contract.",
    "audited_billed_total": "The invoice totals of those same invoices: the part of the billing that was checked against a contract.",
    "verified_variance": "Invoice total minus contracted amount, summed over the invoices compared with a contract (positive: billed above the contract).",
    "average_invoice_variance": "The mean |invoice total - contracted amount| over the invoices compared with a contract.",
    "cost_per_shipment": "Invoiced total / shipments whose invoice total was read, per currency.",
    "exception_amounts": (
        "The financial impact of the carrier's exceptions by review state (open, disputed, waived, resolved), "
        "per currency, only where the audit could determine an amount. A waived amount was accepted; a "
        "disputed one is raised with the carrier."
    ),
    "completeness_rate": "Shipments with a usable BOL, invoice and POD / shipments.",
    "audit_pass_rate": "Invoices matched against the contract (exactly or within tolerance) / invoices the freight audit compared with a rate confirmation.",
    "invoice_accuracy_rate": "Invoices whose line items add up to their total / invoices whose arithmetic could be checked.",
    "exceptions_per_shipment": "Currently detected exceptions / shipments.",
    "repeated_exceptions": "An exception category raised on two or more of the carrier's shipments.",
}


# ============================================================
# ONE SHIPMENT
# ============================================================

def fields_of(audit, document_type):
    """[fields] of the shipment's documents of one type (freight audit inputs)."""

    return [item.get("fields") or {} for item in (audit or {}).get("inputs") or [] if item.get("document_type") == document_type]


def extracted(fields, name):
    """The field's value when it was read (EXTRACTED), else None."""

    item = fields.get(name) or {}

    return item.get("value") if item.get("state") == "EXTRACTED" else None


def first(audit, document_type, name):

    for fields in fields_of(audit, document_type):
        value = extracted(fields, name)
        if value is not None:
            return value

    return None


def carrier_of(item):
    """(key, display name, names given) of a shipment: key None when not stated, CONFLICTING when the documents disagree."""

    audit = item.get("freight_audit") or {}
    names = [
        str(extracted(fields, "carrier"))
        for document in audit.get("inputs") or []
        for fields in [document.get("fields") or {}]
        if extracted(fields, "carrier")
    ]

    # A shipment the freight audit did not read (an ID collision): what the reconciliation read
    if not audit.get("inputs"):
        carrier = (item.get("result") or {}).get("carrier") or {}
        names = [str(carrier[role]) for role in ("bol", "invoice", "pod") if carrier.get(role)]

    names = [" ".join(name.split()) for name in names]
    keys = {carrier_key(name) for name in names}

    if not keys:
        return None, NOT_STATED, []

    if len(keys) > 1:
        return CONFLICTING, CONFLICTING, sorted(set(names))

    # The form written most often, then the first alphabetically, as its name
    counts = Counter(names)
    name = sorted(counts, key=lambda value: (-counts[value], value))[0]

    return keys.pop(), name, sorted(set(names))


def iso_date(value):

    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def facts(item, completeness_row, exceptions):
    """Everything about one shipment the figures are made of."""

    result = item.get("result") or {}
    audit = item.get("freight_audit") or {}
    success = bool(result.get("success"))
    key, name, names = carrier_of(item)

    pickup = iso_date(first(audit, "BOL", "pickup_date"))
    delivered = iso_date(first(audit, "POD", "delivery_date"))
    invoiced_on = iso_date(first(audit, "INVOICE", "invoice_date"))
    shipment_date = pickup or delivered or invoiced_on

    delivery = (result.get("delivery") or {}) if success else {}
    invoice = (result.get("invoice") or {}) if success else {}

    invoices = fields_of(audit, "INVOICE")
    total = extracted(invoices[0], "invoice_total") if len(invoices) == 1 else None
    currency = (extracted(invoices[0], "currency") if len(invoices) == 1 else None) or NOT_STATED

    contract_line = next((line for line in audit.get("charges") or [] if line.get("charge") == "Invoice total against the contract"), None)
    contracted = (audit.get("documents") or {}).get("contract") or []
    compared = bool(contracted) and contract_line is not None and contract_line.get("expected") is not None \
        and contract_line.get("billed") is not None

    if not success:
        not_measured = "not reconciled"
    elif delivery.get("status") not in ON_TIME_STATUSES:
        not_measured = "delivery dates missing, unreadable or conflicting"
    elif pickup is None:
        not_measured = "no BOL pickup date"
    else:
        not_measured = None

    own = [e for e in exceptions if e["shipment_key"] == item["shipment_key"]]

    return {
        "shipment_id": item["shipment_id"],
        "shipment_key": item["shipment_key"],
        "carrier_key": key,
        "carrier": name,
        "carrier_names": names,
        "date": shipment_date,
        "date_basis": "pickup" if pickup else "delivery" if delivered else "invoice" if invoiced_on else None,
        "final_status": result.get("final_status") if success else "NOT RECONCILED",
        "completed": delivered is not None,
        "delivery_status": delivery.get("status"),
        "measured": not_measured is None,
        "not_measured_reason": not_measured,
        "days_late": delivery.get("days_late") if not_measured is None else None,
        "invoices": len(invoices),
        "invoice_total": total,
        "currency": currency,
        "arithmetic_status": invoice.get("status") if invoices else None,
        "arithmetic_difference": invoice.get("difference"),
        "has_contract": bool(contracted),
        "freight_outcome": audit.get("outcome") if contracted and len(invoices) == 1 else None,
        "contracted": contract_line["expected"] if compared else None,
        "contract_variance": contract_line["variance"] if compared else None,
        "complete": completeness_row is not None and not completeness_row.get("missing"),
        "missing": list((completeness_row or {}).get("missing") or []),
        "exceptions": [
            {"exception_key": e["exception_key"], "category": e["category"], "severity": e.get("severity"),
             "review_status": e.get("review_status") or "open", "financial_impact": e.get("financial_impact")}
            for e in own
        ],
    }


# ============================================================
# A GROUP OF SHIPMENTS (ONE CARRIER, ONE MONTH, OR ALL)
# ============================================================

def money(value):

    return round(float(value), 2)


def by_currency(rows, value, currency=lambda row: row["currency"]):
    """{currency: [values]} of the rows whose value was read."""

    groups = defaultdict(list)

    for row in rows:
        amount = value(row)
        if amount is not None:
            groups[currency(row)].append(float(amount))

    return groups


def financial(rows):
    """Per currency: billed, contract-audited and variance figures (never across currencies)."""

    invoiced = by_currency(rows, lambda row: row["invoice_total"])
    contracted = by_currency(rows, lambda row: row["contracted"])
    audited = by_currency(rows, lambda row: row["invoice_total"] if row["contracted"] is not None else None)
    variance = by_currency(rows, lambda row: row["contract_variance"])
    currencies = sorted(set(invoiced) | set(contracted), key=lambda c: (c == NOT_STATED, c))

    result = []

    for currency in currencies:
        result.append({
            "currency": currency,
            "invoices_with_total": len(invoiced.get(currency, [])),
            "invoiced_total": money(sum(invoiced[currency])) if currency in invoiced else None,
            "invoices_compared_with_contract": len(contracted.get(currency, [])),
            "contracted_total": money(sum(contracted[currency])) if currency in contracted else None,
            "audited_billed_total": money(sum(audited[currency])) if currency in audited else None,
            "verified_variance": money(sum(variance[currency])) if currency in variance else None,
            "average_invoice_variance": money(mean(abs(v) for v in variance[currency])) if variance.get(currency) else None,
            "cost_per_shipment": money(mean(invoiced[currency])) if invoiced.get(currency) else None,
        })

    return {
        "by_currency": result,
        "invoices": sum(row["invoices"] > 0 for row in rows),
        "invoices_total_not_read": sum(row["invoices"] > 0 and row["invoice_total"] is None for row in rows),
    }


def exception_amounts(rows):
    """{state group: [{currency, amount, exceptions}]} of the exceptions whose amount is known."""

    groups = {"open": OPEN_STATES, "disputed": ("disputed",), "waived": ("waived",), "resolved": ("resolved",)}
    result = {}

    for label, states in groups.items():
        totals = defaultdict(lambda: [0.0, 0])

        for row in rows:
            for exception in row["exceptions"]:
                impact = exception.get("financial_impact")
                if exception["review_status"] in states and impact and impact.get("amount") is not None:
                    entry = totals[impact.get("currency") or NOT_STATED]
                    entry[0] += float(impact["amount"])
                    entry[1] += 1

        result[label] = [{"currency": c, "amount": money(a), "exceptions": n} for c, (a, n) in sorted(totals.items())]

    return result


def delivery(rows):

    measured = [row for row in rows if row["measured"]]
    on_time = [row for row in measured if row["delivery_status"] != DELAYED]
    late = [row for row in measured if row["delivery_status"] == DELAYED]

    return {
        "completed": sum(row["completed"] for row in rows),
        "measured": len(measured),
        "on_time": len(on_time),
        "late": len(late),
        "on_time_rate": proportion(len(on_time), len(measured)),
        "average_delay_days": round(mean(row["days_late"] for row in late), 2) if late else None,
        "not_measured": dict(Counter(row["not_measured_reason"] for row in rows if not row["measured"])),
    }


def documents(rows):

    return {
        "shipments": len(rows),
        "complete": sum(row["complete"] for row in rows),
        "missing_bol": sum("BOL" in row["missing"] for row in rows),
        "missing_invoice": sum("INVOICE" in row["missing"] for row in rows),
        "missing_pod": sum("POD" in row["missing"] for row in rows),
        "completeness_rate": proportion(sum(row["complete"] for row in rows), len(rows)),
    }


def audit_figures(rows):

    outcomes = Counter(row["freight_outcome"] for row in rows if row["freight_outcome"])
    audited = sum(outcomes.values())
    arithmetic = Counter(row["arithmetic_status"] for row in rows if row["arithmetic_status"])
    checked = arithmetic.get("MATCH", 0) + arithmetic.get("MISMATCH", 0)

    return {
        "invoices": sum(row["invoices"] > 0 for row in rows),
        "audited_against_contract": audited,
        "without_contract": sum(row["invoices"] > 0 and not row["has_contract"] for row in rows),
        "matched": sum(outcomes.get(o, 0) for o in MATCHED_OUTCOMES),
        "discrepant": outcomes.get("DISCREPANCY", 0),
        "pending_review": outcomes.get("PENDING_REVIEW", 0),
        "insufficient_evidence": outcomes.get("INSUFFICIENT_EVIDENCE", 0),
        "duplicate": outcomes.get("DUPLICATE", 0),
        "audit_pass_rate": proportion(sum(outcomes.get(o, 0) for o in MATCHED_OUTCOMES), audited),
        "arithmetic_checked": checked,
        "arithmetic_match": arithmetic.get("MATCH", 0),
        "arithmetic_mismatch": arithmetic.get("MISMATCH", 0),
        "arithmetic_needs_review": arithmetic.get("NEEDS REVIEW", 0),
        "invoice_accuracy_rate": proportion(arithmetic.get("MATCH", 0), checked),
    }


def exception_figures(rows):

    exceptions = [e for row in rows for e in row["exceptions"]]
    shipments_by_category = defaultdict(set)

    for row in rows:
        for e in row["exceptions"]:
            shipments_by_category[e["category"]].add(row["shipment_id"])

    return {
        "total": len(exceptions),
        "open": sum(e["review_status"] in OPEN_STATES for e in exceptions),
        "by_status": dict(Counter(e["review_status"] for e in exceptions)),
        "by_category": dict(sorted(Counter(e["category"] for e in exceptions).items())),
        "exceptions_per_shipment": round(len(exceptions) / len(rows), 3) if rows else None,
        "repeated": [
            {"category": category, "shipments": sorted(ids)}
            for category, ids in sorted(shipments_by_category.items()) if len(ids) >= 2
        ],
        "amounts": exception_amounts(rows),
    }


def figures(rows):

    return {
        "shipments": len(rows),
        "delivery": delivery(rows),
        "financial": financial(rows),
        "documents": documents(rows),
        "audit": audit_figures(rows),
        "exceptions": exception_figures(rows),
    }


def monthly(rows):
    """Per calendar month of the shipment date (undated shipments left out)."""

    months = defaultdict(list)

    for row in rows:
        if row["date"] is not None:
            months[row["date"].strftime("%Y-%m")].append(row)

    return [
        {
            "month": month,
            "shipments": len(members),
            "measured": sum(r["measured"] for r in members),
            "on_time": sum(r["measured"] and r["delivery_status"] != DELAYED for r in members),
            "late": sum(r["measured"] and r["delivery_status"] == DELAYED for r in members),
            "on_time_rate": proportion(sum(r["measured"] and r["delivery_status"] != DELAYED for r in members),
                                       sum(r["measured"] for r in members)),
            "invoiced": [
                {"currency": c, "total": money(sum(v)), "invoices": len(v)}
                for c, v in sorted(by_currency(members, lambda r: r["invoice_total"]).items())
            ],
            "arithmetic_mismatches": sum(r["arithmetic_status"] == "MISMATCH" for r in members),
            "contract_discrepancies": sum(r["freight_outcome"] == "DISCREPANCY" for r in members),
            "exceptions": sum(len(r["exceptions"]) for r in members),
        }
        for month, members in sorted(months.items())
    ]


# ============================================================
# ALL CARRIERS
# ============================================================

def loose_key(name):
    """Letters and digits only: to point out names that may be the same carrier (never to merge them)."""

    return re.sub(r"[^0-9a-z]", "", str(name).casefold())


def shipment_rows(shipments, exceptions, completeness_rows):

    completeness_by_key = {row["shipment_key"]: row for row in completeness_rows}

    return [facts(item, completeness_by_key.get(item["shipment_key"]), exceptions) for item in shipments]


def in_period(row, start=None, end=None):

    if start is None and end is None:
        return True

    if row["date"] is None:
        return False

    return (start is None or row["date"] >= start) and (end is None or row["date"] <= end)


def analyze(shipments, exceptions, completeness_rows, start=None, end=None):
    """
    Every carrier's figures for the shipments dated in [start, end] (both
    optional; undated shipments are left out only when a range is given).
    Returns {"period", "definitions", "carriers": [...], "unattributed",
    "possible_duplicates", "totals", "monthly"}; each carrier has "carrier_key",
    "carrier", "names", "shipments" and the figures.
    """

    rows = shipment_rows(shipments, exceptions, completeness_rows)
    selected = [row for row in rows if in_period(row, start, end)]

    groups = defaultdict(list)

    for row in selected:
        if row["carrier_key"] not in (None, CONFLICTING):
            groups[row["carrier_key"]].append(row)

    carriers = []

    for key, members in groups.items():
        counts = Counter(name for row in members for name in row["carrier_names"])
        carriers.append({
            "carrier_key": key,
            "carrier": sorted(counts, key=lambda name: (-counts[name], name))[0],
            "names": sorted(counts),
            **figures(members),
        })

    carriers.sort(key=lambda card: card["carrier"].casefold())

    loose = defaultdict(list)

    for card in carriers:
        loose[loose_key(card["carrier"])].append(card["carrier"])

    return {
        "period": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
        "definitions": DEFINITIONS,
        "carriers": carriers,
        "unattributed": {
            "not_stated": [row["shipment_id"] for row in selected if row["carrier_key"] is None],
            "conflicting": [{"shipment_id": row["shipment_id"], "names": row["carrier_names"]}
                            for row in selected if row["carrier_key"] == CONFLICTING],
            "undated_excluded": [row["shipment_id"] for row in rows if row["date"] is None]
            if (start or end) else [],
        },
        "possible_duplicates": [sorted(names) for names in loose.values() if len(names) > 1],
        "totals": figures(selected),
        "monthly": monthly(selected),
    }


def carrier_detail(shipments, exceptions, completeness_rows, key, start=None, end=None, offset=0, limit=50):
    """One carrier: its figures, monthly trend and shipments (a page of them), or None when it has none."""

    rows = [
        row for row in shipment_rows(shipments, exceptions, completeness_rows)
        if row["carrier_key"] == key and in_period(row, start, end)
    ]

    if not rows:
        return None

    counts = Counter(name for row in rows for name in row["carrier_names"])
    rows.sort(key=lambda row: (row["date"] is None, row["date"] or date.min, row["shipment_id"]))

    return {
        "carrier_key": key,
        "carrier": sorted(counts, key=lambda name: (-counts[name], name))[0],
        "names": sorted(counts),
        "identifier": None,
        "contact": None,
        "identity_note": "The documents' carrier name is the identity: carrier numbers (SCAC, MC, DOT) and contact details are not extracted yet.",
        "period": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
        "definitions": DEFINITIONS,
        **figures(rows),
        "monthly": monthly(rows),
        "shipment_count": len(rows),
        "offset": offset,
        "limit": limit,
        "shipment_rows": [
            {**{k: v for k, v in row.items() if k != "date"}, "date": row["date"].isoformat() if row["date"] else None}
            for row in rows[offset:offset + limit]
        ],
    }


# ============================================================
# EXPORT
# ============================================================

CSV_COLUMNS = (
    "carrier", "names", "shipments", "completed", "deliveries_measured", "on_time", "late", "on_time_rate",
    "average_delay_days", "complete_documents", "missing_bol", "missing_invoice", "missing_pod", "completeness_rate",
    "invoices", "audited_against_contract", "matched", "discrepant", "pending_review", "insufficient_evidence",
    "duplicate", "audit_pass_rate", "arithmetic_checked", "arithmetic_mismatch", "invoice_accuracy_rate",
    "currency", "invoiced_total", "contracted_total", "audited_billed_total", "verified_variance",
    "average_invoice_variance", "cost_per_shipment", "exceptions", "open_exceptions", "repeated_categories",
)


def rate_cell(item):

    return None if item["rate"] is None else f"{item['rate'] * 100:.1f}% ({item['count']}/{item['n']})"


def carriers_csv(result):
    """One row per carrier and currency (amounts are never added across currencies)."""

    rows = []

    for card in result["carriers"]:
        d, docs, a, e = card["delivery"], card["documents"], card["audit"], card["exceptions"]
        money_rows = card["financial"]["by_currency"] or [{"currency": None}]

        for money_row in money_rows:
            rows.append([
                card["carrier"], "; ".join(card["names"]), card["shipments"], d["completed"], d["measured"], d["on_time"],
                d["late"], rate_cell(d["on_time_rate"]), d["average_delay_days"], docs["complete"], docs["missing_bol"],
                docs["missing_invoice"], docs["missing_pod"], rate_cell(docs["completeness_rate"]), a["invoices"],
                a["audited_against_contract"], a["matched"], a["discrepant"], a["pending_review"],
                a["insufficient_evidence"], a["duplicate"], rate_cell(a["audit_pass_rate"]), a["arithmetic_checked"],
                a["arithmetic_mismatch"], rate_cell(a["invoice_accuracy_rate"]), money_row.get("currency"),
                money_row.get("invoiced_total"), money_row.get("contracted_total"), money_row.get("audited_billed_total"),
                money_row.get("verified_variance"), money_row.get("average_invoice_variance"),
                money_row.get("cost_per_shipment"), e["total"], e["open"],
                "; ".join(item["category"] for item in e["repeated"]),
            ])

    return write_csv(CSV_COLUMNS, [[cell(value) for value in row] for row in rows])
