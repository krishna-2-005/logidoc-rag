"""
Invoice line-item corrections, their two-person approval, and the controlled
re-audit that records their effect (docs/CORRECTION_AWARE_AUDIT.md).

Synthetic PDFs (SHJ-2026-101: base freight $1,400, fuel $200 against a contracted
$180, detention $75, total $1,675) in a temporary folder, through the real API;
no network, no model.

Run from the project root:
    python -m pytest tests/test_line_item_corrections.py
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_extraction as de  # noqa: E402
import freight_audit as fa  # noqa: E402
import line_item_corrections  # noqa: E402
from audit_history import HistoryStore  # noqa: E402
from auth import UserStore  # noqa: E402
from authz import ADMIN  # noqa: E402
from test_api import PASSWORD, Rig  # noqa: E402
from test_freight_audit import SHIPMENT, charge, invoice, write_contracted_shipment  # noqa: E402

INVOICE_FILE = "INVOICE_SHJ-2026-101.pdf"
KEY = "SHJ2026101"
REASON = "The carrier's credit note CN-17 reduces the fuel surcharge to the contracted amount."


@pytest.fixture
def rig(tmp_path):

    result = Rig(tmp_path, "required")
    UserStore(tmp_path / "auth.sqlite3").create_user("dave", PASSWORD, ADMIN)
    write_contracted_shipment(result.raw)
    result.state.service.invalidate()

    return result


def extract(rig, name=INVOICE_FILE):

    reply = rig.client("alice").post("/api/extractions/run", json={"document": name})
    assert reply.status_code == 200, reply.text

    return reply.json()["id"]


def fuel_line(rig, extraction_id):

    detail = rig.client("alice").get(f"/api/extractions/{extraction_id}").json()

    return next(item for item in detail["line_items"] if item["extracted"]["description"] == "Fuel Surcharge")


def propose(rig, user, extraction_id, index, amount="180.00", based_on=None, reason=REASON):

    return rig.client(user).post(f"/api/extractions/{extraction_id}/line-items/{index}/corrections",
                                 json={"amount": amount, "reason": reason, "based_on": based_on})


def correct_fuel(rig):
    """bob proposes the invoice's fuel surcharge at $180, dave approves it."""

    extraction_id = extract(rig)
    index = fuel_line(rig, extraction_id)["index"]
    proposal = propose(rig, "bob", extraction_id, index)
    assert proposal.status_code == 200, proposal.text
    decision = rig.client("dave").post(f"/api/extractions/corrections/{proposal.json()['id']}/decision",
                                       json={"decision": "approve", "note": "Credit note checked."})
    assert decision.status_code == 200, decision.text

    return extraction_id, index, proposal.json(), decision.json()


def freight(rig):

    return rig.client("alice").get(f"/api/shipments/{SHIPMENT}/freight-audit").json()["freight_audit"]


def runs(rig):

    return HistoryStore(rig.locations.history_db).runs(1000) if rig.locations.history_db.is_file() else []


# ============================================================
# THE PROPOSAL AND ITS APPROVAL
# ============================================================

def test_a_line_item_correction_changes_nothing_until_another_administrator_approves_it(rig):

    extraction_id = extract(rig)
    line = fuel_line(rig, extraction_id)
    assert line["extracted"]["amount"] == 200.0 and line["source"] == "extracted" and line["approved"] is None

    proposal = propose(rig, "bob", extraction_id, line["index"])
    assert proposal.status_code == 200, proposal.text
    body = proposal.json()
    assert body["field_name"] == f"line_item:{line['index']}" and body["status"] == "pending"
    assert body["previous"] == {"value": {"description": "Fuel Surcharge", "amount": 200.0}, "state": "EXTRACTED", "source": "extracted"}
    assert body["corrected"] == {"description": "Fuel Surcharge", "amount": 180.0,
                                 "of": {"description": "Fuel Surcharge", "amount": 200.0}}

    # Pending: the audit still reads the invoice as printed
    assert charge(freight(rig), "Fuel surcharge")["outcome"] == fa.DISCREPANCY
    assert fuel_line(rig, extraction_id)["proposed"]["id"] == body["id"]

    # The reviewer cannot decide; an administrator cannot decide their own proposal
    assert rig.client("bob").post(f"/api/extractions/corrections/{body['id']}/decision",
                                  json={"decision": "approve"}).status_code == 403
    own = propose(rig, "alice", extract(rig, "INVOICE_SHJ-2026-001.pdf"), 0, amount="1.00")
    assert own.status_code == 200, own.text
    refused = rig.client("alice").post(f"/api/extractions/corrections/{own.json()['id']}/decision", json={"decision": "approve"})
    assert refused.status_code == 403 and "own correction" in refused.json()["detail"]

    decision = rig.client("dave").post(f"/api/extractions/corrections/{body['id']}/decision", json={"decision": "approve"})
    assert decision.status_code == 200, decision.text
    # The approval records no run: the controlled re-audit does
    assert decision.json()["recompute"] == {"status": "re_audit_required", "shipments": [KEY]}


