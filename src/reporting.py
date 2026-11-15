"""
Freight audit and carrier reporting (docs/REPORTING.md).

Four portfolio reports, built from what the application already holds and never
recalculating an audit:

    freight-audit-summary   one row per shipment: statuses, freight audit
                            outcome, invoice and contract figures, exceptions,
                            risk
    discrepancies           one row per exception, with its review state, the
                            amount at stake and its evidence references
    carrier-performance     one row per carrier: delivery, audit, exception and
                            risk figures (carrier_intelligence.figures)
    corrections             every correction proposed and decided, every
                            controlled re-audit recorded and every TMS decision

(The shipment-level audit report is the existing per-shipment CSV and PDF,
audit_export and audit_pdf_export.)

Every report carries the time it was generated (UTC), the filters applied in
words, where its figures come from, its rows and its totals. Rules:

    - Money is never added across currencies: every amount total is per
      currency, and an unstated currency is its own group ("Not stated").
    - A missing value is "NOT AVAILABLE", never 0, and a count says what it
      counts (rows without the value are counted separately).
    - Every CSV cell goes through audit_export.safe_cell (formula injection).
    - The PDF is drawn as plain text with the standard fonts
      (audit_pdf_export.Report): no markup is interpreted, nothing is embedded
      or linked, characters the fonts cannot show are replaced.
    - Nothing names a server path, a database or a credential. File names are
      the documents' own names.

Filters: shipment date range (the BOL pickup date, else the POD delivery date,
else the invoice date, as Carrier Intelligence dates a shipment; undated
shipments are left out when a range is given, and counted), carrier (its key),
shipment (its ID contains the text), freight audit outcome, exception review
status, current risk category. A filter that does not apply to a report is
refused by the API, never silently ignored. The corrections report filters on
the date of each event.
"""

import csv
import io
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import audit_analytics as analytics
import carrier_intelligence as carriers_module
import risk_analytics as risk
from audit_export import NOT_AVAILABLE, safe_cell
from shipment_identity import identifier_key

NOT_STATED = "Not stated"
NOT_RUN = "NOT_RUN"
UNRESOLVED = ("open", "in_review", "disputed")
REVIEW_STATUSES = ("open", "in_review", "disputed", "resolved", "dismissed", "waived")
OUTCOMES = ("MATCHED", "MATCHED_WITH_TOLERANCE", "DISCREPANCY", "INSUFFICIENT_EVIDENCE", "DUPLICATE", "PENDING_REVIEW", NOT_RUN)

FILTER_LABELS = {
    "dates": "Date range", "carrier": "Carrier", "shipment": "Shipment", "outcome": "Freight audit outcome",
    "exception_status": "Exception status", "risk_category": "Risk category",
}

REPORTS = {
    "freight-audit-summary": {
        "title": "Freight audit summary",
        "description": "Every shipment with its audit statuses, freight audit outcome, invoice and contract figures, "
                       "open exceptions and risk.",
        "filters": ("dates", "carrier", "shipment", "outcome", "exception_status", "risk_category"),
        "date_basis": "the shipment date (BOL pickup, else POD delivery, else invoice date)",
    },
    "discrepancies": {
        "title": "Discrepancies and exceptions",
        "description": "Every exception with its review state, severity, amount at stake and evidence references.",
        "filters": ("dates", "carrier", "shipment", "outcome", "exception_status", "risk_category"),
        "date_basis": "the shipment date (BOL pickup, else POD delivery, else invoice date)",
    },
    "carrier-performance": {
        "title": "Carrier performance",
        "description": "Each carrier's delivery, audit, exception and risk figures, with money per currency.",
        "filters": ("dates", "carrier", "risk_category"),
        "date_basis": "the shipment date (BOL pickup, else POD delivery, else invoice date)",
    },
    "corrections": {
        "title": "Corrections and re-audits",
        "description": "Every correction proposed and decided, every controlled re-audit recorded and every TMS decision.",
        "filters": ("dates", "shipment"),
        "date_basis": "the date of each event (UTC)",
    },
}


