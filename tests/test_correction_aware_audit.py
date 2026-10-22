"""
Correction-aware audit: the approval workflow, the audit inputs, history and
recomputation, the assistant's citations, and the migrations behind them.

Synthetic PDFs in a temporary folder; no network, no model.

Run: python -m pytest tests/test_correction_aware_audit.py
"""

import copy
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_history  # noqa: E402
import audit_inputs  # noqa: E402
import extraction_store as store_module  # noqa: E402
from assistant import Assistant  # noqa: E402
from audit_analytics import build_exceptions  # noqa: E402
from audit_history import HistoryStore  # noqa: E402
from auth import UserStore  # noqa: E402
from authz import ADMIN, PermissionDenied, Principal  # noqa: E402
from extraction_store import ExtractionStore, InvalidTransition, StaleCorrection, StoreError  # noqa: E402
from test_api import A, B, PASSWORD, Rig  # noqa: E402

ALICE = Principal("alice", "admin", True, 1)
DAVE = Principal("dave", "admin", True, 4)
BOB = Principal("bob", "reviewer", True, 2)
CAROL = Principal("carol", "reviewer", True, 3)
LOCAL = Principal("local-dev", "admin", False, None)

REASON = "The POD count was misread by the extractor."
POD_A, BOL_A, INVOICE_A = (f"{kind}_SHJ-2026-001.pdf" for kind in ("POD", "BOL", "INVOICE"))


@pytest.fixture
def rig(tmp_path):
    result = Rig(tmp_path, "required")
    UserStore(tmp_path / "auth.sqlite3").create_user("dave", PASSWORD, ADMIN)

    return result


def store_of(rig):
    return ExtractionStore(rig.locations.extraction_path())


def extract(rig, name):
    reply = rig.client("alice").post("/api/extractions/run", json={"document": name})

    assert reply.status_code == 200, reply.text

    return reply.json()["id"]


def propose(rig, extraction_id, field, value, who=BOB, based_on=None):
    return store_of(rig).propose(who, extraction_id, field, value, REASON, based_on=based_on)


def approve(rig, correction_id, who=ALICE):
    return rig.state.service.decide_correction(who, correction_id, "approve", "Checked against the paper POD.")


def quantity(rig, shipment=A):
    return rig.state.service.shipment(shipment)["result"]["quantity"]


def results(rig):
    return {s["shipment_key"]: s["result"] for s in rig.state.service.snapshot()["shipments"]}


def record_run(rig):
    service = rig.state.service
    snapshot = service.snapshot()

    return audit_history.record_run_as(ALICE, service.history_store(), snapshot, build_exceptions(snapshot), label="All shipments")


def corpus_pdf(folder, name, title, shipment, pairs):
    """pairs: (label, value); a label may repeat."""

    document = pymupdf.open()
    page = document.new_page()
    lines = [title, f"Shipment Number: {shipment}"]

    for label, value in pairs:
        lines += [f"{label}:", value]

    for index, line in enumerate(lines):
        page.insert_text((50, 60 + index * 16), line, fontsize=10)

    document.save(str(Path(folder) / name))
    document.close()


def add_shipment(raw, shipment, bol_pairs=None):
    bol = bol_pairs or [("Carrier", "Test Lines"), ("Origin", "Dallas"), ("Destination", "Austin"), ("Pickup Date", "October 1, 2026"),
                        ("Expected Delivery Date", "October 3, 2026"), ("Number of Pieces", "10")]
    corpus_pdf(raw, f"BOL_{shipment}.pdf", "BILL OF LADING", shipment, bol)
    corpus_pdf(raw, f"POD_{shipment}.pdf", "PROOF OF DELIVERY", shipment,
               [("Carrier", "Test Lines"), ("Delivery Date", "October 3, 2026"), ("Number of Pieces Delivered", "10")])
    corpus_pdf(raw, f"INVOICE_{shipment}.pdf", "FREIGHT INVOICE", shipment,
               [("Carrier", "Test Lines"), ("Origin", "Dallas"), ("Destination", "Austin"), ("Base Freight", "$100.00"),
                ("Fuel Surcharge", "$10.00"), ("Detention Charge", "$0.00"), ("Other Accessorial Charges", "$0.00"),
                ("Total Invoice Amount", "$110.00")])


# ---- the audit before any correction ------------------------------------------------------------------

def test_without_corrections_the_audit_is_what_it_was_and_inputs_say_so(rig):
    before = results(rig)
    pod_id = extract(rig, POD_A)                      # an extraction store now exists, with no approvals
    after = results(rig)

    assert before == after and quantity(rig)["status"] == "MISMATCH" and quantity(rig)["pod"] == 45

    inputs = rig.state.service.shipment(A)["inputs"]
    fields = {f["field"]: f for f in inputs["fields"]}

    assert inputs["applied_correction_ids"] == [] and inputs["calculation_version"] == audit_inputs.CALCULATION_VERSION
    assert set(fields) == set(audit_inputs.AUDIT_FIELDS)
    assert all(f["source"] == "legacy_reader" for f in fields.values())
    assert fields["expected_pieces"]["value"] == 48 and fields["expected_pieces"]["document"] == BOL_A and fields["expected_pieces"]["page"] == 1
    assert fields["invoice_total"]["value"] == 1550.0 and fields["expected_date"]["value"] == "2026-10-03"
    # The structured extraction of the POD is a cross-check, never the value
    assert fields["delivered_pieces"]["structured"] == {"extraction_id": pod_id, "extractor_version": fields["delivered_pieces"]["extractor_version"],
                                                        "state": "EXTRACTED", "value": 45, "agrees": True}
    assert fields["expected_pieces"]["structured"] is None


