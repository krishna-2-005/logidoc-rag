"""
LogiDoc-RAG Shipment Intelligence Regression Test

Runs the real analyze_shipment() from src/intelligence_service.py
against the indexed sample shipment SHJ-2026-001 and checks the
business results shown on the Shipment Intelligence dashboard.

Requires the ChromaDB index to be built first:
    python src/vector_store.py

Run from the project root:
    python tests/test_intelligence.py
"""

import sys
from pathlib import Path


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"

for path in (PROJECT_ROOT, SRC_DIR):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from intelligence_service import analyze_shipment


# ============================================================
# EXPECTED RESULTS FOR SHJ-2026-001
# ============================================================

EXPECTED_SHIPMENT_ID = "SHJ-2026-001"


# ============================================================
# HELPERS
# ============================================================

def check(label, actual, expected, display=None):
    """
    Assert one value and print a PASS line.
    """

    assert actual == expected, (
        f"FAIL: {label}: expected {expected!r}, got {actual!r}"
    )

    print(
        f"PASS: {label} = "
        f"{display if display is not None else actual}"
    )


# ============================================================
# TEST
# ============================================================

def test_shipment_intelligence():

    intelligence = analyze_shipment()

    assert isinstance(intelligence, dict), (
        "analyze_shipment() did not return a dict"
    )

    assert intelligence.get("success"), (
        "analyze_shipment() failed: "
        f"{intelligence.get('error', 'Unknown error')}. "
        "Is the index built? Run: python src/vector_store.py"
    )

    check(
        "Shipment ID",
        intelligence["shipment_id"],
        EXPECTED_SHIPMENT_ID
    )

    # --------------------------------------------------------
    # DOCUMENTS
    # --------------------------------------------------------

    documents = intelligence["documents"]

    check(
        "Document types",
        sorted(documents["types"]),
        ["BOL", "INVOICE", "POD"],
        display=", ".join(sorted(documents["types"]))
    )

    check(
        "Documents",
        (documents["found"], documents["expected"]),
        (3, 3),
        display=f"{documents['found']}/{documents['expected']}"
    )

    check(
        "Completeness",
        documents["completeness"],
        100,
        display=f"{documents['completeness']}%"
    )

    # --------------------------------------------------------
    # QUANTITY
    # --------------------------------------------------------

    quantity = intelligence["quantity"]

    check(
        "BOL pieces",
        quantity["bol_pieces"],
        48
    )

    check(
        "POD delivered pieces",
        quantity["delivered_pieces"],
        45
    )

    check(
        "Quantity difference",
        quantity["difference"],
        3
    )

    check(
        "Quantity status",
        quantity["status"],
        "SHORTAGE"
    )

    # --------------------------------------------------------
    # DELIVERY
    # --------------------------------------------------------

    delivery = intelligence["delivery"]

    check(
        "Expected delivery",
        delivery["expected"],
        "October 3, 2026"
    )

    check(
        "Actual delivery",
        delivery["actual"],
        "October 5, 2026"
    )

    check(
        "Delivery delay",
        delivery["delay_days"],
        2,
        display=f"{delivery['delay_days']} days"
    )

    check(
        "Delivery status",
        delivery["status"],
        "DELAYED"
    )

    # --------------------------------------------------------
    # INVOICE
    # --------------------------------------------------------

    invoice_total = intelligence["invoice_total"]

    assert invoice_total is not None, (
        "FAIL: Invoice total: expected 1550.0, got None"
    )

    check(
        "Invoice total",
        round(invoice_total, 2),
        1550.00,
        display=f"${invoice_total:,.2f}"
    )

    # --------------------------------------------------------
    # EXCEPTIONS
    # --------------------------------------------------------

    exceptions = intelligence["exceptions"]

    check(
        "Exceptions",
        len(exceptions),
        2
    )

    check(
        "Exception count field",
        intelligence["exception_count"],
        len(exceptions)
    )

    categories = [
        exception.get("category")
        for exception in exceptions
    ]

    # Quantity exception is the shortage (status SHORTAGE above);
    # Delivery exception is the delay (status DELAYED above)
    check(
        "Quantity shortage exceptions",
        categories.count("Quantity"),
        1
    )

    check(
        "Delivery delay exceptions",
        categories.count("Delivery"),
        1
    )

    check(
        "Shipment status",
        intelligence["shipment_status"],
        "EXCEPTION"
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print("=" * 70)
    print("INTELLIGENCE REGRESSION TEST")
    print("=" * 70)

    try:

        test_shipment_intelligence()

    except AssertionError as error:

        print(error)
        print()
        print("INTELLIGENCE REGRESSION TEST FAILED")

        sys.exit(1)

    print()
    print("INTELLIGENCE REGRESSION TEST PASSED")
