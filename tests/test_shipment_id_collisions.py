"""
Shipment IDs that differ only in where the separators are.

identifier_key ignores case and separators, so "SHJ-2026-001",
"shj 2026 001" and "SHJ2026001" are one shipment. It also makes
"AB-12-3" and "AB-1-23" the same key (AB123), and those may be two
different shipments. The key is not changed (stored data keeps its
keys); instead a collision is detected and the documents are never used
together: the audit does not reconcile them as one shipment and a
question about them is not answered from a mix of both.

No PDFs, no index, no Ollama and no files are involved.

Run from the project root:
    python -m pytest tests/test_shipment_id_collisions.py
"""

import copy
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
    from audit_analytics import build_snapshot
    from shipment_identity import identifier_collisions, identifier_key, separator_positions

from rag_scope import SCOPE_ABSTAINED, SCOPE_OK, resolve_scope
from test_reconciliation_consistency import chunk, documents


# ============================================================
# DETECTION
# ============================================================

def test_the_key_itself_is_unchanged():

    # Stored data (register, extraction store, reviews, history) is keyed by it
    assert identifier_key("AB-12-3") == identifier_key("AB-1-23") == "AB123"
    assert identifier_key("SHJ-2026-001") == identifier_key("shj 2026 001") == "SHJ2026001"


@pytest.mark.parametrize("value, positions", [
    ("AB-12-3", {2, 4}),
    ("AB-1-23", {2, 3}),
    ("SHJ 2026-001", {3, 7}),
    ("SHJ2026001", set()),
    ("  -SHJ-2026-001-  ", {3, 7}),
    ("SHJ - 2026 - 001", {3, 7}),
    (None, set()),
])
def test_separator_positions(value, positions):

    assert separator_positions(value) == positions


def test_separators_in_different_places_collide():

    assert identifier_collisions(["AB-12-3", "AB-1-23"]) == [("AB-1-23", "AB-12-3")]
    assert identifier_collisions(["AB12-3", "AB1-23"]) == [("AB1-23", "AB12-3")]


@pytest.mark.parametrize("values", [
    ["SHJ-2026-001", "shj 2026 001", "SHJ2026001"],
    ["SHJ-2026-001", "SHJ2026-001", "SHJ 2026-001"],
    ["SHJ-2026-001", "SHJ_2026_001", "SHJ - 2026 - 001", " SHJ-2026-001 "],
    ["shj-2026-001", "SHJ-2026-001"],
    ["SHJ-2026-001", "SHJ-2026-001"],
    ["AB-123", "AB-12-3"],
    ["SHJ-2026-001"],
    [],
])
def test_legitimate_variants_never_collide(values):

    assert identifier_collisions(values) == []


def test_different_keys_never_collide():

    assert identifier_collisions(["AB-12-3", "AB-12-4", "SHJ-2026-001"]) == []


def test_only_the_conflicting_pair_is_reported():

    # AB123 written without separators is compatible with both
    assert identifier_collisions(["AB-12-3", "AB-1-23", "AB123"]) == [("AB-1-23", "AB-12-3")]


def test_empty_values_are_ignored():

    assert identifier_collisions([None, "", "  ", "-", "AB-12-3"]) == []


# ============================================================
# AUDIT
# ============================================================

def colliding_documents():
    """
    BOL and POD printed AB-12-3, the invoice printed AB-1-23: one key.
    """

    bol, pod, _ = documents(shipment="AB-12-3")
    _, _, invoice = documents(shipment="AB-1-23")

    return [bol, pod, invoice]


@pytest.mark.parametrize("selected", ["AB-12-3", "AB-1-23", "ab 12 3"])
def test_colliding_documents_are_not_reconciled_as_one_shipment(selected):

    result = reconciliation.reconcile_shipment(selected, chunks=colliding_documents())

    assert result["success"] is False
    assert "AB-12-3" in result["error"] and "AB-1-23" in result["error"]
    assert "not used together" in result["error"]
    assert result["identity_collision"] == {"key": "AB123", "forms": ["AB-1-23", "AB-12-3"]}


def test_collision_without_a_selected_shipment():

    result = reconciliation.reconcile_shipment(chunks=colliding_documents())

    assert result["success"] is False
    assert result["identity_collision"]["forms"] == ["AB-1-23", "AB-12-3"]


