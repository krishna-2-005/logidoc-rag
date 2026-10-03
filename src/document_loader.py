from pathlib import Path
import sys
import pymupdf

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import RAW_DATA_DIR

from document_classifier import classify_document as classify_content
from identifier_extractor import (
    extract_all_identifiers,
    find_primary_shipment_identifier,
)


def classify_document(filename, text=None):
    """
    Identify the logistics document type.

    Primary method:
        Classify using document CONTENT.

    Fallback:
        Use filename only if document content cannot be classified.
    """

    # --------------------------------------------------------
    # CONTENT-BASED CLASSIFICATION
    # --------------------------------------------------------

    if text and text.strip():

        result = classify_content(text)

        document_type = result.get(
            "document_type",
            "UNKNOWN"
        )

        if document_type != "UNKNOWN":

            return document_type

    # --------------------------------------------------------
    # FILENAME FALLBACK
    # --------------------------------------------------------

    filename_upper = filename.upper()

    if filename_upper.startswith("BOL_"):
        return "BOL"

    if filename_upper.startswith("POD_"):
        return "POD"

    if filename_upper.startswith("INVOICE_"):
        return "INVOICE"

    if filename_upper.startswith("RATE_CONFIRMATION_"):
        return "RATE_CONFIRMATION"

    return "UNKNOWN"


def extract_pdf(pdf_path):
    """
    Extract text from a PDF page by page.
    """

    document = pymupdf.open(pdf_path)

    pages = []

    for page_number, page in enumerate(
        document,
        start=1
    ):

        # sort=True keeps text in visual reading order
        text = page.get_text(
            "text",
            sort=True
        ).strip()

        if text:

            pages.append(
                {
                    "page": page_number,
                    "text": text
                }
            )

    document.close()

    return pages


def load_document(pdf_path):
    """
    Load one logistics PDF and return structured document data.

    Document type and identifiers are detected from the
    actual PDF content rather than relying on the filename.
    """

    pdf_path = Path(pdf_path)

    # --------------------------------------------------------
    # 1. Extract PDF pages
    # --------------------------------------------------------

    pages = extract_pdf(pdf_path)

    # Combine page text for analysis
    full_text = "\n".join(
        page["text"]
        for page in pages
    )

    # --------------------------------------------------------
    # 2. Detect document type from CONTENT
    # --------------------------------------------------------

    document_type = classify_document(
        pdf_path.name,
        full_text
    )

    # --------------------------------------------------------
    # 3. Extract all logistics identifiers
    # --------------------------------------------------------

    identifiers = extract_all_identifiers(
        full_text
    )

    # --------------------------------------------------------
    # 4. Find primary shipment identifier
    # --------------------------------------------------------

    primary_identifier = (
        find_primary_shipment_identifier(
            identifiers
        )
    )

    # --------------------------------------------------------
    # 5. Return structured document
    # --------------------------------------------------------

    return {
        "filename": pdf_path.name,
        "path": str(pdf_path),
        "document_type": document_type,
        "page_count": len(pages),
        "pages": pages,
        "identifiers": identifiers,
        "primary_identifier": primary_identifier,
    }


def load_all_documents():
    """
    Load all PDF documents from the raw data directory.
    """

    documents = []

    pdf_files = sorted(
        RAW_DATA_DIR.glob("*.pdf")
    )

    for pdf_file in pdf_files:

        document = load_document(
            pdf_file
        )

        documents.append(
            document
        )

    return documents


if __name__ == "__main__":

    print("=" * 60)
    print("LogiDoc-RAG Universal Document Loader Test")
    print("=" * 60)

    documents = load_all_documents()

    print(
        f"\nDocuments found: {len(documents)}"
    )

    for document in documents:

        print("\n" + "-" * 60)

        print(
            f"File              : "
            f"{document['filename']}"
        )

        print(
            f"Type              : "
            f"{document['document_type']}"
        )

        print(
            f"Pages             : "
            f"{document['page_count']}"
        )

        print(
            f"Identifiers       : "
            f"{document['identifiers']}"
        )

        print(
            f"Primary Identifier: "
            f"{document['primary_identifier']}"
        )

        for page in document["pages"]:

            print(
                f"\nPage {page['page']}"
            )

            print(
                page["text"][:500]
            )

    print("\n" + "=" * 60)
    print(
        "Universal document loading "
        "completed successfully."
    )
    print("=" * 60)