def test_inputs_hold_no_document_text(rig):
    extract(rig, POD_A)
    text = json.dumps(rig.state.service.shipment(A)["inputs"])

    assert "Industrial" not in text and "Number of Pieces" not in text and "Gulf Manufacturing" not in text


# ---- proposals have no effect --------------------------------------------------------------------------------

def test_a_proposed_correction_changes_nothing_in_the_audit(rig):
    before = results(rig)
    pod_id = extract(rig, POD_A)
    correction = propose(rig, pod_id, "delivered_pieces", "48")

    assert correction["status"] == "pending"
    assert results(rig) == before and quantity(rig)["pod"] == 45
    assert rig.state.service.shipment(A)["inputs"]["applied_correction_ids"] == []
    assert store_of(rig).effective(pod_id)["delivered_pieces"]["value"] == 45


def test_a_rejected_correction_never_influences_the_audit(rig):
    before = results(rig)
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")

    outcome = rig.state.service.decide_correction(ALICE, correction["id"], "reject", "The paper POD says 45.")

    assert outcome["decision"] == "rejected" and outcome["recompute"] == {"status": "not_needed"}
    assert results(rig) == before and store_of(rig).approved_corrections("SHJ2026001") == []
    assert store_of(rig).corrections(correction["extraction_id"])[0]["status"] == "rejected"


def test_a_rejection_needs_a_reason_and_a_decision_is_final(rig):
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")
    service = rig.state.service

    with pytest.raises(StoreError, match="why the correction is rejected"):
        service.decide_correction(ALICE, correction["id"], "reject", "no")

    service.decide_correction(ALICE, correction["id"], "reject", "The paper POD says 45.")

    for decision in ("approve", "reject"):
        with pytest.raises(InvalidTransition, match="already rejected"):
            service.decide_correction(DAVE, correction["id"], decision, "Another look at the same thing.")


# ---- who may decide -----------------------------------------------------------------------------------------------

def test_the_proposer_cannot_approve_their_own_correction(rig):
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48", who=ALICE)

    with pytest.raises(PermissionDenied, match="your own correction"):
        rig.state.service.decide_correction(ALICE, correction["id"], "approve")

    assert store_of(rig).corrections(correction["extraction_id"])[0]["status"] == "pending"
    assert quantity(rig)["pod"] == 45
    # ...and another administrator can
    assert approve(rig, correction["id"], DAVE)["decision"] == "approved"


@pytest.mark.parametrize("who", [BOB, CAROL, LOCAL, None, "alice", Principal("eve", "root")])
def test_only_a_signed_in_administrator_can_decide(rig, who):
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48", who=CAROL if who is not CAROL else BOB)

    with pytest.raises(PermissionDenied):
        rig.state.service.decide_correction(who, correction["id"], "approve")

    assert quantity(rig)["pod"] == 45 and store_of(rig).approved_corrections("SHJ2026001") == []


def test_the_store_enforces_the_rules_itself_without_the_service(rig):
    store = store_of(rig)
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48", who=ALICE)

    for who in (ALICE, BOB, LOCAL, None):
        with pytest.raises(PermissionDenied):
            store.decide(who, correction["id"], "approve")

    assert store.decide(DAVE, correction["id"], "approve")["decision"] == "approved"


def test_the_api_enforces_roles_and_separation(rig):
    pod_id = extract(rig, POD_A)
    proposal = rig.client("bob").post(f"/api/extractions/{pod_id}/corrections",
                                      json={"field": "delivered_pieces", "value": "48", "reason": REASON, "based_on": None}).json()
    url = f"/api/extractions/corrections/{proposal['id']}/decision"
    body = {"decision": "approve", "note": "Verified."}

    assert rig.client().post(url, json=body).status_code == 401
    assert rig.client("bob").post(url, json=body).status_code == 403
    assert rig.client("carol").post(url, json=body).status_code == 403
    assert rig.client("bob").get("/api/extractions/pending").json()["pending"][0]["id"] == proposal["id"]
    assert quantity(rig)["pod"] == 45

    approved = rig.client("alice").post(url, json=body)

    assert approved.status_code == 200 and approved.json()["decision"] == "approved"
    assert rig.client("dave").post(url, json=body).status_code == 409
    assert rig.client("alice").post("/api/extractions/corrections/999/decision", json=body).status_code == 404
    assert rig.client("alice").post(url, json={"decision": "maybe"}).status_code == 422


def test_an_admin_proposal_cannot_be_approved_by_the_same_admin_over_the_api(rig):
    pod_id = extract(rig, POD_A)
    proposal = rig.client("alice").post(f"/api/extractions/{pod_id}/corrections",
                                        json={"field": "delivered_pieces", "value": "48", "reason": REASON, "based_on": None}).json()

    assert rig.client("alice").post(f"/api/extractions/corrections/{proposal['id']}/decision", json={"decision": "approve"}).status_code == 403
    assert rig.client("dave").post(f"/api/extractions/corrections/{proposal['id']}/decision", json={"decision": "approve"}).status_code == 200