class ReportError(ValueError):
    """The request cannot be reported (HTTP 422)."""


@dataclass
class Filters:
    start: date | None = None
    end: date | None = None
    carrier: str | None = None
    shipment: str | None = None
    outcomes: tuple = ()
    statuses: tuple = ()
    categories: tuple = ()

    def given(self):
        """The filters set, by the names REPORTS uses."""

        found = []
        if self.start or self.end:
            found.append("dates")
        if self.carrier:
            found.append("carrier")
        if self.shipment:
            found.append("shipment")
        if self.outcomes:
            found.append("outcome")
        if self.statuses:
            found.append("exception_status")
        if self.categories:
            found.append("risk_category")
        return found


@dataclass
class Inputs:
    """What the reports read, gathered once per request (routes_reporting)."""

    shipments: list
    exceptions: list
    completeness_rows: list
    facts: list
    risk_rows: list
    model: dict
    generated_at: datetime
    corrections: list | None = None              # extraction store; None: not available
    re_audits: list | None = None                # audit history; None: nothing recorded
    tms_resolutions: list | None = None          # integration store; None: no TMS configured
    notes: list = field(default_factory=list)


# ============================================================
# VALUES
# ============================================================

def money(value):

    return round(float(value), 2) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def shown_money(amount, currency=None):

    if amount is None:
        return NOT_AVAILABLE

    return f"{currency + ' ' if currency else ''}{amount:,.2f}"


def rate_text(item):
    """"80.0% (4 of 5)", or NOT AVAILABLE when nothing could be counted (never 0%)."""

    if not item or item.get("rate") is None:
        return NOT_AVAILABLE

    return f"{item['rate'] * 100:.1f}% ({item['count']} of {item['n']})"


def value_text(value):
    """A corrected or decided value in words (a charge line, a party, a date, a number)."""

    if value is None:
        return NOT_AVAILABLE

    if isinstance(value, dict):
        if "amount" in value and "description" in value:
            return f"{value['description']}: {money(value['amount']):,.2f}" if money(value["amount"]) is not None else str(value["description"])
        if "name" in value:
            return str(value["name"])
        if "amount" in value:
            return f"{value['amount']} {value.get('unit') or ''}".strip()
        if "value" in value:
            return value_text(value["value"])

    if isinstance(value, float):
        return f"{value:,.2f}"

    return str(value)


def currency_lines(groups):
    """{currency: amount} as "USD 1,580.00; EUR 20.00" (never added across currencies)."""

    return "; ".join(f"{currency} {amount:,.2f}" for currency, amount in sorted(groups.items())) or NOT_AVAILABLE


def total_rows(groups, unit=""):
    """Totals rows of {currency: [amounts]}: one per currency, with how many amounts it adds."""

    return [{"label": currency, "value": money(sum(amounts)), "display": f"{currency} {sum(amounts):,.2f}",
             "note": f"{len(amounts)} {unit}" if unit else None} for currency, amounts in sorted(groups.items())]


def count_rows(counter, order=None):

    keys = list(order) if order else sorted(counter)

    return [{"label": str(key).replace("_", " ").lower() if key == NOT_RUN else str(key), "value": counter[key],
             "display": f"{counter[key]:,}", "note": None} for key in keys if counter.get(key)]


# ============================================================
# FILTERING
# ============================================================

def describe(report, filters, carriers=None):
    """The filters applied, in words; "No filter" when none is."""

    found = []

    if filters.start or filters.end:
        found.append(f"{REPORTS[report]['date_basis'].capitalize()} from {filters.start or 'the earliest'} to "
                     f"{filters.end or 'the latest'}")

    if filters.carrier:
        found.append(f"Carrier: {(carriers or {}).get(filters.carrier, filters.carrier)}")

    if filters.shipment:
        found.append(f"Shipment ID contains: {filters.shipment}")

    if filters.outcomes:
        found.append("Freight audit outcome: " + ", ".join(outcome.replace("_", " ").lower() for outcome in filters.outcomes))

    if filters.statuses:
        found.append("Exception status: " + ", ".join(status.replace("_", " ") for status in filters.statuses))

    if filters.categories:
        found.append("Risk category: " + ", ".join(filters.categories))

    return found or ["No filter: every shipment" if report != "corrections" else "No filter: every event"]


