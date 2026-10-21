"""
Tests for src/audit_explanations.py: deterministic, evidence-backed
explanations of audit exceptions, and the optional model commentary.

Shipments are reconciled with the real reconcile_shipment() from
synthetic document chunks (no PDFs, no index, no Ollama); the language
model is replaced by small fakes.

Run from the project root:
    python -m pytest tests/test_audit_explanations.py
"""

import copy
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_audit_analytics import A, B, C, clean, edit, snapshot  # noqa: E402
from test_reconciliation_consistency import documents, drop  # noqa: E402

import audit_analytics as analytics  # noqa: E402
import audit_explanations as ex  # noqa: E402


def explanations(chunks):

    shots = snapshot(chunks)
    exceptions = analytics.build_exceptions(shots)

    return shots, exceptions, {
        item["exception_key"].split("|")[1]: item for item in ex.explain_all(shots, exceptions)
    }


# ============================================================
# DISCREPANCIES
# ============================================================

def test_quantity_delivery_and_invoice_explanations():

    _, _, found = explanations(documents(shipment=A))

    quantity = found["quantity"]

    assert (quantity["category"], quantity["kind"], quantity["severity"]) == (
        "Quantity shortage", "Discrepancy", "HIGH",
    )
    assert quantity["expected"] == "BOL: 48 pieces"
    assert quantity["observed"] == "POD: 45 pieces delivered"
    assert quantity["difference_text"] == "+3 pieces (BOL - POD)"
    assert "Shortage = pieces on the BOL" in quantity["rule"]
    assert quantity["next_step"].startswith("Check the piece counts")
    assert [(item["source"], item["page"], item["document_type"], item["shipment_id"], item["text"]) for item in quantity["sources"]] == [
        ("BOL_SHJ-2026-001.pdf", 1, "BOL", A, "Number of Pieces: 48"),
        ("POD_SHJ-2026-001.pdf", 1, "POD", A, "Number of Pieces Delivered: 45"),
    ]
    assert quantity["evidence_note"] is None

    delivery = found["delivery"]

    assert delivery["expected"] == "Expected delivery date (BOL): October 3, 2026"
    assert delivery["observed"] == "Actual delivery date (POD): October 5, 2026"
    assert delivery["difference_text"] == "+2 days late"
    assert "actual delivery date (POD) minus expected delivery date (BOL)" in delivery["rule"]
    assert {item["document_type"] for item in delivery["sources"]} == {"BOL", "POD"}

    invoice = found["invoice"]

    assert invoice["expected"] == "Sum of line items: $1,430.00"
    assert invoice["observed"] == "Total Invoice Amount: $1,550.00"
    assert invoice["difference_text"] == "+$120.00"
    assert "does not prove overbilling" in invoice["rule"]
    assert "payable or recoverable" in invoice["caveat"]
    assert len(invoice["sources"]) == 5
    assert {item["text"] for item in invoice["sources"]} >= {"Total Invoice Amount: $1,550.00", "Base Freight: $1,200.00"}


def test_over_delivery_and_total_below_line_items_keep_their_sign():

    _, _, found = explanations(documents(shipment=A, delivered=50, actual="October 3, 2026", total="$1,400.00"))

    assert found["quantity"]["category"] == "Quantity overage"
    assert found["quantity"]["difference_text"] == "-2 pieces (BOL - POD)"
    assert found["quantity"]["next_step"].startswith("Check the piece counts on the original BOL and the signed POD and ask")

    assert found["invoice"]["difference_text"] == "-$30.00"
    assert found["invoice"]["observed"] == "Total Invoice Amount: $1,400.00"


def test_carrier_and_route_explanations_have_no_invented_evidence():

    chunks = edit(edit(documents(shipment=A, delivered=48, actual="October 3, 2026", total="$1,430.00"),
                       "INVOICE", "Swift Lines", "Other Lines"), "INVOICE", "Chicago, Illinois", "Dallas, Texas")

    _, _, found = explanations(chunks)

    carrier = found["carrier-invoice"]

    assert (carrier["expected"], carrier["observed"]) == ("BOL carrier: Swift Lines", "invoice carrier: Other Lines")
    assert carrier["rule"] == "The carrier named on the BOL is compared with the one on the invoice."
    assert carrier["difference_text"] is None

    # The audit result attaches no text to these comparisons: said so
    assert carrier["sources"] == [] and carrier["evidence_note"] == ex.NO_EVIDENCE

    origin = found["origin"]

    assert (origin["expected"], origin["observed"]) == ("BOL origin: Chicago, Illinois", "Invoice origin: Dallas, Texas")