# ---- approval changes the next audit, and only the relevant part -------------------------------------------------------

def test_an_approved_correction_changes_the_audit_of_its_shipment_only(rig):
    before = results(rig)
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")

    approve(rig, correction["id"])
    after = results(rig)
    key = "SHJ2026001"

    assert quantity(rig) | {} != before[key]["quantity"] | {}
    assert after[key]["quantity"]["pod"] == 48 and after[key]["quantity"]["status"] == "MATCH" and after[key]["quantity"]["difference"] == 0
    # Everything else of this shipment, and every other shipment, is exactly as before
    assert after[key]["delivery"] == before[key]["delivery"] and after[key]["invoice"] == before[key]["invoice"]
    assert {k: v for k, v in after.items() if k != key} == {k: v for k, v in before.items() if k != key}

    evidence = [e for e in after[key]["quantity"]["evidence"] if e["value_origin"] == "approved_correction"]

    assert evidence and evidence[0]["source"] == POD_A and evidence[0]["page"] is None and evidence[0]["correction_id"] == correction["id"]
    assert all(e["page"] == 1 for e in after[key]["quantity"]["evidence"] if e["value_origin"] == "legacy_reader")


def test_the_exposure_and_completeness_follow_only_what_they_consume(rig):
    service = rig.state.service
    before_completeness = json.dumps(service.completeness(), sort_keys=True, default=str)
    before_exposure = {r["shipment_key"]: r for r in service.exposure_rows()}

    approve(rig, propose(rig, extract(rig, POD_A), "delivered_pieces", "48")["id"])

    after_exposure = {r["shipment_key"]: r for r in service.exposure_rows()}

    # A piece count is not an invoice figure: no amount moves. The shipment no longer lists a Quantity discrepancy.
    row_before, row_after = before_exposure["SHJ2026001"], after_exposure["SHJ2026001"]

    assert {k: v for k, v in row_after.items() if k != "discrepancy_categories"} == {k: v for k, v in row_before.items() if k != "discrepancy_categories"}
    assert "Quantity" in row_before["discrepancy_categories"] and "Quantity" not in row_after["discrepancy_categories"]
    assert {k: v for k, v in after_exposure.items() if k != "SHJ2026001"} == {k: v for k, v in before_exposure.items() if k != "SHJ2026001"}
    assert json.dumps(service.completeness(), sort_keys=True, default=str) == before_completeness

    invoice_id = extract(rig, INVOICE_A)
    approve(rig, propose(rig, invoice_id, "invoice_total", "1430.00")["id"], DAVE)

    exposure = {r["shipment_key"]: r for r in service.exposure_rows()}

    assert exposure["SHJ2026001"]["stated_total"] == 1430.0 and exposure["SHJ2026001"] != after_exposure["SHJ2026001"]
    assert {k: v for k, v in exposure.items() if k != "SHJ2026001"} == {k: v for k, v in before_exposure.items() if k != "SHJ2026001"}

    invoice = quantity and rig.state.service.shipment(A)["result"]["invoice"]

    # The corrected total now equals the line items: the audit is not left abstaining on the document's own amount
    assert invoice["total"] == 1430.0 and invoice["status"] == "MATCH" and invoice["line_item_sum"] == 1430.0


def test_a_corrected_date_is_used_by_the_delivery_audit(rig):
    before = results(rig)["SHJ2026001"]["delivery"]
    approve(rig, propose(rig, extract(rig, POD_A), "delivery_date", "October 3, 2026")["id"])
    delivery = rig.state.service.shipment(A)["result"]["delivery"]

    assert before["status"] == "DELAYED" and delivery["status"] == "ON TIME" and delivery["days_late"] == 0
    assert delivery["actual"] == "October 3, 2026"


def test_a_field_no_calculation_consumes_has_no_effect_and_says_so(rig):
    before = results(rig)
    correction = propose(rig, extract(rig, BOL_A), "weight", "13,000 lbs")
    outcome = approve(rig, correction["id"])

    assert outcome["recompute"] == {"status": "no_calculation_consumes_this_field"} and results(rig) == before
    assert store_of(rig).effective(correction["extraction_id"])["weight"]["value"] == {"amount": 13000.0, "unit": "lb"}


# ---- audit inputs: provenance of consumed values ---------------------------------------------------------------------------

def test_the_inputs_of_a_corrected_audit_name_the_correction_and_the_source(rig):
    pod_id = extract(rig, POD_A)
    correction = propose(rig, pod_id, "delivered_pieces", "48")
    approve(rig, correction["id"])

    inputs = rig.state.service.shipment(A)["inputs"]
    field = next(f for f in inputs["fields"] if f["field"] == "delivered_pieces")

    assert inputs["applied_correction_ids"] == [correction["id"]]
    assert field["source"] == "approved_correction" and field["value"] == 48 and field["resolved"] is True
    assert field["correction_id"] == correction["id"] and field["extraction_id"] == pod_id and field["extractor_version"]
    assert field["document"] == POD_A and field["page"] is None and field["validation"] == "approved"
    assert field["document_reading"] == {"state": "extracted", "value": 45}
    assert all(f["source"] == "legacy_reader" for f in inputs["fields"] if f["field"] != "delivered_pieces")
    assert rig.state.service.shipment(B)["inputs"]["applied_correction_ids"] == []


