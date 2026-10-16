"""
LogiDoc-RAG deterministic shipment calculations

Answers questions whose answer has to be calculated from document
fields. The arithmetic is done here, never by the language model, and
every answer cites the exact field text it used.

    quantities  expected pieces (BOL "Number of Pieces") and delivered
                pieces (POD "Number of Pieces Delivered"): each count,
                shortage, over-delivery, difference, whether the counts
                match, and whether all pieces were delivered. Damaged,
                refused, rejected or returned pieces only from a POD
                field that counts them (never the delivered count)
    delivery    expected delivery date (BOL "Expected Delivery Date")
                and actual delivery date (POD "Delivery Date"): on
                time, days late, days early; pickup date (BOL "Pickup
                Date") to actual delivery: days between them
    combined    a quantity and a delivery-timing question together
                ("Were all pieces delivered on time?"): both are
                calculated, and the answer is Yes only if both hold
    invoice     whether the line items add up to "Total Invoice Amount"

Conventions:
    days late      actual delivery date minus expected delivery date,
                   in calendar days: positive = late, 0 = on time,
                   negative = early (reported as that many days early).
                   Delivered early counts as on time, so "on time or
                   early" is answered like "on time". Either-or
                   questions ("late or on time?") are answered with
                   the result, without Yes or No; so are negated
                   questions ("was it not on time?") and thresholds
                   ("more than 2 days late?"), which are not judged.
    days between   actual delivery date minus pickup date, in calendar
    pickup and     days; a delivery date before the pickup date is a
    delivery       contradiction and is not calculated
    shortage       expected pieces minus delivered pieces, if positive
    over-delivery  delivered pieces minus expected pieces, if positive

Every field is read only from documents of its own type (a BOL's
pickup date is never taken for an expected delivery date, an invoice's
billing or due date never for a delivery date) and only from the
records passed in (one shipment's scope). A POD "Delivery Date"
qualified as expected, promised, required, scheduled and so on is not
the actual delivery date. A field found with different values, for
example two BOLs listing different piece counts, is a conflict; a field
not found is missing; a value that cannot be parsed, including a date
written in a form that is not unambiguous (10/05/2026), is unreadable.
In each case, and for an invoice with amounts that are not known line
items, nothing is calculated: the result is an abstention that explains
why. Missing values are never treated as 0.

A question is answered here only when every part of it can be
calculated. Pickup timing ("Was the pickup on time?") cannot be, so it
is an abstention, never a delivery-timing answer. Questions whose
wording is not recognised here return None and are left to the caller.

calculate(question, records) returns None when the question is not a
calculation handled here, otherwise a Calculation.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime


# ============================================================
# RESULTS
# ============================================================

ANSWERED = "answered"
ABSTAINED = "abstained"


@dataclass
class FieldValue:
    """
    One value found in a document: the parsed value (None when it
    cannot be read), the source text it was read from (e.g. "Number of
    Pieces: 48"), the value as written, the record and its position in
    the record's text.
    """

    value: object
    text: str
    raw: str
    record: dict
    start: int


@dataclass
class Calculation:
    """
    status: ANSWERED or ABSTAINED. evidence: the FieldValues the answer
    was calculated from (empty for an abstention). reason: why an
    abstention could not answer (its message without the lead-in).
    value: the calculated number (shortage, days late, ...).
    """

    status: str
    answer: str
    evidence: list = field(default_factory=list)
    reason: str = ""
    value: object = None


def abstention(reason, lead="I cannot calculate this"):

    return Calculation(ABSTAINED, f"{lead}: {reason}.", reason=reason)


# ============================================================
# FIELDS
# ============================================================

MONTHS = (
    "January|February|March|April|May|June|July|August|September|"
    "October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
    "Oct|Nov|Dec"
)

WEEKDAYS = (
    "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    "Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun"
)

DAY = r"\d{1,2}(?:st|nd|rd|th)?"

VALUE_PATTERNS = {
    "count": r"(\d{1,3}(?:,\d{3})+|\d+)(?![\d.,]*\d)",
    # Month names only (or ISO): 10/05/2026 could be May or October
    "date": (
        rf"((?:{MONTHS})\.?\s+{DAY},?\s+\d{{4}}"
        rf"|{DAY}\s+(?:{MONTHS})\.?,?\s+\d{{4}}"
        r"|\d{4}-\d{2}-\d{2})(?!\d)"
    ),
    "money": r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)",
}

VALUES = {
    kind: re.compile(pattern, re.IGNORECASE)
    for kind, pattern in VALUE_PATTERNS.items()
}

# A value that starts like a date but is not one of the forms above
DATE_LIKE = re.compile(rf"\d|(?:{MONTHS}|{WEEKDAYS})\b", re.IGNORECASE)

DATE_FORMATS = ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d")

# Words that make a POD "Delivery Date" something other than the date
# the delivery actually happened
NOT_ACTUAL_DELIVERY = (
    "Expected", "Estimated", "Est.", "Est", "Scheduled", "Rescheduled",
    "Requested", "Planned", "Promised", "Required", "Target", "Targeted",
    "Original", "Revised", "Committed", "Guaranteed", "Appointment",
    "Appt.", "Appt", "Anticipated", "Projected", "Proposed", "Desired",
    "Preferred", "Quoted", "Agreed", "Contracted", "Contract", "Latest",
    "Earliest", "Tentative", "Approximate", "Approx.", "Due", "Must",
)

PIECE_CONDITIONS = ("damaged", "refused", "rejected", "returned")

# name: (document types, labels, value kind, description,
#        words that must not directly precede the label)
FIELDS = {
    "expected_pieces": (
        {"bol"},
        ("Number of Pieces", "Total Pieces", "Piece Count", "Pieces"),
        "count",
        "expected piece count (BOL Number of Pieces)",
        (),
    ),
    "delivered_pieces": (
        {"pod"},
        ("Number of Pieces Delivered", "Pieces Delivered Count",
         "Pieces Delivered", "Quantity Delivered", "Delivered Pieces"),
        "count",
        "delivered piece count (POD Number of Pieces Delivered)",
        (),
    ),
    "expected_date": (
        {"bol"},
        ("Expected Delivery Date", "Expected Delivery"),
        "date",
        "expected delivery date (BOL Expected Delivery Date)",
        (),
    ),
    "actual_date": (
        {"pod"},
        ("Actual Delivery Date", "Delivery Date"),
        "date",
        "actual delivery date (POD Delivery Date)",
        NOT_ACTUAL_DELIVERY,
    ),
    "pickup_date": (
        {"bol"},
        ("Pickup Date", "Pick-up Date", "Pick Up Date"),
        "date",
        "pickup date (BOL Pickup Date)",
        (),
    ),
    "base_freight": (
        {"invoice"}, ("Base Freight",), "money",
        "invoice Base Freight", (),
    ),
    "fuel_surcharge": (
        {"invoice"}, ("Fuel Surcharge",), "money",
        "invoice Fuel Surcharge", (),
    ),
    "detention_charge": (
        {"invoice"}, ("Detention Charge",), "money",
        "invoice Detention Charge", (),
    ),
    "other_charges": (
        {"invoice"}, ("Other Accessorial Charges",), "money",
        "invoice Other Accessorial Charges", (),
    ),
    "invoice_total": (
        {"invoice"}, ("Total Invoice Amount",), "money",
        "invoice Total Invoice Amount", (),
    ),
    # Amounts that may only repeat the total or the line-item sum
    "invoice_summary": (
        {"invoice"},
        ("Total Amount Due", "Amount Due", "Balance Due", "Total Due",
         "Amount Payable", "Total Payable", "Invoice Total", "Grand Total",
         "Total Amount", "Total Charges", "Subtotal", "Sub Total"),
        "money",
        "invoice summary amount",
        (),
    ),
}

for condition in PIECE_CONDITIONS:

    title = condition.capitalize()

    FIELDS[f"{condition}_pieces"] = (
        {"pod"},
        (f"Number of {title} Pieces", f"Number of Pieces {title}",
         f"{title} Pieces", f"Pieces {title}", f"{title} Quantity",
         f"Quantity {title}", f"{title} Count"),
        "count",
        f"{condition} piece count (POD)",
        (),
    )

INVOICE_LINE_ITEMS = (
    ("base_freight", "Base Freight"),
    ("fuel_surcharge", "Fuel Surcharge"),
    ("detention_charge", "Detention Charge"),
    ("other_charges", "Other Accessorial Charges"),
)

# Free-text POD fields quoted when a piece condition is not counted
CONDITION_NOTES = {
    "damaged": ("Damage Report", "Damage Notes", "Damages", "Exceptions"),
}

DEFAULT_NOTES = ("Exceptions",)


def label_pattern(label, excluded=()):
    """
    The label followed by a colon; its value starts at the match end.
    """

    words = r"[\s-]+".join(re.escape(word) for word in re.split(r"[\s-]+", label))

    lookbehinds = "".join(f"(?<!{re.escape(word)} )" for word in excluded)

    return re.compile(
        rf"(?<![A-Za-z]){lookbehinds}{words}\s*:\s*",
        re.IGNORECASE
    )


PATTERNS = {
    name: [label_pattern(label, excluded) for label in labels]
    for name, (_, labels, _, _, excluded) in FIELDS.items()
}


def parse_date(raw):

    text = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE)
    text = " ".join(text.replace(".", " ").replace(",", " ").split())
    text = re.sub(r"\bSept\b", "Sep", text, flags=re.IGNORECASE)

    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue

    return None


def parse_value(kind, raw):

    if kind in ("count", "money"):
        number = float(raw.replace(",", ""))
        return int(number) if kind == "count" else round(number, 2)

    return parse_date(raw)


def date_like_text(text, start):
    """
    The date-like value at start, as written: its first word and the
    following words that are numbers or month or weekday names.
    """

    words = text[start:].split(" ")[:4]
    kept = words[:1]

    for word in words[1:]:

        if not DATE_LIKE.match(word):
            break

        kept.append(word)

    return " ".join(kept).rstrip(".,;")


def read_field(name, records):
    """
    Every value of a field in records of the field's document types.
    Overlapping label matches (e.g. "Pieces" inside "Number of
    Pieces") count once. A date field whose value looks like a date
    but cannot be read is returned with value None, so that it can
    never be skipped silently.
    """

    types = FIELDS[name][0]
    kind = FIELDS[name][2]

    found = {}

    for index, record in enumerate(records):

        if record.get("type") not in types:
            continue

        text = record["normalized"]

        for pattern in PATTERNS[name]:

            for label in pattern.finditer(text):

                value = VALUES[kind].match(text, label.end())

                if value:
                    start = value.start(1)
                    raw = value.group(1).strip()
                    parsed = parse_value(kind, raw)
                    end = value.end()
                elif kind == "date" and DATE_LIKE.match(text, label.end()):
                    start = label.end()
                    raw = date_like_text(text, start)
                    parsed = None
                    end = start + len(raw)
                else:
                    continue

                position = (index, start)

                if position in found:
                    continue

                found[position] = FieldValue(
                    value=parsed,
                    text=" ".join(text[label.start():end].split()),
                    raw=raw,
                    record=record,
                    start=start,
                )

    # Record order, then position in the text: deterministic
    return [found[position] for position in sorted(found)]


def record_source(record):

    metadata = record.get("metadata") or {}

    return f"{metadata.get('source', 'unknown source')} page {metadata.get('page', '?')}"


def source_of(item):

    return record_source(item.record)


def field_problem(name, values):
    """
    Why a field cannot be used when it is missing, unreadable or
    conflicting; None when it has exactly one value.
    """

    description = FIELDS[name][3]

    if not values:
        return f"the {description} was not found in this shipment's documents"

    unreadable = [item for item in values if item.value is None]

    if unreadable:
        return (
            f"the {description} '{unreadable[0].raw}' "
            f"({source_of(unreadable[0])}) could not be read"
        )

    if len({item.value for item in values}) > 1:
        listing = ", ".join(f"{item.raw} ({source_of(item)})" for item in values)
        return (
            "the documents give different values for the "
            f"{description}: {listing}"
        )

    return None


def read_required(records, *names):
    """
    Returns ({name: [FieldValue]}, None) when every field has exactly
    one value, otherwise (None, abstention Calculation).
    """

    readings = {}

    for name in names:

        values = read_field(name, records)
        problem = field_problem(name, values)

        if problem:
            return None, abstention(problem)

        readings[name] = values

    return readings, None


def value_of(readings, name):

    return readings[name][0].value


def raw_of(readings, name):

    return readings[name][0].raw


def evidence_of(readings, *names):

    return [item for name in names for item in readings[name]]


# Free text after a label ends where the next "Label:" starts
NEXT_LABEL = re.compile(r"\s(?:[A-Z][\w&/-]*\s){0,3}[A-Z][\w&/-]*:")


def read_notes(records, labels):
    """
    (label, text, record) for free-text POD fields such as
    "Damage Report: No visible damage reported."
    """

    notes = {}

    for record in records:

        if record.get("type") != "pod":
            continue

        text = record["normalized"]

        for label in labels:

            for match in label_pattern(label).finditer(text):

                rest = text[match.end():match.end() + 200]
                end = NEXT_LABEL.search(rest)
                note = (rest[:end.start()] if end else rest).strip()

                if note:
                    notes.setdefault(
                        (label, note, record_source(record)),
                        (label, note, record)
                    )

    return list(notes.values())


# ============================================================
# QUESTIONS
# ============================================================

YES_NO_WORDS = {"was", "were", "is", "are", "did", "do", "does", "has", "have", "had"}

NEGATION = re.compile(r"\bnot\b|n['’]t\b|\bnever\b")


def is_yes_no(question_lower):

    words = re.findall(r"[a-z]+", question_lower)

    return bool(words) and words[0] in YES_NO_WORDS


def answer_prefix(yes_no, condition):

    if not yes_no:
        return ""

    return "Yes. " if condition else "No. "


def is_invoice_sum_question(question_lower):

    return (
        "invoice" in question_lower
        and bool(
            re.search(
                r"\bline items?\b|\badds? up\b|\bsum\b",
                question_lower
            )
        )
    )


PIECES = r"\bpieces?\b|\bquantit(?:y|ies)\b"

# Whether the whole shipment arrived, without the word "pieces"
COMPLETE = r"\bin full\b|\bcomplete(?:ly)?\b|\bfully\b|\bevery piece\b|\bin its entirety\b"

EXPECTED_TERMS = (
    r"\bexpected\b|\bshipped\b|\bplanned\b|\bordered\b|\bbol\b"
    r"|\bbill of lading\b|\bloaded\b|\bpicked up\b"
)

DELIVERED_TERMS = r"\bdeliver\w*|\breceived\b|\barriv\w*|\bpod\b|\bproof of delivery\b"

MATCH_WORDS = (
    r"\bmatch(?:es|ed|ing)?\b|\bconsistent\b|\bagree(?:s|d)?\b"
    r"|\bsame (?:number|count|quantity|amount|as)\b|\bequal(?:s|led)?\b"
    r"|\bcorrespond\w*|\breconcile\w*|\blines? up\b|\btall(?:y|ies)\b"
)

MISMATCH_WORDS = (
    r"\bmismatch\w*|\bdiffer\w*|\bdiscrepanc\w*|\binconsistent\b"
    r"|\bdisagree\w*|\bvariance\b|\bdeviat\w*"
)

NEUTRAL_COMPARISON_WORDS = r"\bcompar\w*|\bversus\b|\bvs\b"

OVER_WORDS = (
    r"\bover[\s-]?deliver\w*|\bextra\b|\bexcess\b|\bsurplus\b"
    r"|\btoo many\b|\boverage\b"
)

# "not delivered", "weren't delivered", "didn't arrive", "never arrived"
NOT_DELIVERED = (
    r"(?:\b(?:not|never)|n['’]t) (?:been )?"
    r"(?:deliver(?:ed)?|receive[ds]?|arrive[ds]?)\b"
)

MISSING_WORDS = (
    r"\bmissing\b|\bshort(?:age|fall)?\b|\blost\b|\bundelivered\b"
    rf"|{NOT_DELIVERED}|\bunaccounted\b"
)

ALL_DELIVERED = (
    r"\ball\b.*\b(?:deliver|arriv|receiv)|\bfully delivered\b"
    rf"|\bcompletely delivered\b|{COMPLETE}"
)

COUNT_WORDS = r"\bhow many\b|\bnumber of\b|\bcount\b|\bquantity\b"

CONDITION_WORDS = (
    ("damaged", r"\bdamag\w*|\bbroken\b|\bcrushed\b|\bdefective\b"),
    ("refused", r"\brefus\w*"),
    ("rejected", r"\breject\w*"),
    ("returned", r"\breturn\w*"),
)


def count_side(text):
    """
    Which count the text is about: "expected" (BOL), "delivered"
    (POD), "both" or None.
    """

    expected = bool(re.search(EXPECTED_TERMS, text))
    delivered = bool(re.search(DELIVERED_TERMS, text))

    if expected and delivered:
        return "both"

    return "expected" if expected else "delivered" if delivered else None


def comparative_intent(terms, more):
    """
    "more" / "fewer" / "less" comparing the two counts: which way.
    None when the comparison is not between these two counts ("more
    than 40 pieces").
    """

    if re.search(r"\bthan\b", terms):

        left, right = re.split(r"\bthan\b", terms, maxsplit=1)

        # A threshold, not the other count
        if re.match(r"\s*\d", right):
            return None

        left_side, right_side = count_side(left), count_side(right)

        if "both" in (left_side, right_side):
            return "difference"

        if left_side and right_side:

            if left_side == right_side:
                return None

            subject = left_side

        elif left_side:
            subject = left_side

        elif right_side:
            subject = "delivered" if right_side == "expected" else "expected"

        else:
            return "difference"

    else:

        subject = count_side(terms)

        if subject in (None, "both"):
            return "difference"

    if subject == "delivered":
        return "over" if more else "missing"

    return "missing" if more else "over"


def quantity_intent(question_lower):

    q = question_lower

    if "invoice" in q or re.search(r"\bline items?\b", q):
        return None

    # "pieces to be delivered" are the expected pieces
    terms = re.sub(r"\b(?:to be|be|for) deliver\w*", " expected ", q)

    if not (
        re.search(PIECES, q)
        or (re.search(COMPLETE, q) and re.search(DELIVERED_TERMS, terms))
    ):
        return None

    # Never answered with the delivered count
    for condition, words in CONDITION_WORDS:

        if re.search(words, q):
            return condition

    comparative = re.search(r"\b(more|fewer|less)\b", q)

    if comparative:
        return comparative_intent(terms, comparative.group(1) == "more")

    if re.search(rf"{MATCH_WORDS}|{MISMATCH_WORDS}|{NEUTRAL_COMPARISON_WORDS}", q):
        return "difference"

    over = re.search(OVER_WORDS, q)
    missing = re.search(MISSING_WORDS, q)

    if over and missing:
        return "discrepancy"

    if over:
        return "over"

    if missing:
        return "missing"

    if re.search(ALL_DELIVERED, q):
        return "all_delivered"

    if re.search(COUNT_WORDS, q):

        side = count_side(terms)

        return "difference" if side == "both" else side

    return None


def match_polarity(question_lower):
    """
    For a yes/no question comparing the counts: "match" (Yes when
    they are equal), "mismatch" (Yes when they differ) or None.
    """

    match = bool(re.search(MATCH_WORDS, question_lower))
    mismatch = bool(re.search(MISMATCH_WORDS, question_lower))

    if match != mismatch:
        return "match" if match else "mismatch"

    return None


def quantity_prefix(intent, question_lower, yes_no):

    # "not delivered" names the missing pieces; it does not negate
    negated = bool(NEGATION.search(re.sub(NOT_DELIVERED, " ", question_lower)))

    if intent == "all_delivered":
        return not negated

    if intent == "difference":
        return match_polarity(question_lower) if yes_no and not negated else None

    if intent == "discrepancy":
        return "mismatch" if yes_no and not negated else None

    return yes_no


NOT_DELIVERY_EVENTS = r"\binvoice\w*|\bbilling\b|\bbilled\b|\bdue\b|\bpayments?\b|\bpaid\b"

PICKUP_EVENTS = r"\bpick[\s-]?ups?\b|\bpicked[\s-]up\b|\bdepart\w*|\bdispatch\w*"

TIMING_WORDS = (
    ("early", r"\bearly\b|\bearlier\b|\bahead of (?:schedule|time)\b|\bbefore the expected\b"),
    ("late", r"\blate\b|\blater than\b|\bdelay(?:ed|s)?\b|\boverdue\b"
             r"|\bbehind schedule\b|\b(?:after|past|beyond) the expected\b"),
    ("on_time", r"\bon[\s-]time\b|\bon schedule\b|\bpunctual\w*"),
)

# "more than 2 days late": a threshold this calculation does not judge
THRESHOLD = r"\b(?:more|less|fewer) than\b|\bat (?:least|most)\b|\b(?:over|under) \d|\bexceed\w*"

PICKUP_TIMING = "pickup_timing"

PICKUP_TIMING_LEAD = "I cannot answer whether the pickup was on time"

PICKUP_TIMING_REASON = (
    "pickup timing is not calculated (only the delivery date is compared "
    "with the expected delivery date), and the delivery dates do not "
    "show it"
)


def date_intent(question_lower):

    q = question_lower

    if re.search(NOT_DELIVERY_EVENTS, q):
        return None

    timing = {name for name, words in TIMING_WORDS if re.search(words, q)}

    # Pickup timing is not delivery timing
    if timing and re.search(PICKUP_EVENTS, q):
        return PICKUP_TIMING

    if (
        not timing
        and re.search(rf"{PICKUP_EVENTS}|\btransit\b|\bhow long\b", q)
        and re.search(r"\bdeliver\w*|\btransit\b|\barriv\w*", q)
        and re.search(r"\bdays?\b|\bhow long\b|\bduration\b", q)
    ):
        return "transit"

    if re.search(r"\bexpected\b", q) and re.search(r"\bactual\b", q) and re.search(r"\bdates?\b", q):
        return "dates"

    # Early counts as on time
    if timing == {"on_time", "early"}:
        return "on_time"

    # "late or on time", "early or late": the result, without Yes/No
    if len(timing) > 1:
        return "timing"

    return timing.pop() if timing else None


def timing_prefix(intent, yes_no, negated):

    if negated:
        return False

    if intent == "on_time":
        return True

    return yes_no if intent in ("late", "early") else False


# ============================================================
# QUANTITIES
# ============================================================

def pieces(count):

    return f"{count:,} piece" + ("" if count == 1 else "s")


def were(count):

    return "was" if count == 1 else "were"


def quantity_answer(intent, records, prefix=False):
    """
    prefix: whether the answer starts with Yes or No. For "difference"
    and "discrepancy": "match" (Yes when the counts are equal),
    "mismatch" (Yes when they differ) or None.
    """

    if intent in PIECE_CONDITIONS:
        return condition_answer(intent, records, prefix)

    if intent == "expected":

        readings, problem = read_required(records, "expected_pieces")

        if problem:
            return problem

        expected = value_of(readings, "expected_pieces")

        return Calculation(
            ANSWERED,
            f"The BOL lists {pieces(expected)}.",
            evidence_of(readings, "expected_pieces"),
            value=expected,
        )

    if intent == "delivered":

        readings, problem = read_required(records, "delivered_pieces")

        if problem:
            return problem

        delivered = value_of(readings, "delivered_pieces")

        return Calculation(
            ANSWERED,
            f"{pieces(delivered)} {were(delivered)} delivered.",
            evidence_of(readings, "delivered_pieces"),
            value=delivered,
        )

    readings, problem = read_required(records, "expected_pieces", "delivered_pieces")

    if problem:
        return problem

    expected = value_of(readings, "expected_pieces")
    delivered = value_of(readings, "delivered_pieces")
    shortage = expected - delivered

    counts = (
        f"the BOL lists {pieces(expected)} and the POD records "
        f"{pieces(delivered)} delivered"
    )

    if intent == "all_delivered":

        if shortage > 0:
            statement = (
                f"The POD records {pieces(delivered)} delivered, "
                f"{shortage:,} fewer than the {pieces(expected)} on the "
                f"BOL (a shortage of {shortage:,})."
            )
        elif shortage < 0:
            statement = (
                f"The POD records {pieces(delivered)} delivered, "
                f"{-shortage:,} more than the {pieces(expected)} on the "
                f"BOL (an over-delivery of {-shortage:,})."
            )
        else:
            statement = (
                f"The POD records {pieces(delivered)} delivered, "
                f"matching the {pieces(expected)} on the BOL."
            )

        answer = answer_prefix(prefix, shortage <= 0) + statement

    elif intent == "missing":

        if shortage > 0:
            answer = (
                answer_prefix(prefix, True)
                + f"{pieces(shortage)} {were(shortage)} missing: the BOL "
                f"lists {pieces(expected)}, but the POD records "
                f"{pieces(delivered)} delivered (a shortage of {shortage:,})."
            )
        elif shortage < 0:
            answer = (
                answer_prefix(prefix, False)
                + f"No pieces were missing; {pieces(-shortage)} more "
                f"{were(-shortage)} delivered than the BOL lists: {counts} "
                f"(an over-delivery of {-shortage:,})."
            )
        else:
            answer = (
                answer_prefix(prefix, False)
                + f"No pieces were missing: {counts} (a difference of 0)."
            )

    elif intent == "over":

        if shortage < 0:
            answer = (
                answer_prefix(prefix, True)
                + f"{pieces(-shortage)} more {were(-shortage)} delivered "
                f"than the BOL lists: {counts} (an over-delivery of "
                f"{-shortage:,})."
            )
        elif shortage > 0:
            answer = (
                answer_prefix(prefix, False)
                + f"No pieces were over-delivered; {pieces(shortage)} "
                f"{were(shortage)} missing: {counts} (a shortage of "
                f"{shortage:,})."
            )
        else:
            answer = (
                answer_prefix(prefix, False)
                + f"No pieces were over-delivered: {counts} (a difference "
                "of 0)."
            )

    else:

        if shortage > 0:
            result = f"a difference of {shortage:,} (a shortage of {shortage:,})"
        elif shortage < 0:
            result = f"a difference of {-shortage:,} (an over-delivery of {-shortage:,})"
        else:
            result = "a difference of 0; the counts match"

        if prefix == "match":
            lead = answer_prefix(True, shortage == 0)
        elif prefix == "mismatch":
            lead = answer_prefix(True, shortage != 0)
        else:
            lead = ""

        answer = (
            lead
            + f"The BOL lists {pieces(expected)} and the POD records "
            f"{pieces(delivered)} delivered: {result}."
        )

    return Calculation(
        ANSWERED,
        answer,
        evidence_of(readings, "expected_pieces", "delivered_pieces"),
        value=shortage,
    )


def condition_answer(condition, records, prefix=False):
    """
    Damaged, refused, rejected or returned pieces: only from a POD
    field that counts them. The delivered count says nothing about
    them, so without such a field this is an abstention (quoting a
    POD note such as the Damage Report, if there is one).
    """

    name = f"{condition}_pieces"
    values = read_field(name, records)

    if values:

        problem = field_problem(name, values)

        if problem:
            return abstention(problem)

        count = values[0].value

        return Calculation(
            ANSWERED,
            answer_prefix(prefix, count > 0)
            + f"The POD records {count:,} {condition} piece"
            + ("" if count == 1 else "s") + ".",
            values,
            value=count,
        )

    reason = f"this shipment's documents do not give a number of {condition} pieces"

    notes = read_notes(records, CONDITION_NOTES.get(condition, DEFAULT_NOTES))

    if notes:
        reason += "; " + "; ".join(
            f'the POD {label} reads "{note}" ({record_source(record)})'
            for label, note, record in notes
        )

    return abstention(reason, lead="I cannot answer this")


# ============================================================
# DELIVERY DATES
# ============================================================

def days(count):

    return f"{count:,} day" + ("" if count == 1 else "s")


def date_answer(intent, records, prefix=False):
    """
    prefix: whether the answer starts with Yes or No (on time, late
    and early questions only).
    """

    if intent == "transit":

        readings, problem = read_required(records, "pickup_date", "actual_date")

        if problem:
            return problem

        pickup = raw_of(readings, "pickup_date")
        actual = raw_of(readings, "actual_date")
        transit = (value_of(readings, "actual_date") - value_of(readings, "pickup_date")).days

        if transit < 0:
            return abstention(
                "the delivery date "
                f"({actual}, {source_of(readings['actual_date'][0])}) is "
                f"before the pickup date ({pickup}, "
                f"{source_of(readings['pickup_date'][0])})"
            )

        if transit == 0:
            answer = (
                "0 days passed between pickup and delivery: the shipment "
                f"was picked up and delivered on {actual}."
            )
        else:
            answer = (
                f"{days(transit)} passed between pickup and delivery: "
                f"picked up on {pickup} and delivered on {actual}."
            )

        return Calculation(
            ANSWERED,
            answer,
            evidence_of(readings, "pickup_date", "actual_date"),
            value=transit,
        )

    readings, problem = read_required(records, "expected_date", "actual_date")

    if problem:
        return problem

    expected = raw_of(readings, "expected_date")
    actual = raw_of(readings, "actual_date")
    late = (value_of(readings, "actual_date") - value_of(readings, "expected_date")).days

    after = f"{days(late)} after the expected delivery date of {expected}"
    before = f"{days(-late)} before the expected delivery date of {expected}"

    if intent in ("on_time", "timing"):

        if late > 0:
            statement = f"The shipment was delivered {days(late)} late: on {actual}, {after}."
        elif late < 0:
            statement = f"The shipment was delivered {days(-late)} early: on {actual}, {before}."
        else:
            statement = (
                f"The shipment was delivered on time: on {actual}, the "
                "expected delivery date (0 days late)."
            )

        answer = answer_prefix(prefix, late <= 0) + statement

    elif intent == "late":

        if late > 0:
            answer = (
                answer_prefix(prefix, True)
                + f"The delivery was {days(late)} late: delivered on {actual}, {after}."
            )
        elif late < 0:
            answer = (
                answer_prefix(prefix, False)
                + f"The delivery was not late; it was {days(-late)} early: "
                f"delivered on {actual}, {before}."
            )
        else:
            answer = (
                answer_prefix(prefix, False)
                + f"The delivery was not late (0 days): delivered on {actual}, "
                "the expected delivery date."
            )

    elif intent == "early":

        if late < 0:
            answer = (
                answer_prefix(prefix, True)
                + f"The delivery was {days(-late)} early: delivered on {actual}, {before}."
            )
        elif late > 0:
            answer = (
                answer_prefix(prefix, False)
                + f"The delivery was not early; it was {days(late)} late: "
                f"delivered on {actual}, {after}."
            )
        else:
            answer = (
                answer_prefix(prefix, False)
                + f"The delivery was not early (0 days): delivered on "
                f"{actual}, the expected delivery date."
            )

    else:

        if late > 0:
            timing = f"{days(late)} late"
        elif late < 0:
            timing = f"{days(-late)} early"
        else:
            timing = "on time (0 days late)"

        answer = (
            f"The expected delivery date was {expected} and the actual "
            f"delivery date was {actual}: {timing}."
        )

    return Calculation(
        ANSWERED,
        answer,
        evidence_of(readings, "expected_date", "actual_date"),
        value=late,
    )


# ============================================================
# QUANTITY AND DELIVERY TIMING TOGETHER
# ============================================================

# Whether the timing a yes/no question asks about holds, by days late
TIMING_HOLDS = {
    "on_time": lambda late: late <= 0,
    "late": lambda late: late > 0,
    "early": lambda late: late < 0,
}


def combined_answer(quantity, timing, records, prefix=False):
    """
    Both parts, or the first abstention: a question is never answered
    from only one of them. prefix: Yes only when all pieces were
    delivered and the timing asked about holds.
    """

    parts = [quantity_answer(quantity, records), date_answer(timing, records)]

    for part in parts:

        if part.status != ANSWERED:
            return part

    answer = " ".join(part.answer for part in parts)

    if prefix:
        holds = parts[0].value <= 0 and TIMING_HOLDS[timing](parts[1].value)
        answer = answer_prefix(True, holds) + answer

    evidence = {}

    for part in parts:

        for item in part.evidence:
            evidence.setdefault((id(item.record), item.start), item)

    return Calculation(ANSWERED, answer, list(evidence.values()))


# ============================================================
# INVOICE LINE ITEMS VS TOTAL
# ============================================================

CURRENCY = r"(?:US\$|USD|CAD|EUR|GBP|\$|€|£)"

AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"

NOT_MONEY_UNITS = (
    r"%|lbs?\b|kgs?\b|pounds\b|kilograms\b|miles?\b|mi\b|km\b|hrs?\b"
    r"|hours?\b|ft\b|feet\b|cwt\b|pcs\b|pieces\b|pallets?\b"
)

CHARGE_WORDS = (
    "fees?|charges?|surcharges?|accessorials?|freight|tolls?|detention|"
    "layover|lumper|tax|taxes|vat|gst|hst|discounts?|credits?|"
    "adjustments?|amount|subtotal|total|due|balance|costs?|deposit|"
    "payments?|paid|rebate|deductions?|allowance|penalty|storage|"
    "demurrage|liftgate|redelivery|reconsignment|insurance|handling|rate"
)

# (pattern, whether to show the whole match or only the number)
MONEY_PATTERNS = (
    # A currency symbol or code before it: $75.00, USD 120
    (re.compile(rf"{CURRENCY}\s*({AMOUNT})(?![\d.,]*\d)", re.IGNORECASE), True),
    # A currency code after it: 120.00 USD
    (re.compile(rf"(?<![\w.,])({AMOUNT})\s*(?:USD|CAD|EUR|GBP)\b", re.IGNORECASE), True),
    # Cents: 120.00
    (re.compile(
        rf"(?<![\w.,/])(\d{{1,3}}(?:,\d{{3}})*\.\d{{2}}|\d+\.\d{{2}})"
        rf"(?![\d.,]*\d)(?!\s*(?:{NOT_MONEY_UNITS}))",
        re.IGNORECASE
    ), False),
    # After a charge-like label: "Lumper Fee: 120"
    (re.compile(
        rf"\b(?:{CHARGE_WORDS})\s*(?:\([^)]*\))?\s*:\s*(?:{CURRENCY}\s*)?"
        rf"[-(]?\s*({AMOUNT})(?![\d.,]*\d)(?!\s*(?:{NOT_MONEY_UNITS}))",
        re.IGNORECASE
    ), False),
)


def money_amounts(text):
    """
    {position of the number: the amount as written} for every
    monetary amount in text, with or without a currency symbol.
    """

    found = {}

    for pattern, whole in MONEY_PATTERNS:

        for match in pattern.finditer(text):

            shown = match.group(0) if whole else match.group(1)

            found.setdefault(match.start(1), re.sub(r"\$\s+", "$", " ".join(shown.split())))

    return dict(sorted(found.items()))


def invoice_sum_answer(records):

    names = [name for name, _ in INVOICE_LINE_ITEMS] + ["invoice_total"]

    readings, problem = read_required(records, *names)

    if problem:
        return problem

    items = {
        label: value_of(readings, name)
        for name, label in INVOICE_LINE_ITEMS
    }

    total = value_of(readings, "invoice_total")
    line_sum = round(sum(items.values()), 2)

    # Every amount on the invoice documents must be one of these
    # fields, or the sum could leave a charge out
    accounted = {
        (id(item.record), item.start)
        for name in names
        for item in readings[name]
    }

    # A summary such as "Amount Due" may only repeat the total or the
    # line-item sum; it is never added
    for item in read_field("invoice_summary", records):

        if all(abs(item.value - amount) >= 0.005 for amount in (total, line_sum)):
            return abstention(
                f"the invoice's \"{item.text}\" ({source_of(item)}) is "
                f"neither the Total Invoice Amount (${total:,.2f}) nor the "
                f"sum of the line items (${line_sum:,.2f})",
                lead="I cannot verify the invoice total"
            )

        accounted.add((id(item.record), item.start))

    unknown = [
        f"{shown} ({record_source(record)})"
        for record in records
        if record.get("type") == "invoice"
        for start, shown in money_amounts(record["normalized"]).items()
        if (id(record), start) not in accounted
    ]

    if unknown:
        return abstention(
            "the invoice lists amount(s) that are not known line items: "
            + ", ".join(unknown),
            lead="I cannot verify the invoice total"
        )

    breakdown = " + ".join(
        f"{label} ${value:,.2f}"
        for label, value in items.items()
    )

    if abs(line_sum - total) < 0.005:
        answer = (
            f"Yes. The invoice line items ({breakdown}) add up to "
            f"${line_sum:,.2f}, which equals the Total Invoice Amount of "
            f"${total:,.2f}."
        )
    else:
        answer = (
            f"No. The invoice line items ({breakdown}) add up to "
            f"${line_sum:,.2f}, but the Total Invoice Amount is "
            f"${total:,.2f}, a difference of ${abs(total - line_sum):,.2f}."
        )

    return Calculation(ANSWERED, answer, evidence_of(readings, *names), value=line_sum)


# ============================================================
# ENTRY POINT
# ============================================================

def calculate(question, records):
    """
    A Calculation for a supported calculation question, using only
    the given records (one shipment's scope); None otherwise.
    """

    question_lower = " ".join((question or "").lower().split())
    yes_no = is_yes_no(question_lower)

    if is_invoice_sum_question(question_lower):
        return invoice_sum_answer(records)

    quantity = quantity_intent(question_lower)
    timing = date_intent(question_lower)

    if timing == PICKUP_TIMING:
        return abstention(PICKUP_TIMING_REASON, lead=PICKUP_TIMING_LEAD)

    # Negated and threshold questions get the result without Yes or No
    negated = bool(
        NEGATION.search(question_lower)
        or (timing and re.search(THRESHOLD, question_lower))
    )

    if quantity and timing:
        return combined_answer(
            quantity,
            timing,
            records,
            yes_no
            and not negated
            and quantity == "all_delivered"
            and timing in TIMING_HOLDS,
        )

    if quantity:
        return quantity_answer(
            quantity,
            records,
            quantity_prefix(quantity, question_lower, yes_no),
        )

    if timing:
        return date_answer(timing, records, timing_prefix(timing, yes_no, negated))

    return None
