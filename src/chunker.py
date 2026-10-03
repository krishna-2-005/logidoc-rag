from pathlib import Path
import sys

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CHUNK_SIZE, CHUNK_OVERLAP
from document_loader import load_all_documents
from metadata_extractor import extract_metadata


def split_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """
    Split text into overlapping chunks.

    Example:
        chunk_size = 1000
        overlap = 200

    Each new chunk keeps some text from the previous chunk
    so important context is not lost at boundaries.
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
    """

    metadata = extract_metadata(document)

    chunks = []

    for page in document["pages"]:

        page_chunks = split_text(page["text"])

        for chunk_number, chunk_text in enumerate(
            page_chunks,
            start=1
        ):

            chunk = {
                "text": chunk_text,

                "metadata": {
                    "source": document["filename"],
                    "document_type": document["document_type"],
                    "shipment_id": metadata["shipment_id"],
                    "invoice_number": metadata["invoice_number"],
                    "page": page["page"],
                    "chunk": chunk_number,
                }
            }

            chunks.append(chunk)

    return chunks


def create_all_chunks():

    documents = load_all_documents()

    all_chunks = []

    for document in documents:

        document_chunks = create_chunks(document)

        all_chunks.extend(document_chunks)

    return all_chunks


def main():

    print("=" * 70)
    print("LogiDoc-RAG Smart Chunking Test")
    print("=" * 70)

    chunks = create_all_chunks()

    print(f"\nTotal chunks created: {len(chunks)}")

    for index, chunk in enumerate(chunks, start=1):

        print("\n" + "-" * 70)

        print(f"Chunk ID      : {index}")
        print(f"Source        : {chunk['metadata']['source']}")
        print(f"Document Type : {chunk['metadata']['document_type']}")
        print(f"Shipment ID   : {chunk['metadata']['shipment_id']}")
        print(f"Page          : {chunk['metadata']['page']}")
        print(f"Chunk Number  : {chunk['metadata']['chunk']}")

        print("\nText:")
        print(chunk["text"][:500])

    print("\n" + "=" * 70)
    print("Chunking completed successfully.")
    print("=" * 70)


if __name__ == "__main__":
    main()