def test_unresolved_values_stay_unresolved_and_are_never_zero(rig):
    add_shipment(rig.raw, "SHJ-2026-811", [("Carrier", "Test Lines"), ("Origin", "Dallas"), ("Destination", "Austin"),
                                          ("Pickup Date", "October 1, 2026"), ("Expected Delivery Date", "October 3, 2026"),
                                          ("Number of Pieces", "10"), ("Number of Pieces", "12")])
    shipment = rig.state.service.shipment("SHJ-2026-811")
    field = next(f for f in shipment["inputs"]["fields"] if f["field"] == "expected_pieces")

    assert field["state"] == "conflicting" and field["resolved"] is False and field["value"] is None
    assert shipment["result"]["quantity"]["status"] == "NEEDS REVIEW" and shipment["result"]["quantity"]["bol"] is None
    assert shipment["result"]["final_status"] == "NEEDS REVIEW"


def test_an_approved_correction_can_resolve_a_conflicting_field(rig):
    add_shipment(rig.raw, "SHJ-2026-811", [("Carrier", "Test Lines"), ("Origin", "Dallas"), ("Destination", "Austin"),
                                          ("Pickup Date", "October 1, 2026"), ("Expected Delivery Date", "October 3, 2026"),
                                          ("Number of Pieces", "10"), ("Number of Pieces", "12")])
    bol_id = extract(rig, "BOL_SHJ-2026-811.pdf")

    assert store_of(rig).effective(bol_id)["piece_count"]["state"] == "CONFLICTING"

    approve(rig, propose(rig, bol_id, "piece_count", "10")["id"])
    shipment = rig.state.service.shipment("SHJ-2026-811")

    assert shipment["result"]["quantity"]["bol"] == 10 and shipment["result"]["quantity"]["status"] == "MATCH"
    assert next(f for f in shipment["inputs"]["fields"] if f["field"] == "expected_pieces")["document_reading"]["state"] == "conflicting"


def test_approved_corrections_on_different_documents_that_disagree_are_unresolved(rig):
    add_shipment(rig.raw, "SHJ-2026-812")
    corpus_pdf(rig.raw, "BOL_SHJ-2026-812_copy.pdf", "BILL OF LADING", "SHJ-2026-812",
               [("Carrier", "Test Lines"), ("Expected Delivery Date", "October 3, 2026"), ("Number of Pieces", "10")])
    first, second = extract(rig, "BOL_SHJ-2026-812.pdf"), extract(rig, "BOL_SHJ-2026-812_copy.pdf")

    approve(rig, propose(rig, first, "piece_count", "11")["id"])
    approve(rig, propose(rig, second, "piece_count", "13")["id"], DAVE)

    shipment = rig.state.service.shipment("SHJ-2026-812")
    field = next(f for f in shipment["inputs"]["fields"] if f["field"] == "expected_pieces")

    assert field["state"] == "conflicting" and field["resolved"] is False and "approved_corrections_disagree" in field["warnings"]
    assert shipment["result"]["quantity"]["status"] == "NEEDS REVIEW"


def test_the_latest_approval_for_a_document_and_field_wins_and_the_earlier_is_listed(rig):
    pod_id = extract(rig, POD_A)
    first = propose(rig, pod_id, "delivered_pieces", "47")
    approve(rig, first["id"])
    second = propose(rig, pod_id, "delivered_pieces", "48", based_on=first["id"])
    approve(rig, second["id"], DAVE)

    field = next(f for f in rig.state.service.shipment(A)["inputs"]["fields"] if f["field"] == "delivered_pieces")

    assert field["value"] == 48 and field["correction_id"] == second["id"] and field["superseded_correction_ids"] == [first["id"]]


def test_a_correction_stops_applying_when_its_document_changes(rig):
    pod_id = extract(rig, POD_A)
    correction = propose(rig, pod_id, "delivered_pieces", "48")
    approve(rig, correction["id"])

    assert quantity(rig)["pod"] == 48

    pod = rig.raw / POD_A
    pod.write_bytes(pod.read_bytes() + b"\n%edited after the correction")
    rig.state.service.invalidate()
    shipment = rig.state.service.shipment(A)

    assert shipment["result"]["quantity"]["pod"] == 45 and shipment["inputs"]["applied_correction_ids"] == []
    assert shipment["inputs"]["ignored"] == [{"correction_id": correction["id"], "reason": "document_changed_or_removed"}]


def test_a_correction_of_one_shipment_cannot_reach_another(rig):
    pod_a = extract(rig, POD_A)
    approve(rig, propose(rig, pod_a, "delivered_pieces", "48")["id"])

    assert store_of(rig).approved_corrections("SHJ2026002") == []
    assert rig.state.service.shipment(B)["result"]["quantity"] == results(rig)["SHJ2026002"]["quantity"]
    assert rig.state.service.shipment(B)["result"]["quantity"]["pod"] == 60
    assert all(e["shipment_id"] == B for e in rig.state.service.shipment(B)["result"]["quantity"]["evidence"])


# ---- history: recorded inputs, supersession, staleness ---------------------------------------------------------------------------

