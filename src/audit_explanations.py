"""
LogiDoc-RAG evidence-backed exception explanations

Explains each exception of audit_analytics.build_exceptions() so an
auditor can follow it without searching the PDFs: the finding, what
was expected and what was observed, the signed difference, the rule
that was applied, the source documents, pages and field text, why a
value needs review, and a suggested next verification step.

Everything here is deterministic and comes from the reconciliation
result and its evidence; nothing is recalculated and no language model
is involved. Evidence of another shipment is never shown (it is
excluded and counted), missing evidence is stated, and a conflict is
never presented as a verified value.

An optional commentary from a language model can be requested for an
explanation (generate_commentary). It is returned separately, labelled
as generated, is never merged into the explanation, and cannot change
any status, number or review state: the explanation is checked to be
identical before and after, and figures in the commentary that the
audit does not contain are listed.
"""

import copy
import re

from display_text import money_text
from shipment_identity import identifier_key


MAX_TEXT = 240

NO_EVIDENCE = (
    "No source text is attached to this finding in the audit result; "
    "check the original documents."
)

RULES = {
    "quantity": (
        "Shortage = pieces on the BOL (Number of Pieces) minus pieces on the "
        "POD (Number of Pieces Delivered). A positive difference means pieces "
        "are missing; a negative one is an over-delivery."
    ),
    "delivery": (
        "Days late = actual delivery date (POD) minus expected delivery date "
        "(BOL), in calendar days. Delivered on the expected date or earlier "
        "is not a discrepancy."
    ),
    "invoice": (
        "The line items (Base Freight, Fuel Surcharge, Detention Charge, "
        "Other Accessorial Charges) are added and compared with the Total "
        "Invoice Amount; difference = total minus line items. A mismatch is "
        "an arithmetic finding: it does not prove overbilling."
    ),
    "carrier": "The carrier named on the BOL is compared with the one on the {other}.",
    "route": "The {field} on the BOL is compared with the one on the invoice.",
    "review": (
        "A value that is missing, unreadable or in conflict is never "
        "treated as a match: the audit stops at that value and asks for "
        "review."
    ),
    "reconciliation": (
        "A shipment is reconciled only when its BOL, invoice and POD are "
        "all present and readable."
    ),
}

NEXT_STEPS = {
    "quantity": (
        "Check the piece counts on the original BOL and the signed POD, and "
        "ask the carrier whether the remaining pieces are in transit, were "
        "not loaded, or were delivered elsewhere."
    ),
    "quantity-over": (
        "Check the piece counts on the original BOL and the signed POD and "
        "ask the carrier or the consignee to confirm the quantity received."
    ),
    "delivery": (
        "Confirm the delivery date on the signed POD and with the carrier, "
        "and look for a recorded reason for the delay."
    ),
    "invoice": (
        "Compare each line item with the rate agreement or quote and ask the "
        "carrier to confirm or correct the invoice."
    ),
    "carrier": "Check which carrier handled the shipment and whether a document belongs to another carrier.",
    "route": "Check the origin and destination on the BOL and on the invoice against the shipment.",
    "unavailable": (
        "Find the value in the original document, or upload the missing or "
        "a more readable copy; then run the audit again."
    ),
    "conflicting": (
        "Decide which document is authoritative (for example the corrected "
        "one), then remove or replace the other and run the audit again."
    ),
    "unreadable": (
        "Replace the document with a readable copy, or correct the value "
        "and its format (for example a written-out date), then run the audit again."
    ),
    "reconciliation": (
        "Upload the missing document, or a readable copy of the one that "
        "failed (see the document completeness register)."
    ),
}


# ============================================================
# HELPERS
# ============================================================

def clip(text, limit=MAX_TEXT):

    text = " ".join(str(text or "").split())

    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def signed(value, unit):
    """
    A difference with its sign and unit; None when unknown.
    """

    if value is None:
        return None

    if str(unit or "").startswith("USD"):
        sign = "+" if value > 0 else "-" if value < 0 else ""
        return f"{sign}{money_text(abs(value))}"

    sign = "+" if value > 0 else ""

    return f"{sign}{value:,} {unit or ''}".strip()


def sources(result, audit, shipment_key):
    """
    The evidence of one audit of the result as source rows, only for
    this shipment; (rows, number of rows of another shipment that
    were excluded).
    """

    rows, foreign = [], 0

    for item in (result.get(audit) or {}).get("evidence") or []:

        if identifier_key(item.get("shipment_id")) != shipment_key:
            foreign += 1
            continue

        rows.append({
            "source": item.get("source"),
            "page": item.get("page"),
            "document_type": item.get("document_type"),
            "shipment_id": item.get("shipment_id"),
            "text": clip(item.get("text")),
        })

    return rows, foreign


