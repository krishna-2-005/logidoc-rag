"""
Universal logistics identifier extraction.

Extracts shipment-related identifiers from document content
using multiple terminology variations.
"""

import re
from logistics_fields import IDENTIFIER_ALIASES


def normalize_value(value):
    """Clean an extracted identifier value."""
    if not value:
        return None

    value = value.strip()

    # Remove common trailing punctuation
    value = value.rstrip(".,;:")

    # Normalize repeated whitespace
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def build_label_pattern(aliases):
    """
    Build a regex pattern that matches any known label.
    Longer aliases are checked first.
    """
    sorted_aliases = sorted(
        aliases,
        key=len,
        reverse=True
    )

    escaped = [
        re.escape(alias)
        for alias in sorted_aliases
    ]

    return "|".join(escaped)


def extract_identifier(text, identifier_type):
    """
    Extract one identifier type from document text.

    Example:
        Shipment Number: SHJ-2026-001
    """

    if not text:
        return None

    aliases = IDENTIFIER_ALIASES.get(identifier_type, [])

    if not aliases:
        return None

    label_pattern = build_label_pattern(aliases)

    pattern = rf"""
        (?im)
        (?:^|\n)
        \s*
        (?:{label_pattern})
        \s*
        (?::|=|-|\#)?
        \s*
        ([A-Za-z0-9][A-Za-z0-9_./\- ]{{0,100}})
        \s*$
    """

    matches = re.findall(
        pattern,
        text,
        flags=re.IGNORECASE | re.MULTILINE | re.VERBOSE
    )

    if not matches:
        return None

    for match in matches:
        value = normalize_value(match)

        if value:
            return value

    return None


def extract_all_identifiers(text):
    """
    Extract all supported logistics identifiers.

    Returns:
        {
            "shipment_id": "...",
            "load_id": "...",
            "bol_number": "...",
            ...
        }
    """

    results = {}

    for identifier_type in IDENTIFIER_ALIASES:

        value = extract_identifier(
            text,
            identifier_type
        )

        if value:
            results[identifier_type] = value

    return results


def extract_identifiers_from_documents(documents):
    """
    Extract identifiers from multiple documents.

    Args:
        documents:
            List of dictionaries containing:
            {
                "text": "...",
                "source": "filename.pdf"
            }

    Returns:
        List of extracted identifier records.
    """

    results = []

    for document in documents:

        text = document.get("text", "")
        source = document.get("source")

        identifiers = extract_all_identifiers(text)

        results.append(
            {
                "source": source,
                "identifiers": identifiers
            }
        )

    return results


def find_primary_shipment_identifier(identifiers):
    """
    Select the most likely primary shipment identifier.

    Priority:
        shipment_id
        load_id
        pro_number
        bol_number
        booking_number
        consignment_number
        waybill_number
        reference_number
    """

    priority = [
        "shipment_id",
        "load_id",
        "pro_number",
        "bol_number",
        "booking_number",
        "consignment_number",
        "waybill_number",
        "reference_number",
    ]

    for identifier_type in priority:

        value = identifiers.get(identifier_type)

        if value:
            return {
                "type": identifier_type,
                "value": value
            }

    return None