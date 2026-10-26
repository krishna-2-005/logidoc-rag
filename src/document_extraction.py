"""
LogiDoc-RAG structured document extraction

Typed extraction of freight documents (BOL, POD, invoice, rate confirmation)
with, for every field, its provenance and an honest extraction state. It reads
the PDF itself (page by page) and applies one deterministic label-pair parser;
no language model is involved and nothing is guessed.

What a field says (ExtractedField)

    name            the schema field
    state           EXTRACTED       a value was read and parsed
                    MISSING         the document has no such field (or the label
                                    has no value); the whole document was readable
                    UNPARSEABLE     the text is there but is not a valid value
                                    of the field's type (the raw text is kept)
                    UNREADABLE      the source could not be read: an image-only
                                    PDF, a damaged file, or a PDF with pages that
                                    have no text, so absence cannot be claimed
                    UNSUPPORTED     the extractor cannot read this field at all
                    CONFLICTING     the document gives different values; the
                                    alternatives are listed and none is chosen
    value           typed (int, float, ISO date text, dict) only when EXTRACTED;
                    None otherwise. Never 0 or "" for a missing value
    raw_text        the text it was read from (at most 200 characters)
    source_file, page   where it was read; the page is None only when it is not
                    known, never a guess
    method          how it was read ("label_pair", "label_pair+symbol", ...)
    validation      valid | warning | invalid | not_checked (deterministic checks)
    basis           why the value can be trusted: "exact_label" (the label matched
                    one of the field's known labels exactly). There is NO numeric
                    confidence: nothing here is calibrated, and none is claimed
    warnings, alternatives

Document status: extracted | partial (some pages have no text) | needs_ocr
(no page has text; there is no OCR) | unreadable (the file cannot be opened).
A document is complete only when its status is extracted, its required fields
are EXTRACTED and nothing needs review (review_reasons is empty).

Extraction never changes an audit: reconciliation keeps reading values through
its own readers and does its own arithmetic. validate_shipment() only reports
inconsistencies between documents of one shipment (carrier, route, currency,
shipment ID, total versus line items); it chooses nothing.
"""

import hashlib
import re
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pymupdf

import pdf_access
from document_loader import classify_document
from identifier_extractor import extract_all_identifiers
from logistics_fields import IDENTIFIER_ALIASES
from shipment_calculations import VALUES, parse_date
from shipment_identity import identifier_key

# The stored-extraction cache key (extraction_store: file hash + version) must
# change whenever the parsing changes. A hand-maintained number is easy to
# forget, so the version is the schema number plus a fingerprint of the source
# of every module the extraction depends on (line endings normalized): any edit
# to them gives a new version, and old stored rows are kept, not reused.
EXTRACTOR_SCHEMA = "1.1"

FINGERPRINT_SOURCES = (
    "document_extraction.py", "document_loader.py", "document_classifier.py", "identifier_extractor.py",
    "logistics_fields.py", "shipment_calculations.py", "shipment_identity.py", "pdf_access.py",
)


# What else decides the extracted text: the PDF library. A new PyMuPDF can read the same file
# differently, so its version is part of the extractor version too.
FINGERPRINT_EXTRA = (f"pymupdf={pymupdf.__version__}",)


def source_fingerprint(paths, extra=()):
    digest = hashlib.sha256()

    for path in paths:
        digest.update(Path(path).name.encode("utf-8") + b"\0")
        digest.update(Path(path).read_bytes().replace(b"\r\n", b"\n") + b"\0")

    for item in extra:
        digest.update(str(item).encode("utf-8") + b"\0")

    return digest.hexdigest()[:12]


EXTRACTOR_VERSION = (
    f"{EXTRACTOR_SCHEMA}+{source_fingerprint((Path(__file__).parent / name for name in FINGERPRINT_SOURCES), FINGERPRINT_EXTRA)}"
)

EXTRACTED = "EXTRACTED"
MISSING = "MISSING"
UNPARSEABLE = "UNPARSEABLE"
UNREADABLE = "UNREADABLE"
UNSUPPORTED = "UNSUPPORTED"
CONFLICTING = "CONFLICTING"

STATES = (EXTRACTED, MISSING, UNPARSEABLE, UNREADABLE, UNSUPPORTED, CONFLICTING)

VALID, WARNING, INVALID, NOT_CHECKED = "valid", "warning", "invalid", "not_checked"

BOL, POD, INVOICE, RATE = "BOL", "POD", "INVOICE", "RATE_CONFIRMATION"
DOCUMENT_TYPES = (BOL, POD, INVOICE, RATE)

# Document status
S_EXTRACTED, S_PARTIAL, S_NEEDS_OCR, S_UNREADABLE = "extracted", "partial", "needs_ocr", "unreadable"

MAX_RAW = 200

CURRENCY_CODES = {"USD", "CAD", "EUR", "GBP", "MXN", "AUD", "JPY", "CHF"}
SYMBOL_CURRENCY = {"€": "EUR", "£": "GBP", "US$": "USD"}


# ============================================================
# SCHEMA
# ============================================================

@dataclass(frozen=True)
class Spec:
    kind: str                 # text | party | date | money | count | weight | currency | identifier
    labels: tuple = ()        # exact labels (lower case) that carry the field
    note: str = ""
    max_lines: int = 1        # lines of a value that belong to it; more are not used and need review
    review_if_blank: bool = False   # a blank placeholder where this was expected means a person must look (evidence of receipt)


def labels(*names):
    return tuple(name.lower() for name in names)


PARTY = ("shipper", "consignee")

