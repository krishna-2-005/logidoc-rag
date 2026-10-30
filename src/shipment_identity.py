from pathlib import Path
import re
import sys

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from chunker import create_all_chunks


# ============================================================
# Identifier Fields
# ============================================================

IDENTIFIER_FIELDS = [
    "shipment_id",
    "load_id",
    "bol_number",
    "pro_number",
    "po_number",
    "invoice_number",
    "tracking_number",
    "booking_number",
    "reference_number",
    "consignment_number",
    "waybill_number",
    "carrier_reference",
    "customer_reference",
]


# ============================================================
# Identifier Priority
# ============================================================

PRIMARY_IDENTIFIER_PRIORITY = [
    "shipment_id",
    "load_id",
    "pro_number",
    "bol_number",
    "booking_number",
    "consignment_number",
    "waybill_number",
    "reference_number",
]


# ============================================================
# Normalize Identifier
# ============================================================

def normalize_identifier(value):
    """
    Normalize an identifier for reliable comparison.
    """

    if value is None:
        return None

    value = str(value).strip().upper()

    if not value:
        return None

    return value


def identifier_key(value):
    """
    Comparison key that ignores case, spaces, hyphens and other
    separators, so "SHJ-2026-001", "shj 2026 001" and "SHJ2026001"
    compare equal. Returns None for an empty identifier.
    """

    normalized = normalize_identifier(value)

    if not normalized:
        return None

    key = "".join(
        character
        for character in normalized
        if character.isalnum()
    )

    return key or None


def separator_positions(value):
    """
    Where a printed identifier is split by separators, as positions in
    its key: "AB-12-3" -> {2, 4}, "SHJ 2026-001" -> {3, 7},
    "SHJ2026001" -> set(). Leading and trailing separators do not count.
    """

    positions = set()
    count = 0
    pending = False

    for character in normalize_identifier(value) or "":

        if character.isalnum():
            if pending and count:
                positions.add(count)
            pending = False
            count += 1
        else:
            pending = True

    return positions


def identifier_collisions(values):
    """
    Pairs of printed identifiers that share a key (identifier_key) but
    split it at different places, such as "AB-12-3" and "AB-1-23" (both
    AB123): they may be two different shipments, so they must not be
    treated as one.

    Forms that only leave out or add separators ("SHJ-2026-001",
    "shj 2026 001", "SHJ2026001", "SHJ2026-001", "SHJ - 2026 - 001")
    are the same identifier written differently and never collide: one
    form's separator positions contain the other's.

    Returns the colliding (a, b) pairs, sorted; empty when the values
    agree. The key itself is unchanged, so stored data keeps its keys.
    """

    forms = sorted({
        str(value).strip()
        for value in values
        if identifier_key(value)
    })

    pairs = []

    for index, first in enumerate(forms):

        for second in forms[index + 1:]:

            if identifier_key(first) != identifier_key(second):
                continue

            a, b = separator_positions(first), separator_positions(second)

            if not (a <= b or b <= a):
                pairs.append((first, second))

    return pairs


def collision_message(pairs):
    """
    Why documents with colliding shipment IDs are not used together.
    """

    forms = sorted({form for pair in pairs for form in pair})
    key = identifier_key(forms[0]) if forms else ""

    return (
        f"Documents print the shipment IDs {', '.join(forms)}, which differ "
        f"only in where the separators are and so cannot be told apart "
        f"(all become {key}). They may be different shipments, so their "
        "documents are not used together. Correct the shipment ID on the "
        "documents that are wrong and upload them again."
    )


def belongs_to_shipment(metadata, text, shipment_id):
    """
    Whether a chunk belongs to a shipment. A chunk with a shipment_id
    in its metadata belongs to that shipment only (case, spaces and
    hyphens ignored); naming another shipment's ID in its text does
    not make it part of this one. Only a chunk without a shipment_id
    in its metadata is judged by its text, which must contain the ID
    as a whole identifier. Without a shipment_id every chunk belongs.
    """

    requested = identifier_key(shipment_id)

    if not requested:
        return True

    own = identifier_key((metadata or {}).get("shipment_id"))

    if own:
        return own == requested

    pattern = (
        r"(?<![A-Za-z0-9])"
        + r"[\W_]{0,3}".join(re.escape(character) for character in requested)
        + r"(?![A-Za-z0-9])"
    )

    return bool(re.search(pattern, str(text or ""), re.IGNORECASE))


# ============================================================
# Extract Document Identifiers
# ============================================================