def check_filters(report, filters):

    if report not in REPORTS:
        raise ReportError(f"Unknown report {report!r} (one of: {', '.join(REPORTS)}).")

    if filters.start and filters.end and filters.start > filters.end:
        raise ReportError("The start date is after the end date.")

    refused = [name for name in filters.given() if name not in REPORTS[report]["filters"]]

    if refused:
        raise ReportError(f"The {REPORTS[report]['title'].lower()} cannot be filtered by "
                          + ", ".join(FILTER_LABELS[name].lower() for name in refused) + ".")


def shipment_matches(text, shipment_id, key):

    wanted = " ".join(str(text).split()).casefold()

    return wanted in str(shipment_id).casefold() or (identifier_key(text) or "\0") in str(key)


def outcomes_of(inputs):
    """{shipment key: its freight audit's outcome, or None when it did not run}."""

    return {item["shipment_key"]: (item.get("freight_audit") or {}).get("outcome") for item in inputs.shipments}


def selected_shipments(inputs, filters):
    """(the shipments' facts that pass the filters, how many undated were left out)."""

    risk_by_key = {row["shipment_key"]: row for row in inputs.risk_rows}
    exceptions_by_key = defaultdict(list)

    for exception in inputs.exceptions:
        exceptions_by_key[exception["shipment_key"]].append(exception)

    outcomes = outcomes_of(inputs)
    found, undated = [], 0

    for row in inputs.facts:
        key = row["shipment_key"]

        if filters.start or filters.end:
            if row["date"] is None:
                undated += 1
                continue
            if not carriers_module.in_period(row, filters.start, filters.end):
                continue

        if filters.carrier and row["carrier_key"] != filters.carrier:
            continue

        if filters.shipment and not shipment_matches(filters.shipment, row["shipment_id"], key):
            continue

        outcome = outcomes.get(key) or NOT_RUN

        if filters.outcomes and outcome not in filters.outcomes:
            continue

        if filters.statuses and not any((e.get("review_status") or "open") in filters.statuses for e in exceptions_by_key[key]):
            continue

        scored = risk_by_key.get(key)

        if filters.categories and (scored is None or scored["category"] not in filters.categories):
            continue

        found.append(row)

    return found, undated


def provenance(inputs, extra=()):
    """Where the figures come from, in words (no path, no database name)."""

    return [
        f"The current audit of {len(inputs.shipments)} shipment(s), computed from the documents at "
        f"{inputs.generated_at.isoformat(timespec='seconds')} (UTC); approved corrections and TMS decisions applied as "
        "the audit reads them.",
        "Exception review states from the review store.",
        f"Risk: model {inputs.model['version']} (digest {risk.digest_of(inputs.model)}).",
        *extra,
    ]


# ============================================================
# THE REPORTS
# ============================================================

