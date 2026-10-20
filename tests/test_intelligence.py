"""
LogiDoc-RAG Shipment Intelligence Regression Test

Runs the real analyze_shipment() from src/intelligence_service.py
against the sample shipment SHJ-2026-001 and checks the business
results shown on the Shipment Intelligence dashboard.

The test is isolated from the live data/raw folder and chroma_db
index, whose contents change with every upload: the SHJ-2026-001
samples (tests/fixtures/shipment_SHJ-2026-001) are ingested into a
temporary folder and index with the real loader and chunker. Ollama
is not needed: the intelligence reads PDF text and document
metadata only, so stand-in embeddings are used.

Run from the project root:
    python -m pytest tests/test_intelligence.py
"""

import sys
from pathlib import Path


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
TESTS_DIR = PROJECT_ROOT / "tests"

for path in (PROJECT_ROOT, SRC_DIR, TESTS_DIR):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pytest

from ingestion_manager import ingest_files
from intelligence_service import analyze_shipment
from isolation import SAMPLE_SHIPMENT_DIR, sample_workspace
from test_ingestion_manager import fake_embedding

TEMP_PREFIX = "logidoc_intelligencetest_"


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

    with sample_workspace(TEMP_PREFIX, copy_samples=False) as (
        raw_dir,
        chroma_dir,
    ):

        indexed = ingest_files(
            sorted(SAMPLE_SHIPMENT_DIR.glob("*.pdf")),
            raw_dir,
            chroma_dir,
            embed_text=fake_embedding
        )

        assert indexed["files_added"] == 3, (
            "FAIL: the SHJ-2026-001 samples could not be indexed: "
            f"{indexed['errors']}"
        )

        check_shipment_intelligence()


def check_shipment_intelligence():

    intelligence = analyze_shipment()

    assert isinstance(intelligence, dict), (
        "analyze_shipment() did not return a dict"
    )

    assert intelligence.get("success"), (
        "analyze_shipment() failed: "
        f"{intelligence.get('error', 'Unknown error')}"
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

    # SHJ-2026-001 also has an invoice whose line items ($1,430.00)
    # do not add up to the Total Invoice Amount ($1,550.00): the same
    # arithmetic as the chat answer, now an exception of its own
    check(
        "Exceptions",
        len(exceptions),
        3
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
        "Invoice mismatch exceptions",
        categories.count("Invoice"),
        1
    )

    invoice = intelligence["invoice"]

    check("Invoice check", invoice["status"], "MISMATCH")

    check(
        "Invoice line-item sum",
        (invoice["line_item_sum"], invoice["difference"]),
        (1430.00, 120.00),
        display=f"${invoice['line_item_sum']:,.2f}"
    )

    check(
        "Shipment status",
        intelligence["shipment_status"],
        "EXCEPTION"
    )


# ============================================================
# MAIN
# ============================================================

# The production-data check runs for every session in
# tests/conftest.py.

if __name__ == "__main__":

    sys.exit(pytest.main([__file__, "-q"]))
