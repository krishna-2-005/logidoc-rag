"""
Freight audit: three-way matching of one shipment's carrier invoice against
its contract (the rate confirmation) and its execution evidence (BOL, POD).

    result = audit_freight(extractions, texts=..., policy=load_policy(), other_invoices=...)

extractions are document_extraction.DocumentExtraction objects of ONE
shipment's documents (never mixed with another shipment's). Every
comparison is built from values the extractor read, with their source file
and page; nothing missing is filled in, and a value that is missing,
unreadable or conflicting gives INSUFFICIENT_EVIDENCE or PENDING_REVIEW,
never a match.

Charges (charge-level reconciliation)
    Each invoice line item is classified as base freight, fuel surcharge or
    an accessorial. Base freight and fuel are compared with the rate
    confirmation's agreed rate and fuel surcharge; an accessorial the rate
    confirmation does not list is checked for contractual evidence (the
    rate confirmation's text mentions it) and execution evidence (the BOL or
    POD mentions it). An unlisted charge is never called invalid: without
    any evidence it is INSUFFICIENT_EVIDENCE, with some it is PENDING_REVIEW.
    The invoice total is compared with the contracted amount, and the line
    items with the stated total.

References (the rest of the three-way match)
    Shipment reference, carrier, origin, destination, pickup and delivery
    dates, weight, currency and payment terms, read from the rate
    confirmation, the BOL/POD and the invoice. An invoice number on another
    invoice gives DUPLICATE.

Tolerances
    One central policy (load_policy, set by LOGIDOC_FREIGHT_TOLERANCES): per
    rule an absolute amount and a percentage of the expected amount. The
    default is exact matching; a difference inside the tolerance is
    MATCHED_WITH_TOLERANCE, outside it DISCREPANCY.

Outcomes
    MATCHED, MATCHED_WITH_TOLERANCE, DISCREPANCY, INSUFFICIENT_EVIDENCE,
    DUPLICATE, PENDING_REVIEW. Every line, and the shipment's overall
    outcome, says why it received its outcome.

Read-only: nothing is stored and no PDF is changed.
"""

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import document_extraction as de
from document_extraction import BOL, CONFLICTING, EXTRACTED, INVOICE, POD, RATE, comparable, line_total, money
from shipment_identity import identifier_collisions


# ============================================================
# OUTCOMES
# ============================================================

MATCHED = "MATCHED"
MATCHED_WITH_TOLERANCE = "MATCHED_WITH_TOLERANCE"
DISCREPANCY = "DISCREPANCY"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
DUPLICATE = "DUPLICATE"
PENDING_REVIEW = "PENDING_REVIEW"

# Worst first: the shipment's overall outcome is its worst line
SEVERITY = (DUPLICATE, DISCREPANCY, INSUFFICIENT_EVIDENCE, PENDING_REVIEW, MATCHED_WITH_TOLERANCE, MATCHED)

# Outcomes a person has to act on
ACTIONABLE = (DUPLICATE, DISCREPANCY, INSUFFICIENT_EVIDENCE, PENDING_REVIEW)


# ============================================================
# TOLERANCE POLICY
# ============================================================

# Rules a comparison can name; each falls back to "default"
RULES = ("default", "base_freight", "fuel_surcharge", "accessorial", "invoice_total", "invoice_arithmetic", "weight")


@dataclass(frozen=True)
class Tolerance:
    """How far a billed value may be from the expected one: the larger of an
    absolute amount and a percentage of the expected amount."""

    absolute: Decimal = Decimal("0")
    percent: Decimal = Decimal("0")

    def allowed(self, expected):
        return max(self.absolute, abs(expected) * self.percent / Decimal("100"))

    def describe(self):
        if not self.absolute and not self.percent:
            return "exact match"

        parts = ([f"{self.absolute}"] if self.absolute else []) + ([f"{self.percent}%"] if self.percent else [])

        return "within " + " or ".join(parts)


class PolicyError(ValueError):
    """The tolerance configuration cannot be used."""