def freight_audit_summary(inputs, filters):

    rows_in, undated = selected_shipments(inputs, filters)
    risk_by_key = {row["shipment_key"]: row for row in inputs.risk_rows}
    exceptions_by_key = defaultdict(list)

    for exception in inputs.exceptions:
        exceptions_by_key[exception["shipment_key"]].append(exception)

    shipment_outcomes = outcomes_of(inputs)
    rows = []
    invoiced, variance, at_stake = defaultdict(list), defaultdict(list), defaultdict(list)
    statuses, outcomes, categories = Counter(), Counter(), Counter()
    without_total = 0

    for fact in sorted(rows_in, key=lambda row: row["shipment_id"]):
        key = fact["shipment_key"]
        own = exceptions_by_key[key]
        unresolved = [e for e in own if (e.get("review_status") or "open") in UNRESOLVED]
        stake = defaultdict(float)

        for exception in unresolved:
            impact = exception.get("financial_impact") or {}
            if money(impact.get("amount")) is not None:
                stake[impact.get("currency") or NOT_STATED] += float(impact["amount"])
                at_stake[impact.get("currency") or NOT_STATED].append(float(impact["amount"]))

        scored = risk_by_key.get(key) or {}
        outcome = shipment_outcomes.get(key) or NOT_RUN
        currency = fact["currency"] or NOT_STATED

        if money(fact["invoice_total"]) is not None:
            invoiced[currency].append(float(fact["invoice_total"]))
        else:
            without_total += 1

        if money(fact["contract_variance"]) is not None:
            variance[currency].append(float(fact["contract_variance"]))

        statuses[fact["final_status"]] += 1
        outcomes[outcome] += 1
        categories[scored.get("category")] += 1

        rows.append({
            "shipment_id": fact["shipment_id"], "carrier": fact["carrier"],
            "date": fact["date"].isoformat() if fact["date"] else None, "date_basis": fact["date_basis"],
            "final_status": fact["final_status"], "freight_outcome": outcome.replace("_", " ").lower() if outcome != NOT_RUN else "not run",
            "currency": currency, "invoice_total": money(fact["invoice_total"]), "contracted": money(fact["contracted"]),
            "contract_variance": money(fact["contract_variance"]),
            "discrepancies": sum(1 for e in own if e.get("kind") == risk.DISCREPANCY),
            "open_exceptions": len(unresolved), "at_stake": currency_lines(stake) if stake else None,
            "risk_score": scored.get("score"), "risk_category": scored.get("category"),
        })

    totals = [
        {"title": "Shipments", "rows": [{"label": "Shipments", "value": len(rows), "display": f"{len(rows):,}", "note": None}]},
        {"title": "Final audit status", "rows": count_rows(statuses)},
        {"title": "Freight audit outcome", "rows": count_rows(outcomes, OUTCOMES)},
        {"title": "Invoiced, by currency", "rows": total_rows(invoiced, "invoice(s) with a readable total")
         + ([{"label": "Without a readable total", "value": without_total, "display": f"{without_total:,} shipment(s)", "note": "not counted as 0"}]
            if without_total else [])},
        {"title": "Contract variance (billed - contracted), by currency", "rows": total_rows(variance, "shipment(s) compared")},
        {"title": "Amount at stake in unresolved exceptions, by currency", "rows": total_rows(at_stake, "exception(s)")},
        {"title": "Risk category", "rows": count_rows(categories, risk.CATEGORIES)},
    ]

    columns = [
        ("shipment_id", "Shipment", "text"), ("carrier", "Carrier", "text"), ("date", "Date", "date"),
        ("final_status", "Final status", "text"), ("freight_outcome", "Freight audit", "text"), ("currency", "Currency", "text"),
        ("invoice_total", "Invoice total", "money"), ("contracted", "Contracted", "money"),
        ("contract_variance", "Variance", "money"), ("discrepancies", "Discrepancies", "count"),
        ("open_exceptions", "Open exceptions", "count"), ("at_stake", "At stake", "text"),
        ("risk_score", "Risk score", "count"), ("risk_category", "Risk", "text"),
    ]

    notes = [f"{undated} undated shipment(s) are left out because a date range is set."] if undated else []

    return columns, rows, totals, notes, ["shipment_id", "carrier", "final_status", "freight_outcome", "invoice_total",
                                          "open_exceptions", "risk_category"]