def extract_document_identifiers(metadata):
    """
    Extract every available logistics identifier
    from document metadata.
    """

    identifiers = {}

    for field in IDENTIFIER_FIELDS:

        value = normalize_identifier(
            metadata.get(field)
        )

        if value:

            identifiers[field] = value

    return identifiers


# ============================================================
# Get Document Key
# ============================================================

def get_document_key(metadata):
    """
    Create a unique key for a document.

    Source + document type are used because the same document
    can generate multiple chunks.
    """

    source = metadata.get("source", "")
    document_type = metadata.get(
        "document_type",
        ""
    )

    return (
        f"{source}::{document_type}"
    )


# ============================================================
# Collect Unique Documents
# ============================================================

def collect_documents():
    """
    Convert chunks into a unique document-level structure.

    Multiple chunks from the same document are combined.
    """

    chunks = create_all_chunks()

    documents = {}

    for chunk in chunks:

        metadata = chunk.get(
            "metadata",
            {}
        )

        document_key = get_document_key(
            metadata
        )

        if document_key not in documents:

            documents[document_key] = {
                "source": metadata.get(
                    "source"
                ),
                "document_type": metadata.get(
                    "document_type"
                ),
                "identifiers": {}
            }

        identifiers = extract_document_identifiers(
            metadata
        )

        for field, value in identifiers.items():

            documents[document_key][
                "identifiers"
            ][field] = value

    return list(documents.values())


# ============================================================
# Find Primary Identity
# ============================================================

def get_primary_identity(identifiers):
    """
    Select the strongest available identifier.
    """

    for field in PRIMARY_IDENTIFIER_PRIORITY:

        value = identifiers.get(field)

        if value:

            return value

    # If none of the preferred identifiers exist,
    # use any available identifier.
    for value in identifiers.values():

        if value:

            return value

    return "UNKNOWN_SHIPMENT"


# ============================================================
# Build Identifier Index
# ============================================================

def build_identifier_index(documents):
    """
    Create an index:

        identifier value
              ->
        documents containing that identifier
    """

    identifier_index = {}

    for document_index, document in enumerate(
        documents
    ):

        identifiers = document[
            "identifiers"
        ]

        for field, value in identifiers.items():

            normalized_value = normalize_identifier(
                value
            )

            if not normalized_value:
                continue

            key = (
                field,
                normalized_value
            )

            if key not in identifier_index:

                identifier_index[key] = []

            identifier_index[key].append(
                document_index
            )

    return identifier_index


# ============================================================
# Union-Find
# ============================================================

class UnionFind:
    """
    Simple Union-Find structure used to connect
    documents that share identifiers.
    """

    def __init__(self, size):

        self.parent = list(
            range(size)
        )

    def find(self, value):

        while self.parent[value] != value:

            self.parent[value] = self.parent[
                self.parent[value]
            ]

            value = self.parent[value]

        return value

    def union(self, first, second):

        root_first = self.find(first)
        root_second = self.find(second)

        if root_first != root_second:

            self.parent[root_second] = root_first


# ============================================================
# Link Documents
# ============================================================

def link_documents_by_identifiers(documents):
    """
    Connect documents that share the same identifier.

    Example:

        BOL  -> BOL-5566
        POD  -> BOL-5566

    These documents will be placed in the same shipment group.
    """

    if not documents:
        return []

    union_find = UnionFind(
        len(documents)
    )

    identifier_index = build_identifier_index(
        documents
    )

    # --------------------------------------------------------
    # Connect documents sharing an identifier
    # --------------------------------------------------------

    for document_indexes in identifier_index.values():

        if len(document_indexes) < 2:
            continue

        first_document = document_indexes[0]

        for other_document in document_indexes[1:]:

            union_find.union(
                first_document,
                other_document
            )

    # --------------------------------------------------------
    # Build connected groups
    # --------------------------------------------------------

    groups = {}

    for document_index in range(
        len(documents)
    ):

        root = union_find.find(
            document_index
        )

        if root not in groups:

            groups[root] = []

        groups[root].append(
            documents[document_index]
        )

    return list(
        groups.values()
    )


# ============================================================
# Build Shipment Identity Map
# ============================================================