def load_policy(text=None):
    """
    {rule: Tolerance} from JSON text such as
    '{"default": {"absolute": 0}, "fuel_surcharge": {"absolute": 5, "percent": 2}}'
    (config.FREIGHT_TOLERANCES when text is None). Rules not given use
    "default"; the default default is exact matching. Unknown rules,
    unknown keys and negative or non-numeric values are refused.
    """

    if text is None:
        import config

        text = config.FREIGHT_TOLERANCES

    policy = {"default": Tolerance()}

    if not str(text or "").strip():
        return policy

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise PolicyError(f"The freight tolerances are not valid JSON: {error}") from None

    if not isinstance(raw, dict):
        raise PolicyError("The freight tolerances must be a JSON object of rules.")

    for rule, values in raw.items():
        if rule not in RULES:
            raise PolicyError(f"Unknown tolerance rule {rule!r}; known rules: {', '.join(RULES)}.")

        if not isinstance(values, dict) or set(values) - {"absolute", "percent"}:
            raise PolicyError(f"Tolerance rule {rule!r} takes only 'absolute' and 'percent'.")

        try:
            numbers = {key: Decimal(str(value)) for key, value in values.items()}
        except (InvalidOperation, ValueError):
            raise PolicyError(f"Tolerance rule {rule!r} has a value that is not a number.") from None

        if any(not number.is_finite() or number < 0 for number in numbers.values()):
            raise PolicyError(f"Tolerance rule {rule!r} must not be negative.")

        policy[rule] = Tolerance(**numbers)

    return policy


def rule_of(policy, name):

    return policy.get(name) or policy["default"]


def compare_amounts(expected, billed, tolerance):
    """(outcome, variance, how) for two known amounts; variance = billed - expected."""

    variance = billed - expected

    if variance == 0:
        return MATCHED, variance, "equal"

    if abs(variance) <= tolerance.allowed(expected):
        return MATCHED_WITH_TOLERANCE, variance, f"inside the tolerance ({tolerance.describe()})"

    return DISCREPANCY, variance, f"outside the tolerance ({tolerance.describe()})"


# ============================================================
# CHARGE CLASSIFICATION
# ============================================================

BASE_FREIGHT, FUEL, ACCESSORIAL = "base_freight", "fuel_surcharge", "accessorial"

CHARGE_PATTERNS = (
    (BASE_FREIGHT, re.compile(r"\b(base freight|line ?haul|freight charge|base rate|transportation charge|flat rate)\b")),
    (FUEL, re.compile(r"\bfuel\b")),
)

# Words that say what an accessorial is, looked for in the contract and the execution documents
ACCESSORIAL_TERMS = {
    "detention": ("detention", "wait time", "waiting time", "detained"),
    "lumper": ("lumper", "unloading fee", "unloading service"),
    "liftgate": ("liftgate", "lift gate"),
    "layover": ("layover",),
    "storage": ("storage",),
    "truck ordered not used": ("truck ordered not used", "tonu"),
    "inside delivery": ("inside delivery",),
    "residential delivery": ("residential",),
    "redelivery": ("redelivery", "re-delivery"),
    "stop-off": ("stop-off", "stop off", "extra stop", "additional stop"),
}

# Label words that do not say what a charge is
GENERIC_WORDS = {"charge", "charges", "fee", "fees", "other", "accessorial", "accessorials", "misc", "miscellaneous",
                 "additional", "service", "services", "surcharge", "amount", "total"}


def charge_type(description):

    text = " ".join(str(description or "").lower().split())

    for name, pattern in CHARGE_PATTERNS:
        if pattern.search(text):
            return name

    return ACCESSORIAL


def accessorial_terms(description):
    """The words to look for as evidence of an accessorial; empty for a generic label."""

    text = " ".join(str(description or "").lower().split())

    for terms in ACCESSORIAL_TERMS.values():
        if any(term in text for term in terms):
            return terms

    words = [word for word in re.findall(r"[a-z]{4,}", text) if word not in GENERIC_WORDS]

    return tuple(words)


