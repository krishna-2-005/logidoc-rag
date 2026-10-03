from pathlib import Path
import sys

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from shipment_identity import (
    build_shipment_identity_map,
    find_shipment_by_identifier
)


# ============================================================
# Get Available Shipments
# ============================================================

def get_available_shipments():
    """
    Return all shipments detected from the uploaded documents.

    Each shipment contains:
    - primary shipment ID
    - all linked identifiers
    - document list
    - document count
    - document types
    """

    shipment_map = (
        build_shipment_identity_map()
    )

    shipments = []

    for shipment_id, shipment in shipment_map.items():

        documents = shipment.get(
            "documents",
            []
        )

        document_types = []

        for document in documents:

            document_type = document.get(
                "document_type"
            )

            if (
                document_type
                and document_type not in document_types
            ):

                document_types.append(
                    document_type
                )

        shipments.append(
            {
                "shipment_id": shipment_id,
                "identifiers": shipment.get(
                    "identifiers",
                    {}
                ),
                "documents": documents,
                "document_count": len(
                    documents
                ),
                "document_types": document_types
            }
        )

    return shipments


# ============================================================
# Get Shipment Options
# ============================================================

def get_shipment_options():
    """
    Return only the shipment IDs.

    This is useful for a Streamlit selectbox.
    """

    shipments = get_available_shipments()

    return [
        shipment["shipment_id"]
        for shipment in shipments
    ]


# ============================================================
# Get Shipment Details
# ============================================================

def get_shipment_details(
    shipment_identifier
):
    """
    Find shipment details using any supported identifier.

    Example:

        SHJ-2026-001
        INV-SHJ-2026-001
        BOL-5566
        LOAD-7788
    """

    shipment_map = (
        build_shipment_identity_map()
    )

    shipment = find_shipment_by_identifier(
        shipment_map,
        shipment_identifier
    )

    if not shipment:
        return None

    documents = shipment.get(
        "documents",
        []
    )

    document_types = []

    for document in documents:

        document_type = document.get(
            "document_type"
        )

        if (
            document_type
            and document_type not in document_types
        ):

            document_types.append(
                document_type
            )

    return {
        "shipment_id": shipment[
            "shipment_id"
        ],
        "identifiers": shipment.get(
            "identifiers",
            {}
        ),
        "documents": documents,
        "document_count": len(
            documents
        ),
        "document_types": document_types
    }


# ============================================================
# Display Shipment Selector
# ============================================================

def print_shipment_selector():
    """
    Display shipment information in a format
    suitable for the future UI selector.
    """

    print("\n" + "=" * 70)
    print("SHIPMENT SELECTOR")
    print("=" * 70)

    shipments = get_available_shipments()

    if not shipments:

        print("\nNo shipments detected.")

        return

    print(
        f"\nAvailable shipments: "
        f"{len(shipments)}"
    )

    for index, shipment in enumerate(
        shipments,
        start=1
    ):

        print("\n" + "-" * 70)

        print(
            f"{index}. "
            f"Shipment ID: "
            f"{shipment['shipment_id']}"
        )

        print(
            f"   Documents: "
            f"{shipment['document_count']}"
        )

        print(
            f"   Types: "
            f"{', '.join(shipment['document_types'])}"
        )

        print("   Files:")

        for document in shipment[
            "documents"
        ]:

            print(
                f"      - "
                f"{document['source']} "
                f"-> "
                f"{document['document_type']}"
            )

    print("\n" + "=" * 70)
    print("SHIPMENT SELECTOR DATA READY")
    print("=" * 70)


# ============================================================
# Main Test
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # Test 1: Display all shipments
    # --------------------------------------------------------

    print_shipment_selector()

    # --------------------------------------------------------
    # Test 2: Display selector options
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("SELECTOR OPTIONS")
    print("=" * 70)

    options = get_shipment_options()

    for index, option in enumerate(
        options,
        start=1
    ):

        print(
            f"{index}. {option}"
        )

    # --------------------------------------------------------
    # Test 3: Lookup shipment
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("SHIPMENT DETAILS TEST")
    print("=" * 70)

    test_identifier = (
        "SHJ-2026-001"
    )

    details = get_shipment_details(
        test_identifier
    )

    print(
        f"\nSearching for: "
        f"{test_identifier}"
    )

    if details:

        print(
            f"Shipment found: "
            f"{details['shipment_id']}"
        )

        print(
            f"Document count: "
            f"{details['document_count']}"
        )

        print(
            f"Document types: "
            f"{', '.join(details['document_types'])}"
        )

    else:

        print(
            "Shipment not found."
        )

    print("\n" + "=" * 70)
    print("ALL SHIPMENT SELECTOR TESTS COMPLETED")
    print("=" * 70)