def build_shipment_identity_map():
    """
    Build shipment groups using cross-document
    identifier relationships.
    """

    documents = collect_documents()

    if not documents:
        return {}

    groups = link_documents_by_identifiers(
        documents
    )

    shipment_map = {}

    for group_index, group in enumerate(
        groups,
        start=1
    ):

        # ----------------------------------------------------
        # Collect all identifiers in the group
        # ----------------------------------------------------

        combined_identifiers = {}

        for document in group:

            for field, value in document[
                "identifiers"
            ].items():

                if field not in combined_identifiers:

                    combined_identifiers[field] = []

                if value not in combined_identifiers[
                    field
                ]:

                    combined_identifiers[field].append(
                        value
                    )

        # ----------------------------------------------------
        # Determine primary shipment identity
        # ----------------------------------------------------

        primary_identifier = None

        for field in PRIMARY_IDENTIFIER_PRIORITY:

            values = combined_identifiers.get(
                field,
                []
            )

            if values:

                primary_identifier = values[0]

                break

        if not primary_identifier:

            primary_identifier = (
                f"UNKNOWN_SHIPMENT_{group_index}"
            )

        # ----------------------------------------------------
        # Create shipment entry
        # ----------------------------------------------------

        shipment_map[primary_identifier] = {
            "shipment_id": primary_identifier,
            "identifiers": combined_identifiers,
            "documents": group
        }

    return shipment_map


# ============================================================
# Display Shipment Identity
# ============================================================

def print_shipment_identity(shipment_map):
    """
    Display the resolved shipment groups.
    """

    print("\n" + "=" * 70)
    print("CROSS-DOCUMENT SHIPMENT IDENTITY")
    print("=" * 70)

    if not shipment_map:

        print("\nNo shipment groups found.")

        return

    print(
        f"\nTotal shipment groups: "
        f"{len(shipment_map)}"
    )

    for shipment_id, shipment in shipment_map.items():

        print("\n" + "-" * 70)

        print(
            f"PRIMARY SHIPMENT ID: "
            f"{shipment_id}"
        )

        print("\nAll linked identifiers:")

        identifiers = shipment[
            "identifiers"
        ]

        for field in IDENTIFIER_FIELDS:

            values = identifiers.get(
                field,
                []
            )

            if values:

                print(
                    f"  {field:<22}: "
                    f"{', '.join(values)}"
                )

        print("\nLinked documents:")

        for index, document in enumerate(
            shipment["documents"],
            start=1
        ):

            print(
                f"  {index}. "
                f"{document['source']} "
                f"-> "
                f"{document['document_type']}"
            )

    print("\n" + "=" * 70)
    print(
        "CROSS-DOCUMENT IDENTITY COMPLETED"
    )
    print("=" * 70)


# ============================================================
# Find Shipment By Any Identifier
# ============================================================

def find_shipment_by_identifier(
    shipment_map,
    identifier
):
    """
    Find a shipment using any identifier.
    """

    normalized_identifier = normalize_identifier(
        identifier
    )

    if not normalized_identifier:
        return None

    for shipment in shipment_map.values():

        # ----------------------------------------------------
        # Check primary shipment ID
        # ----------------------------------------------------

        if (
            shipment["shipment_id"]
            == normalized_identifier
        ):

            return shipment

        # ----------------------------------------------------
        # Check all linked identifiers
        # ----------------------------------------------------

        identifiers = shipment[
            "identifiers"
        ]

        for values in identifiers.values():

            if normalized_identifier in values:

                return shipment

    return None


# ============================================================
# Main Test
# ============================================================

if __name__ == "__main__":

    shipment_map = (
        build_shipment_identity_map()
    )

    print_shipment_identity(
        shipment_map
    )

    # --------------------------------------------------------
    # Test 1: Shipment ID
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("IDENTIFIER LOOKUP TEST 1")
    print("=" * 70)

    test_identifier = "SHJ-2026-001"

    result = find_shipment_by_identifier(
        shipment_map,
        test_identifier
    )

    print(
        f"\nSearching for: "
        f"{test_identifier}"
    )

    if result:

        print(
            f"Shipment found: "
            f"{result['shipment_id']}"
        )

    else:

        print(
            "Shipment not found."
        )

    # --------------------------------------------------------
    # Test 2: Invoice Number
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("IDENTIFIER LOOKUP TEST 2")
    print("=" * 70)

    test_identifier = "INV-SHJ-2026-001"

    result = find_shipment_by_identifier(
        shipment_map,
        test_identifier
    )

    print(
        f"\nSearching for: "
        f"{test_identifier}"
    )

    if result:

        print(
            f"Shipment found: "
            f"{result['shipment_id']}"
        )

    else:

        print(
            "Shipment not found."
        )

    print("\n" + "=" * 70)
    print(
        "ALL IDENTITY TESTS COMPLETED"
    )
    print("=" * 70)