def test_collision_with_unreadable_files_names_them():

    problems = [{"filename": "scan.pdf", "status": "no_text", "error": "no text"}]

    result = reconciliation.reconcile_shipment("AB-12-3", chunks=colliding_documents(), unreadable_files=problems)

    assert result["success"] is False
    assert "scan.pdf" in result["error"]
    assert result["unreadable_files"] == ["scan.pdf"]


def test_legitimate_variants_are_still_one_shipment():

    uniform = reconciliation.reconcile_shipment("SHJ-2026-001", chunks=documents())

    bol, pod, invoice = documents()
    pod["metadata"]["shipment_id"] = "shj 2026 001"
    invoice["metadata"]["shipment_id"] = "SHJ2026001"

    variants = reconciliation.reconcile_shipment("SHJ 2026 001", chunks=[bol, pod, invoice])

    assert uniform["success"] is True and variants["success"] is True
    assert "identity_collision" not in variants

    for field in ("final_status", "discrepancies", "review_items"):
        assert variants[field] == uniform[field]

    # Same figures; the evidence quotes each document's ID as printed on it
    for category in ("quantity", "delivery", "invoice"):
        assert {k: v for k, v in variants[category].items() if k != "evidence"} == \
            {k: v for k, v in uniform[category].items() if k != "evidence"}
        assert [item["source"] for item in variants[category]["evidence"]] == \
            [item["source"] for item in uniform[category]["evidence"]]


def test_other_shipments_are_unaffected_by_a_collision():

    chunks = colliding_documents() + documents(shipment="SHJ-2026-002")

    alone = reconciliation.reconcile_shipment("SHJ-2026-002", chunks=documents(shipment="SHJ-2026-002"))
    beside = reconciliation.reconcile_shipment("SHJ-2026-002", chunks=chunks)

    assert beside["success"] is True
    assert beside["final_status"] == alone["final_status"]
    assert beside["quantity"] == alone["quantity"]


def test_the_documents_are_not_changed():

    chunks = colliding_documents()
    before = copy.deepcopy(chunks)

    reconciliation.reconcile_shipment("AB-12-3", chunks=chunks)

    assert chunks == before


def test_snapshot_lists_the_collision_once_and_audits_the_rest():

    chunks = colliding_documents() + documents(shipment="SHJ-2026-002")

    snapshot = build_snapshot(chunks, reconciliation.reconcile_shipment)

    by_key = {item["shipment_key"]: item for item in snapshot["shipments"]}

    assert sorted(by_key) == ["AB123", "SHJ2026002"]
    assert by_key["AB123"]["result"]["success"] is False
    assert by_key["AB123"]["result"]["identity_collision"]["forms"] == ["AB-1-23", "AB-12-3"]
    assert by_key["SHJ2026002"]["result"]["success"] is True


# ============================================================
# QUESTIONS
# ============================================================

def records(*chunks):

    return [{"document": item["text"], "metadata": item["metadata"]} for item in chunks]


def test_selected_colliding_shipment_abstains():

    scope = resolve_scope("What is the invoice total?", "AB-12-3", records(*colliding_documents()))

    assert scope["status"] == SCOPE_ABSTAINED
    assert "AB-12-3" in scope["message"] and "AB-1-23" in scope["message"]


def test_question_naming_a_colliding_shipment_abstains():

    all_records = records(*colliding_documents(), *documents(shipment="SHJ-2026-002"))

    scope = resolve_scope("What is the invoice total for AB-1-23?", None, all_records)

    assert scope["status"] == SCOPE_ABSTAINED
    assert "AB-12-3" in scope["message"]


def test_other_shipment_questions_are_unaffected():

    all_records = records(*colliding_documents(), *documents(shipment="SHJ-2026-002"))

    selected = resolve_scope("What is the invoice total?", "SHJ-2026-002", all_records)
    named = resolve_scope("What is the invoice total for SHJ-2026-002?", None, all_records)

    assert selected["status"] == SCOPE_OK and selected["keys"] == ["SHJ2026002"]
    assert named["status"] == SCOPE_OK and named["keys"] == ["SHJ2026002"]


def test_legitimate_variants_still_answer():

    bol, pod, invoice = documents()
    pod["metadata"]["shipment_id"] = "shj 2026 001"
    invoice["metadata"]["shipment_id"] = "SHJ2026001"

    scope = resolve_scope("What is the invoice total?", "SHJ-2026-001", records(bol, pod, invoice))

    assert scope["status"] == SCOPE_OK
    assert scope["keys"] == ["SHJ2026001"]