def mentions(texts, terms, document_types):
    """The first passage of the given document types that mentions one of the terms."""

    for item in texts:
        if str(item.get("document_type") or "").upper() not in document_types:
            continue

        body = str(item.get("text") or "")
        lower = body.lower()

        for term in terms:
            position = lower.find(term)

            if position >= 0:
                start = max(position - 60, 0)
                return {
                    "source": item.get("source"),
                    "page": item.get("page"),
                    "document_type": item.get("document_type"),
                    "text": " ".join(body[start:position + len(term) + 60].split()),
                }

    return None


# ============================================================
# READING THE EXTRACTIONS
# ============================================================

def usable(extractions, document_type):

    return [e for e in extractions if e.document_type == document_type and e.status in (de.S_EXTRACTED, de.S_PARTIAL)]


def field_of(extraction, name):

    return extraction.fields.get(name) if extraction is not None else None


def source(extraction, item):

    return {"file": extraction.filename, "page": item.page if item is not None else None, "document_type": extraction.document_type}


def amount(value):

    return money(value) if value is not None else None


def text_amount(value, currency):

    if value is None:
        return "not stated"

    return f"{currency + ' ' if currency else ''}{value:,.2f}"


def line(charge, rule, outcome, explanation, expected=None, billed=None, variance=None, currency=None,
         expected_source=None, billed_source=None, evidence=(), kind="charge"):
    """One comparison of the charge-level reconciliation."""

    return {
        "charge": charge,
        "kind": kind,
        "rule": rule,
        "expected": None if expected is None else float(expected),
        "billed": None if billed is None else float(billed),
        "variance": None if variance is None else float(variance),
        "currency": currency,
        "expected_source": expected_source,
        "billed_source": billed_source,
        "outcome": outcome,
        "explanation": explanation,
        "evidence": list(evidence),
    }


def check(name, outcome, explanation, values=()):
    """One reference comparison (carrier, route, dates, ...)."""

    return {"check": name, "outcome": outcome, "explanation": explanation, "values": list(values)}


# ============================================================
# THE AUDIT
# ============================================================

def audit_freight(extractions, texts=(), policy=None, other_invoices=()):
    """
    The freight audit of one shipment.

    texts: [{"source", "page", "document_type", "text"}] of the shipment's
    documents (its chunks), searched for evidence of accessorial charges.
    other_invoices: DocumentExtraction of the invoices of OTHER shipments,
    for duplicate invoice numbers.

    Returns {"outcome", "explanation", "documents", "currency", "charges",
    "checks", "financial_impact", "tolerances"}.
    """

    policy = policy if policy is not None else load_policy()
    extractions = list(extractions)
    texts = list(texts)

    invoices, rates = usable(extractions, INVOICE), usable(extractions, RATE)
    bols, pods = usable(extractions, BOL), usable(extractions, POD)

    invoice = invoices[0] if len(invoices) == 1 else None
    rate = rates[0] if len(rates) == 1 else None

    currencies = {
        e.value("currency") for e in invoices + rates if e.value("currency")
    }
    currency = next(iter(currencies)) if len(currencies) == 1 else None

    charges = charge_lines(invoice, rate, invoices, rates, texts, policy, currency, len(currencies) > 1)
    checks = reference_checks(extractions, invoices, rates, bols, pods, other_invoices, policy)

    # A reference that only one document states is shown, but does not decide the outcome
    deciding = [item["outcome"] for item in charges] + [
        item["outcome"] for item in checks if item["outcome"] != INSUFFICIENT_EVIDENCE
    ]
    outcome = worst(deciding) if deciding else INSUFFICIENT_EVIDENCE

    return {
        "outcome": outcome,
        "explanation": explain(outcome, charges, checks, invoices, rates),
        "documents": {
            "contract": [e.filename for e in rates],
            "execution": [e.filename for e in bols + pods],
            "invoice": [e.filename for e in invoices],
        },
        "currency": currency,
        "charges": charges,
        "checks": checks,
        "financial_impact": impact(charges, currency),
        "tolerances": {name: {"absolute": float(value.absolute), "percent": float(value.percent)}
                       for name, value in sorted(policy.items())},
    }


def worst(outcomes):

    return min(outcomes, key=SEVERITY.index)