def discrepancies(inputs, filters):

    selected, undated = selected_shipments(inputs, Filters(filters.start, filters.end, filters.carrier, filters.shipment,
                                                           filters.outcomes, (), filters.categories))
    facts = {row["shipment_key"]: row for row in selected}
    rows = []
    statuses, categories = Counter(), Counter()
    all_amounts, open_amounts = defaultdict(list), defaultdict(list)
    unknown = 0

    for exception in sorted(inputs.exceptions, key=lambda e: (e["shipment_id"], e["exception_key"])):
        fact = facts.get(exception["shipment_key"])
        status = exception.get("review_status") or "open"

        if fact is None or (filters.statuses and status not in filters.statuses):
            continue

        impact = exception.get("financial_impact") or {}
        amount = money(impact.get("amount"))
        currency = (impact.get("currency") or NOT_STATED) if amount is not None else None

        if amount is not None:
            all_amounts[currency].append(amount)
            if status in UNRESOLVED:
                open_amounts[currency].append(amount)
        elif exception.get("kind") == risk.DISCREPANCY:
            unknown += 1

        statuses[status] += 1
        categories[exception["category"]] += 1
        # The reference only (file, page, document type): the passage it quotes stays out of the report
        evidence = analytics.evidence_summary(exception.get("evidence"))

        rows.append({
            "shipment_id": exception["shipment_id"], "carrier": fact["carrier"],
            "date": fact["date"].isoformat() if fact["date"] else None, "category": exception["category"],
            "kind": exception.get("kind"), "severity": exception.get("severity"), "review_status": status.replace("_", " "),
            "description": exception.get("description"), "amount": amount, "currency": currency,
            "basis": impact.get("basis") if amount is not None else None,
            "freight_outcome": (exception.get("audit_outcome") or "").replace("_", " ").lower() or None,
            "evidence": evidence or None,
        })

    totals = [
        {"title": "Exceptions", "rows": [{"label": "Exceptions", "value": len(rows), "display": f"{len(rows):,}", "note": None}]},
        {"title": "Review status", "rows": count_rows(statuses, REVIEW_STATUSES)},
        {"title": "Category", "rows": count_rows(categories)},
        {"title": "Amount at stake, every listed exception, by currency", "rows": total_rows(all_amounts, "exception(s)")},
        {"title": "Amount at stake, unresolved only, by currency", "rows": total_rows(open_amounts, "exception(s)")},
    ]

    notes = []

    if unknown:
        notes.append(f"{unknown} discrepanc{'y has' if unknown == 1 else 'ies have'} no determinable amount: not counted as 0.")

    if undated:
        notes.append(f"{undated} undated shipment(s) are left out because a date range is set.")

    columns = [
        ("shipment_id", "Shipment", "text"), ("carrier", "Carrier", "text"), ("date", "Date", "date"),
        ("category", "Exception", "text"), ("kind", "Kind", "text"), ("severity", "Severity", "text"),
        ("review_status", "Status", "text"), ("description", "Description", "text"), ("amount", "Amount", "money"),
        ("currency", "Currency", "text"), ("basis", "Basis", "text"), ("freight_outcome", "Freight audit", "text"),
        ("evidence", "Evidence", "text"),
    ]

    return columns, rows, totals, notes, ["shipment_id", "category", "severity", "review_status", "amount", "currency"]


