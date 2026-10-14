"""
LogiDoc-RAG question scope

Decides which indexed document chunks a question may use:

    scope = resolve_scope(question, selected_shipment, records)
    allowed = records_in_scope(records, scope)

A shipment selected in the UI limits the question to the chunks whose
shipment_id metadata matches it. IDs are compared with
shipment_identity.identifier_key, so case, spaces and hyphens do not
matter. Chunks without a shipment_id cannot be attributed to any
shipment, so they are never used for a selected shipment.

Without a selection ("All Shipments") the question is unscoped, unless
it names one or more indexed shipments.

Shipment IDs are recognised in the question by matching the IDs that
are actually indexed, not a fixed ID format. Other identifier-like
references (letters followed by digits, at least three digits in
total, e.g. "PO 12345") are checked against the document text: a
reference found only in other shipments' documents asks for
clarification, and one found in no document leads to an abstention.

Nothing is guessed: when the scope is empty, conflicting or cannot be
verified, the status is "abstained" or "clarification" and the message
explains why.
"""

import re

from shipment_identity import identifier_key


# ============================================================
# CONSTANTS
# ============================================================

ALL_SHIPMENTS = "All Shipments"

SCOPE_OK = "ok"
SCOPE_ABSTAINED = "abstained"
SCOPE_CLARIFICATION = "clarification"

UNSCOPED_LABEL = (
    "All Shipments (unscoped: every indexed shipment is searched)"
)

# Separators allowed between the characters of a shipment ID: spaces,
# punctuation and any dash, including unicode ones such as U+2011
SEPARATORS = r"[\W_]{0,3}"

# Letters followed by digit groups, e.g. "PO 12345", "SHJ-2026-001"
REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,}(?:[\W_]?\d+)+)(?![A-Za-z0-9])"
)

# Words that start dates or amounts rather than identifiers
NOT_IDENTIFIER_PREFIXES = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec", "net", "page", "pages", "day", "days",
    "lbs", "usd",
}


# ============================================================
# HELPERS
# ============================================================

def record_key(record):
    """
    Shipment key of a record (rag.get_document_records format),
    or None when the chunk has no shipment_id.
    """

    return identifier_key(
        (record.get("metadata") or {}).get("shipment_id")
    )


def indexed_shipments(records):
    """
    Returns ({key: sorted shipment_id values}, unidentified_count).
    """

    shipments = {}
    unidentified = 0

    for record in records:

        key = record_key(record)

        if not key:
            unidentified += 1
            continue

        value = str(record["metadata"]["shipment_id"]).strip()

        shipments.setdefault(key, set()).add(value)

    return (
        {key: sorted(values) for key, values in shipments.items()},
        unidentified,
    )


def display_name(key, shipments):

    return shipments.get(key, [key])[0]


def shipment_pattern(shipment_id):
    """
    Regex for an indexed shipment ID that tolerates case and
    separator differences ("SHJ-2026-001" ~ "shj 2026 001").
    """

    characters = [
        re.escape(character)
        for character in str(shipment_id)
        if character.isalnum()
    ]

    return re.compile(
        r"(?<![A-Za-z0-9])"
        + SEPARATORS.join(characters)
        + r"(?![A-Za-z0-9])",
        re.IGNORECASE
    )


def mentioned_shipments(question, shipments):
    """
    Keys of the indexed shipments the question names.
    """

    return {
        key
        for key, values in shipments.items()
        if any(
            shipment_pattern(value).search(question or "")
            for value in values
        )
    }


def identifier_references(question):
    """
    Identifier-like references in the question, e.g. "PO 12345".
    """

    references = []

    for match in REFERENCE_PATTERN.finditer(question or ""):

        text = match.group(1).strip()

        prefix = re.match(r"[A-Za-z]+", text).group(0).lower()
        digits = sum(character.isdigit() for character in text)

        if prefix in NOT_IDENTIFIER_PREFIXES or digits < 3:
            continue

        references.append(text)

    return references


def text_key(text):

    return "".join(
        character
        for character in str(text or "").upper()
        if character.isalnum()
    )


def make_scope(status, message=None, **fields):

    scope = {
        "status": status,
        "message": message,
        "mode": "all",
        "keys": None,
        "shipment_ids": None,
        "source": None,
        "label": UNSCOPED_LABEL,
        "excluded_unidentified": 0,
        "unidentified_included": 0,
    }

    scope.update(fields)

    return scope


