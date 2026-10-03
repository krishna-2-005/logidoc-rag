from pathlib import Path
import sys

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CHUNK_SIZE, CHUNK_OVERLAP
from document_loader import load_all_documents


def split_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """
    Split text into overlapping chunks.
    """

    if not text:
        return []

    chunks = []

    start = 0
    text_length = len(text)

    while start < text_length:

        end = start + chunk_size

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= text_length:
            break

        start = end - overlap

    return chunks


def create_chunks(document):
    """
    Convert a loaded logistics document into RAG chunks.

    All universal logistics identifiers detected from the
    document content are preserved in chunk metadata.
    """

    identifiers = document.get(
        "identifiers",
        {}
    )

    shipment_id = identifiers.get(
        "shipment_id"
    )

    invoice_number = identifiers.get(
        "invoice_number"
    )

    chunks = []

    for page in document["pages"]:

        page_chunks = split_text(
            page["text"]
        )

        for chunk_number, chunk_text in enumerate(
            page_chunks,
            start=1
        ):

            chunk_metadata = {
                "source": document["filename"],
                "document_type": document["document_type"],

                # Universal logistics identifiers
                "shipment_id": shipment_id,
                "load_id": identifiers.get("load_id"),
                "bol_number": identifiers.get("bol_number"),
                "pro_number": identifiers.get("pro_number"),
                "po_number": identifiers.get("po_number"),
                "invoice_number": invoice_number,
                "tracking_number": identifiers.get("tracking_number"),
                "booking_number": identifiers.get("booking_number"),
                "reference_number": identifiers.get("reference_number"),
                "consignment_number": identifiers.get("consignment_number"),
                "waybill_number": identifiers.get("waybill_number"),
                "carrier_reference": identifiers.get("carrier_reference"),
                "customer_reference": identifiers.get("customer_reference"),

                # Page/chunk information
                "page": page["page"],
                "chunk": chunk_number,
            }

            chunk = {
                "text": chunk_text,
                "metadata": chunk_metadata,
            }

            chunks.append(chunk)

    return chunks


def create_all_chunks():

    documents = load_all_documents()

    all_chunks = []

    for document in documents:

        document_chunks = create_chunks(
            document
        )

        all_chunks.extend(
            document_chunks
        )

    return all_chunks


def main():

    print("=" * 70)
    print("LogiDoc-RAG Universal Chunking Test")
    print("=" * 70)

    chunks = create_all_chunks()

    print(
        f"\nTotal chunks created: {len(chunks)}"
    )

    for index, chunk in enumerate(
        chunks,
        start=1
    ):

        metadata = chunk["metadata"]

        print("\n" + "-" * 70)

        print(
            f"Chunk ID        : {index}"
        )

        print(
            f"Source          : "
            f"{metadata['source']}"
        )

        print(
            f"Document Type   : "
            f"{metadata['document_type']}"
        )

        print(
            f"Shipment ID     : "
            f"{metadata['shipment_id']}"
        )

        print(
            f"BOL Number      : "
            f"{metadata['bol_number']}"
        )

        print(
            f"Invoice Number  : "
            f"{metadata['invoice_number']}"
        )

        print(
            f"Tracking Number : "
            f"{metadata['tracking_number']}"
        )

        print(
            f"Page            : "
            f"{metadata['page']}"
        )

        print(
            f"Chunk Number    : "
            f"{metadata['chunk']}"
        )

        print("\nText:")
        print(
            chunk["text"][:500]
        )

    print("\n" + "=" * 70)
    print(
        "Universal chunking completed successfully."
    )
    print("=" * 70)


if __name__ == "__main__":
    main()