def carrier_performance(inputs, filters):

    selected, undated = selected_shipments(inputs, filters)
    risk_by_key = {row["shipment_key"]: row for row in inputs.risk_rows}
    groups = defaultdict(list)
    unattributed = Counter()

    for row in selected:
        if row["carrier_key"] in (None, carriers_module.CONFLICTING):
            unattributed["not stated" if row["carrier_key"] is None else "conflicting names"] += 1
        else:
            groups[row["carrier_key"]].append(row)

    rows = []

    for key, members in groups.items():
        figures = carriers_module.figures(members)
        scores = [risk_by_key[row["shipment_key"]]["score"] for row in members if row["shipment_key"] in risk_by_key]
        invoiced = {item["currency"]: item["invoiced_total"] for item in figures["financial"]["by_currency"]
                    if item.get("invoiced_total") is not None}
        rows.append({
            "carrier": members[0]["carrier"], "shipments": figures["shipments"],
            "deliveries_measured": figures["delivery"]["measured"], "on_time_rate": rate_text(figures["delivery"]["on_time_rate"]),
            "late": figures["delivery"]["late"], "average_delay_days": figures["delivery"]["average_delay_days"],
            "audited_against_contract": figures["audit"]["audited_against_contract"],
            "audit_pass_rate": rate_text(figures["audit"]["audit_pass_rate"]), "discrepant": figures["audit"]["discrepant"],
            "duplicate": figures["audit"]["duplicate"], "invoice_accuracy_rate": rate_text(figures["audit"]["invoice_accuracy_rate"]),
            "exceptions": figures["exceptions"]["total"], "open_exceptions": figures["exceptions"]["open"],
            "incomplete": figures["documents"]["shipments"] - figures["documents"]["complete"],
            "mean_risk": round(sum(scores) / len(scores), 1) if scores else None,
            "high_risk": sum(1 for row in members if (risk_by_key.get(row["shipment_key"]) or {}).get("category") in ("high", "critical")),
            "invoiced": currency_lines(invoiced) if invoiced else None,
        })

    rows.sort(key=lambda row: str(row["carrier"]).casefold())
    everything = carriers_module.figures(selected)
    invoiced = defaultdict(list)

    for row in selected:
        if money(row["invoice_total"]) is not None:
            invoiced[row["currency"] or NOT_STATED].append(float(row["invoice_total"]))

    totals = [
        {"title": "Carriers and shipments", "rows": [
            {"label": "Carriers", "value": len(rows), "display": f"{len(rows):,}", "note": None},
            {"label": "Shipments", "value": len(selected), "display": f"{len(selected):,}", "note": None},
            *[{"label": f"Shipments without one carrier ({label})", "value": count, "display": f"{count:,}", "note": "not in any carrier row"}
              for label, count in sorted(unattributed.items())],
        ]},
        {"title": "All selected shipments", "rows": [
            {"label": "On time", "value": None, "display": rate_text(everything["delivery"]["on_time_rate"]), "note": None},
            {"label": "Freight audit pass rate", "value": None, "display": rate_text(everything["audit"]["audit_pass_rate"]), "note": None},
            {"label": "Invoice accuracy", "value": None, "display": rate_text(everything["audit"]["invoice_accuracy_rate"]), "note": None},
            {"label": "Open exceptions", "value": everything["exceptions"]["open"], "display": f"{everything['exceptions']['open']:,}", "note": None},
        ]},
        {"title": "Invoiced, by currency", "rows": total_rows(invoiced, "invoice(s) with a readable total")},
    ]

    columns = [
        ("carrier", "Carrier", "text"), ("shipments", "Shipments", "count"), ("deliveries_measured", "Deliveries measured", "count"),
        ("on_time_rate", "On time", "text"), ("late", "Late", "count"), ("average_delay_days", "Average delay (days)", "number"),
        ("audited_against_contract", "Audited against a contract", "count"), ("audit_pass_rate", "Audit pass rate", "text"),
        ("discrepant", "Contract discrepancies", "count"), ("duplicate", "Duplicate invoices", "count"),
        ("invoice_accuracy_rate", "Invoice accuracy", "text"), ("exceptions", "Exceptions", "count"),
        ("open_exceptions", "Open exceptions", "count"), ("incomplete", "Incomplete documents", "count"),
        ("mean_risk", "Mean risk score", "number"), ("high_risk", "High or critical risk", "count"), ("invoiced", "Invoiced", "text"),
    ]

    notes = [f"{undated} undated shipment(s) are left out because a date range is set."] if undated else []
    notes.append("Rates show their count and denominator; a rate with nothing to count is NOT AVAILABLE, never 0%.")

    return columns, rows, totals, notes, ["carrier", "shipments", "on_time_rate", "audit_pass_rate", "open_exceptions",
                                          "mean_risk", "high_risk"]


def label_of(field_name):

    import line_item_corrections

    index = line_item_corrections.index_of(field_name)

    return f"Charge line {index + 1}" if index is not None else str(field_name).replace("_", " ")