def test_an_approved_amount_is_what_both_audits_read_and_the_document_is_unchanged(rig):

    extraction_id, index, proposal, _ = correct_fuel(rig)
    after = freight(rig)

    fuel = charge(after, "Fuel surcharge")
    assert (fuel["expected"], fuel["billed"], fuel["outcome"]) == (180.0, 180.0, fa.MATCHED)
    assert fuel["billed_source"]["corrected"] is True
    applied = [item for item in after["corrections_applied"] if item["field"] == f"line_item:{index}"]
    assert applied == [{
        "correction_id": proposal["id"], "file": INVOICE_FILE, "document_type": "INVOICE", "field": f"line_item:{index}",
        "original_state": "EXTRACTED", "original_value": {"description": "Fuel Surcharge", "amount": 200.0},
        "corrected_value": {"description": "Fuel Surcharge", "amount": 180.0}, "proposed_by": "bob", "approved_by": "dave",
        "decided_at": applied[0]["decided_at"],
    }]
    # The charges now add up to $1,655 against the stated $1,675: the arithmetic says so
    assert charge(after, "Line items against the invoice total")["outcome"] == fa.DISCREPANCY
    invoice_input = next(item for item in after["inputs"] if item["file"] == INVOICE_FILE)
    assert [item["corrected"] for item in invoice_input["line_items"]].count(True) == 1

    # The reconciliation's invoice audit reads the corrected charge too, and says where it came from
    inputs = rig.state.service.shipment(SHIPMENT)["inputs"]
    entry = next(item for item in inputs["fields"] if item["field"] == "fuel_surcharge")
    assert (entry["source"], entry["value"], entry["correction_id"]) == ("approved_correction", 180.0, proposal["id"])
    assert proposal["id"] in inputs["applied_correction_ids"]

    # The extraction keeps what was read; the line view shows both
    detail = rig.client("alice").get(f"/api/extractions/{extraction_id}").json()
    assert detail["payload"]["line_items"][index]["amount"] == 200.0
    line = detail["line_items"][index]
    assert (line["amount"], line["source"], line["applies"], line["correction_id"]) == (180.0, "approved_correction", True, proposal["id"])

    # The hash chain covers line-item corrections like any other
    integrity = rig.client("alice").get("/api/extractions/integrity").json()
    assert integrity["ok"] and integrity["corrections_checked"] >= 1


def test_a_rejected_line_item_correction_has_no_effect(rig):

    extraction_id = extract(rig)
    index = fuel_line(rig, extraction_id)["index"]
    proposal = propose(rig, "bob", extraction_id, index).json()
    reply = rig.client("dave").post(f"/api/extractions/corrections/{proposal['id']}/decision",
                                    json={"decision": "reject", "note": "The credit note was not issued."})

    assert reply.status_code == 200 and reply.json()["recompute"] == {"status": "not_needed"}
    assert charge(freight(rig), "Fuel surcharge")["outcome"] == fa.DISCREPANCY
    assert fuel_line(rig, extraction_id)["source"] == "extracted"


@pytest.mark.parametrize("change, status, words", [
    ({"amount": "abc"}, 422, "not a money amount"),
    ({"amount": "200.00"}, 422, "same as the current amount"),
    ({"reason": "short"}, 422, "at least 10"),
    ({"based_on": 99}, 409, "Reload it"),
])
def test_an_invalid_or_stale_line_item_proposal_is_refused(rig, change, status, words):

    extraction_id = extract(rig)
    index = fuel_line(rig, extraction_id)["index"]
    body = {"amount": "180.00", "reason": REASON, "based_on": None, **change}
    reply = rig.client("bob").post(f"/api/extractions/{extraction_id}/line-items/{index}/corrections", json=body)

    assert reply.status_code == status and words in reply.json()["detail"]


