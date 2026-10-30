"""
Universal logistics document classifier.

Classifies documents using their CONTENT instead of relying
on filenames.
"""

import re

from logistics_fields import DOCUMENT_TYPE_ALIASES


# ============================================================
# DOCUMENT TYPE KEYWORDS
# ============================================================

DOCUMENT_KEYWORDS = {

    "BOL": [
        "bill of lading",
        "shipper",
        "consignee",
        "freight terms",
        "number of pieces",
        "total weight",
        "ship from",
        "ship to",
    ],

    "POD": [
        "proof of delivery",
        "received by",
        "delivery date",
        "pieces delivered",
        "delivered by",
        "signature",
        "receiver comments",
        "delivery status",
    ],

    "INVOICE": [
        "invoice number",
        "invoice date",
        "invoice amount",
        "total invoice amount",
        "subtotal",
        "fuel surcharge",
        "payment terms",
        "amount due",
        "grand total",
    ],

    "RATE_CONFIRMATION": [
        "rate confirmation",
        "rate con",
        "load confirmation",
        "agreed rate",
        "linehaul rate",
        "carrier rate",
    ],
}


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_text(text):
    """Normalize document text for classification."""

    if not text:
        return ""

    text = text.lower()

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text)

    return text.strip()


# ============================================================
# KEYWORD SCORING
# ============================================================

def score_document_type(text, document_type):
    """
    Calculate how strongly a document matches a document type.
    """

    normalized = normalize_text(text)

    keywords = DOCUMENT_KEYWORDS.get(
        document_type,
        []
    )

    score = 0
    matched_keywords = []

    for keyword in keywords:

        keyword_normalized = normalize_text(keyword)

        if keyword_normalized in normalized:

            score += 1
            matched_keywords.append(keyword)

    return {
        "score": score,
        "matched_keywords": matched_keywords,
    }


# ============================================================
# DOCUMENT TITLE
# ============================================================

# The name a document gives itself. A rate confirmation lists freight,
# fuel and payment terms like an invoice, so counting keywords alone
# calls it an invoice; its title says what it is.
TITLE_PHRASES = {
    "RATE_CONFIRMATION": ("rate confirmation", "load confirmation"),
    "BOL": ("bill of lading",),
    "POD": ("proof of delivery", "delivery receipt"),
    "INVOICE": ("invoice",),
}

# Lines read as the heading
HEADING_LINES = 3


def title_type(text):
    """
    (document type, phrase) named in the document's heading, or (None, None).
    The type named first wins; a phrase followed by a colon, "number",
    "no" or "#" is a reference to another document ("Bill of Lading No:
    123"), not a title.
    """

    lines = [line.strip().lower() for line in str(text or "").splitlines() if line.strip()][:HEADING_LINES]
    heading = " | ".join(lines)

    found = []

    for document_type, phrases in TITLE_PHRASES.items():
        for phrase in phrases:
            for match in re.finditer(rf"\b{re.escape(phrase)}\b(?!\s*(?::|#|number\b|no\b|no\.))", heading):
                found.append((match.start(), document_type, phrase))

    if not found:
        return None, None

    _, document_type, phrase = min(found)

    return document_type, phrase


# ============================================================
# DOCUMENT CLASSIFICATION
# ============================================================

def classify_document(text):
    """
    Classify a document based on its content: by the type its heading
    names (title_type), otherwise by keyword counts.

    Returns:

        {
            "document_type": "BOL",
            "confidence": 0.75,
            "score": 6,
            "matched_keywords": [...]
        }
    """

    scores = {}

    for document_type in DOCUMENT_KEYWORDS:

        scores[document_type] = score_document_type(
            text,
            document_type
        )

    titled, phrase = title_type(text)

    if titled:

        return {
            "document_type": titled,
            "confidence": 1.0,
            "score": scores[titled]["score"],
            "matched_keywords": [phrase] + [k for k in scores[titled]["matched_keywords"] if k != phrase],
            "all_scores": scores,
            "basis": "title",
        }

    # Sort document types by score
    ranked = sorted(
        scores.items(),
        key=lambda item: item[1]["score"],
        reverse=True
    )

    best_type, best_result = ranked[0]

    best_score = best_result["score"]

    # Calculate simple confidence
    total_score = sum(
        result["score"]
        for result in scores.values()
    )

    if total_score > 0:

        confidence = best_score / total_score

    else:

        confidence = 0.0

    # Unknown if no meaningful keywords found
    if best_score == 0:

        return {
            "document_type": "UNKNOWN",
            "confidence": 0.0,
            "score": 0,
            "matched_keywords": [],
            "all_scores": scores,
        }

    return {
        "document_type": best_type,
        "confidence": round(confidence, 3),
        "score": best_score,
        "matched_keywords": best_result[
            "matched_keywords"
        ],
        "all_scores": scores,
    }


# ============================================================
# CLASSIFY MULTIPLE DOCUMENTS
# ============================================================

def classify_documents(documents):
    """
    Classify multiple documents.

    Expected input:

        [
            {
                "source": "document.pdf",
                "text": "..."
            }
        ]
    """

    results = []

    for document in documents:

        text = document.get("text", "")

        classification = classify_document(text)

        results.append(
            {
                "source": document.get("source"),
                **classification,
            }
        )

    return results