def corrections(inputs, filters):

    names = {item["shipment_key"]: item["shipment_id"] for item in inputs.shipments}
    events = []

    for item in inputs.corrections or []:
        shipment_key = item.get("shipment_key")
        subject = f"{label_of(item['field_name'])} of {item.get('filename') or 'a document'}"
        change = f"{value_text((item.get('previous') or {}).get('value'))} -> {value_text(item.get('corrected'))}"
        events.append({"at": item["at"], "shipment_key": shipment_key, "event": "Correction proposed", "subject": subject,
                       "change": change, "actor": item["actor"], "note": item["reason"], "reference": f"correction #{item['id']}"})

        if item.get("decided_at"):
            events.append({"at": item["decided_at"], "shipment_key": shipment_key,
                           "event": "Correction approved" if item["decision"] == "approved" else "Correction rejected",
                           "subject": subject, "change": change, "actor": item.get("decided_by"), "note": item.get("decision_note"),
                           "reference": f"correction #{item['id']}"})

    for run in inputs.re_audits or []:
        actor, _, reason = str(run["label"])[len("Re-audit by "):].partition(": ")
        for key in run["shipment_keys"]:
            events.append({"at": run["created_at"], "shipment_key": key, "event": "Re-audit recorded",
                           "subject": f"Audit run #{run['run_id']}", "change": None, "actor": actor or None, "note": reason or None,
                           "reference": f"run #{run['run_id']}"})

    for item in inputs.tms_resolutions or []:
        events.append({"at": item["at"], "shipment_key": item["shipment_key"],
                       "event": "TMS value used" if item["choice"] == "tms" else "Documents' value kept",
                       "subject": str(item["field"]).replace("_", " "),
                       "change": f"documents {value_text(item['document_value'])}, TMS {value_text(item['tms_value'])}",
                       "actor": item["actor"], "note": item["reason"],
                       "reference": f"TMS decision #{item['id']} ({item['external_id']})"})

    rows, kinds = [], Counter()

    for event in sorted(events, key=lambda item: (str(item["at"]), item["reference"])):
        day = str(event["at"])[:10]

        if filters.start and day < filters.start.isoformat():
            continue
        if filters.end and day > filters.end.isoformat():
            continue

        shipment_id = names.get(event["shipment_key"], event["shipment_key"] or NOT_STATED)

        if filters.shipment and not shipment_matches(filters.shipment, shipment_id, event["shipment_key"] or ""):
            continue

        kinds[event["event"]] += 1
        rows.append({"at": event["at"], "shipment_id": shipment_id, "event": event["event"], "subject": event["subject"],
                     "change": event["change"], "actor": event["actor"], "note": event["note"], "reference": event["reference"]})

    totals = [
        {"title": "Events", "rows": [{"label": "Events", "value": len(rows), "display": f"{len(rows):,}", "note": None}]},
        {"title": "By kind", "rows": count_rows(kinds)},
    ]

    notes = []

    if inputs.corrections is None:
        notes.append("The extraction store could not be read: corrections are not listed.")
    if inputs.re_audits is None:
        notes.append("No audit history is recorded: no re-audit is listed.")
    if inputs.tms_resolutions is None:
        notes.append("No TMS integration is configured: no TMS decision is listed.")

    columns = [
        ("at", "When (UTC)", "date"), ("shipment_id", "Shipment", "text"), ("event", "Event", "text"),
        ("subject", "Subject", "text"), ("change", "Change", "text"), ("actor", "By", "text"), ("note", "Reason or note", "text"),
        ("reference", "Reference", "text"),
    ]

    return columns, rows, totals, notes, ["at", "shipment_id", "event", "subject", "actor"]


BUILDERS = {
    "freight-audit-summary": freight_audit_summary,
    "discrepancies": discrepancies,
    "carrier-performance": carrier_performance,
    "corrections": corrections,
}