def charge_lines(invoice, rate, invoices, rates, texts, policy, currency, mixed_currencies):

    if not invoices:
        return [line("Invoice charges", "default", INSUFFICIENT_EVIDENCE,
                     "No usable carrier invoice for this shipment, so there is nothing to audit.", kind="total")]

    if invoice is None:
        return [line("Invoice charges", "default", PENDING_REVIEW,
                     f"{len(invoices)} invoices belong to this shipment ({', '.join(e.filename for e in invoices)}); "
                     "each has to be audited against the contract by a person, and the extra ones may be duplicates.",
                     kind="total")]

    lines = []
    items = invoice.line_items
    readable = bool(items) and not invoice.unclassified_amounts and not invoice.percentage_charges

    grouped = {BASE_FREIGHT: [], FUEL: [], ACCESSORIAL: []}

    for item in items:
        grouped[charge_type(item.description)].append(item)

    def billed_of(kind):
        """(sum, source) of the invoice lines of a kind; 0 when the line items were read and it has none."""

        found = grouped[kind]

        if found:
            total = sum((money(item.amount) for item in found), Decimal("0"))
            return total, {"file": invoice.filename, "page": found[0].page, "document_type": INVOICE,
                           "lines": [item.description for item in found]}

        return (Decimal("0"), None) if readable else (None, None)

    billed_base, base_source = billed_of(BASE_FREIGHT)
    billed_fuel, fuel_source = billed_of(FUEL)

    # --- the contract -------------------------------------------------
    agreed_field = field_of(rate, "agreed_rate")
    fuel_field = field_of(rate, "fuel_surcharge")
    agreed = amount(agreed_field.value) if agreed_field is not None and agreed_field.state == EXTRACTED else None
    contract_fuel = amount(fuel_field.value) if fuel_field is not None and fuel_field.state == EXTRACTED else None

    def contract_gap():
        """(outcome, why) when the contract amount cannot be used; (None, None) when it can."""

        if not rates:
            return INSUFFICIENT_EVIDENCE, "No rate confirmation for this shipment, so the billed amount cannot be checked against a contract."

        if rate is None:
            return PENDING_REVIEW, f"{len(rates)} rate confirmations belong to this shipment; which one applies has to be decided by a person."

        if mixed_currencies:
            return PENDING_REVIEW, "The invoice and the rate confirmation state different currencies; the amounts are not compared."

        if agreed_field is not None and agreed_field.state == CONFLICTING:
            return PENDING_REVIEW, "The rate confirmation states conflicting agreed rates."

        if agreed_field is None or agreed_field.state != EXTRACTED:
            state = agreed_field.state.lower() if agreed_field is not None else "missing"
            return INSUFFICIENT_EVIDENCE, f"The rate confirmation's agreed rate is {state}, so the billed amount cannot be checked."

        return None, None

    def compare(charge, rule_name, expected, billed, expected_src, billed_src, note=""):

        tolerance = rule_of(policy, rule_name)
        outcome, variance, how = compare_amounts(expected, billed, tolerance)
        direction = "above" if variance > 0 else "below"
        detail = (
            f"Billed {text_amount(billed, currency)} against the contracted {text_amount(expected, currency)}: equal."
            if outcome == MATCHED else
            f"Billed {text_amount(billed, currency)} is {text_amount(abs(variance), currency)} {direction} the "
            f"contracted {text_amount(expected, currency)}, {how}."
        )

        return line(charge, rule_name, outcome, (note + " " + detail).strip(), expected, billed, variance, currency,
                    expected_src, billed_src)

    gap_outcome, gap = contract_gap()
    rate_src = source(rate, agreed_field) if rate is not None and agreed_field is not None else None

    # --- base freight and fuel ----------------------------------------
    if gap:
        for kind, label, billed, src in ((BASE_FREIGHT, "Base freight", billed_base, base_source),
                                         (FUEL, "Fuel surcharge", billed_fuel, fuel_source)):
            if billed is None or billed or kind == BASE_FREIGHT:
                lines.append(line(label, kind, gap_outcome, gap, billed=billed, currency=currency, billed_source=src))

    elif billed_base is None:
        lines.append(line("Base freight", BASE_FREIGHT, INSUFFICIENT_EVIDENCE,
                          "The invoice's charges could not all be read, so the billed base freight is not known.",
                          expected=agreed, currency=currency, expected_source=rate_src))

    elif contract_fuel is not None:
        # Line haul and fuel stated separately: each against its own
        lines.append(compare("Base freight", BASE_FREIGHT, agreed, billed_base, rate_src, base_source))

        if billed_fuel is None:
            lines.append(line("Fuel surcharge", FUEL, INSUFFICIENT_EVIDENCE,
                              "The invoice's charges could not all be read, so the billed fuel surcharge is not known.",
                              expected=contract_fuel, currency=currency, expected_source=source(rate, fuel_field)))
        else:
            lines.append(compare("Fuel surcharge", FUEL, contract_fuel, billed_fuel, source(rate, fuel_field), fuel_source,
                                 "" if fuel_source else "The invoice bills no fuel surcharge."))

    elif not billed_fuel:
        lines.append(compare("Base freight", BASE_FREIGHT, agreed, billed_base, rate_src, base_source))

    else:
        # One rate and no separate fuel surcharge: the document does not say whether it includes fuel
        tolerance = rule_of(policy, BASE_FREIGHT)
        all_in, _, _ = compare_amounts(agreed, billed_base + billed_fuel, tolerance)
        line_haul, _, _ = compare_amounts(agreed, billed_base, tolerance)
        single = "The rate confirmation states one rate and no separate fuel surcharge."

        if all_in in (MATCHED, MATCHED_WITH_TOLERANCE):
            lines.append(compare("Base freight and fuel surcharge", BASE_FREIGHT, agreed, billed_base + billed_fuel,
                                 rate_src, base_source, single + " Base freight plus fuel equals it, so it is read as an all-in rate."))
        elif line_haul in (MATCHED, MATCHED_WITH_TOLERANCE):
            lines.append(compare("Base freight", BASE_FREIGHT, agreed, billed_base, rate_src, base_source,
                                 single + " The base freight equals it, so it is read as the line haul."))
            lines.append(line("Fuel surcharge", FUEL, PENDING_REVIEW,
                              f"Billed {text_amount(billed_fuel, currency)}, but the rate confirmation states no fuel surcharge; "
                              "check it against the agreed fuel schedule before paying.",
                              billed=billed_fuel, currency=currency, billed_source=fuel_source))
        else:
            lines.append(line("Base freight and fuel surcharge", BASE_FREIGHT, PENDING_REVIEW,
                              single + f" It ({text_amount(agreed, currency)}) matches neither the billed base freight "
                              f"({text_amount(billed_base, currency)}) nor base freight plus fuel "
                              f"({text_amount(billed_base + billed_fuel, currency)}), and the document does not say "
                              "whether the rate includes fuel.",
                              expected=agreed, billed=billed_base + billed_fuel, variance=billed_base + billed_fuel - agreed,
                              currency=currency, expected_source=rate_src, billed_source=base_source))

    # --- accessorials -------------------------------------------------
    for item in grouped[ACCESSORIAL]:
        billed = money(item.amount)
        billed_src = {"file": invoice.filename, "page": item.page, "document_type": INVOICE}

        if billed == 0:
            lines.append(line(item.description, ACCESSORIAL, MATCHED, "Nothing billed.", billed=billed, currency=currency,
                              billed_source=billed_src))
            continue

        terms = accessorial_terms(item.description)
        contract = mentions(texts, terms, {RATE}) if terms else None
        execution = mentions(texts, terms, {BOL, POD}) if terms else None

        if billed < 0:
            outcome, why = PENDING_REVIEW, "A credit on the invoice; confirm it was agreed."
        elif contract:
            outcome, why = PENDING_REVIEW, (
                f"The rate confirmation mentions it ({contract['source']}, page {contract['page']}) but states no amount "
                "the audit can read; check the billed amount against those terms."
            )
        elif execution:
            outcome, why = PENDING_REVIEW, (
                f"Not on the rate confirmation; the {execution['document_type']} mentions it ({execution['source']}, "
                f"page {execution['page']}), so the service may have been performed. Approve or dispute it."
            )
        else:
            outcome, why = INSUFFICIENT_EVIDENCE, (
                ("Not on the rate confirmation, and no BOL or POD mentions it."
                 if terms else "The label does not say what the charge is, and it is not on the rate confirmation.")
                + " Ask the carrier for supporting documents before paying it."
            )

        lines.append(line(item.description, ACCESSORIAL, outcome, f"Billed {text_amount(billed, currency)}. {why}",
                          billed=billed, currency=currency, billed_source=billed_src,
                          evidence=[e for e in (contract, execution) if e]))

    # --- the invoice total --------------------------------------------
    total_field = field_of(invoice, "invoice_total")
    total = amount(total_field.value) if total_field is not None and total_field.state == EXTRACTED else None
    total_src = source(invoice, total_field) if total_field is not None else None

    if total is None:
        lines.append(line("Invoice total", "invoice_total", INSUFFICIENT_EVIDENCE,
                          f"The invoice total is {total_field.state.lower() if total_field is not None else 'missing'}.",
                          currency=currency, kind="total"))
    else:
        computed, reason = line_total(invoice)

        if computed is None:
            lines.append(line("Line items against the invoice total", "invoice_arithmetic", INSUFFICIENT_EVIDENCE,
                              f"Not checked: {reason}.", billed=total, currency=currency, billed_source=total_src,
                              kind="arithmetic"))
        else:
            outcome, variance, how = compare_amounts(computed, total, rule_of(policy, "invoice_arithmetic"))
            lines.append(line("Line items against the invoice total", "invoice_arithmetic", outcome,
                              f"The line items add up to {text_amount(computed, currency)} and the stated total is "
                              f"{text_amount(total, currency)}: {'equal' if outcome == MATCHED else how}.",
                              computed, total, variance, currency, None, total_src, kind="arithmetic"))

        contracted = [item for item in lines if item["kind"] == "charge" and item["expected"] is not None
                      and item["rule"] in (BASE_FREIGHT, FUEL)]

        # Billed charges with no contracted amount, still to be approved or supported
        uncovered = sum(
            (Decimal(str(item["billed"])) for item in lines
             if item["kind"] == "charge" and item["expected"] is None and item["billed"] is not None
             and item["outcome"] in (PENDING_REVIEW, INSUFFICIENT_EVIDENCE)),
            Decimal("0"),
        )

        if gap or not contracted:
            lines.append(line("Invoice total against the contract", "invoice_total", gap_outcome or INSUFFICIENT_EVIDENCE,
                              gap or "No contracted amount could be read.",
                              billed=total, currency=currency, billed_source=total_src, kind="total"))
        else:
            expected = sum((Decimal(str(item["expected"])) for item in contracted), Decimal("0"))
            outcome, variance, how = compare_amounts(expected, total, rule_of(policy, "invoice_total"))
            note = ""

            # Differences already accepted charge by charge (inside their tolerance)
            tolerated = sum(
                (Decimal(str(item["variance"])) for item in contracted
                 if item["outcome"] == MATCHED_WITH_TOLERANCE and item["variance"] is not None),
                Decimal("0"),
            )

            if any(item["outcome"] == PENDING_REVIEW for item in contracted) and outcome != MATCHED:
                outcome = PENDING_REVIEW
                note = "It depends on how the rate confirmation's rate is read (see the base freight line)."
            elif outcome == DISCREPANCY and uncovered and variance == tolerated + uncovered:
                outcome = PENDING_REVIEW
                note = (f"The difference is the billed charges the contract does not cover "
                        f"({text_amount(uncovered, currency)})"
                        + (f" and charge differences inside their tolerance ({text_amount(tolerated, currency)})" if tolerated else "")
                        + "; it stands or falls with them.")
            elif outcome == DISCREPANCY and tolerated and variance == tolerated:
                outcome = MATCHED_WITH_TOLERANCE
                note = "The difference is the charge differences already inside their tolerance."

            lines.append(line("Invoice total against the contract", "invoice_total", outcome,
                              (f"The invoice total {text_amount(total, currency)} against the contracted "
                               f"{text_amount(expected, currency)}: {'equal' if variance == 0 else how}. " + note).strip(),
                              expected, total, variance, currency, rate_src, total_src, kind="total"))

    return lines