SCHEMAS = {
    BOL: {
        "shipment_id": Spec("identifier"),
        "shipper": Spec("party", labels("Shipper", "Ship From"), max_lines=4),
        "consignee": Spec("party", labels("Consignee", "Ship To"), max_lines=4),
        "carrier": Spec("text", labels("Carrier")),
        "origin": Spec("text", labels("Origin")),
        "destination": Spec("text", labels("Destination")),
        "pickup_date": Spec("date", labels("Pickup Date")),
        "expected_delivery_date": Spec("date", labels("Expected Delivery Date", "Expected Delivery")),
        "piece_count": Spec("count", labels("Number of Pieces", "Total Pieces", "Piece Count")),
        "weight": Spec("weight", labels("Total Weight", "Weight")),
        "equipment": Spec("text", labels("Equipment")),
        "commodity": Spec("text", labels("Commodity"), max_lines=2),
    },
    POD: {
        "shipment_id": Spec("identifier"),
        "carrier": Spec("text", labels("Carrier")),
        "delivery_date": Spec("date", labels("Delivery Date", "Actual Delivery Date")),
        "delivered_pieces": Spec("count", labels("Number of Pieces Delivered", "Pieces Delivered", "Delivered Pieces", "Quantity Delivered")),
        "delivery_status": Spec("text", labels("Delivery Status")),
        "received_by": Spec("text", labels("Received By"), review_if_blank=True),
        "signature": Spec("text", labels("Signature"), "the printed or typed name next to the signature line", review_if_blank=True),
        "damage_report": Spec("text", labels("Damage Report"), max_lines=3),
        "signature_image": Spec("text", (), "a handwritten signature drawn on the page is not detected"),
    },
    INVOICE: {
        "shipment_id": Spec("identifier"),
        "invoice_number": Spec("text", labels("Invoice Number", "Invoice No", "Invoice #")),
        "invoice_date": Spec("date", labels("Invoice Date", "Billing Date", "Date Issued")),
        "due_date": Spec("date", labels("Due Date", "Payment Due")),
        "payment_terms": Spec("text", labels("Payment Terms")),
        "carrier": Spec("text", labels("Carrier")),
        "origin": Spec("text", labels("Origin")),
        "destination": Spec("text", labels("Destination")),
        "currency": Spec("currency", labels("Currency")),
        "subtotal": Spec("money", labels("Subtotal", "Sub Total")),
        "tax": Spec("money", labels("Tax", "Sales Tax", "VAT", "GST", "HST", "Total Tax")),
        "invoice_total": Spec("money", labels("Total Invoice Amount", "Invoice Total", "Grand Total", "Total Amount", "Amount Due", "Total Due",
                                              "Total", "Total Payable", "Amount Payable")),
    },
    RATE: {
        "shipment_id": Spec("identifier"),
        "carrier": Spec("text", labels("Carrier")),
        "shipper": Spec("party", labels("Shipper", "Ship From"), max_lines=4),
        "consignee": Spec("party", labels("Consignee", "Ship To"), max_lines=4),
        "origin": Spec("text", labels("Origin", "Pickup Location")),
        "destination": Spec("text", labels("Destination", "Delivery Location")),
        "pickup_date": Spec("date", labels("Pickup Date")),
        "delivery_date": Spec("date", labels("Delivery Date", "Expected Delivery Date")),
        "equipment": Spec("text", labels("Equipment")),
        "weight": Spec("weight", labels("Total Weight", "Weight")),
        "currency": Spec("currency", labels("Currency")),
        "agreed_rate": Spec("money", labels("Agreed Rate", "Total Rate", "All-in Rate", "Line Haul", "Rate")),
        "fuel_surcharge": Spec("money", labels("Fuel Surcharge")),
        "payment_terms": Spec("text", labels("Payment Terms")),
        "terms": Spec("text", labels("Terms", "Terms and Conditions"), max_lines=6),
    },
}

# Fields that make a document usable for its purpose; absence means review
REQUIRED = {
    BOL: ("shipment_id", "piece_count", "expected_delivery_date"),
    POD: ("shipment_id", "delivered_pieces", "delivery_date"),
    INVOICE: ("shipment_id", "invoice_number", "invoice_total"),
    RATE: ("shipment_id", "agreed_rate"),
}

# Labels whose money value is a total or a tax, never a line item. Derived from the
# schema so that a label cannot be both a field and a charge
NON_ITEM_LABELS = (
    set(SCHEMAS[INVOICE]["invoice_total"].labels) | set(SCHEMAS[INVOICE]["tax"].labels) | set(SCHEMAS[INVOICE]["subtotal"].labels)
    | {"balance", "balance due", "agreed rate", "total rate"}
)

# Every label any schema knows: a line holding several of them is split at each
KNOWN_LABELS = {label for schema in SCHEMAS.values() for spec in schema.values() for label in spec.labels} | NON_ITEM_LABELS


# ============================================================
# RESULT TYPES
# ============================================================

@dataclass
class ExtractedField:
    name: str
    state: str
    value: object = None
    raw_text: str = None
    source_file: str = None
    page: int = None
    method: str = None
    validation: str = NOT_CHECKED
    basis: str = None
    warnings: list = field(default_factory=list)
    alternatives: list = field(default_factory=list)
    needs_review: bool = False

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"Unknown extraction state: {self.state!r}")

        if self.state == EXTRACTED and self.value is None:
            raise ValueError(f"{self.name}: an extracted field needs a value")

        if self.state != EXTRACTED and self.value is not None:
            raise ValueError(f"{self.name}: a field that is {self.state} cannot carry a value")

        if self.page is not None and (not isinstance(self.page, int) or self.page < 1):
            raise ValueError(f"{self.name}: page must be a positive integer or None")

        if self.raw_text is not None:
            self.raw_text = self.raw_text[:MAX_RAW]


@dataclass
class LineItem:
    description: str
    amount: float
    quantity: float = None
    rate: float = None
    page: int = None
    raw_text: str = None
    source_file: str = None
    warnings: list = field(default_factory=list)


@dataclass
class DocumentExtraction:
    filename: str
    sha256: str
    document_type: str
    status: str
    pages: int = None
    pages_without_text: list = field(default_factory=list)
    extractor_version: str = field(default_factory=lambda: EXTRACTOR_VERSION)
    fields: dict = field(default_factory=dict)
    line_items: list = field(default_factory=list)
    unclassified_amounts: list = field(default_factory=list)
    excluded_amounts: list = field(default_factory=list)
    percentage_charges: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    review_reasons: list = field(default_factory=list)
    error: str = None

    @property
    def shipment_id(self):
        item = self.fields.get("shipment_id")

        return item.value if item and item.state == EXTRACTED else None

    @property
    def complete(self):
        return self.status == S_EXTRACTED and not self.review_reasons

    def value(self, name):
        item = self.fields.get(name)

        return item.value if item and item.state == EXTRACTED else None

    def to_dict(self):
        return asdict(self)


