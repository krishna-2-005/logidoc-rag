"""
Universal document analyzer.

Reads PDF content and extracts:
- document text
- logistics identifiers
- page count

The analysis is based on document content, not filename.
"""

import fitz

from identifier_extractor import (
    extract_all_identifiers,
    find_primary_shipment_identifier,
)


def extract_pdf_text(pdf_path):
    """
    Extract all text from a PDF.

    Returns:
        {
            "text": "...",
            "page_count": number
        }
    """

    document = fitz.open(pdf_path)

    pages = []

    for page in document:
        text = page.get_text("text", sort=True).strip()

        if text:
            pages.append(text)

    page_count = len(document)

    document.close()

    return {
        "text": "\n".join(pages),
        "page_count": page_count,
    }


def analyze_pdf(pdf_path):
    """
    Analyze one PDF using its actual document content.
    """

    pdf_data = extract_pdf_text(pdf_path)

    text = pdf_data["text"]

    identifiers = extract_all_identifiers(text)

    primary_identifier = find_primary_shipment_identifier(
        identifiers
    )

    return {
        "source": pdf_path.name,
        "page_count": pdf_data["page_count"],
        "identifiers": identifiers,
        "primary_identifier": primary_identifier,
    }


def analyze_pdf_directory(pdf_directory):
    """
    Analyze every PDF inside a directory.
    """

    pdf_files = sorted(
        pdf_directory.glob("*.pdf")
    )

    results = []

    for pdf_path in pdf_files:

        try:
            result = analyze_pdf(pdf_path)

            results.append(result)

        except Exception as error:

            results.append(
                {
                    "source": pdf_path.name,
                    "error": str(error),
                }
            )

    return results