def reference_checks(extractions, invoices, rates, bols, pods, other_invoices, policy):

    checks = []

    def values(name, groups):
        found = []

        for document_type, documents in groups:
            for extraction in documents:
                item = field_of(extraction, name)

                if item is not None and item.state in (EXTRACTED, CONFLICTING):
                    found.append({
                        "document_type": document_type,
                        "file": extraction.filename,
                        "page": item.page,
                        "value": item.value if item.state == EXTRACTED else None,
                        "state": item.state,
                    })

        return found

    # --- shipment reference -------------------------------------------
    printed = [e.shipment_id for e in extractions if e.shipment_id]
    collisions = identifier_collisions(printed)

    if collisions:
        from shipment_identity import collision_message

        checks.append(check("Shipment reference", DISCREPANCY, collision_message(collisions),
                            [{"document_type": e.document_type, "file": e.filename, "value": e.shipment_id}
                             for e in extractions if e.shipment_id]))
    elif printed:
        checks.append(check("Shipment reference", MATCHED, "Every document names the same shipment.",
                            [{"document_type": e.document_type, "file": e.filename, "value": e.shipment_id}
                             for e in extractions if e.shipment_id]))

    # --- text fields read from several documents ----------------------
    for label, name, groups in (
        ("Carrier", "carrier", ((RATE, rates), (BOL, bols), (POD, pods), (INVOICE, invoices))),
        ("Origin", "origin", ((RATE, rates), (BOL, bols), (INVOICE, invoices))),
        ("Destination", "destination", ((RATE, rates), (BOL, bols), (INVOICE, invoices))),
        ("Currency", "currency", ((RATE, rates), (INVOICE, invoices))),
        ("Payment terms", "payment_terms", ((RATE, rates), (INVOICE, invoices))),
    ):
        found = values(name, groups)
        checks.append(agreement(label, found, needs=RATE if name in ("currency", "payment_terms") else None))

    # --- dates: contract (scheduled) against execution ----------------
    for label, scheduled, actual, actual_type, actual_docs in (
        ("Pickup date", "pickup_date", "pickup_date", BOL, bols),
        ("Delivery date", "delivery_date", "delivery_date", POD, pods),
    ):
        planned = values(scheduled, ((RATE, rates),))
        done = values(actual, ((actual_type, actual_docs),))
        checks.append(date_check(label, planned, done))

    # --- weight -------------------------------------------------------
    checks.append(weight_check(values("weight", ((RATE, rates), (BOL, bols))), rule_of(policy, "weight")))

    # --- duplicate invoice numbers ------------------------------------
    for invoice in invoices:
        number = invoice.value("invoice_number")

        if not number:
            continue

        same = [
            other for other in list(other_invoices) + [e for e in invoices if e is not invoice]
            if other.value("invoice_number") and comparable(other.value("invoice_number")) == comparable(number)
            and other.sha256 != invoice.sha256
        ]

        if same:
            checks.append(check("Invoice number", DUPLICATE,
                                f"Invoice number {number} ({invoice.filename}) also appears on "
                                f"{', '.join(sorted(e.filename for e in same))}; the same charges may be billed twice.",
                                [{"document_type": INVOICE, "file": e.filename, "value": e.value("invoice_number")}
                                 for e in [invoice] + same]))
        else:
            checks.append(check("Invoice number", MATCHED, f"Invoice number {number} appears on no other invoice.",
                                [{"document_type": INVOICE, "file": invoice.filename, "value": number}]))

    return [item for item in checks if item is not None]


