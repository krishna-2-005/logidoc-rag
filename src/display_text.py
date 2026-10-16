"""
LogiDoc-RAG display helpers for the Streamlit dashboard

Presentation only: they change how a string is shown, never the
answer, the citations or the evidence they come from.
"""

import re


# A "$" not already escaped with a backslash
UNESCAPED_DOLLAR = re.compile(r"(?<!\\)\$")

# One cited field: "Base Freight: $1,200.00", "Delivery Date: October 5, 2026"
FIELD = re.compile(r"[A-Za-z][A-Za-z0-9 &/().'-]{0,60}:\s*\S.*")

MAX_FIELD_LENGTH = 160


def escape_markdown_dollars(text):
    """
    Streamlit renders Markdown, where text between two "$" is LaTeX:
    "Base Freight $1,200.00 + Fuel Surcharge $180.00" would lose its
    dollar signs and change font. Each "$" is escaped so it is shown
    as written. A "$" that is already escaped is left alone, so
    escaping twice changes nothing.
    """

    return UNESCAPED_DOLLAR.sub(r"\\$", str(text or ""))


def cited_fields(text):
    """
    The fields of evidence that already lists exactly what an answer
    used, as calculations cite it ("Base Freight: $1,200.00 | Fuel
    Surcharge: $180.00 | ..."), in their original order. None for
    other evidence, such as a passage of document text.
    """

    segments = [segment.strip() for segment in str(text or "").split(" | ")]

    if len(segments) > 1 and all(
        len(segment) <= MAX_FIELD_LENGTH and FIELD.fullmatch(segment)
        for segment in segments
    ):
        return segments

    return None