def freight_line(shipment, category):
    """The freight audit line ("Freight audit: <charge or check>") an exception was raised from, or None."""

    audit = shipment.get("freight_audit") or {}
    name = str(category).split(": ", 1)[-1].casefold()

    for item in (audit.get("charges") or []) + (audit.get("checks") or []):
        label = str(item.get("charge") or item.get("check")).casefold()

        if label == name or (name == "duplicate invoice" and label == "invoice number" and item["outcome"] == "DUPLICATE"):
            return item

    return None


def freight_sources(line, shipment):
    """The documents behind a freight audit line, as source rows of this shipment."""

    rows = []

    for role, src in (("contract", line.get("expected_source")), ("invoice", line.get("billed_source"))):
        if src:
            rows.append({"source": src.get("file"), "page": src.get("page"), "document_type": src.get("document_type"),
                         "shipment_id": shipment["shipment_id"], "text": f"{role}: {', '.join(src.get('lines') or []) or role}"})

    for value in line.get("values") or []:
        rows.append({"source": value.get("file"), "page": value.get("page"), "document_type": value.get("document_type"),
                     "shipment_id": shipment["shipment_id"], "text": clip(str(value.get("value")))})

    for item in line.get("evidence") or []:
        rows.append({"source": item.get("source"), "page": item.get("page"), "document_type": item.get("document_type"),
                     "shipment_id": shipment["shipment_id"], "text": clip(item.get("text"))})

    return rows


def review_kind(reason):
    """
    Why a value needs review, from the reconciliation's reason text.
    """

    reason = str(reason or "")

    if "different values" in reason:
        return "conflicting"

    if "was not found" in reason:
        return "unavailable"

    return "unreadable"


# ============================================================
# EXPLANATIONS
# ============================================================

def explain(exception, shipment):
    """
    The explanation of one exception of a shipment of an
    audit_analytics.build_snapshot() snapshot. Returns a dict (see the
    keys below); it never changes the exception or the result.
    """

    result = shipment["result"]
    key = shipment["shipment_key"]
    code = exception["exception_key"].split("|", 1)[1]
    success = bool(result.get("success"))

    quantity = (result.get("quantity") or {}) if success else {}
    delivery = (result.get("delivery") or {}) if success else {}
    invoice = (result.get("invoice") or {}) if success else {}
    carrier = (result.get("carrier") or {}) if success else {}
    route = (result.get("route") or {}) if success else {}

    explanation = {
        "exception_key": exception["exception_key"],
        "shipment_id": shipment["shipment_id"],
        "category": exception["category"],
        "kind": exception["kind"],
        "severity": exception["severity"],
        "finding": exception["description"],
        "expected": None,
        "observed": None,
        "difference": exception.get("difference"),
        "difference_text": signed(exception.get("difference"), exception.get("unit")),
        "rule": None,
        "reason": None,
        "conflicting": False,
        "sources": [],
        "evidence_note": None,
        "excluded_foreign_evidence": 0,
        "next_step": None,
        "caveat": None,
    }

    audit = None

    if code == "quantity":

        audit = "quantity"
        over = (quantity.get("difference") or 0) < 0
        explanation.update(
            expected=f"BOL: {quantity.get('bol'):,} pieces" if quantity.get("bol") is not None else None,
            observed=f"POD: {quantity.get('pod'):,} pieces delivered" if quantity.get("pod") is not None else None,
            rule=RULES["quantity"],
            next_step=NEXT_STEPS["quantity-over" if over else "quantity"],
        )

    elif code == "delivery":

        audit = "delivery"
        explanation.update(
            expected=f"Expected delivery date (BOL): {delivery.get('expected')}",
            observed=f"Actual delivery date (POD): {delivery.get('actual')}",
            rule=RULES["delivery"],
            next_step=NEXT_STEPS["delivery"],
        )

    elif code == "invoice":

        audit = "invoice"
        explanation.update(
            expected=(
                f"Sum of line items: {money_text(invoice['line_item_sum'])}"
                if invoice.get("line_item_sum") is not None else None
            ),
            observed=(
                f"Total Invoice Amount: {money_text(invoice['total'])}"
                if invoice.get("total") is not None else None
            ),
            rule=RULES["invoice"],
            next_step=NEXT_STEPS["invoice"],
            caveat=(
                "This is a calculation mismatch. It does not show that the "
                "invoice is wrong in the carrier's favour, or that any amount "
                "is payable or recoverable."
            ),
        )

    elif code in ("carrier-invoice", "carrier-pod"):

        other = "invoice" if code == "carrier-invoice" else "POD"

        explanation.update(
            expected=f"BOL carrier: {carrier.get('bol')}",
            observed=f"{other} carrier: {carrier.get('invoice' if other == 'invoice' else 'pod')}",
            rule=RULES["carrier"].format(other=other),
            next_step=NEXT_STEPS["carrier"],
        )

    elif code in ("origin", "destination"):

        explanation.update(
            expected=f"BOL {code}: {route.get(code)}",
            observed=f"Invoice {code}: {route.get('invoice_' + code)}",
            rule=RULES["route"].format(field=code),
            next_step=NEXT_STEPS["route"],
        )

    elif code == "reconciliation":

        reason = str(result.get("error") or exception["description"])

        explanation.update(
            reason=reason,
            rule=RULES["reconciliation"],
            next_step=NEXT_STEPS["reconciliation"],
        )

    elif code.startswith("freight-"):

        line = freight_line(shipment, exception["category"])
        currency = (shipment.get("freight_audit") or {}).get("currency") or ""

        def amount_text(value):
            return f"{currency} {value:,.2f}".strip() if value is not None else None

        if line is not None and "charge" in line:
            explanation.update(
                expected=f"Contract (rate confirmation): {amount_text(line['expected'])}" if line.get("expected") is not None else None,
                observed=f"Billed (invoice): {amount_text(line['billed'])}" if line.get("billed") is not None else None,
                rule=f"Freight audit, tolerance rule '{line['rule']}': the outcome is {line['outcome']}.",
            )
        elif line is not None:
            explanation.update(rule=f"Freight audit, {line['check'].lower()} check: the outcome is {line['outcome']}.")

        explanation.update(
            reason=exception["description"],
            next_step=exception.get("recommended_action"),
            caveat="A freight audit finding is for a person to decide on: no amount is approved, disputed or paid automatically.",
        )

        if line is not None:
            explanation["sources"] = freight_sources(line, shipment)

    elif code.startswith("review-"):

        audit = code.split("-", 1)[1]
        reason = exception["description"]
        kind = review_kind(reason)

        explanation.update(
            reason=reason,
            conflicting=kind == "conflicting",
            rule=RULES["review"],
            next_step=NEXT_STEPS[kind],
            caveat=(
                "The documents give different values; none of them is treated as verified."
                if kind == "conflicting" else
                "The value could not be verified, so no comparison was made."
            ),
        )

    if audit:

        explanation["sources"], explanation["excluded_foreign_evidence"] = sources(result, audit, key)

    if not explanation["sources"]:
        explanation["evidence_note"] = NO_EVIDENCE

    if explanation["excluded_foreign_evidence"]:
        explanation["evidence_note"] = (
            f"{explanation['excluded_foreign_evidence']} evidence item(s) of another "
            "shipment were excluded."
            + (" " + NO_EVIDENCE if not explanation["sources"] else "")
        )

    return explanation


