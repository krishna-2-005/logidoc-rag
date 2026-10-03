from pathlib import Path
import sys

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from chunker import create_all_chunks


# ============================================================
# Shipment Grouping
# ============================================================

def group_documents_by_shipment():
    """
    Group documents according to their extracted shipment ID.

    The grouping is based on identifiers extracted from the
    document content, not on filenames.
    """

    chunks = create_all_chunks()

    if not chunks:
        print("\nNo document chunks found.")
        return {}

    shipments = {}

    for chunk in chunks:

        metadata = chunk.get("metadata", {})

        shipment_id = metadata.get("shipment_id")

        source = metadata.get("source")
        document_type = metadata.get("document_type")

        # ----------------------------------------------------
        # Documents without a shipment identifier
        # ----------------------------------------------------

        if not shipment_id:
            shipment_id = "UNKNOWN_SHIPMENT"

        # ----------------------------------------------------
        # Create shipment group
        # ----------------------------------------------------

        if shipment_id not in shipments:

            shipments[shipment_id] = {
                "shipment_id": shipment_id,
                "documents": []
            }

        # ----------------------------------------------------
        # Avoid adding the same document multiple times
        # ----------------------------------------------------

        existing_sources = [
            document["source"]
            for document in shipments[shipment_id]["documents"]
        ]

        if source not in existing_sources:

            shipments[shipment_id]["documents"].append(
                {
                    "source": source,
                    "document_type": document_type
                }
            )

    return shipments


# ============================================================
# Display Shipment Groups
# ============================================================

def print_shipment_groups(shipments):
    """
    Display all shipments and their associated documents.
    """

    print("\n" + "=" * 70)
    print("SHIPMENT GROUPING")
    print("=" * 70)

    if not shipments:
        print("\nNo shipments found.")
        return

    print(
        f"\nTotal shipments detected: {len(shipments)}"
    )

    for shipment_id, shipment in shipments.items():

        print("\n" + "-" * 70)

        print(
            f"Shipment ID: {shipment_id}"
        )

        documents = shipment["documents"]

        print(
            f"Documents  : {len(documents)}"
        )

        for index, document in enumerate(
            documents,
            start=1
        ):

            print(
                f"  {index}. "
                f"{document['source']} "
                f"-> "
                f"{document['document_type']}"
            )

    print("\n" + "=" * 70)
    print("SHIPMENT GROUPING COMPLETED")
    print("=" * 70)


# ============================================================
# Find a Specific Shipment
# ============================================================

def get_shipment(shipments, shipment_id):
    """
    Return information for a specific shipment.
    """

    return shipments.get(shipment_id)


# ============================================================
# Main Test
# ============================================================

if __name__ == "__main__":

    shipments = group_documents_by_shipment()

    print_shipment_groups(shipments)