def test_a_second_proposal_waits_for_the_first_and_only_invoices_have_lines(rig):

    extraction_id = extract(rig)
    index = fuel_line(rig, extraction_id)["index"]
    first = propose(rig, "bob", extraction_id, index).json()

    waiting = propose(rig, "carol", extraction_id, index, amount="190.00", based_on=first["id"])
    assert waiting.status_code == 422 and "awaiting a decision" in waiting.json()["detail"]
    assert propose(rig, "bob", extraction_id, 40).status_code == 422
    assert rig.client("bob").post(f"/api/extractions/{extraction_id}/line-items/1000/corrections",
                                  json={"amount": "1", "reason": REASON, "based_on": None}).status_code == 422

    bol = extract(rig, "BOL_SHJ-2026-101.pdf")
    refused = propose(rig, "bob", bol, 0)
    assert refused.status_code == 422 and "invoice" in refused.json()["detail"]
    assert rig.client("alice").get(f"/api/extractions/{bol}").json()["line_items"] == []
    assert rig.client().post(f"/api/extractions/{extraction_id}/line-items/{index}/corrections",
                             json={"amount": "1", "reason": REASON, "based_on": None}).status_code == 401


def test_a_correction_is_only_applied_to_the_line_it_was_made_on():

    document = invoice(items=(("Base Freight", 1400.0), ("Fuel Surcharge", 200.0)), total=1600.0)
    correction = {"correction_id": 3, "decision_id": 4, "filename": document.filename, "sha256": document.sha256,
                  "field_name": "line_item:1", "approved_by": "dave",
                  "value": {"description": "Fuel Surcharge", "amount": 180.0, "of": {"description": "Fuel Surcharge", "amount": 200.0}}}

    corrected, applied, ignored = fa.apply_corrections([document], [correction])
    assert corrected[0].line_items[1].amount == 180.0 and document.line_items[1].amount == 200.0 and ignored == []
    assert applied[0]["corrected_value"] == {"description": "Fuel Surcharge", "amount": 180.0}

    # Another extractor version read the lines in another order: never moved onto another line
    elsewhere = {**correction, "field_name": "line_item:0"}
    _, applied, ignored = fa.apply_corrections([document], [elsewhere])
    assert applied == [] and "no longer reads as it did" in ignored[0]["reason"]

    beyond = {**correction, "field_name": "line_item:5"}
    _, applied, ignored = fa.apply_corrections([document], [beyond])
    assert applied == [] and ignored[0]["field"] == "line_item:5"


def test_line_item_helpers_never_touch_the_extractor_version():

    # Correcting a charge does not change how a document is read: the module is not part of the version
    assert "line_item_corrections.py" not in de.FINGERPRINT_SOURCES
    assert line_item_corrections.index_of("line_item:12") == 12
    assert line_item_corrections.index_of("invoice_total") is None and line_item_corrections.index_of("line_item:x") is None
    assert fa.consumes("INVOICE", "line_item:0") and not fa.consumes("BOL", "line_item:0")


# ============================================================
# THE CONTROLLED RE-AUDIT
# ============================================================

def record_first_run(rig):

    reply = rig.client("alice").post("/api/history/runs", json={"label": "Before the credit note"})
    assert reply.status_code == 201, reply.text

    return reply.json()["run_id"]


