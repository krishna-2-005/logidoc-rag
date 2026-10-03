from pathlib import Path
import sys
import chromadb

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CHROMA_DIR, COLLECTION_NAME
from shipment_identity import (
    build_shipment_identity_map,
    find_shipment_by_identifier
)


# ============================================================
# ChromaDB Collection
# ============================================================

def get_collection():
    """
    Load the existing ChromaDB collection.
    """

    client = chromadb.PersistentClient(
        path=str(CHROMA_DIR)
    )

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME
    )

    return collection


# ============================================================
# Resolve Shipment
# ============================================================

def resolve_shipment(identifier):
    """
    Resolve any known logistics identifier to a shipment.

    Examples:

        SHJ-2026-001
        INV-SHJ-2026-001
        BOL-5566
        LOAD-7788

    can all potentially resolve to the same shipment.
    """

    shipment_map = build_shipment_identity_map()

    shipment = find_shipment_by_identifier(
        shipment_map,
        identifier
    )

    return shipment


# ============================================================
# Get Shipment ID
# ============================================================

def get_resolved_shipment_id(identifier):
    """
    Return the primary shipment ID associated with
    the supplied identifier.
    """

    shipment = resolve_shipment(
        identifier
    )

    if not shipment:
        return None

    return shipment["shipment_id"]


# ============================================================
# Get Shipment Documents
# ============================================================

def get_shipment_documents(identifier):
    """
    Retrieve all ChromaDB vectors belonging to
    the resolved shipment.
    """

    shipment_id = get_resolved_shipment_id(
        identifier
    )

    if not shipment_id:
        return []

    collection = get_collection()

    results = collection.get(
        where={
            "shipment_id": shipment_id
        },
        include=[
            "documents",
            "metadatas"
        ]
    )

    documents = []

    result_documents = results.get(
        "documents",
        []
    )

    result_metadatas = results.get(
        "metadatas",
        []
    )

    for index, document_text in enumerate(
        result_documents
    ):

        metadata = {}

        if index < len(result_metadatas):

            metadata = (
                result_metadatas[index]
                or {}
            )

        documents.append(
            {
                "text": document_text,
                "metadata": metadata
            }
        )

    return documents


# ============================================================
# Get Unique Shipment Sources
# ============================================================

def get_shipment_sources(identifier):
    """
    Return unique document sources belonging
    to the shipment.
    """

    documents = get_shipment_documents(
        identifier
    )

    sources = []

    for document in documents:

        source = document[
            "metadata"
        ].get("source")

        if source and source not in sources:

            sources.append(source)

    return sources


# ============================================================
# Display Shipment Documents
# ============================================================

def print_shipment_documents(identifier):
    """
    Display documents belonging to a shipment.
    """

    print("\n" + "=" * 70)
    print("SHIPMENT-SPECIFIC DOCUMENT FILTER")
    print("=" * 70)

    print(
        f"\nRequested identifier: "
        f"{identifier}"
    )

    shipment_id = get_resolved_shipment_id(
        identifier
    )

    if not shipment_id:

        print(
            "\nShipment could not be resolved."
        )

        return

    print(
        f"Resolved shipment ID: "
        f"{shipment_id}"
    )

    documents = get_shipment_documents(
        identifier
    )

    print(
        f"\nVectors retrieved: "
        f"{len(documents)}"
    )

    if not documents:

        print(
            "\nNo documents found for this shipment."
        )

        return

    print("\nDocuments:")

    sources = get_shipment_sources(
        identifier
    )

    for index, source in enumerate(
        sources,
        start=1
    ):

        document_type = ""

        for document in documents:

            metadata = document[
                "metadata"
            ]

            if metadata.get("source") == source:

                document_type = metadata.get(
                    "document_type",
                    ""
                )

                break

        print(
            f"  {index}. "
            f"{source} "
            f"-> "
            f"{document_type}"
        )

    print("\n" + "=" * 70)
    print(
        "SHIPMENT-SPECIFIC FILTER COMPLETED"
    )
    print("=" * 70)


# ============================================================
# Main Test
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # Test 1: Direct Shipment ID
    # --------------------------------------------------------

    print_shipment_documents(
        "SHJ-2026-001"
    )

    # --------------------------------------------------------
    # Test 2: Invoice Number
    # --------------------------------------------------------

    print_shipment_documents(
        "INV-SHJ-2026-001"
    )