# ============================================================
# SCOPE
# ============================================================

def resolve_scope(question, selected_shipment, records):
    """
    Decide which records the question may use.

    selected_shipment: the UI selection; None or "All Shipments"
    means unscoped. records: every indexed record.
    """

    shipments, unidentified = indexed_shipments(records)

    selected = (
        None
        if not selected_shipment
        or str(selected_shipment).strip() == ALL_SHIPMENTS
        else str(selected_shipment).strip()
    )

    selected_key = identifier_key(selected) if selected else None

    mentioned = mentioned_shipments(question, shipments)

    unidentified_note = (
        f" {unidentified} indexed chunk(s) have no shipment ID; they "
        "are never used for a selected shipment."
        if unidentified
        else ""
    )

    # --------------------------------------------------------
    # Which shipments
    # --------------------------------------------------------

    if selected:

        if not selected_key or selected_key not in shipments:

            return make_scope(
                SCOPE_ABSTAINED,
                f"No documents are indexed for shipment {selected}, so "
                "this question cannot be answered. Check the shipment "
                "ID or upload its documents." + unidentified_note,
                mode="shipment",
                source="selected",
                label=f"Shipment {selected} (selected)",
            )

        others = sorted(mentioned - {selected_key})

        if others:

            names = ", ".join(display_name(key, shipments) for key in others)

            return make_scope(
                SCOPE_CLARIFICATION,
                f"The question refers to shipment {names}, but shipment "
                f"{display_name(selected_key, shipments)} is selected. "
                "Select that shipment (or All Shipments) to ask about it.",
                mode="shipment",
                source="selected",
                label=(
                    f"Shipment {display_name(selected_key, shipments)} "
                    "(selected)"
                ),
            )

        keys = {selected_key}
        source = "selected"

    elif mentioned:

        keys = mentioned
        source = "question"

    else:

        keys = None
        source = None

    # --------------------------------------------------------
    # Other identifier-like references must exist in scope
    # --------------------------------------------------------

    in_scope = [
        record
        for record in records
        if keys is None or record_key(record) in keys
    ]

    out_of_scope = [
        record
        for record in records
        if keys is not None and record_key(record) not in keys
    ]

    in_scope_text = [text_key(record["document"]) for record in in_scope]
    out_of_scope_text = [text_key(record["document"]) for record in out_of_scope]

    for reference in identifier_references(question):

        key = identifier_key(reference)

        if key in shipments:
            continue

        if any(key in text for text in in_scope_text):
            continue

        if any(key in text for text in out_of_scope_text):

            return make_scope(
                SCOPE_CLARIFICATION,
                f"The question mentions '{reference}', which appears only "
                "in the documents of another shipment. Select that "
                "shipment (or All Shipments) to ask about it.",
                mode="shipment",
                keys=sorted(keys),
                source=source,
            )

        return make_scope(
            SCOPE_ABSTAINED,
            f"The question mentions '{reference}', which does not appear "
            "in any indexed document, so it cannot be answered.",
            mode="shipment" if keys else "all",
            keys=sorted(keys) if keys else None,
            source=source,
        )

    # --------------------------------------------------------
    # Scope
    # --------------------------------------------------------

    if keys is None:

        return make_scope(
            SCOPE_OK,
            unidentified_included=unidentified,
        )

    names = ", ".join(
        display_name(key, shipments)
        for key in sorted(keys)
    )

    return make_scope(
        SCOPE_OK,
        mode="shipment",
        keys=sorted(keys),
        shipment_ids=sorted(
            value
            for key in keys
            for value in shipments[key]
        ),
        source=source,
        label=(
            f"Shipment {names} (selected)"
            if source == "selected"
            else f"Shipment {names} (named in the question)"
        ),
        excluded_unidentified=unidentified,
    )


def records_in_scope(records, scope):
    """
    The records a resolved scope allows.
    """

    if scope["keys"] is None:
        return list(records)

    allowed = set(scope["keys"])

    return [
        record
        for record in records
        if record_key(record) in allowed
    ]


def group_records_by_shipment(records):
    """
    {shipment key or None: records}, in a stable order, so answers
    from different shipments are never mixed.
    """

    groups = {}

    for record in records:
        groups.setdefault(record_key(record), []).append(record)

    return dict(
        sorted(
            groups.items(),
            key=lambda item: (item[0] is None, item[0] or "")
        )
    )