def agreement(label, found, needs=None):

    if any(item["state"] == CONFLICTING for item in found):
        return check(label, PENDING_REVIEW, f"A document gives conflicting values for the {label.lower()}.", found)

    if needs and not any(item["document_type"] == needs for item in found):
        return check(label, INSUFFICIENT_EVIDENCE, f"No rate confirmation states the {label.lower()}.", found) if found else None

    if len(found) < 2:
        return check(label, INSUFFICIENT_EVIDENCE,
                     f"The {label.lower()} is stated by {'one document' if found else 'no document'}, so it cannot be compared.",
                     found)

    distinct = {comparable(item["value"]) for item in found}

    if len(distinct) == 1:
        return check(label, MATCHED, f"Every document gives the same {label.lower()}.", found)

    listing = "; ".join(f"{item['value']} ({item['file']})" for item in found)

    return check(label, DISCREPANCY, f"The documents give different values for the {label.lower()}: {listing}.", found)


def date_check(label, planned, done):

    found = planned + done

    if any(item["state"] == CONFLICTING for item in found):
        return check(label, PENDING_REVIEW, f"A document gives conflicting {label.lower()}s.", found)

    if not planned or not done:
        missing = "rate confirmation" if not planned else ("BOL" if label.startswith("Pickup") else "POD")
        return check(label, INSUFFICIENT_EVIDENCE, f"No {missing} states the {label.lower()}.", found) if found else None

    scheduled, actual = planned[0]["value"], done[0]["value"]

    if scheduled == actual:
        return check(label, MATCHED, f"The {label.lower()} matches the rate confirmation ({actual}).", found)

    try:
        from datetime import date

        days = (date.fromisoformat(actual) - date.fromisoformat(scheduled)).days
        when = f"{abs(days)} day{'s' if abs(days) != 1 else ''} {'after' if days > 0 else 'before'}"
    except (TypeError, ValueError):
        when = "different from"

    return check(label, DISCREPANCY,
                 f"The {label.lower()} {actual} ({done[0]['file']}) is {when} the rate confirmation's {scheduled} "
                 f"({planned[0]['file']}).", found)


