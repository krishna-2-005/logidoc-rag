from pathlib import Path
import sys
import chromadb
import ollama

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    CHROMA_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    TOP_K
)

from shipment_identity import (
    build_shipment_identity_map,
    find_shipment_by_identifier
)


# ============================================================
# ChromaDB
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
# Create Query Embedding
# ============================================================

def create_embedding(text):
    """
    Create an embedding for the user's query.
    """

    response = ollama.embeddings(
        model=EMBEDDING_MODEL,
        prompt=text
    )

    return response["embedding"]


# ============================================================
# Resolve Shipment
# ============================================================

def resolve_shipment(identifier):
    """
    Resolve any supported logistics identifier
    to the primary shipment ID.
    """

    shipment_map = (
        build_shipment_identity_map()
    )

    shipment = find_shipment_by_identifier(
        shipment_map,
        identifier
    )

    if not shipment:
        return None

    return shipment["shipment_id"]


# ============================================================
# Search Shipment
# ============================================================

def search_shipment(
    shipment_identifier,
    query,
    top_k=None
):
    """
    Search only documents belonging to the
    requested shipment.

    The shipment can be identified using:
        - Shipment ID
        - Invoice Number
        - Load ID
        - BOL Number
        - PRO Number
        - Tracking Number
        - Other supported identifiers
    """

    if top_k is None:
        top_k = TOP_K

    # --------------------------------------------------------
    # Resolve identifier
    # --------------------------------------------------------

    shipment_id = resolve_shipment(
        shipment_identifier
    )

    if not shipment_id:
        return {
            "shipment_id": None,
            "query": query,
            "results": []
        }

    # --------------------------------------------------------
    # Create query embedding
    # --------------------------------------------------------

    query_embedding = create_embedding(
        query
    )

    collection = get_collection()

    # --------------------------------------------------------
    # Search ONLY inside this shipment
    # --------------------------------------------------------

    results = collection.query(
        query_embeddings=[
            query_embedding
        ],
        n_results=top_k,
        where={
            "shipment_id": shipment_id
        },
        include=[
            "documents",
            "metadatas",
            "distances"
        ]
    )

    documents = results.get(
        "documents",
        [[]]
    )[0]

    metadatas = results.get(
        "metadatas",
        [[]]
    )[0]

    distances = results.get(
        "distances",
        [[]]
    )[0]

    formatted_results = []

    for index, document in enumerate(
        documents
    ):

        metadata = {}

        if index < len(metadatas):

            metadata = (
                metadatas[index]
                or {}
            )

        distance = None

        if index < len(distances):

            distance = distances[index]

        formatted_results.append(
            {
                "text": document,
                "metadata": metadata,
                "distance": distance
            }
        )

    return {
        "shipment_id": shipment_id,
        "query": query,
        "results": formatted_results
    }


# ============================================================
# Display Search Results
# ============================================================

def print_search_results(
    shipment_identifier,
    query
):
    """
    Run and display a shipment-specific search.
    """

    print("\n" + "=" * 70)
    print("SHIPMENT-SPECIFIC RAG SEARCH")
    print("=" * 70)

    print(
        f"\nRequested identifier: "
        f"{shipment_identifier}"
    )

    print(
        f"Query: "
        f"{query}"
    )

    search_result = search_shipment(
        shipment_identifier,
        query
    )

    shipment_id = search_result[
        "shipment_id"
    ]

    if not shipment_id:

        print(
            "\nShipment could not be resolved."
        )

        return

    print(
        f"\nResolved shipment: "
        f"{shipment_id}"
    )

    results = search_result[
        "results"
    ]

    print(
        f"Results returned: "
        f"{len(results)}"
    )

    if not results:

        print(
            "\nNo matching documents found."
        )

        return

    for index, result in enumerate(
        results,
        start=1
    ):

        metadata = result[
            "metadata"
        ]

        print("\n" + "-" * 70)

        print(
            f"Result {index}"
        )

        print(
            f"Source        : "
            f"{metadata.get('source')}"
        )

        print(
            f"Document Type : "
            f"{metadata.get('document_type')}"
        )

        print(
            f"Shipment ID   : "
            f"{metadata.get('shipment_id')}"
        )

        print(
            f"Page          : "
            f"{metadata.get('page')}"
        )

        print(
            f"Distance      : "
            f"{result['distance']}"
        )

        print("\nText:")

        print(
            result["text"]
        )

    print("\n" + "=" * 70)
    print(
        "SHIPMENT-SPECIFIC SEARCH COMPLETED"
    )
    print("=" * 70)


# ============================================================
# Main Tests
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # Test 1
    # Search using Shipment ID
    # --------------------------------------------------------

    print_search_results(
        "SHJ-2026-001",
        "What is the total invoice amount?"
    )

    # --------------------------------------------------------
    # Test 2
    # Search using Invoice Number
    # --------------------------------------------------------

    print_search_results(
        "INV-SHJ-2026-001",
        "How many pieces were delivered?"
    )