def validate_extraction(extraction):
    """Structural check of a DocumentExtraction; returns problems (empty when valid)."""

    problems = []
    schema = SCHEMAS.get(extraction.document_type, {"shipment_id": None})

    for name, item in extraction.fields.items():
        if name not in schema:
            problems.append(f"{name} is not a field of {extraction.document_type}")

        if item.name != name:
            problems.append(f"{name}: field name mismatch")

        if item.state == EXTRACTED and not item.source_file:
            problems.append(f"{name}: an extracted field has no source file")

    if extraction.status not in (S_EXTRACTED, S_PARTIAL, S_NEEDS_OCR, S_UNREADABLE):
        problems.append(f"unknown status {extraction.status}")

    return problems


# ============================================================
# THE LABEL-PAIR PARSER
# ============================================================

LABEL_CHARS = r"A-Za-z0-9 /&.#()'’%-"
LABEL_ONLY = re.compile(rf"^([A-Za-z][{LABEL_CHARS}]{{1,60}}):\s*$")
LABEL_VALUE = re.compile(rf"^([A-Z][{LABEL_CHARS}]{{1,40}}):\s+(\S.*)$")

# A rate written in a label ("VAT 20%", "Sales Tax (8.25%)") is not part of what the label is
PERCENT_NOTE = re.compile(r"\s*\(?\s*(\d+(?:\.\d+)?)\s*%\s*\)?\s*$")

# A known label followed by its colon, optionally with such a rate
KNOWN_LABEL_RE = re.compile(
    r"(?<![\w])(" + "|".join(re.escape(label) for label in sorted(KNOWN_LABELS, key=len, reverse=True)) + r")"
    r"(?:\s*\(?\s*\d+(?:\.\d+)?\s*%\s*\)?)?\s*:",
    re.IGNORECASE,
)


@dataclass
class Pair:
    label: str
    key: str
    lines: list
    page: int
    percent: float = None        # the rate written in the label, when there is one
    ambiguous: bool = False      # lines under a line that held several labels cannot be assigned to one of them

    @property
    def raw(self):
        return " ".join(self.lines)


def normalize_label(label):
    text = " ".join(label.lower().replace("’", "'").split())

    return PERCENT_NOTE.sub("", text).strip()


def label_percent(label):
    found = PERCENT_NOTE.search(label)

    return float(found.group(1)) if found else None


def split_known(line):
    """
    The line cut before every known label that follows an earlier "label:" on it,
    so each value stops at the next recognised label ("Shipper: A   Consignee: B").
    A label is only cut when whitespace precedes it and something labelled came
    first: "Base Rate:" is one label, not "Base" followed by the label "Rate".
    """

    first = line.find(":")

    if first < 0:
        return [line]

    # The line starts with a known label, or a later label stands apart (two spaces or a tab):
    # otherwise "Special Instructions: Max Weight: 40,000 lbs" would make a weight out of a note
    starts_known = bool(KNOWN_LABEL_RE.match(line))

    def stands_apart(position):
        before = line[:position]
        gap = before[len(before.rstrip()):]

        return len(gap) >= 2 or "\t" in gap

    cuts = [m.start() for m in KNOWN_LABEL_RE.finditer(line, first + 1)
            if line[m.start() - 1].isspace() and (starts_known or stands_apart(m.start()))]

    if not cuts:
        return [line]

    bounds = [0, *cuts, len(line)]

    return [line[a:b].strip() for a, b in zip(bounds, bounds[1:]) if line[a:b].strip()]


def parse_pairs(text, page, strays=None):
    """
    The "Label:" / value pairs of one page of text, in order. A value is the
    non-blank lines after a label up to the next label; "Label: value" on one
    line is also a pair, and a line holding several known labels gives one pair
    per label. Text before the first label is not a pair. Lines that follow a line
    of several labels might belong to any of them (columns): those pairs are
    marked ambiguous. strays, when given, collects the lines before the first label
    (they belong to no pair).
    """

    pairs, current, shared = [], None, None

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        segments = split_known(line)
        members = []

        for index, segment in enumerate(segments):
            only = LABEL_ONLY.match(segment)
            inline = None if only else LABEL_VALUE.match(segment)
            known = KNOWN_LABEL_RE.match(segment)

            if known:
                label, value = segment[: known.end() - 1].strip(), segment[known.end():].strip()
                current = Pair(label, normalize_label(label), [value] if value else [], page, label_percent(label))
            elif only:
                current = Pair(only.group(1).strip(), normalize_label(only.group(1)), [], page, label_percent(only.group(1)))
            elif inline:
                current = Pair(inline.group(1).strip(), normalize_label(inline.group(1)), [inline.group(2).strip()], page, label_percent(inline.group(1)))
            elif current is not None:
                current.lines.append(segment)

                if shared:
                    for member in shared:
                        member.ambiguous = True

                continue
            else:
                if strays is not None:
                    strays.append(segment)

                continue

            pairs.append(current)
            members.append(current)

        # The pairs of one line of several labels share whatever lines follow it
        shared = members if len(members) > 1 else None

    return pairs


# ============================================================
# VALUE PARSERS
# ============================================================

# An ISO code may lead ("CAD 500.00") or follow ("500.00 CAD"); a leading one must be a known code
MONEY = re.compile(r"^\(?-?\s*(?:(?:USD|CAD|EUR|GBP|MXN|AUD|JPY|CHF)\s*)?(?:US\$|[$€£])?\s*-?\s*"
                   r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\)?\s*(?:[A-Z]{3})?$")
WEIGHT = re.compile(r"^(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(lbs?|pounds?|kgs?|kilograms?|tons?|tonnes?|t)?\.?$", re.IGNORECASE)
WEIGHT_UNITS = {"lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb", "kg": "kg", "kgs": "kg", "kilogram": "kg",
                "kilograms": "kg", "ton": "ton", "tons": "ton", "tonne": "tonne", "tonnes": "tonne", "t": "tonne"}
COUNT = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+)$")


def parse_money(raw):
    """(amount, currency code or None, symbol or None); amount is None when it is not a money amount."""

    text = raw.strip()
    match = MONEY.match(text)

    if not match:
        return None, None, None

    amount = float(match.group(1).replace(",", ""))
    negative = text.startswith("(") or "-" in text.split(match.group(1))[0]

    codes = {c for c in CURRENCY_CODES if re.search(rf"\b{c}\b", text)}

    # "USD 10 EUR" names two currencies: not one amount
    if len(codes) > 1:
        return None, None, None

    code = next(iter(codes), None)
    symbol = next((s for s in ("US$", "€", "£", "$") if s in text), None)

    if code is None and symbol in SYMBOL_CURRENCY:
        code = SYMBOL_CURRENCY[symbol]

    return (-amount if negative else amount), code, symbol