# ============================================================
# NEEDS REVIEW: MISSING, CONFLICTING, UNREADABLE
# ============================================================

def test_missing_value_needs_review_with_its_reason():

    _, _, found = explanations(drop(documents(shipment=A), "POD", "Number of Pieces Delivered"))

    review = found["review-quantity"]

    assert (review["category"], review["kind"]) == ("Quantity needs review", "Needs review")
    assert "was not found" in review["reason"]
    assert not review["conflicting"]
    assert review["next_step"].startswith("Find the value in the original document")
    assert review["difference_text"] is None and review["expected"] is None and review["observed"] is None
    assert "no comparison was made" in review["caveat"]

    # The discrepancy it replaced is not explained as if it still held
    assert "quantity" not in found


def test_conflicting_values_are_never_presented_as_verified():

    conflicting = clean(B) + [
        dict(item, text=item["text"].replace("Number of Pieces:\n48", "Number of Pieces:\n50"),
             metadata=dict(item["metadata"], source=f"BOL_{B}_revised.pdf"))
        for item in clean(B) if item["metadata"]["document_type"] == "BOL"
    ]

    _, _, found = explanations(conflicting)

    review = found["review-quantity"]

    assert review["conflicting"]
    assert "different values" in review["reason"]
    assert f"48 (BOL_{B}.pdf page 1)" in review["reason"] and f"50 (BOL_{B}_revised.pdf page 1)" in review["reason"]
    assert "none of them is treated as verified" in review["caveat"]
    assert review["next_step"].startswith("Decide which document is authoritative")

    # No expected or observed value is offered as if it were verified
    assert (review["expected"], review["observed"], review["difference_text"]) == (None, None, None)


def test_unreadable_value_explanation():

    _, _, found = explanations(edit(documents(shipment=A), "POD", "October 5, 2026", "10/05/2026"))

    review = found["review-delivery"]

    assert "could not be read" in review["reason"]
    assert review["next_step"].startswith("Replace the document with a readable copy")
    assert not review["conflicting"]


def test_a_shipment_that_cannot_be_reconciled():

    _, _, found = explanations([item for item in clean(C) if item["metadata"]["document_type"] != "POD"])

    item = found["reconciliation"]

    assert item["reason"] == "Reconciliation requires BOL, Invoice and POD."
    assert item["sources"] == [] and item["evidence_note"] == ex.NO_EVIDENCE
    assert "document completeness register" in item["next_step"]


# ============================================================
# EVIDENCE: ISOLATION, MISSING, LONG TEXT
# ============================================================

def test_evidence_of_another_shipment_is_excluded():

    shots = snapshot(documents(shipment=A))

    leaked = {"source": f"INVOICE_{B}.pdf", "page": 1, "document_type": "INVOICE",
              "shipment_id": B, "text": "Total Invoice Amount: $9.00"}

    shots["shipments"][0]["result"]["invoice"]["evidence"].append(leaked)

    exceptions = analytics.build_exceptions(shots)

    invoice = next(item for item in ex.explain_all(shots, exceptions) if item["exception_key"].endswith("|invoice"))

    assert invoice["excluded_foreign_evidence"] == 1
    assert {item["shipment_id"] for item in invoice["sources"]} == {A}
    assert all(B not in item["source"] and "$9.00" not in item["text"] for item in invoice["sources"])
    assert "1 evidence item(s) of another shipment were excluded." in invoice["evidence_note"]


def test_missing_evidence_is_reported():

    shots = snapshot(documents(shipment=A))
    shots["shipments"][0]["result"]["quantity"]["evidence"] = []

    exceptions = analytics.build_exceptions(shots)

    quantity = next(item for item in ex.explain_all(shots, exceptions) if item["exception_key"].endswith("|quantity"))

    assert quantity["sources"] == []
    assert quantity["evidence_note"] == ex.NO_EVIDENCE


