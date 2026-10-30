"""
Pickup and delivery dates in an impossible order.

A delivery (BOL expected or POD actual) dated before the BOL pickup
date cannot happen, so one of the dates was read or printed wrong. The
audit then marks the delivery NEEDS REVIEW instead of reporting it as
late, early or on time, and the chat answer abstains with the same
reason. Without a readable pickup date nothing changes.

No PDFs, no index, no Ollama and no files are involved.

Run from the project root:
    python -m pytest tests/test_date_chronology.py
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from offline_imports import stubbed_services

with stubbed_services():

    import reconciliation

from shipment_calculations import ABSTAINED, ANSWERED, calculate
from test_reconciliation_consistency import as_records, documents

# Line items add up and every piece arrives, so only the dates matter
CLEAN = {"delivered": 48, "total": "$1,430.00"}


def audit(**overrides):

    return reconciliation.reconcile_shipment("SHJ-2026-001", chunks=documents(**{**CLEAN, **overrides}))


def ask(question, **overrides):

    chunks = documents(**{**CLEAN, **overrides})

    return calculate(question, as_records(chunks))


def without_pickup(chunks):

    for item in chunks:
        item["text"] = item["text"].replace("Pickup Date:\nOctober 1, 2026\n\n", "")

    return chunks


def test_pod_delivery_before_pickup_needs_review():

    result = audit(expected="October 3, 2026", actual="September 28, 2026")

    assert result["delivery"]["status"] == "NEEDS REVIEW"
    assert result["delivery"]["days_late"] is None
    assert "POD delivery date (September 28, 2026) is before the BOL pickup date (October 1, 2026)" in result["delivery"]["reason"]
    assert result["final_status"] == "NEEDS REVIEW"
    assert {"category": "Delivery", "reason": result["delivery"]["reason"]} in result["review_items"]

    # The pickup date is shown as evidence beside the two delivery dates
    assert "BOL" in [item["document_type"] for item in result["delivery"]["evidence"]]
    assert len(result["delivery"]["evidence"]) == 3


def test_expected_delivery_before_pickup_needs_review():

    result = audit(expected="September 30, 2026", actual="October 3, 2026")

    assert result["delivery"]["status"] == "NEEDS REVIEW"
    assert "BOL expected delivery date (September 30, 2026)" in result["delivery"]["reason"]


@pytest.mark.parametrize("expected, actual, status, late", [
    ("October 3, 2026", "October 5, 2026", "DELAYED", 2),
    ("October 3, 2026", "October 3, 2026", "ON TIME", 0),
    ("October 5, 2026", "October 3, 2026", "EARLY", -2),
    ("October 1, 2026", "October 1, 2026", "ON TIME", 0),  # same day as the pickup
])
def test_possible_dates_are_unchanged(expected, actual, status, late):

    result = audit(expected=expected, actual=actual)

    assert result["delivery"]["status"] == status
    assert result["delivery"]["days_late"] == late
    assert len(result["delivery"]["evidence"]) == 2


def test_without_a_pickup_date_nothing_is_checked():

    chunks = without_pickup(documents(**{**CLEAN, "expected": "October 3, 2026", "actual": "September 28, 2026"}))

    result = reconciliation.reconcile_shipment("SHJ-2026-001", chunks=chunks)

    assert result["delivery"]["status"] == "EARLY"
    assert result["delivery"]["days_late"] == -5


def test_chat_abstains_with_the_audit_reason():

    answer = ask("Was the shipment delivered on time?", expected="October 3, 2026", actual="September 28, 2026")

    assert answer.status == ABSTAINED
    assert "before the BOL pickup date" in answer.answer


def test_chat_answers_possible_dates_as_before():

    answer = ask("Was the shipment delivered on time?", expected="October 3, 2026", actual="October 5, 2026")

    assert answer.status == ANSWERED
    assert "2 days late" in answer.answer