# Characters that only mark where something would be written: lines, dashes, boxes, brackets
PLACEHOLDER_CHARS = "_-–—−‐.…=*~/\\|[](){}<>:;·•☐□▢▭"
PLACEHOLDER_WORDS = {"signature", "sign here", "sign", "print name", "printed name", "name", "n/a", "na"}
PERCENT_VALUE = re.compile(r"^\(?\s*-?\s*(\d+(?:\.\d+)?)\s*%")


def is_placeholder(text):
    """
    Whether a line holds nothing a person wrote: only rule characters or empty
    boxes, a bare X mark ("X ______"), or the form's own caption ("(signature)").
    """

    core = text.strip()

    if not core:
        return False

    letters = "".join(c for c in core if c not in PLACEHOLDER_CHARS and not c.isspace())

    if letters == "" or letters.lower() == "x":
        return True

    return core.strip(PLACEHOLDER_CHARS + " ").lower() in PLACEHOLDER_WORDS


def parse_date_text(raw):
    """(ISO date text, None) or (None, reason). Month-name or ISO dates only: 10/05/2026 is ambiguous."""

    match = VALUES["date"].match(raw.strip())

    if not match or match.end() != len(raw.strip()):
        return None, "not a date in a supported form (month name or YYYY-MM-DD)"

    parsed = parse_date(match.group(1))

    if parsed is None:
        return None, "not a valid calendar date"

    return parsed.isoformat(), None


def parse_weight(raw):
    match = WEIGHT.match(raw.strip())

    if not match:
        return None, "not a weight"

    unit = WEIGHT_UNITS.get((match.group(2) or "").lower().rstrip("."))

    return {"amount": float(match.group(1).replace(",", "")), "unit": unit}, (None if unit else "no unit written")


def parse_party(lines):
    name = lines[0].strip()

    return {"name": name, "address_lines": [line.strip() for line in lines[1:]]}


# ============================================================
# EXTRACTING ONE FIELD
# ============================================================

def make(name, state, spec_pair=None, filename=None, **kw):
    page = spec_pair.page if spec_pair else None
    raw = spec_pair.raw if spec_pair else None

    return ExtractedField(name=name, state=state, source_file=filename, page=page, raw_text=raw, **kw)


def interpret(spec, pair):
    """(value, warnings, error) for one pair; error is the reason it cannot be parsed."""

    raw = pair.raw.strip()

    if spec.kind == "text":
        return raw, [], None

    if spec.kind == "party":
        return parse_party(pair.lines), [], None

    if spec.kind == "date":
        value, reason = parse_date_text(pair.lines[0])
        return value, [], reason

    if spec.kind == "money":
        if PERCENT_VALUE.match(pair.lines[0].strip()):
            return None, [], "percentage_without_basis: the value is a percentage and no basis is stated, so no amount is calculated"

        amount, _, _ = parse_money(pair.lines[0])
        return amount, [], (None if amount is not None else "not a money amount")

    if spec.kind == "count":
        if COUNT.match(pair.lines[0].strip()):
            return int(pair.lines[0].replace(",", "")), [], None

        return None, [], "not a whole number"

    if spec.kind == "weight":
        value, reason = parse_weight(pair.lines[0])
        return (value, [reason] if value and reason else [], None if value else reason)

    raise ValueError(f"cannot interpret kind {spec.kind}")


def comparable(value):
    return " ".join(str(value).casefold().split()) if not isinstance(value, dict) else repr(sorted(value.items()))


def read_spec_field(name, spec, pairs, filename, unreadable):
    """
    The field `name` from the pairs of a document. unreadable: True when some
    page of the document had no text, so a field that was not found cannot be
    called missing.
    """

    found = [pair for pair in pairs if pair.key in spec.labels]

    if not found:
        state = UNREADABLE if unreadable else MISSING
        warnings = ["Some pages of the document have no text, so the field may be on one of them."] if unreadable else []

        return ExtractedField(name=name, state=state, source_file=filename, warnings=warnings)

    # Warnings and errors name the problem, never quote the document: they are stored
    readings = []

    for pair in found:
        if not pair.lines:
            readings.append({"pair": pair, "value": None, "warnings": [], "error": None, "empty": True, "cut": 0, "review": False})
            continue

        # Placeholder lines (rules, dashes, empty boxes) hold nothing written; a name under one is read, with a flag
        lines = list(pair.lines)
        skipped = 0

        while lines and is_placeholder(lines[0]):
            lines.pop(0)
            skipped += 1

        if not lines:
            readings.append({"pair": pair, "value": None, "warnings": [], "error": None, "empty": True, "cut": 0, "review": False, "placeholder": True})
            continue

        kept = replace(pair, lines=lines[:spec.max_lines])
        value, warnings, error = interpret(spec, kept)
        notes = list(warnings)

        if skipped:
            notes.append("placeholder_line_skipped: a blank line or box came first, and the next text was read instead")

        if pair.ambiguous:
            notes.append("columns_ambiguous: the lines under a line holding several labels cannot be assigned to one of them")

        readings.append({"pair": kept, "value": value, "warnings": notes, "error": error, "empty": False,
                         "cut": len(lines) - len(kept.lines), "review": bool(skipped or pair.ambiguous)})

    good = [r for r in readings if r["value"] is not None]
    unparseable = [r for r in readings if r["value"] is None and not r["empty"]]
    empty = [r for r in readings if r["empty"]]

    distinct = {comparable(r["value"]) for r in good}

    # Different values, or a value next to an occurrence that cannot be read
    # (which might say something else): none is chosen
    if len(distinct) > 1 or (good and unparseable):
        warning = ("The document gives different values; none was chosen." if len(distinct) > 1 else
                   "unreadable_occurrence: the field is written more than once and one occurrence cannot be parsed; none was chosen.")

        return ExtractedField(
            name=name, state=CONFLICTING, source_file=filename, method="label_pair", basis="conflict", warnings=[warning],
            alternatives=[{"value": r["value"], "page": r["pair"].page, "raw_text": r["pair"].raw[:MAX_RAW]} for r in good]
            + [{"value": None, "page": r["pair"].page, "raw_text": r["pair"].raw[:MAX_RAW], "problem": "unparseable"}
               for r in unparseable],
        )

    if good:
        first = good[0]
        warnings = first["warnings"] + [f"also read on page {r['pair'].page}" for r in good[1:] if r["pair"].page != first["pair"].page]
        needs_review = False

        if empty:
            warnings.append("empty_occurrence: the label is also written without a value")
            needs_review = True

        if any(r["cut"] for r in good):
            warnings.append(f"extra_lines_ignored: lines after the value were not used (this field takes at most {spec.max_lines})")
            needs_review = True

        if any(r["review"] for r in good):
            needs_review = True

        return make(name, EXTRACTED, first["pair"], filename, value=first["value"], method="label_pair", basis="exact_label",
                    validation=WARNING if warnings else VALID, warnings=warnings, needs_review=needs_review)

    if unparseable:
        return make(name, UNPARSEABLE, unparseable[0]["pair"], filename, method="label_pair", validation=INVALID,
                    warnings=[unparseable[0]["error"]])

    if empty[0].get("placeholder"):
        return make(name, MISSING, empty[0]["pair"], filename, method="label_pair", validation=NOT_CHECKED, needs_review=spec.review_if_blank,
                    warnings=["placeholder_only: the field holds only a blank line or box, nothing was written"])

    return make(name, MISSING, empty[0]["pair"], filename, method="label_pair", validation=NOT_CHECKED,
                warnings=["The label is present but has no value."])