def explain_all(snapshot, exceptions):
    """
    The explanation of every exception, each with the shipment it
    belongs to (the exceptions of audit_analytics.build_exceptions()).
    """

    shipments = {item["shipment_key"]: item for item in snapshot["shipments"]}

    return [
        explain(exception, shipments[exception["shipment_key"]])
        for exception in exceptions
        if exception["shipment_key"] in shipments
    ]


# ============================================================
# OPTIONAL LANGUAGE-MODEL COMMENTARY
# ============================================================

COMMENTARY_LABEL = (
    "Generated commentary (language model). It is not part of the audit "
    "and does not change any status, number or review state."
)

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def facts_text(explanation):
    """
    The deterministic facts of an explanation as plain text.
    """

    lines = [
        f"Shipment: {explanation['shipment_id']}",
        f"Finding: {explanation['finding']}",
        f"Category: {explanation['category']} ({explanation['kind']}, {explanation['severity']})",
    ]

    for label, key in (("Expected", "expected"), ("Observed", "observed"),
                       ("Difference", "difference_text"), ("Rule", "rule"),
                       ("Reason", "reason")):
        if explanation.get(key):
            lines.append(f"{label}: {explanation[key]}")

    for item in explanation["sources"]:
        lines.append(f"Source: {item['source']} page {item['page']}: {item['text']}")

    return "\n".join(lines)


def commentary_prompt(explanation):

    return (
        "You are helping a freight auditor. Using ONLY the facts below, write "
        "two or three plain sentences explaining what the finding means and "
        "what to check next. Do not introduce any number, date or name that "
        "is not in the facts. Do not say whether money is owed.\n\n"
        + facts_text(explanation)
    )


def unsupported_numbers(commentary, explanation):
    """
    Numbers in the commentary that appear nowhere in the facts of the
    explanation.
    """

    def plain(number):
        return number.replace(",", "").rstrip(".")

    # Compared by value: "120" is the audit's "120.00"
    allowed = {float(plain(number)) for number in NUMBER.findall(facts_text(explanation))}

    return sorted({
        plain(number) for number in NUMBER.findall(commentary or "")
        if float(plain(number)) not in allowed
    })


def generate_commentary(explanation, llm):
    """
    Optional commentary from llm (a function prompt -> text). Returns
    {"text", "label", "generated": True, "unsupported_numbers": [...]}
    and never changes the explanation.
    """

    before = copy.deepcopy(explanation)

    text = str(llm(commentary_prompt(explanation)) or "").strip()

    if explanation != before:
        raise RuntimeError("The language model step must not change the explanation.")

    return {
        "text": text,
        "label": COMMENTARY_LABEL,
        "generated": True,
        "unsupported_numbers": unsupported_numbers(text, explanation),
    }