def test_every_recorded_run_records_its_inputs(rig):
    run = record_run(rig)
    history = rig.state.service.history_store()
    inputs = history.run_inputs(run["run_id"], "SHJ2026001")

    assert {i["field_name"] for i in inputs} == set(audit_inputs.AUDIT_FIELDS)
    assert all(i["source"] == "legacy_reader" and i["calculation_version"] == audit_inputs.CALCULATION_VERSION for i in inputs)
    pieces = next(i for i in inputs if i["field_name"] == "expected_pieces")

    assert pieces["value"] == 48 and pieces["document"] == BOL_A and pieces["page"] == 1 and pieces["resolved"] is True
    assert pieces["consumed_by"] == ["quantity"] and history.runs(1)[0]["calculation_version"] == audit_inputs.CALCULATION_VERSION


def test_approval_records_a_new_run_that_supersedes_the_old_one_without_touching_it(rig):
    first = record_run(rig)
    history = rig.state.service.history_store()
    old_inputs, old_snapshots = history.run_inputs(first["run_id"]), history.snapshots("SHJ2026001")

    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")
    outcome = approve(rig, correction["id"])

    assert outcome["recompute"]["status"] == "recorded" and outcome["recompute"]["shipments"] == ["SHJ2026001"]

    new_run = outcome["recompute"]["run_id"]
    history = rig.state.service.history_store()

    # The old run and its snapshot are exactly as they were
    assert history.run_inputs(first["run_id"]) == old_inputs
    assert history.snapshots("SHJ2026001")[0] == {**old_snapshots[0], "runs": old_snapshots[0]["runs"]}
    assert history.superseded_by(first["run_id"], "SHJ2026001") == new_run

    runs = {r["id"]: r for r in history.runs()}

    assert runs[first["run_id"]]["superseded_by"] == [{"shipment_key": "SHJ2026001", "new_run_id": new_run, "correction_ids": [correction["id"]]}]
    assert runs[new_run]["supersedes"][0]["old_run_id"] == first["run_id"]
    # Targeted: the new run did not touch the other shipments
    assert runs[new_run]["shipments_checked"] == 1 and {i["shipment_key"] for i in history.run_inputs(new_run)} == {"SHJ2026001"}

    pod = next(i for i in history.run_inputs(new_run) if i["field_name"] == "delivered_pieces")

    assert pod["source"] == "approved_correction" and pod["value"] == 48 and pod["correction_id"] == correction["id"]
    assert pod["document"] == POD_A and pod["page"] is None and pod["extraction_id"] == correction["extraction_id"]

    latest = history.snapshots("SHJ2026001")[-1]

    assert latest["quantity_status"] == "MATCH" and latest["pod_pieces"] == 48 and latest["run_id"] == new_run
    assert history.snapshots("SHJ2026001")[0]["quantity_status"] == "MISMATCH" and history.snapshots("SHJ2026001")[0]["pod_pieces"] == 45
    assert history.snapshots("SHJ2026002") == history.snapshots("SHJ2026002")[:1] and rig.state.service.staleness() == []


def test_unrelated_shipments_get_no_new_snapshot_and_no_supersession(rig):
    record_run(rig)
    approve(rig, propose(rig, extract(rig, POD_A), "delivered_pieces", "48")["id"])
    history = rig.state.service.history_store()

    assert len(history.snapshots("SHJ2026002")) == 1 and len(history.snapshots("SHJ2026701")) == 1
    assert all(r["shipment_key"] == "SHJ2026001" for run in history.runs() for r in run["supersedes"] + run["superseded_by"])


def test_staleness_is_derived_and_cleared_by_the_recorded_run(rig):
    record_run(rig)
    service = rig.state.service
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")

    # An approval by an administrator without the right to record leaves the shipment stale
    store_of(rig).decide(DAVE, correction["id"], "approve")
    service.invalidate()
    stale = service.staleness()

    assert [s["shipment_key"] for s in stale] == ["SHJ2026001"] and stale[0]["missing_correction_ids"] == [correction["id"]]
    assert service.history_store().superseded_by(stale[0]["recorded_run_id"], "SHJ2026001") is None
    assert rig.client("alice").get("/api/extractions/staleness").json()["stale"][0]["shipment_key"] == "SHJ2026001"

    recorded = service.record_corrected_run(ALICE)

    assert recorded["recorded"] and recorded["shipments"] == ["SHJ2026001"] and service.staleness() == []
    assert service.record_corrected_run(ALICE) == {"recorded": False, "run_id": None, "shipments": []}

    with pytest.raises(PermissionDenied):
        service.record_corrected_run(BOB)


def test_the_history_api_shows_inputs_and_what_supersedes_a_run(rig):
    first = record_run(rig)
    approve(rig, propose(rig, extract(rig, POD_A), "delivered_pieces", "48")["id"])
    alice = rig.client("alice")

    reply = alice.get(f"/api/history/runs/{first['run_id']}/inputs?shipment_id={A}").json()

    assert reply["recorded"] and {i["field_name"] for i in reply["inputs"]} == set(audit_inputs.AUDIT_FIELDS)
    assert reply["superseded_by"] == {"SHJ2026001": first["run_id"] + 1}
    assert rig.client().get(f"/api/history/runs/{first['run_id']}/inputs").status_code == 401
    assert alice.get("/api/history/runs/999/inputs").status_code == 404
    assert alice.get(f"/api/history/runs/{first['run_id']}/inputs?shipment_id=%20").status_code == 422