# ---- shipment ID and references (the existing identifier extractor, per page) ----

def read_shipment_id(pages, filename, unreadable):
    seen = {}

    for number, text in pages:
        value = extract_all_identifiers(text).get("shipment_id")

        if value:
            seen.setdefault(identifier_key(value), (value, number, text))

    if not seen:
        return ExtractedField(name="shipment_id", state=UNREADABLE if unreadable else MISSING, source_file=filename,
                              warnings=["Some pages have no text."] if unreadable else [])

    if len(seen) > 1:
        return ExtractedField(
            name="shipment_id", state=CONFLICTING, source_file=filename, method="identifier_extractor", basis="conflict",
            warnings=["The document names more than one shipment; it is not attributed to any."],
            alternatives=[{"value": v, "page": n, "raw_text": None} for v, n, _ in seen.values()],
        )

    value, number, _ = next(iter(seen.values()))

    return ExtractedField(name="shipment_id", state=EXTRACTED, value=value, source_file=filename, page=number,
                          method="identifier_extractor", basis="exact_label", validation=VALID, raw_text=f"Shipment Number: {value}")


def read_references(pages):
    """Other identifiers (PO, PRO, booking...) with their pages: {name: {"value", "page"}}; conflicts are dropped."""

    found = {}

    for number, text in pages:
        for name, value in extract_all_identifiers(text).items():
            if name != "shipment_id":
                found.setdefault(name, []).append((value, number))

    return {
        name: {"value": items[0][0], "page": items[0][1]}
        for name, items in sorted(found.items())
        if len({v for v, _ in items}) == 1
    }


# ---- currency ----

def read_currency(pairs, texts, filename, unreadable):
    """
    The currency, only when the document states it: a "Currency" label, an ISO
    code next to an amount, or an unambiguous symbol (euro, pound, US$). A bare
    "$" is not a currency (USD, CAD, AUD...), so it is reported as missing with
    that warning and no default is applied.
    """

    labelled = [p for p in pairs if p.key == "currency"]

    if labelled:
        # One line is the value (later lines are other text); unknown codes are never quoted
        codes = {p.lines[0].strip().upper() for p in labelled if p.lines}
        bad = [c for c in codes if c not in CURRENCY_CODES]

        if len(codes) > 1:
            return ExtractedField("currency", CONFLICTING, source_file=filename, method="label_pair", basis="conflict",
                                  warnings=["Different currencies are written."],
                                  alternatives=[{"value": c if c in CURRENCY_CODES else None, "page": p.page,
                                                 "raw_text": p.raw[:MAX_RAW], **({} if c in CURRENCY_CODES else {"problem": "unrecognized_code"})}
                                                for p in labelled if p.lines for c in [p.lines[0].strip().upper()]])

        if bad:
            return make("currency", UNPARSEABLE, labelled[0], filename, method="label_pair", validation=INVALID,
                        warnings=["unrecognized_code: the Currency label does not hold a currency code this extractor knows"])

        if any(len(p.lines) > 1 for p in labelled):
            first = Pair(labelled[0].label, labelled[0].key, labelled[0].lines[:1], labelled[0].page)

            return make("currency", EXTRACTED, first, filename, value=next(iter(codes)), method="label_pair", basis="exact_label",
                        validation=WARNING, needs_review=True,
                        warnings=["extra_lines_ignored: lines after the value were not used (this field takes at most 1)"])

        return make("currency", EXTRACTED, labelled[0], filename, value=next(iter(codes)), method="label_pair",
                    basis="exact_label", validation=VALID)

    codes, symbol_page, bare_dollar = {}, None, False

    for number, text in texts:
        for match in re.finditer(r"(US\$|[$€£])\s*\d|\d\s*(USD|CAD|EUR|GBP|MXN|AUD|JPY|CHF)\b|\b(USD|CAD|EUR|GBP|MXN|AUD|JPY|CHF)\s*[$€£]?\s*\d", text):
            token = match.group(1) or match.group(2) or match.group(3)

            if token == "$":
                bare_dollar = True
            else:
                codes.setdefault(SYMBOL_CURRENCY.get(token, token), number)

    if len(codes) > 1:
        return ExtractedField("currency", CONFLICTING, source_file=filename, method="symbol_or_code", basis="conflict",
                              warnings=["Amounts are written in different currencies."],
                              alternatives=[{"value": c, "page": p, "raw_text": None} for c, p in codes.items()])

    if codes:
        code, page = next(iter(codes.items()))

        return ExtractedField("currency", EXTRACTED, value=code, source_file=filename, page=page, method="symbol_or_code",
                              basis="exact_label", validation=VALID, raw_text=None)

    state = UNREADABLE if unreadable else MISSING
    warnings = ["Only a '$' symbol is written: it does not say which dollar, so no currency is assumed."] if bare_dollar else []

    return ExtractedField("currency", state, source_file=filename, warnings=warnings)


# ---- line items ----

