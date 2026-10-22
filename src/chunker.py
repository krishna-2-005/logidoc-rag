"""
Chunking utilities for logistics documents.

Phase 2:
Preserves source and page-level information so every retrieved
chunk can be traced back to the original PDF.
"""

from pathlib import Path

import document_loader
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


UNREADABLE = "unreadable"
NO_TEXT = "no_text"


def read_chunks(raw_dir=None, loader=None):
    """
    Like create_all_chunks(), but a PDF that cannot be used does not
    stop the others, and is not lost silently either. Returns
    (chunks, problems); problems lists every file that gave no chunks:

        {"filename", "status", "error"}

    status "unreadable": the PDF could not be opened or analysed (the
    error is kept); "no_text": it opened but no page has extractable
    text (an image-only scan; there is no OCR).

    With no problems the chunks are exactly create_all_chunks()'s.
    raw_dir defaults to the project's document folder; a missing
    folder gives no chunks and no problems.
    """

    raw_dir = Path(raw_dir) if raw_dir is not None else Path(document_loader.RAW_DATA_DIR)
    load = loader or document_loader.load_document

    chunks, problems = [], []

    if not raw_dir.exists():
        return chunks, problems

    for path in sorted(raw_dir.glob("*.pdf")):

        try:
            document = load(path)
        except Exception as error:
            problems.append({
                "filename": path.name,
                "status": UNREADABLE,
                "error": f"{type(error).__name__}: {error}"[:200],
            })
            continue

        document_chunks = create_document_chunks(document)

        if not document_chunks:
            problems.append({"filename": path.name, "status": NO_TEXT, "error": None})
            continue

        chunks.extend(document_chunks)

    return chunks, problems


def describe_problems(problems):
    """
    The problem files as one line of text.
    """

    labels = {
        UNREADABLE: "unreadable",
        NO_TEXT: "no extractable text",
        # Ingestion register codes (ingestion_service)
        "needs_ocr": "image-only scan, needs OCR",
        "corrupted": "damaged PDF",
        "password_protected": "password-protected",
        "interrupted": "ingestion did not finish",
        "retries_exhausted": "ingestion failed, retry needed",
    }

    return ", ".join(
        f"{item['filename']} ({labels.get(item['status'], item['status'])})"
        for item in problems
    )


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