def test_the_controlled_re_audit_records_exactly_what_was_previewed(rig):

    first = record_first_run(rig)
    _, index, proposal, decision = correct_fuel(rig)
    alice = rig.client("alice")

    # Nothing was recorded by the approval, and the plain recompute leaves it to the controlled re-audit
    assert [run["id"] for run in runs(rig)] == [first]
    stale = alice.get("/api/extractions/staleness").json()["stale"]
    assert stale[0]["controlled"] is True and stale[0]["controlled_correction_ids"] == [proposal["id"]]
    recompute = alice.post("/api/extractions/recompute", json={"shipment_id": SHIPMENT}).json()
    assert recompute["recorded"] is False and recompute["re_audit_required"] == [KEY]
    queue = alice.get("/api/re-audits").json()["re_audits"]
    assert queue == [{"shipment_id": SHIPMENT, "shipment_key": KEY, "recorded_run_id": first,
                      "line_item_correction_ids": [proposal["id"]], "tms_resolution_ids": []}]

    preview = alice.get(f"/api/shipments/{SHIPMENT}/re-audit").json()
    assert preview["needed"] is True and preview["recorded"]["run_id"] == first
    assert preview["corrections"]["added"] == [proposal["id"]] and preview["corrections"]["line_items"] == [proposal["id"]]
    assert preview["current"]["freight_outcome"] == fa.DISCREPANCY

    # Only an administrator who can record history, with a reason, and the previewed state
    assert rig.client("bob").post(f"/api/shipments/{SHIPMENT}/re-audit",
                                  json={"token": preview["token"], "reason": "Credit note applied."}).status_code == 403
    assert alice.post(f"/api/shipments/{SHIPMENT}/re-audit", json={"token": preview["token"], "reason": "short"}).status_code == 422
    assert alice.post(f"/api/shipments/{SHIPMENT}/re-audit", json={"token": "0" * 64, "reason": "Credit note applied."}).status_code == 409

    recorded = alice.post(f"/api/shipments/{SHIPMENT}/re-audit", json={"token": preview["token"], "reason": "Credit note CN-17 applied."})
    assert recorded.status_code == 201, recorded.text
    body = recorded.json()
    assert body["previous_run_id"] == first and body["corrections_added"] == [proposal["id"]] and body["snapshot_written"] is True

    history = HistoryStore(rig.locations.history_db)
    assert [run["id"] for run in history.runs(10)][0] == body["run_id"]
    assert history.runs(10)[0]["label"] == "Re-audit by alice: Credit note CN-17 applied."
    assert history.superseded_by(first, KEY) == body["run_id"]
    inputs = {item["field_name"]: item for item in history.run_inputs(body["run_id"], KEY)}
    assert inputs["fuel_surcharge"]["source"] == "approved_correction" and inputs["fuel_surcharge"]["value"] == 180.0
    freight_record = history.latest_recorded(KEY)["snapshot"]["freight_audit"]
    assert [item["field"] for item in freight_record["corrections_applied"]] == [f"line_item:{index}"]

    # Done: nothing left to re-audit, and doing it again is refused
    assert alice.get(f"/api/shipments/{SHIPMENT}/re-audit").json()["needed"] is False
    again = alice.post(f"/api/shipments/{SHIPMENT}/re-audit", json={"token": alice.get(f"/api/shipments/{SHIPMENT}/re-audit").json()["token"],
                                                                     "reason": "Credit note CN-17 applied."})
    assert again.status_code == 409 and "Nothing to re-audit" in again.json()["detail"]
    assert alice.get("/api/re-audits").json()["re_audits"] == [] and alice.get("/api/extractions/staleness").json()["stale"] == []


def test_two_administrators_confirming_the_same_preview_record_it_once(rig):

    record_first_run(rig)
    correct_fuel(rig)
    token = rig.client("alice").get(f"/api/shipments/{SHIPMENT}/re-audit").json()["token"]

    first = rig.client("alice").post(f"/api/shipments/{SHIPMENT}/re-audit", json={"token": token, "reason": "Credit note applied."})
    second = rig.client("dave").post(f"/api/shipments/{SHIPMENT}/re-audit", json={"token": token, "reason": "Credit note applied."})

    assert first.status_code == 201 and second.status_code == 409
    assert len(runs(rig)) == 2


def test_a_run_recorded_meanwhile_stops_the_re_audit_inside_the_transaction(rig):

    record_first_run(rig)
    service = rig.state.service
    snapshot = service.snapshot()
    history = HistoryStore(rig.locations.history_db)
    latest = history.latest_recorded(KEY)["run_id"]

    # Another process records a run for the shipment between the preview and the write
    history.record_run(snapshot, [], label="Meanwhile", shipment_keys=[KEY])
    outcome = history.record_run(snapshot, [], label="Re-audit", shipment_keys=[KEY], expect_latest={KEY: latest})

    assert outcome["run_id"] is None and outcome["conflict"] is True
    assert [run["label"] for run in history.runs(10)] == ["Meanwhile", "Before the credit note"]


def test_a_preview_of_a_shipment_never_recorded_and_an_unknown_one(rig):

    alice = rig.client("alice")
    preview = alice.get(f"/api/shipments/{SHIPMENT}/re-audit").json()

    assert preview["recorded"] is None and preview["needed"] is True
    assert alice.get("/api/shipments/NOPE-1/re-audit").status_code == 404
    assert rig.client().get(f"/api/shipments/{SHIPMENT}/re-audit").status_code == 401
    # Viewing a preview records nothing and creates no history file
    assert not rig.locations.history_db.is_file()


def test_line_item_corrections_are_recorded_as_append_only_rows(rig):

    extraction_id, index, proposal, _ = correct_fuel(rig)

    with closing(sqlite3.connect(rig.locations.extraction_path())) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE field_corrections SET corrected = '{}' WHERE id = ?", (proposal["id"],))