QTY_RATE = re.compile(r"^(\d+(?:\.\d+)?)\s*[x×@]\s*([$€£]?\s*\d[\d,]*(?:\.\d+)?)\s*=?\s*(.+)$", re.IGNORECASE)

# A word of a label that says the number is not a charge: a count, a weight, an
# identifier, a date, or money already paid or carried over
NON_CHARGE_WORDS = {
    "number", "no", "id", "ref", "reference", "account", "acct", "po", "pro", "bol", "invoice", "date",
    "pieces", "piece", "count", "quantity", "qty", "units", "pallets", "cartons", "weight", "lbs", "kg",
    "paid", "payment", "payments", "deposit", "balance", "previous", "prior", "received",
}

# A word of a label whose amount changes the total in a way the extractor cannot
# decide (deducted or not): it is listed for review and the arithmetic is not checked
ADJUSTMENT_WORDS = {"discount", "discounts", "rebate", "allowance", "adjustment", "writeoff"}

# A label that is a total is never a charge, whichever total it is
TOTAL_WORDS = {"total", "subtotal", "grand"}

# A tax this extractor has no field for: unresolved, never silently a charge or dropped
TAX_WORDS = {"tax", "taxes", "vat", "gst", "hst", "pst", "qst"}

# Words that make a label with "tax" in it a total, not a tax amount ("Subtotal Before Tax", "Total Incl. Tax")
TAX_QUALIFIERS = {"before", "excluding", "excl", "ex", "without", "pre", "including", "incl", "inc", "with", "plus"}

CODES = "USD|CAD|EUR|GBP|MXN|AUD|JPY|CHF"
CURRENCY_MARK = re.compile(rf"US\$|[$€£]|\b(?:{CODES})\b")

# An amount that says which money it is: a symbol or a code before or after the number
MARKED_AMOUNT = re.compile(
    rf"(?:US\$|[$€£])\s*\(?-?\d[\d,]*(?:\.\d+)?\)?|\b(?:{CODES})\s*\(?-?\d[\d,]*(?:\.\d+)?\)?|\(?-?\d[\d,]*(?:\.\d+)?\)?\s*(?:{CODES})\b"
)

# "Fuel: $18.00" inside a value: a label that is followed by an amount
EMBEDDED_CHARGE = re.compile(
    rf"(?:^|(?<=\s))([A-Za-z][{LABEL_CHARS}]{{0,40}}?)\s*:\s*(?=\(?-?\s*(?:US\$|[$€£]|\d|(?:{CODES})\b))"
)


def split_units(label, text):
    """
    [(label, text, split)] for the charge or charges a line holds, or None when
    the line holds several amounts that cannot be told apart reliably. A line is
    split only into "label: amount" units where every unit is exactly one amount.
    """

    if QTY_RATE.match(text):
        return [(label, text, False)]

    embedded = list(EMBEDDED_CHARGE.finditer(text))

    if len(MARKED_AMOUNT.findall(text)) <= 1 and not embedded:
        return [(label, text, False)]

    if embedded:
        cuts = [m.start() for m in embedded] + [len(text)]
        parts = [(label, text[:cuts[0]].strip(), True)]
        parts += [(m.group(1).strip(), text[m.end():cuts[i + 1]].strip(), True) for i, m in enumerate(embedded)]

        if all(part and parse_money(part)[0] is not None for _, part, _ in parts):
            return parts

    return None


def read_line_items(pairs, filename):
    """
    The charges of an invoice, as (items, unclassified, excluded, percentages).

    A charge is a "Label: amount" pair whose label is not a total, a subtotal
    or a tax, whose label does not say it is something else (a count, a weight,
    an identifier, a payment or a balance: excluded), and whose amount carries
    a currency mark ($, €, £, US$ or an ISO code). An amount may be written as
    "2 x $50.00 = $100.00" (quantity, rate and amount). Credits (negative
    amounts) are kept with their sign. The labels are not limited to a fixed
    list.

    Nothing that looks like money is dropped silently. A total (by any label with
    the word total) is excluded, not a charge. A tax without its own field, a
    discount, an amount without a currency mark, an amount that cannot be read, an
    amount on a later line, and a line holding several amounts that cannot be
    separated are unclassified: listed by label and page (never by their text),
    they make the invoice need review and its arithmetic unchecked. A charge
    stated as a percentage is kept as such, with no amount calculated.
    A line is split into several charges only when it reads as "label: amount"
    units, and each is then marked split_from_one_line.
    """

    items, unclassified, excluded, percentages = [], [], [], []

    def unresolved(label, page, reason):
        unclassified.append({"label": label, "page": page, "reason": reason})

    def read_unit(label, text, page, split):
        key = normalize_label(label)
        words = set(re.findall(r"[a-z0-9]+", key.replace("-", "")))

        if key in NON_ITEM_LABELS:
            return

        percent = PERCENT_VALUE.match(text)

        if percent:
            percentages.append({"label": label, "percent": float(percent.group(1)), "page": page})
            return

        quantity = rate = None
        warnings = ["split_from_one_line"] if split else []
        match = QTY_RATE.match(text)

        if match:
            quantity = float(match.group(1))
            rate, _, _ = parse_money(match.group(2))
            amount, _, _ = parse_money(match.group(3))

            if amount is not None and rate is not None and abs(round(quantity * rate, 2) - abs(amount)) > 0.01:
                warnings.append("quantity x rate does not equal the amount")
                unresolved(label, page, "quantity_rate_mismatch")
        else:
            amount, _, _ = parse_money(text)

        if amount is None:
            if MARKED_AMOUNT.search(text):
                unresolved(label, page, "amount_not_understood")

            return

        if words & ADJUSTMENT_WORDS:
            unresolved(label, page, "adjustment_needs_review")
        elif words & TAX_WORDS and not words & TAX_QUALIFIERS:
            unresolved(label, page, "tax_without_field")
        elif words & NON_CHARGE_WORDS:
            excluded.append({"label": label, "page": page, "reason": "not_a_charge"})
        elif words & TOTAL_WORDS:
            excluded.append({"label": label, "page": page, "reason": "total_like_label"})
        elif words & TAX_WORDS:
            excluded.append({"label": label, "page": page, "reason": "total_like_label"})
        elif not CURRENCY_MARK.search(text):
            unresolved(label, page, "no_currency_mark")
        else:
            items.append(LineItem(description=label, amount=amount, quantity=quantity, rate=rate, page=page,
                                  raw_text=text[:MAX_RAW], source_file=filename, warnings=warnings))

    for pair in pairs:
        if pair.key in NON_ITEM_LABELS or not pair.lines:
            continue

        units = split_units(pair.label, pair.lines[0].strip())

        if units is None:
            unresolved(pair.label, pair.page, "multiple_amounts_on_one_line")
        else:
            for label, text, split in units:
                read_unit(label, text, pair.page, split)

        # Amounts on the lines after the first belong to no charge: they are not dropped silently
        if any(MARKED_AMOUNT.search(line) or PERCENT_VALUE.match(line.strip()) for line in pair.lines[1:]):
            unresolved(pair.label, pair.page, "amount_on_following_line")

    # The same label and amount twice may be a recap or a repeated charge: the text cannot say which, so
    # both stay in the items (nothing is merged) and the set is unresolved: review, arithmetic unchecked
    seen = {}

    for item in items:
        seen.setdefault((normalize_label(item.description), item.amount), []).append(item)

    for (_, _), group in seen.items():
        if len(group) > 1:
            unresolved(group[1].description, group[1].page, "repeated_charge")

    return items, unclassified, excluded, percentages


