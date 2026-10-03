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
# DOCUMENT CLASSIFICATION
# ============================================================

def classify_document(text):
    """
    Classify a document based on its content.

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