def test_long_evidence_text_is_clipped():

    shots = snapshot(documents(shipment=A))
    shots["shipments"][0]["result"]["quantity"]["evidence"][0]["text"] = "Number of Pieces: 48 " + "remark " * 300

    exceptions = analytics.build_exceptions(shots)

    quantity = next(item for item in ex.explain_all(shots, exceptions) if item["exception_key"].endswith("|quantity"))

    text = quantity["sources"][0]["text"]

    assert len(text) <= ex.MAX_TEXT and text.endswith("…") and text.startswith("Number of Pieces: 48")


def test_explanations_belong_to_their_own_shipment():

    shots, exceptions, _ = explanations(documents(shipment=A) + clean(B) + drop(clean("SHJ-2026-404"), "POD", "Delivery Date"))

    for item in ex.explain_all(shots, exceptions):

        key = item["exception_key"].split("|")[0]

        assert key == "".join(ch for ch in item["shipment_id"] if ch.isalnum()).upper()
        assert all("".join(ch for ch in source["shipment_id"] if ch.isalnum()).upper() == key for source in item["sources"])


def test_every_exception_has_a_rule_and_a_next_step():

    chunks = (
        documents(shipment=A)
        + drop(clean("SHJ-2026-404"), "POD", "Delivery Date")
        + [item for item in clean(C) if item["metadata"]["document_type"] != "POD"]
    )

    shots = snapshot(chunks)
    exceptions = analytics.build_exceptions(shots)
    items = ex.explain_all(shots, exceptions)

    assert len(items) == len(exceptions) >= 5

    for item in items:
        assert item["rule"] and item["next_step"], item["exception_key"]


# ============================================================
# THE EXPLANATION DOES NOT CHANGE THE AUDIT
# ============================================================

def test_explaining_changes_nothing():

    shots = snapshot(documents(shipment=A) + clean(B))
    exceptions = analytics.build_exceptions(shots)

    before = copy.deepcopy((shots, exceptions))

    ex.explain_all(shots, exceptions)
    ex.explain_all(shots, exceptions)

    assert (shots, exceptions) == before


# ============================================================
# OPTIONAL MODEL COMMENTARY
# ============================================================

def one_explanation():

    _, _, found = explanations(documents(shipment=A))

    return found["invoice"]


def test_commentary_is_separate_labelled_and_leaves_the_explanation_alone():

    item = one_explanation()
    before = copy.deepcopy(item)
    prompts = []

    def llm(prompt):
        prompts.append(prompt)
        return "The line items add up to $1,430.00 but the total is $1,550.00; ask the carrier to check it."

    commentary = ex.generate_commentary(item, llm)

    assert item == before
    assert commentary["generated"] and commentary["label"] == ex.COMMENTARY_LABEL
    assert "not part of the audit" in commentary["label"]
    assert commentary["unsupported_numbers"] == []

    # The model is only given the audit's facts
    assert "ONLY the facts below" in prompts[0] and "Total Invoice Amount: $1,550.00" in prompts[0]
    assert "BILL OF LADING" not in prompts[0]


def test_commentary_cannot_override_a_calculation():

    item = one_explanation()
    before = copy.deepcopy(item)

    commentary = ex.generate_commentary(
        item, lambda prompt: "Actually the total is $1,430.00 and the shipment was 7 days late."
    )

    # The audit's numbers stand; the unsupported figures are listed
    assert item == before
    assert item["difference_text"] == "+$120.00" and item["observed"] == "Total Invoice Amount: $1,550.00"
    assert commentary["unsupported_numbers"] == ["7"]


def test_a_model_step_that_changes_the_explanation_is_refused():

    item = one_explanation()

    def meddling(prompt):
        item["difference"] = 0
        return "ok"

    with pytest.raises(RuntimeError, match="must not change"):
        ex.generate_commentary(item, meddling)


def test_unsupported_number_detection():

    item = one_explanation()

    assert ex.unsupported_numbers("", item) == []
    assert ex.unsupported_numbers("about $1,550.00 versus $1,430.00 (120)", item) == []
    assert ex.unsupported_numbers("a loss of $9,999.50", item) == ["9999.50"]