# ============================================================
# EXTRACTING A DOCUMENT
# ============================================================

def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def empty_fields(document_type, filename, state, warning):
    schema = SCHEMAS.get(document_type) or {"shipment_id": Spec("identifier")}

    return {name: ExtractedField(name=name, state=UNSUPPORTED if (spec.kind == "text" and not spec.labels) else state,
                                 source_file=filename, warnings=[warning] if state != UNSUPPORTED else [])
            for name, spec in schema.items()}


def extract_document(path, filename=None):
    """
    Extracts one PDF. Never raises for a damaged or image-only file: the
    result says so (status unreadable / needs_ocr, every field UNREADABLE).
    """

    path = Path(path)
    filename = filename or path.name

    try:
        data = path.read_bytes()
    except OSError as error:
        return DocumentExtraction(filename, "", "UNKNOWN", S_UNREADABLE, error=f"{type(error).__name__}: {error}"[:200])

    digest = hashlib.sha256(data).hexdigest()

    try:
        # PyMuPDF is single-threaded: one document at a time (pdf_access)
        with pdf_access.PDF_LOCK:
            document = pymupdf.open(stream=data, filetype="pdf")
            try:
                texts = [(n, page.get_text("text", sort=True).strip()) for n, page in enumerate(document, start=1)]
                total = len(document)
            finally:
                document.close()
    except Exception as error:
        return DocumentExtraction(filename, digest, "UNKNOWN", S_UNREADABLE, error=f"{type(error).__name__}: {error}"[:200],
                                  fields=empty_fields("UNKNOWN", filename, UNREADABLE, "The PDF could not be opened."),
                                  review_reasons=["the PDF could not be opened"])

    with_text = [(n, t) for n, t in texts if t]
    blank = [n for n, t in texts if not t]

    if not with_text:
        reason = "no page has extractable text (an image-only scan); there is no OCR"

        return DocumentExtraction(filename, digest, "UNKNOWN", S_NEEDS_OCR, pages=total, pages_without_text=blank,
                                  fields=empty_fields("UNKNOWN", filename, UNREADABLE, reason), review_reasons=[reason])

    full_text = "\n".join(t for _, t in with_text)
    document_type = classify_document(filename, full_text)
    unreadable = bool(blank)

    pairs, strays = [], []

    for n, t in with_text:
        found = []
        pairs.extend(parse_pairs(t, n, strays=found))
        strays.extend((n, line) for line in found)

    schema = SCHEMAS.get(document_type)

    extraction = DocumentExtraction(filename, digest, document_type, S_PARTIAL if blank else S_EXTRACTED, pages=total,
                                    pages_without_text=blank)

    extraction.fields["shipment_id"] = read_shipment_id(with_text, filename, unreadable)

    if schema is None:
        extraction.review_reasons.append(f"document type {document_type} is not supported by the extractor")
        extraction.warnings.append("Only the shipment ID was read.")
    else:
        for name, spec in schema.items():
            if name == "shipment_id":
                continue

            if spec.kind == "currency":
                extraction.fields[name] = read_currency(pairs, with_text, filename, unreadable)
            elif not spec.labels:
                extraction.fields[name] = ExtractedField(name=name, state=UNSUPPORTED, source_file=filename,
                                                         warnings=[spec.note or "not supported by the extractor"])
            else:
                extraction.fields[name] = read_spec_field(name, spec, pairs, filename, unreadable)

    if document_type == INVOICE:
        (extraction.line_items, extraction.unclassified_amounts, extraction.excluded_amounts,
         extraction.percentage_charges) = read_line_items(pairs, filename)

        # An amount on a line before the page's first label belongs to no label: never dropped silently
        for page, line in strays:
            if MARKED_AMOUNT.search(line):
                extraction.unclassified_amounts.append({"label": "(a line before the first label)", "page": page, "reason": "amount_outside_a_label"})

    if blank:
        extraction.warnings.append(
            f"Pages without text: {', '.join(map(str, blank))}. Fields not found may be on those pages."
        )

    review(extraction)

    return extraction


def review(extraction):
    """
    Fills review_reasons: what a person has to look at before this extraction
    is relied on. Reasons name fields and problems; they never quote the
    document (they are stored).
    """

    reasons = extraction.review_reasons

    for name, item in extraction.fields.items():
        if item.state == CONFLICTING:
            reasons.append(f"{name}: conflicting values")
        elif item.state == UNPARSEABLE:
            reasons.append(f"{name}: unparseable" + (f" (page {item.page})" if item.page else ""))

        if item.needs_review:
            reasons.append(f"{name}: needs review (see its warnings)")

    if extraction.unclassified_amounts:
        reasons.append(f"unclassified_amounts: {len(extraction.unclassified_amounts)} amount(s) could not be classified as charges")

    if extraction.percentage_charges:
        reasons.append(f"percentage_charges: {len(extraction.percentage_charges)} charge(s) are stated as a percentage with no basis; no amount was calculated")

    for name in REQUIRED.get(extraction.document_type, ("shipment_id",)):
        item = extraction.fields.get(name)

        if item is None or item.state != EXTRACTED:
            reasons.append(f"required field {name} is {item.state.lower() if item else 'absent'}")

    if extraction.status == S_PARTIAL:
        reasons.append("some pages have no text")

    extraction.review_reasons = list(dict.fromkeys(reasons))