def weight_check(found, tolerance):

    if not found:
        return None

    if any(item["state"] == CONFLICTING for item in found):
        return check("Weight", PENDING_REVIEW, "A document gives conflicting weights.", found)

    if len(found) < 2:
        return check("Weight", INSUFFICIENT_EVIDENCE, "Only one document states the weight, so it cannot be compared.", found)

    units = {(item["value"] or {}).get("unit") for item in found}

    if len(units) > 1 or None in units:
        return check("Weight", PENDING_REVIEW, "The weights are in different or unstated units; they are not converted.", found)

    unit = next(iter(units))
    first = Decimal(str(found[0]["value"]["amount"]))
    results = [
        (compare_amounts(first, Decimal(str(item["value"]["amount"])), tolerance), item)
        for item in found[1:]
    ]
    (outcome, _, how), other = min(results, key=lambda pair: SEVERITY.index(pair[0][0]))

    return check("Weight", outcome,
                 f"{found[0]['file']} states {first} {unit} and {other['file']} {other['value']['amount']} {unit}: "
                 f"{'equal' if outcome == MATCHED else how}.", found)


def impact(charges, currency):
    """What is at stake: amounts billed above the contract and amounts billed without enough evidence."""

    def total(outcomes, value):
        return float(sum(
            (Decimal(str(value(item))) for item in charges
             if item["kind"] == "charge" and item["outcome"] in outcomes and value(item) is not None),
            Decimal("0"),
        ))

    return {
        "currency": currency,
        "billed_above_contract": total((DISCREPANCY,), lambda item: max(item["variance"], 0) if item["variance"] is not None else None),
        "without_evidence": total((INSUFFICIENT_EVIDENCE,), lambda item: item["billed"] if item["rule"] == ACCESSORIAL else None),
        "pending_review": total((PENDING_REVIEW,), lambda item: item["billed"] if item["rule"] == ACCESSORIAL else None),
    }


def explain(outcome, charges, checks, invoices, rates):
    """Why the shipment received its outcome: the lines that decided it."""

    if not invoices:
        return ["No carrier invoice belongs to this shipment, so there is nothing to audit."]

    if outcome == MATCHED:
        return ["Every charge and reference agrees with the contract and the execution documents."]

    reasons = []

    if not rates:
        reasons.append("No rate confirmation belongs to this shipment: the invoice cannot be matched against a contract.")

    deciding = [item for item in charges if item["outcome"] == outcome] + [
        item for item in checks if item["outcome"] == outcome and outcome != INSUFFICIENT_EVIDENCE
    ]

    for item in deciding:
        reasons.append(f"{item.get('charge') or item.get('check')}: {item['explanation']}")

    return list(dict.fromkeys(reasons))
