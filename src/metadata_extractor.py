from pathlib import Path
import re
import sys

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from document_loader import load_all_documents


def extract_shipment_id(text):
    """
    Extract shipment ID from document text.
    Example:
    Shipment Number: SHJ-2026-001
    """

    pattern = r"Shipment Number:\s*([A-Za-z0-9-]+)"

    match = re.search(pattern, text, re.IGNORECASE)

    if match:
        return match.group(1)

    return "N/A"


def extract_invoice_number(text):
    """
    Extract invoice number from invoice documents.
    """

    pattern = r"Invoice Number:\s*([A-Za-z0-9-]+)"

    match = re.search(pattern, text, re.IGNORECASE)

    if match:
        return match.group(1)

    return "N/A"


def extract_metadata(document):
    """
    Extract important logistics metadata from a loaded document.
    """

    full_text = "\n".join(
        page["text"]
        for page in document["pages"]
    )

    shipment_id = extract_shipment_id(full_text)

    invoice_number = extract_invoice_number(full_text)

    metadata = {
        "filename": document["filename"],
        "document_type": document["document_type"],
        "shipment_id": shipment_id,
        "invoice_number": invoice_number,
        "page_count": document["page_count"],
    }

    return metadata


def main():

    print("=" * 70)
    print("LogiDoc-RAG Metadata Extraction Test")
    print("=" * 70)

    documents = load_all_documents()

    print(f"\nDocuments found: {len(documents)}")

    for document in documents:

        metadata = extract_metadata(document)

        print("\n" + "-" * 70)

        print(f"File          : {metadata['filename']}")
        print(f"Document Type : {metadata['document_type']}")
        print(f"Shipment ID   : {metadata['shipment_id']}")
        print(f"Invoice No.   : {metadata['invoice_number']}")
        print(f"Page Count    : {metadata['page_count']}")

    print("\n" + "=" * 70)
    print("Metadata extraction completed successfully.")
    print("=" * 70)


if __name__ == "__main__":
    main()