# ============================================================
# CROSS-DOCUMENT VALIDATION (reports; never chooses a value)
# ============================================================

def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def line_total(extraction):
    """(sum of line items + tax, reason it cannot be computed). Never treats a missing amount as 0."""

    if not extraction.line_items:
        return None, "no line items were read"

    if extraction.unclassified_amounts:
        return None, "some amounts could not be classified as charges"

    if extraction.percentage_charges:
        return None, "a charge is stated as a percentage and no amount was calculated"

    tax = extraction.fields.get("tax")

    if tax is not None and tax.state in (CONFLICTING, UNPARSEABLE, UNREADABLE):
        return None, f"tax is {tax.state.lower()}"

    total = sum((money(item.amount) for item in extraction.line_items), Decimal("0"))

    if tax is not None and tax.state == EXTRACTED:
        total += money(tax.value)

    return total, None


def validate_shipment(extractions):
    """
    Consistency of the documents of ONE shipment. Documents of other
    shipments, and documents that name no (or several) shipments, are never
    mixed in: group first with group_by_shipment(). Returns issues, each
    {code, severity, message, sources}.
    """

    issues = []
    usable = [e for e in extractions if e.status in (S_EXTRACTED, S_PARTIAL)]

    def issue(code, severity, message, sources):
        issues.append({"code": code, "severity": severity, "message": message, "sources": sources})

    keys = {identifier_key(e.shipment_id) for e in usable if e.shipment_id}

    if len(keys) > 1:
        issue("shipment_mismatch", "error", "The documents name different shipments.", [e.filename for e in usable])

    for name in ("carrier", "origin", "destination"):
        values = {}

        for extraction in usable:
            item = extraction.fields.get(name)

            if item is not None and item.state == EXTRACTED:
                values.setdefault(comparable(item.value), []).append((extraction.filename, item.value))

        if len(values) > 1:
            listing = "; ".join(f"{v[0][1]} ({', '.join(f for f, _ in v)})" for v in values.values())
            issue(f"{name}_mismatch", "warning", f"The documents give different values for {name}: {listing}.",
                  sorted({f for v in values.values() for f, _ in v}))

    currencies = {}

    for extraction in usable:
        item = extraction.fields.get("currency")

        if item is not None and item.state == EXTRACTED:
            currencies.setdefault(item.value, []).append(extraction.filename)

    if len(currencies) > 1:
        issue("currency_mismatch", "error", "The documents state different currencies; amounts must not be combined.",
              sorted({f for v in currencies.values() for f in v}))

    for extraction in usable:
        if extraction.document_type != INVOICE:
            continue

        total = extraction.fields.get("invoice_total")

        if total is None or total.state != EXTRACTED:
            continue

        computed, reason = line_total(extraction)

        if computed is None:
            issue("invoice_arithmetic_unchecked", "info", f"{extraction.filename}: total not checked against line items ({reason}).",
                  [extraction.filename])
        elif computed != money(total.value):
            issue("invoice_arithmetic_mismatch", "warning",
                  f"{extraction.filename}: line items{' and tax' if extraction.fields.get('tax') and extraction.fields['tax'].state == EXTRACTED else ''} "
                  f"add up to {computed} but the stated total is {money(total.value)} (difference {money(total.value) - computed}). "
                  "This is an arithmetic finding, not proof of overbilling.", [extraction.filename])

    digests = {}

    for extraction in extractions:
        digests.setdefault(extraction.sha256, []).append(extraction.filename)

    for names in digests.values():
        if len(names) > 1:
            issue("duplicate_document", "info", f"Identical content: {', '.join(sorted(names))}.", sorted(names))

    return issues


def group_by_shipment(extractions):
    """
    {shipment key: [extractions]} and the extractions that cannot be attributed
    (no shipment ID, several, or an unreadable file): those are listed under
    None and never joined to a shipment.
    """

    groups = {}

    for extraction in extractions:
        item = extraction.fields.get("shipment_id")
        key = identifier_key(extraction.shipment_id) if item is not None and item.state == EXTRACTED else None

        groups.setdefault(key, []).append(extraction)

    return groups


# ============================================================
# REVIEWER CORRECTIONS: a typed value from text a person typed
# ============================================================

def coerce_correction(document_type, field_name, text):
    """
    The typed value a reviewer's text means for a field, by the same parsers
    the extractor uses. Raises ValueError when the field does not exist for
    this document type, cannot be corrected (UNSUPPORTED), or the text is not
    a valid value of its kind. A correction is never a blank, 0 or a guess.
    """

    schema = SCHEMAS.get(document_type)

    if schema is None or field_name not in schema:
        raise ValueError(f"{field_name} is not a field of {document_type}")

    # A shipment ID decides which shipment a document belongs to (the stored
    # shipment key, the audit grouping). Changing it needs its own approved
    # operation that moves the key in the same transaction; until that exists
    # it is not correctable here. Fix the document or its ingestion instead.
    if field_name == "shipment_id":
        raise ValueError("shipment ID corrections are not supported: correct the document or its ingestion record instead")

    spec = schema[field_name]
    text = (text or "").strip()

    if not text:
        raise ValueError("a corrected value cannot be empty")

    if spec.kind != "currency" and not spec.labels:
        raise ValueError(f"{field_name} is not supported by the extractor and cannot be corrected")

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    if spec.kind in ("text", "identifier"):
        return " ".join(lines)

    if spec.kind == "party":
        return parse_party(lines)

    if spec.kind == "currency":
        code = text.upper()

        if code not in CURRENCY_CODES:
            raise ValueError(f"{text!r} is not a supported currency code")

        return code

    if spec.kind == "date":
        value, reason = parse_date_text(lines[0])

        if value is None:
            raise ValueError(reason or "not a valid date")

        return value

    if spec.kind == "money":
        amount, _, _ = parse_money(lines[0])

        if amount is None:
            raise ValueError("not a money amount")

        return amount

    if spec.kind == "count":
        if not COUNT.match(lines[0]):
            raise ValueError("not a whole number")

        return int(lines[0].replace(",", ""))

    if spec.kind == "weight":
        value, reason = parse_weight(lines[0])

        if value is None:
            raise ValueError(reason or "not a weight")

        return value

    raise ValueError(f"cannot correct kind {spec.kind}")
