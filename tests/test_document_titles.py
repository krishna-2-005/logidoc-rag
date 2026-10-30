"""
Document type from the document's own heading.

A rate confirmation lists freight, fuel and payment terms like an invoice, so
counting keywords alone called it an invoice. The type a document names in its
heading now decides; a reference such as "Bill of Lading No: 123" is not a
heading, and without one keyword counts decide as before.

Run from the project root:
    python -m pytest tests/test_document_titles.py
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from document_classifier import classify_document


@pytest.mark.parametrize("text, expected", [
    # Invoice-like fields, but it calls itself a rate confirmation
    ("RATE CONFIRMATION\nShipment Number: X\nFuel Surcharge: $180.00\nPayment Terms: Net 30", "RATE_CONFIRMATION"),
    ("Swift Lines LLC\nLoad Confirmation\nAgreed Rate: $1,400.00", "RATE_CONFIRMATION"),
    # A reference to another document is not a title
    ("FREIGHT INVOICE\nBill of Lading No: 123\nTotal Invoice Amount: $10.00", "INVOICE"),
    ("Carrier: Swift Lines\nBill of Lading Number: 123\nINVOICE\nInvoice Number: 9", "INVOICE"),
    ("BILL OF LADING\nShipper: A\nConsignee: B\nNumber of Pieces: 4", "BOL"),
    ("PROOF OF DELIVERY\nReceived By: C\nDelivery Date: Oct 3", "POD"),
    # No title: keyword counts decide, as before
    ("Shipment Number: X\nInvoice Number: 9\nTotal Invoice Amount: $10\nPayment Terms: Net 30", "INVOICE"),
    # A phrase inside another word is not a title
    ("Accurate Consulting Group\nInvoice Number: 9\nTotal Invoice Amount: $10", "INVOICE"),
])
def test_the_title_decides_the_document_type(text, expected):

    assert classify_document(text)["document_type"] == expected