def build(report, inputs, filters, carriers=None):
    """
    The whole report: {"report", "title", "description", "generated_at",
    "date_basis", "filters", "provenance", "columns", "rows", "totals",
    "notes", "pdf_columns"}. ReportError for an unknown report or a filter it
    cannot take.
    """

    check_filters(report, filters)
    columns, rows, totals, notes, pdf_columns = BUILDERS[report](inputs, filters)
    extra = ["Corrections from the extraction store; re-audits from the recorded audit history; TMS decisions from the "
             "integration store."] if report == "corrections" else []

    return {
        "report": report, "title": REPORTS[report]["title"], "description": REPORTS[report]["description"],
        "generated_at": inputs.generated_at.isoformat(timespec="seconds"), "date_basis": REPORTS[report]["date_basis"],
        "filters": describe(report, filters, carriers), "provenance": provenance(inputs, extra),
        "columns": [{"key": key, "label": label, "kind": kind} for key, label, kind in columns],
        "rows": rows, "totals": [section for section in totals if section["rows"]],
        "notes": [*inputs.notes, *notes], "pdf_columns": pdf_columns,
    }


def catalog():
    """The reports and the filters each takes."""

    return [{"report": key, "title": item["title"], "description": item["description"], "filters": list(item["filters"]),
             "date_basis": item["date_basis"]} for key, item in REPORTS.items()]


# ============================================================
# FILES
# ============================================================

def cell_text(value, kind):

    if value is None or value == "":
        return NOT_AVAILABLE

    if kind == "money":
        return f"{float(value):.2f}"

    return str(value)


def to_csv(report):
    """
    UTF-8 with a byte-order mark: a header block (report, generated, filters,
    sources, notes), the table, then the totals. Every cell is protected
    against formula injection.
    """

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    write = lambda values: writer.writerow([safe_cell(value) for value in values])  # noqa: E731

    write(["Report", report["title"]])
    write(["Generated (UTC)", report["generated_at"]])
    write(["Dates", report["date_basis"]])
    for line in report["filters"]:
        write(["Filter", line])
    for line in report["provenance"]:
        write(["Source", line])
    for line in report["notes"]:
        write(["Note", line])
    write([])

    write([column["label"] for column in report["columns"]])
    for row in report["rows"]:
        write([cell_text(row.get(column["key"]), column["kind"]) for column in report["columns"]])

    write([])
    write(["Totals"])
    for section in report["totals"]:
        for item in section["rows"]:
            write([section["title"], item["label"], item["display"], item.get("note") or ""])

    return ("﻿" + buffer.getvalue()).encode("utf-8")


def to_pdf(report):
    """
    The report as PDF bytes: drawn as plain text (no markup is interpreted,
    nothing is embedded or linked), A4, the header block, the totals and the
    rows (the columns in pdf_columns; the CSV has every column).
    """

    from audit_pdf_export import Report

    document = Report(report["title"], header=f"LogiDoc-RAG — {report['title']} — generated {report['generated_at']} UTC",
                      metadata={"title": report["title"], "subject": f"LogiDoc-RAG {report['title'].lower()} report"})

    try:
        document.title(f"LOGIDOC-RAG — {report['title'].upper()}")
        document.paragraph(f"Generated {report['generated_at']} (UTC). Dates: {report['date_basis']}.")
        document.heading("Filters")
        for line in report["filters"]:
            document.paragraph(line)
        document.heading("Sources")
        for line in report["provenance"]:
            document.paragraph(line)
        if report["notes"]:
            document.heading("Notes")
            for line in report["notes"]:
                document.paragraph(line)

        document.heading("Totals")
        totals = [[section["title"], item["label"], item["display"], item.get("note") or ""]
                  for section in report["totals"] for item in section["rows"]]
        document.table(["Total", "Of", "Value", "Note"], totals or [["No row", "", "", ""]], [3, 2, 2, 2])

        columns = [column for column in report["columns"] if column["key"] in report["pdf_columns"]]
        document.heading(f"Rows ({len(report['rows'])})")

        if report["rows"]:
            document.table([column["label"] for column in columns],
                           [[cell_text(row.get(column["key"]), column["kind"]) for column in columns] for row in report["rows"]],
                           [2] * len(columns))
        else:
            document.paragraph("No row matches the filters.")

        return document.finish()
    finally:
        document.close()


def filename(report, generated_at, extension):

    return f"{report}_{generated_at.strftime('%Y%m%dT%H%M%SZ')}.{extension}"
