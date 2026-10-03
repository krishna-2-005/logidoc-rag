from pathlib import Path
import sys
import chromadb
import ollama

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CHROMA_DIR, COLLECTION_NAME, EMBEDDING_MODEL
from chunker import create_all_chunks


def get_collection():
    """
    Create or load the ChromaDB collection.
    """

    client = chromadb.PersistentClient(
        path=str(CHROMA_DIR)
    )

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME
    )

    return collection


def create_embedding(text):
    """
    Generate an embedding using Ollama.
    """

    response = ollama.embeddings(
        model=EMBEDDING_MODEL,
        prompt=text
    )

    return response["embedding"]


def build_vector_database():
    """
    Convert all document chunks into embeddings
    and store them in ChromaDB.
    """

    print("=" * 70)
    print("LOGIDOC-RAG VECTOR DATABASE")
    print("=" * 70)

    chunks = create_all_chunks()

    if not chunks:
        print("\nNo chunks found.")
        return

    collection = get_collection()

    # --------------------------------------------------------
    # Clear previous vectors
    # --------------------------------------------------------

    existing = collection.get()

    if existing["ids"]:

        collection.delete(
            ids=existing["ids"]
        )

        print(
            f"\nRemoved {len(existing['ids'])} old vectors."
        )

    # --------------------------------------------------------
    # Create embeddings
    # --------------------------------------------------------

    print(
        f"\nCreating embeddings for "
        f"{len(chunks)} chunks..."
    )

    ids = []
    documents = []
    embeddings = []
    metadatas = []

    for index, chunk in enumerate(chunks):

        print(
            f"Embedding {index + 1}/{len(chunks)}...",
            end="\r"
        )

        # Create embedding
        embedding = create_embedding(
            chunk["text"]
        )

        # Get metadata
        metadata = chunk["metadata"]

        # ----------------------------------------------------
        # Create a unique vector ID
        # ----------------------------------------------------

        shipment_id = (
            metadata.get("shipment_id")
            or "UNKNOWN_SHIPMENT"
        )

        vector_id = (
            f"{shipment_id}_{index}"
        )

        ids.append(
            vector_id
        )

        documents.append(
            chunk["text"]
        )

        embeddings.append(
            embedding
        )

        # ----------------------------------------------------
        # Make metadata compatible with ChromaDB
        #
        # ChromaDB does not accept None values.
        # Convert None to an empty string.
        # ----------------------------------------------------

        safe_metadata = {
            key: (
                "" if value is None else value
            )
            for key, value in metadata.items()
        }

        metadatas.append(
            safe_metadata
        )

    # --------------------------------------------------------
    # Store everything in ChromaDB
    # --------------------------------------------------------

    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas
    )

    print("\n")

    print("=" * 70)
    print(
        "VECTOR DATABASE CREATED SUCCESSFULLY"
    )
    print("=" * 70)

    print(
        f"\nDocuments indexed : {len(chunks)}"
    )

    print(
        f"Collection        : {COLLECTION_NAME}"
    )

    print(
        f"Database path     : {CHROMA_DIR}"
    )

    print(
        f"Embedding model   : {EMBEDDING_MODEL}"
    )

    print(
        "\nVector database is ready for RAG."
    )


def inspect_metadata():
    """
    Inspect the metadata stored in ChromaDB.

    This verifies that universal logistics identifiers
    are actually stored with each vector.
    """

    collection = get_collection()

    results = collection.get(
        include=[
            "metadatas"
        ]
    )

    metadatas = results.get(
        "metadatas",
        []
    )

    print("\n" + "=" * 70)
    print("CHROMADB METADATA INSPECTION")
    print("=" * 70)

    print(
        f"\nVectors found: {len(metadatas)}"
    )

    for index, metadata in enumerate(
        metadatas,
        start=1
    ):

        print("\n" + "-" * 70)

        print(
            f"Vector {index}"
        )

        print(
            f"Source          : "
            f"{metadata.get('source')}"
        )

        print(
            f"Document Type   : "
            f"{metadata.get('document_type')}"
        )

        print(
            f"Shipment ID     : "
            f"{metadata.get('shipment_id')}"
        )

        print(
            f"Load ID         : "
            f"{metadata.get('load_id')}"
        )

        print(
            f"BOL Number      : "
            f"{metadata.get('bol_number')}"
        )

        print(
            f"PRO Number      : "
            f"{metadata.get('pro_number')}"
        )

        print(
            f"PO Number       : "
            f"{metadata.get('po_number')}"
        )

        print(
            f"Invoice Number  : "
            f"{metadata.get('invoice_number')}"
        )

        print(
            f"Tracking Number : "
            f"{metadata.get('tracking_number')}"
        )

        print(
            f"Booking Number  : "
            f"{metadata.get('booking_number')}"
        )

        print(
            f"Reference Number: "
            f"{metadata.get('reference_number')}"
        )

        print(
            f"Consignment No. : "
            f"{metadata.get('consignment_number')}"
        )

        print(
            f"Waybill Number  : "
            f"{metadata.get('waybill_number')}"
        )

        print(
            f"Carrier Ref.    : "
            f"{metadata.get('carrier_reference')}"
        )

        print(
            f"Customer Ref.   : "
            f"{metadata.get('customer_reference')}"
        )

        print(
            f"Page            : "
            f"{metadata.get('page')}"
        )

        print(
            f"Chunk           : "
            f"{metadata.get('chunk')}"
        )

    print("\n" + "=" * 70)
    print(
        "Metadata inspection completed."
    )
    print("=" * 70)


if __name__ == "__main__":

    build_vector_database()

    inspect_metadata()