# ---- concurrency and stale versions -------------------------------------------------------------------------------------------------

def test_concurrent_decisions_on_one_correction_record_exactly_one(rig):
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")
    path, outcomes = rig.locations.extraction_path(), []

    def decide(who, decision):
        try:
            outcomes.append(("ok", ExtractionStore(path).decide(who, correction["id"], decision, "Reviewed against the paper copy.")["decision"]))
        except InvalidTransition:
            outcomes.append(("refused", decision))

    threads = [threading.Thread(target=decide, args=(who, d)) for who, d in ((ALICE, "approve"), (DAVE, "reject"), (DAVE, "approve"), (ALICE, "reject"))]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert [o[0] for o in outcomes].count("ok") == 1 and [o[0] for o in outcomes].count("refused") == 3
    assert len(store_of(rig).corrections(correction["extraction_id"])) == 1
    assert store_of(rig).verify_history()["ok"] and store_of(rig).verify_history()["decisions_checked"] == 1


def test_a_stale_proposal_is_refused_and_a_pending_one_blocks_a_second(rig):
    pod_id = extract(rig, POD_A)
    first = propose(rig, pod_id, "delivered_pieces", "46")

    with pytest.raises(StaleCorrection):
        propose(rig, pod_id, "delivered_pieces", "47", who=CAROL, based_on=None)

    with pytest.raises(StoreError, match="awaiting a decision"):
        propose(rig, pod_id, "delivered_pieces", "47", who=CAROL, based_on=first["id"])

    rig.state.service.decide_correction(ALICE, first["id"], "reject", "The paper POD does not say 46.")

    assert propose(rig, pod_id, "delivered_pieces", "47", who=CAROL, based_on=first["id"])["status"] == "pending"


# ---- rollback and retry ---------------------------------------------------------------------------------------------------------------------

def test_a_failed_decision_leaves_the_correction_pending_and_can_be_retried(rig, monkeypatch):
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")

    def boom(*args, **kwargs):
        raise RuntimeError("disk trouble")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "decision_hash", boom)

        with pytest.raises(RuntimeError):
            rig.state.service.decide_correction(ALICE, correction["id"], "approve")

    assert store_of(rig).corrections(correction["extraction_id"])[0]["status"] == "pending" and quantity(rig)["pod"] == 45
    assert store_of(rig).verify_history()["decisions_checked"] == 0
    assert approve(rig, correction["id"])["decision"] == "approved" and quantity(rig)["pod"] == 48


