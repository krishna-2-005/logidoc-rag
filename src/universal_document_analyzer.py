"""
Universal logistics document analyzer.

Combines:
1. PDF text extraction
2. Content-based document classification
3. Universal identifier extraction
4. Primary shipment identification
"""

from pathlib import Path

from document_analyzer import extract_pdf_text
from identifier_extractor import (
    extract_all_identifiers,
    find_primary_shipment_identifier,
)
from document_classifier import classify_document


def analyze_document(pdf_path):
    """
    Analyze one PDF completely.

    The filename is NOT used to determine the document type.
    """

    pdf_path = Path(pdf_path)

    # --------------------------------------------------------
    # 1. Extract PDF text
    # --------------------------------------------------------

    pdf_data = extract_pdf_text(pdf_path)

    text = pdf_data["text"]
    page_count = pdf_data["page_count"]

    # --------------------------------------------------------
    # 2. Detect document type from CONTENT
    # --------------------------------------------------------

    classification = classify_document(text)

    # --------------------------------------------------------
    # 3. Extract all logistics identifiers
    # --------------------------------------------------------

    identifiers = extract_all_identifiers(text)

    # --------------------------------------------------------
    # 4. Find primary shipment identifier
    # --------------------------------------------------------

    primary_identifier = find_primary_shipment_identifier(
        identifiers
    )

    # --------------------------------------------------------
    # 5. Build complete document record
    # --------------------------------------------------------

    return {
        "source": pdf_path.name,
        "page_count": page_count,
        "document_type": classification["document_type"],
        "classification_confidence": classification["confidence"],
        "classification_score": classification["score"],
        "matched_keywords": classification[
            "matched_keywords"
        ],
        "identifiers": identifiers,
        "primary_identifier": primary_identifier,
    }


def analyze_directory(directory):
    """
    Analyze every PDF in a directory.
    """

    directory = Path(directory)

    pdf_files = sorted(
        directory.glob("*.pdf")
    )

    results = []

    for pdf_path in pdf_files:

        try:

            result = analyze_document(pdf_path)

            results.append(result)

        except Exception as error:

            results.append(
                {
                    "source": pdf_path.name,
                    "error": str(error),
                }
            )

    return results


def print_analysis(result):
    """
    Print a readable analysis result.
    """

    print("\n" + "=" * 60)

    print("DOCUMENT:", result.get("source"))

    if "error" in result:

        print("ERROR:", result["error"])
        return

    print("PAGES:", result.get("page_count"))

    print(
        "DOCUMENT TYPE:",
        result.get("document_type")
    )

    print(
        "CONFIDENCE:",
        result.get("classification_confidence")
    )

    print(
        "MATCHED KEYWORDS:",
        result.get("matched_keywords")
    )

    print(
        "IDENTIFIERS:",
        result.get("identifiers")
    )

    print(
        "PRIMARY IDENTIFIER:",
        result.get("primary_identifier")
    )

    print("=" * 60)