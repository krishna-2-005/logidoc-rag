"""
Tests for the structured audit results in src/shipment_calculations.py
(audit_quantity, audit_delivery, audit_invoice) on synthetic document
records: no PDFs, no index, no Ollama.

The audits are what Reconciliation and Intelligence show, so they are
also checked against calculate(), the function behind the chat answers,
on the same records: the same numbers, and NEEDS REVIEW exactly where
chat abstains.

Run from the project root:
    python -m pytest tests/test_audit_results.py
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from shipment_calculations import (
    ABSTAINED,
    ANSWERED,
    NEEDS_REVIEW,
    audit_delivery,
    audit_invoice,
    audit_quantity,
    calculate,
)
from test_shipment_calculations import SAMPLE_ITEMS, bol, invoice, pod


INVOICE_QUESTION = "Do the invoice line items add up to the total invoice amount?"


# ============================================================
# QUANTITY
# ============================================================

@pytest.mark.parametrize(
    "expected, delivered, status, shortage",
    [
        (48, 45, "SHORTAGE", 3),
        (45, 48, "OVERAGE", -3),
        (60, 60, "MATCH", 0),
        ("1,200", 1150, "SHORTAGE", 50),
    ],
)
def test_quantity_status(expected, delivered, status, shortage):

    audit = audit_quantity([bol(expected), pod(delivered)])

    assert audit.status == status
    assert audit.values["shortage"] == shortage
    assert audit.values["difference"] == abs(shortage)
    assert audit.reason == ""


@pytest.mark.parametrize("written", ["48.5", "4,8", "1,20", "1,2000", "abc", "-5"])
def test_malformed_quantity_is_never_accepted(written):

    audit = audit_quantity([bol(written), pod(45)])

    assert audit.status == NEEDS_REVIEW
    assert audit.values["expected"] is None
    assert audit.values["difference"] is None


def test_quantity_followed_by_a_unit_is_read():

    assert audit_quantity([bol("48 pieces"), pod(45)]).values["expected"] == 48


def test_quantity_cites_both_fields_with_source_and_page():

    audit = audit_quantity([bol(48), pod(45)])

    assert [
        (
            item.record["metadata"]["source"],
            item.record["metadata"]["page"],
            item.text,
        )
        for item in audit.evidence
    ] == [
        ("BOL_TEST.pdf", 1, "Number of Pieces: 48"),
        ("POD_TEST.pdf", 1, "Number of Pieces Delivered: 45"),
    ]


def test_quantity_missing_delivered_is_never_a_match():

    audit = audit_quantity([bol(48), pod(None)])

    assert audit.status == NEEDS_REVIEW
    assert audit.values["expected"] == 48
    assert audit.values["delivered"] is None
    assert audit.values["difference"] is None
    assert "delivered piece count" in audit.reason
    assert "was not found" in audit.reason


def test_quantity_conflicting_bols_need_review():

    audit = audit_quantity([
        bol(48, source="BOL_A.pdf"),
        bol(50, source="BOL_B.pdf"),
        pod(45),
    ])

    assert audit.status == NEEDS_REVIEW
    assert audit.values["expected"] is None
    assert audit.values["delivered"] == 45
    assert "BOL_A.pdf" in audit.reason and "BOL_B.pdf" in audit.reason
    assert {item.record["metadata"]["source"] for item in audit.evidence} == {
        "BOL_A.pdf", "BOL_B.pdf", "POD_TEST.pdf"
    }


def test_quantity_with_nothing_found_needs_review():

    audit = audit_quantity([])

    assert audit.status == NEEDS_REVIEW
    assert audit.values["difference"] is None
    assert audit.evidence == []


def test_quantity_reports_every_problem():

    audit = audit_quantity([bol(None), pod(None)])

    assert audit.status == NEEDS_REVIEW
    assert "expected piece count" in audit.reason
    assert "delivered piece count" in audit.reason


# ============================================================
# DELIVERY
# ============================================================

@pytest.mark.parametrize(
    "expected, actual, status, days_late",
    [
        ("October 3, 2026", "October 5, 2026", "DELAYED", 2),
        ("October 9, 2026", "October 8, 2026", "EARLY", -1),
        ("October 8, 2026", "October 8, 2026", "ON TIME", 0),
        ("February 28, 2028", "March 1, 2028", "DELAYED", 2),
    ],
)
def test_delivery_status(expected, actual, status, days_late):

    audit = audit_delivery([bol(expected=expected), pod(date=actual)])

    assert audit.status == status
    assert audit.values == {
        "expected": expected, "actual": actual, "days_late": days_late
    }


def test_early_delivery_is_not_delayed_and_not_a_review():

    audit = audit_delivery(
        [bol(expected="October 9, 2026"), pod(date="October 8, 2026")]
    )

    assert audit.status == "EARLY"
    assert audit.reason == ""


@pytest.mark.parametrize(
    "records, fragment",
    [
        ([bol(), pod(date="October 5, 2026")], "expected delivery date"),
        ([bol(expected="October 3, 2026"), pod()], "actual delivery date"),
        # 10/05/2026 could be May or October
        (
            [bol(expected="October 3, 2026"), pod(date="10/05/2026")],
            "'10/05/2026'",
        ),
    ],
)
def test_delivery_missing_or_unreadable_needs_review(records, fragment):

    audit = audit_delivery(records)

    assert audit.status == NEEDS_REVIEW
    assert audit.values["days_late"] is None
    assert fragment in audit.reason


def test_delivery_conflicting_dates_need_review():

    audit = audit_delivery([
        bol(expected="October 3, 2026"),
        pod(date="October 5, 2026", source="POD_A.pdf"),
        pod(date="October 6, 2026", source="POD_B.pdf"),
    ])

    assert audit.status == NEEDS_REVIEW
    assert audit.values["actual"] is None
    assert audit.values["expected"] == "October 3, 2026"
    assert "POD_A.pdf" in audit.reason and "POD_B.pdf" in audit.reason


def test_a_promised_delivery_date_is_not_the_actual_date():

    audit = audit_delivery([
        bol(expected="October 3, 2026"),
        pod(date=None, extra="Promised Delivery Date: October 1, 2026"),
    ])

    assert audit.status == NEEDS_REVIEW
    assert "actual delivery date" in audit.reason


# ============================================================
# INVOICE
# ============================================================

def test_invoice_mismatch_uses_the_chat_arithmetic():

    audit = audit_invoice([invoice(SAMPLE_ITEMS, "1,550.00")])

    assert audit.status == "MISMATCH"
    assert audit.values["line_sum"] == 1430.00
    assert audit.values["total"] == 1550.00
    assert audit.values["difference"] == 120.00
    assert audit.values["line_items"] == [
        ("Base Freight", 1200.00),
        ("Fuel Surcharge", 180.00),
        ("Detention Charge", 0.00),
        ("Other Accessorial Charges", 50.00),
    ]
    assert audit.values["breakdown"] in calculate(
        INVOICE_QUESTION, [invoice(SAMPLE_ITEMS, "1,550.00")]
    ).answer


def test_invoice_total_below_the_line_item_sum_is_a_negative_difference():

    audit = audit_invoice([invoice(SAMPLE_ITEMS, "1,400.00")])

    assert audit.status == "MISMATCH"
    assert audit.values["line_sum"] == 1430.00
    assert audit.values["difference"] == -30.00


def test_invoice_that_adds_up():

    audit = audit_invoice([invoice(SAMPLE_ITEMS, "1,430.00")])

    assert audit.status == "MATCH"
    assert audit.values["difference"] == 0


def test_invoice_cents_do_not_drift():

    items = {
        "Base Freight": "0.10",
        "Fuel Surcharge": "0.20",
        "Detention Charge": "0.00",
        "Other Accessorial Charges": "1,000.05",
    }

    audit = audit_invoice([invoice(items, "1,000.35")])

    assert audit.status == "MATCH"
    assert audit.values["difference"] == 0


def test_invoice_cites_every_amount_used_with_source_and_page():

    audit = audit_invoice([invoice(SAMPLE_ITEMS, "1,550.00")])

    assert [
        (item.record["metadata"]["source"], item.text)
        for item in audit.evidence
    ] == [
        ("INVOICE_TEST.pdf", "Base Freight: $1,200.00"),
        ("INVOICE_TEST.pdf", "Fuel Surcharge: $180.00"),
        ("INVOICE_TEST.pdf", "Detention Charge: $0.00"),
        ("INVOICE_TEST.pdf", "Other Accessorial Charges: $50.00"),
        ("INVOICE_TEST.pdf", "Total Invoice Amount: $1,550.00"),
    ]
    assert all(item.record["metadata"]["page"] == 1 for item in audit.evidence)


def test_invoice_without_a_total_needs_review():

    audit = audit_invoice([invoice(SAMPLE_ITEMS, None)])

    assert audit.status == NEEDS_REVIEW
    assert audit.values["difference"] is None
    assert audit.values["total"] is None
    assert audit.values["line_items"][0] == ("Base Freight", 1200.00)
    assert "Total Invoice Amount" in audit.reason


def test_invoice_with_a_missing_line_item_needs_review():

    items = dict(SAMPLE_ITEMS)
    del items["Detention Charge"]

    audit = audit_invoice([invoice(items, "1,430.00")])

    assert audit.status == NEEDS_REVIEW
    assert ("Detention Charge", None) in audit.values["line_items"]
    assert "Detention Charge" in audit.reason


def test_invoice_with_an_unknown_charge_needs_review():

    audit = audit_invoice(
        [invoice(SAMPLE_ITEMS, "1,430.00", extra="Lumper Fee: $75.00")]
    )

    assert audit.status == NEEDS_REVIEW
    assert "not known line items" in audit.reason
    assert "$75.00" in audit.reason


def test_two_different_invoices_need_review():

    audit = audit_invoice([
        invoice(SAMPLE_ITEMS, "1,550.00", source="INVOICE_A.pdf"),
        invoice(SAMPLE_ITEMS, "1,430.00", source="INVOICE_B.pdf"),
    ])

    assert audit.status == NEEDS_REVIEW
    assert audit.values["total"] is None
    assert "INVOICE_A.pdf" in audit.reason and "INVOICE_B.pdf" in audit.reason


# ============================================================
# AGREEMENT WITH THE CHAT CALCULATIONS
# ============================================================

QUANTITY_QUESTION = "How many pieces were missing between the BOL and the POD?"
DELIVERY_QUESTION = "Was the shipment delivered on time?"

SCENARIOS = {
    "clean_shortage_and_delay": [
        bol(48, expected="October 3, 2026"),
        pod(45, date="October 5, 2026"),
        invoice(SAMPLE_ITEMS, "1,550.00"),
    ],
    "early_and_matching": [
        bol(60, expected="October 9, 2026"),
        pod(60, date="October 8, 2026"),
        invoice(SAMPLE_ITEMS, "1,430.00"),
    ],
    "missing_values": [
        bol(None, expected=None),
        pod(None, date=None),
        invoice(SAMPLE_ITEMS, None),
    ],
    "conflicting_values": [
        bol(48, expected="October 3, 2026", source="BOL_A.pdf"),
        bol(50, expected="October 4, 2026", source="BOL_B.pdf"),
        pod(45, date="October 5, 2026"),
        invoice(SAMPLE_ITEMS, "1,550.00", source="INVOICE_A.pdf"),
        invoice(SAMPLE_ITEMS, "1,430.00", source="INVOICE_B.pdf"),
    ],
    "unreadable_date": [
        bol(48, expected="October 3, 2026"),
        pod(45, date="10/05/2026"),
        invoice(SAMPLE_ITEMS, "1,430.00", extra="Lumper Fee: $75.00"),
    ],
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_audits_agree_with_the_chat_answers(name):

    records = SCENARIOS[name]

    quantity = audit_quantity(records)
    chat = calculate(QUANTITY_QUESTION, records)

    assert (quantity.status == NEEDS_REVIEW) == (chat.status == ABSTAINED)

    if chat.status == ANSWERED:
        assert quantity.values["shortage"] == chat.value

    delivery = audit_delivery(records)
    chat = calculate(DELIVERY_QUESTION, records)

    assert (delivery.status == NEEDS_REVIEW) == (chat.status == ABSTAINED)

    if chat.status == ANSWERED:
        assert delivery.values["days_late"] == chat.value

    invoice_audit = audit_invoice(records)
    chat = calculate(INVOICE_QUESTION, records)

    assert (invoice_audit.status == NEEDS_REVIEW) == (chat.status == ABSTAINED)

    if chat.status == ANSWERED:

        assert invoice_audit.values["line_sum"] == chat.value
        assert chat.answer.startswith(
            "Yes." if invoice_audit.status == "MATCH" else "No."
        )