def test_a_failed_run_leaves_the_approval_and_the_next_attempt_records_it(rig, monkeypatch):
    record_run(rig)
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")
    runs_before = len(rig.state.service.history_store().runs())

    def broken(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(HistoryStore, "record_run", broken)
        outcome = approve(rig, correction["id"])

    assert outcome["decision"] == "approved" and outcome["recompute"]["status"] == "failed"
    assert "locked" not in json.dumps(outcome["recompute"])
    assert len(rig.state.service.history_store().runs()) == runs_before and quantity(rig)["pod"] == 48
    assert [s["shipment_key"] for s in rig.state.service.staleness()] == ["SHJ2026001"]

    retry = rig.client("alice").post("/api/extractions/recompute", json={"shipment_id": A})

    assert retry.status_code == 200 and retry.json()["recorded"] is True and rig.state.service.staleness() == []


def test_a_run_that_fails_while_recording_inputs_records_nothing(rig, monkeypatch):
    record_run(rig)
    history = rig.state.service.history_store()
    before = (len(history.runs()), len(history.run_inputs(1)))

    def broken(self, *args, **kwargs):
        raise RuntimeError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(HistoryStore, "record_inputs", broken)

        with pytest.raises(RuntimeError):
            record_run(rig)

    assert (len(history.runs()), len(history.run_inputs(1))) == before
    assert len(HistoryStore(rig.locations.history_db).runs()) == before[0]
    assert record_run(rig)["run_id"] == before[0] + 1


# ---- migrations -------------------------------------------------------------------------------------------------------------------------------

def v3_store(path):
    """An extraction store as schema 3 left it, with a correction that was effective immediately."""

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")

    for version in (1, 2, 3):
        for statement in store_module.statements(store_module.MIGRATIONS[version]):
            connection.execute(statement)

    connection.execute("INSERT INTO schema_info VALUES (3)")
    payload = json.dumps({"fields": {"delivered_pieces": {"value": 45, "state": "EXTRACTED", "page": 1}}})
    connection.execute("INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, shipment_id, shipment_key, "
                       "payload, payload_sha256, created_at) VALUES (?, 'h1', '1.1+old', 'POD', 'extracted', 'SHJ-2026-001', 'SHJ2026001', ?, ?, 'then')",
                       (POD_A, payload, store_module.sha256_text(payload)))
    previous, corrected = json.dumps({"value": 45, "state": "EXTRACTED", "source": "extracted"}), json.dumps(48)
    digest = store_module.correction_hash("", 1, "delivered_pieces", previous, corrected, "old reason text", "bob", 1, "then", None)
    connection.execute("INSERT INTO field_corrections (extraction_id, field_name, previous, corrected, reason, actor, authenticated, at, "
                       "based_on, prev_hash, hash) VALUES (1, 'delivered_pieces', ?, ?, 'old reason text', 'bob', 1, 'then', NULL, '', ?)",
                       (previous, corrected, digest))
    connection.commit()
    connection.close()


def test_legacy_corrections_are_marked_legacy_and_never_promoted(tmp_path):
    path = tmp_path / "x.sqlite3"
    v3_store(path)
    store = ExtractionStore(path)

    assert store.version() == store_module.SCHEMA_VERSION
    legacy = store.corrections(1)[0]

    assert legacy["status"] == "legacy" and legacy["legacy"] is True and legacy["corrected"] == 48
    assert store.approved_corrections("SHJ2026001") == [] and store.pending() == []
    assert store.effective(1)["delivered_pieces"]["value"] == 45 and store.effective(1)["delivered_pieces"]["approved"] is None

    with pytest.raises(InvalidTransition, match="before approvals existed"):
        store.decide(ALICE, legacy["id"], "approve")

    # The chain still verifies and a new proposal goes through the normal workflow
    assert store.verify_history()["ok"]
    proposal = store.propose(BOB, 1, "delivered_pieces", "48", REASON, based_on=legacy["id"])

    assert proposal["status"] == "pending" and store.decide(ALICE, proposal["id"], "approve")["decision"] == "approved"
    assert [c["status"] for c in store.corrections(1)] == ["legacy", "approved"] and store.verify_history()["ok"]


def test_a_failed_store_upgrade_rolls_back_and_the_retry_succeeds(tmp_path, monkeypatch):
    path = tmp_path / "x.sqlite3"
    v3_store(path)
    good = dict(store_module.MIGRATIONS)
    monkeypatch.setattr(store_module, "MIGRATIONS", {**good, 4: good[4] + "\nTHIS IS NOT SQL;"})

    with pytest.raises(sqlite3.DatabaseError):
        ExtractionStore(path)

    connection = sqlite3.connect(path)

    assert connection.execute("SELECT version FROM schema_info").fetchone() == (3,)
    assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'correction_decisions'").fetchone() is None
    assert "legacy" not in {r[1] for r in connection.execute("PRAGMA table_info(field_corrections)")}
    connection.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", good)

    assert ExtractionStore(path).corrections(1)[0]["status"] == "legacy"


def v2_history(path):
    connection = sqlite3.connect(path)
    connection.executescript(audit_history.SCHEMA)
    connection.execute("INSERT INTO schema_info VALUES (1)")

    for statement in audit_history.MIGRATION_V2:
        connection.execute(statement)

    connection.execute("UPDATE schema_info SET version = 2")
    connection.execute("INSERT INTO audit_runs (created_at, label, shipments_checked, snapshots_written) VALUES ('then', 'old', 1, 1)")
    connection.execute("INSERT INTO shipment_snapshots (run_id, shipment_key, shipment_id, created_at, input_fingerprint, digest, reconciled, "
                       "discrepancy_count, review_count, audit_states) VALUES (1, 'K1', 'K-1', 'then', 'f', 'd', 1, 0, 0, '{}')")
    connection.execute("INSERT INTO run_shipments (run_id, shipment_key, snapshot_id) VALUES (1, 'K1', 1)")
    connection.commit()
    connection.close()


def test_a_version_2_history_is_upgraded_and_old_runs_have_no_invented_inputs(tmp_path):
    path = tmp_path / "h.sqlite3"
    v2_history(path)
    history = HistoryStore(path)

    assert history.run_inputs(1) == [] and history.runs()[0]["calculation_version"] is None
    assert history.runs()[0]["supersedes"] == [] and history.snapshots("K1")[0]["id"] == 1

    connection = sqlite3.connect(path)

    assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == audit_history.SCHEMA_VERSION

    connection.execute("INSERT INTO run_supersessions (shipment_key, old_run_id, new_run_id, correction_ids, at) VALUES ('K1', 1, 1, '[]', 't')")
    connection.execute("INSERT INTO run_input_sets (run_id, shipment_key, calculation_version, applied, ignored, created_at) VALUES (1, 'K1', '2', '[]', '[]', 't')")
    connection.execute("INSERT INTO run_inputs (run_id, shipment_key, shipment_id, field_name, value, source, resolved, state, warnings, "
                       "consumed_by, calculation_version, created_at) VALUES (1, 'K1', 'K-1', 'expected_pieces', '1', 'legacy_reader', 1, 'extracted', '[]', '[]', '2', 't')")

    for statement in ("UPDATE run_inputs SET value = 'x'", "DELETE FROM run_supersessions", "UPDATE run_input_sets SET applied = '[]'"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(statement)

    connection.close()


def test_a_failed_history_upgrade_changes_nothing_and_retries(tmp_path, monkeypatch):
    path = tmp_path / "h.sqlite3"
    v2_history(path)
    monkeypatch.setattr(audit_history, "MIGRATION_V3", audit_history.MIGRATION_V3 + ("ALTER TABLE nope ADD COLUMN x TEXT",))

    with pytest.raises(sqlite3.OperationalError):
        HistoryStore(path)

    connection = sqlite3.connect(path)

    assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 2
    assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'run_inputs'").fetchone() is None
    assert "calculation_version" not in {r[1] for r in connection.execute("PRAGMA table_info(audit_runs)")}
    connection.close()
    monkeypatch.undo()

    assert HistoryStore(path).run_inputs(1) == []


def test_history_without_corrections_digests_as_before():
    record = {"a": 1, "carrier": None, "currency": None, "applied_corrections": None}

    assert audit_history.digest(record) == audit_history.digest({"a": 1})
    assert audit_history.digest({**record, "applied_corrections": [3]}) != audit_history.digest(record)


# ---- integrity ---------------------------------------------------------------------------------------------------------------------------------

def test_the_integrity_check_finds_a_self_approval_and_a_changed_decision(rig):
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")
    approve(rig, correction["id"])
    store = store_of(rig)

    assert store.verify_history()["ok"] and store.verify_history()["decisions_checked"] == 1

    connection = sqlite3.connect(store.path)
    connection.execute("DROP TRIGGER correction_decisions_no_update")
    connection.execute("UPDATE correction_decisions SET note = 'edited'")
    connection.commit()

    assert any("does not match its hash" in p for p in store.verify_history()["problems"])

    # A decision by the proposer, written with correct hashes, is still reported
    row = connection.execute("SELECT * FROM correction_decisions").fetchone()
    digest = store_module.decision_hash(row[7], row[1], row[2], row[3], "bob", row[5], row[6])
    connection.execute("UPDATE correction_decisions SET actor = 'bob', hash = ?", (digest,))
    connection.commit()
    connection.close()

    assert any("decided by its own proposer" in p for p in store.verify_history()["problems"])


def test_a_decision_cannot_be_changed_or_deleted_by_ordinary_sql(rig):
    correction = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")
    approve(rig, correction["id"])
    connection = sqlite3.connect(rig.locations.extraction_path())

    for statement in ("UPDATE correction_decisions SET decision = 'rejected'", "DELETE FROM correction_decisions",
                      "INSERT INTO correction_decisions (correction_id, decision, actor, authenticated, at, prev_hash, hash) "
                      "VALUES (1, 'rejected', 'x', 1, 't', '', 'h')"):
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)

    connection.close()


# ---- the assistant and its citations ----------------------------------------------------------------------------------------------------------

def ask(rig, question, shipment):
    return Assistant(rig.state.service, BOB).ask(question, shipment)


def test_the_assistant_says_when_a_result_rests_on_an_approved_correction(rig):
    plain = ask(rig, "Do the BOL and POD piece counts match?", A)

    assert "approved correction" not in json.dumps(plain["uncertainty"]) and plain["status"] == "answered"
    assert all(c["value_origin"] == "legacy_reader" and isinstance(c["page"], int) for c in plain["citations"])

    approve(rig, propose(rig, extract(rig, POD_A), "delivered_pieces", "48")["id"])
    reply = ask(rig, "Do the BOL and POD piece counts match?", A)

    assert reply["status"] == "answered" and any("reviewer-approved correction" in u and "delivered pieces" in u for u in reply["uncertainty"])

    corrected = [c for c in reply["citations"] if c["value_origin"] == "approved_correction"]

    assert corrected and corrected[0]["source"] == POD_A and corrected[0]["page"] is None and corrected[0]["shipment_id"] == A
    assert all(c["shipment_id"] == A for c in reply["citations"])
    assert [c for c in reply["citations"] if c["value_origin"] == "legacy_reader"][0]["source"] == BOL_A


def test_other_shipments_answers_and_citations_are_unchanged(rig):
    before = ask(rig, "Do the BOL and POD piece counts match?", B)
    approve(rig, propose(rig, extract(rig, POD_A), "delivered_pieces", "48")["id"])
    after = ask(rig, "Do the BOL and POD piece counts match?", B)

    assert before == after and after["uncertainty"] == [] and all(c["shipment_id"] == B for c in after["citations"])


def test_pending_and_rejected_corrections_never_appear_in_answers_or_citations(rig):
    pod_id = extract(rig, POD_A)
    before = ask(rig, "Do the BOL and POD piece counts match?", A)
    pending = propose(rig, pod_id, "delivered_pieces", "48")

    assert ask(rig, "Do the BOL and POD piece counts match?", A) == before

    rig.state.service.decide_correction(ALICE, pending["id"], "reject", "The paper POD says 45 pieces.")

    assert ask(rig, "Do the BOL and POD piece counts match?", A) == before


def test_the_extraction_view_keeps_the_four_values_apart(rig):
    pod_id = extract(rig, POD_A)
    approved = propose(rig, pod_id, "delivered_pieces", "47")
    approve(rig, approved["id"])
    pending = propose(rig, pod_id, "delivered_pieces", "48", based_on=approved["id"])
    detail = rig.client("bob").get(f"/api/extractions/{pod_id}").json()
    field = detail["effective"]["delivered_pieces"]

    assert field["extracted"]["value"] == 45 and field["approved"]["corrected"] == 47 and field["proposed"]["id"] == pending["id"]
    assert field["value"] == 47 and field["source"] == "approved_correction" and field["page"] is None
    assert detail["payload"]["fields"]["delivered_pieces"]["value"] == 45
    assert any("only an approved correction reaches an audit" in text for text in detail["limitations"])
    assert [c["status"] for c in detail["corrections"]] == ["approved", "pending"]
