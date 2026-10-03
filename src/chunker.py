"""
Chunking utilities for logistics documents.

Phase 2:
Preserves source and page-level information so every retrieved
chunk can be traced back to the original PDF.
"""

from document_loader import load_all_documents


def create_chunks(text, chunk_size=1000, overlap=200):
    """
    Split text into overlapping chunks.
    """

    if not text:
        return []

    chunks = []

    start = 0

    while start < len(text):

        end = start + chunk_size

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break

        start = end - overlap

    return chunks


def create_document_chunks(document):
    """
    Create chunks for one document while preserving
    source, page and identifier metadata.
    """

    all_chunks = []

    identifiers = document.get("identifiers", {})

    for page_data in document.get("pages", []):

        page_number = page_data.get("page", 1)
        page_text = page_data.get("text", "")

        chunks = create_chunks(page_text)

        for chunk_index, chunk_text in enumerate(chunks):

            metadata = {
                "source": document.get("filename"),
                "document_type": document.get("document_type"),
                "shipment_id": identifiers.get("shipment_id"),
                "load_id": identifiers.get("load_id"),
                "bol_number": identifiers.get("bol_number"),
                "pro_number": identifiers.get("pro_number"),
                "po_number": identifiers.get("po_number"),
                "invoice_number": identifiers.get("invoice_number"),
                "tracking_number": identifiers.get("tracking_number"),
                "booking_number": identifiers.get("booking_number"),
                "reference_number": identifiers.get("reference_number"),
                "consignment_number": identifiers.get("consignment_number"),
                "waybill_number": identifiers.get("waybill_number"),
                "carrier_reference": identifiers.get("carrier_reference"),
                "customer_reference": identifiers.get("customer_reference"),
                "page": page_number,
                "chunk": chunk_index,
            }

            all_chunks.append(
                {
                    "text": chunk_text,
                    "metadata": metadata,
                }
            )

    return all_chunks


def create_all_chunks():
    """
    Load all documents and create chunks with
    page-level source metadata.
    """

    documents = load_all_documents()

    all_chunks = []

    for document in documents:

        document_chunks = create_document_chunks(document)

        all_chunks.extend(document_chunks)

    return all_chunks


if __name__ == "__main__":

    chunks = create_all_chunks()

    print("=" * 70)
    print("PHASE 2 - PAGE LEVEL SOURCE TEST")
    print("=" * 70)

    print("Total chunks:", len(chunks))

    for chunk in chunks[:5]:

        metadata = chunk["metadata"]

        print("\nSource:", metadata.get("source"))
        print("Document Type:", metadata.get("document_type"))
        print("Shipment ID:", metadata.get("shipment_id"))
        print("Page:", metadata.get("page"))
        print("Chunk:", metadata.get("chunk"))

        print("Text:")
        print(chunk["text"][:150])

    print("=" * 70)
    print("SOURCE METADATA TEST COMPLETED")
    print("=" * 70)