"""
Authorization of the freight operations workflow, checked at the API (the
backend), not by what the screen shows: who may dispute, waive, approve a
waiver or a correction, and record audit history; that history and decisions
cannot be edited; and that impossible transitions are refused.

Synthetic PDFs in a temporary folder; no network, no model.

Run from the project root:
    python -m pytest tests/test_operations_authorization.py
"""

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from auth import UserStore
from authz import ADMIN
from review_store import Policy
from test_api import PASSWORD, Rig
from test_freight_audit import write_contracted_shipment

FUEL_KEY = "SHJ2026101|freight-fuel-surcharge"
ACTIONS = f"/api/exceptions/{FUEL_KEY}/actions"
RATE_FILE = "RATE_CONFIRMATION_SHJ-2026-101.pdf"


def make_rig(tmp_path, **kwargs):

    rig = Rig(tmp_path, "required", **kwargs)
    UserStore(tmp_path / "auth.sqlite3").create_user("dave", PASSWORD, ADMIN)
    write_contracted_shipment(rig.raw)
    rig.state.service.invalidate()

    # Reading the queue records the detected exceptions as open
    assert rig.client("alice").get("/api/exceptions").status_code == 200

    return rig


@pytest.fixture
def rig(tmp_path):

    return make_rig(tmp_path)


def act(rig, user, action, note=None, evidence=()):

    return rig.client(user).post(ACTIONS, json={"action": action, "note": note, "evidence": list(evidence)})


def status_of(rig):

    return rig.client("alice").get(f"/api/exceptions/{FUEL_KEY}").json()["review"]["status"]


# ============================================================
# SIGNED-OUT USERS
# ============================================================

@pytest.mark.parametrize("action", ["dispute", "waive", "resolve", "approve"])
def test_a_signed_out_user_can_take_no_action(rig, action):

    reply = act(rig, None, action, "A reason long enough for the rule.")

    assert reply.status_code == 401
    assert status_of(rig) == "open"


# ============================================================
# WAIVING: ADMINISTRATORS ONLY, ENFORCED BY THE BACKEND
# ============================================================

def test_a_reviewer_cannot_waive_even_by_calling_the_api_directly(rig):

    # The screen would not offer it ...
    assert "waive" not in rig.client("bob").get(f"/api/exceptions/{FUEL_KEY}").json()["allowed_actions"]

    # ... and the backend refuses it anyway, from every state it could be waived from
    assert act(rig, "bob", "waive", "Accept the difference as it is.").status_code == 403
    assert act(rig, "bob", "dispute", "Fuel billed above the contract.").status_code == 200
    assert act(rig, "bob", "waive", "Accept the difference as it is.").status_code == 403
    assert status_of(rig) == "disputed"


def test_a_reviewer_cannot_dismiss_either(rig):

    assert act(rig, "bob", "dismiss", "Not a real difference at all.").status_code == 403


# ============================================================
# SECOND-ADMINISTRATOR APPROVAL
# ============================================================

def test_a_waiver_needs_a_second_administrator_when_approval_is_on(tmp_path):

    rig = make_rig(tmp_path, policy=Policy(require_approval=True))

    waived = act(rig, "alice", "waive", "Accepted below the write-off threshold agreed with finance.")
    assert waived.status_code == 200 and waived.json()["review"]["approval_state"] == "pending"

    # Not by the administrator who waived it, not by a reviewer
    assert act(rig, "alice", "approve").status_code == 422
    assert act(rig, "bob", "approve").status_code == 403

    approved = act(rig, "dave", "approve", "Checked with finance.")
    assert approved.status_code == 200 and approved.json()["review"]["approval_state"] == "approved"

    history = rig.client("alice").get(f"/api/exceptions/{FUEL_KEY}/history").json()
    assert [(event["event_type"], event["actor"]) for event in history] == [
        ("detected", "system"), ("waive", "alice"), ("approve", "dave")]


def test_a_rejected_waiver_goes_back_to_review(tmp_path):

    rig = make_rig(tmp_path, policy=Policy(require_approval=True))

    act(rig, "alice", "waive", "Accepted below the write-off threshold agreed with finance.")
    rejected = act(rig, "dave", "reject", "Over the threshold: dispute it instead.")

    assert rejected.status_code == 200
    assert rejected.json()["review"]["status"] == "in_review"
    assert rejected.json()["review"]["approval_state"] == "rejected"


# ============================================================
# CORRECTIONS: TWO PEOPLE
# ============================================================

def proposal(rig, user="bob"):

    extraction_id = rig.client("alice").post("/api/extractions/run", json={"document": RATE_FILE}).json()["id"]
    reply = rig.client(user).post(f"/api/extractions/{extraction_id}/corrections",
                                  json={"field": "fuel_surcharge", "value": "200.00", "reason": "Amended rate confirmation.",
                                        "based_on": None})
    assert reply.status_code == 200, reply.text

    return reply.json()["id"]


def decide(rig, user, correction_id):

    return rig.client(user).post(f"/api/extractions/corrections/{correction_id}/decision",
                                 json={"decision": "approve", "note": "Checked against the amended rate confirmation."})


def test_a_reviewer_cannot_approve_a_correction(rig):

    correction_id = proposal(rig)

    assert decide(rig, "bob", correction_id).status_code == 403
    assert decide(rig, "carol", correction_id).status_code == 403


def test_an_administrator_cannot_approve_their_own_proposal(rig):

    correction_id = proposal(rig, user="alice")

    refused = decide(rig, "alice", correction_id)
    assert refused.status_code == 403 and "another administrator must" in refused.json()["detail"]
    assert decide(rig, "dave", correction_id).status_code == 200


def test_an_unapproved_correction_does_not_reach_the_freight_audit(rig):

    proposal(rig)

    freight = rig.client("alice").get("/api/shipments/SHJ-2026-101").json()["freight_audit"]

    assert freight["corrections_applied"] == []
    assert next(item for item in freight["charges"] if item["charge"] == "Fuel surcharge")["expected"] == 180.0


# ============================================================
# AUDIT HISTORY AND DECISIONS CANNOT BE CHANGED
# ============================================================

def test_a_reviewer_cannot_record_or_change_audit_history(rig):

    assert rig.client("bob").post("/api/history/runs", json={"label": "Mine"}).status_code == 403
    assert not rig.locations.history_db.exists()


def test_no_api_route_edits_history_or_decisions():

    import api.routes_audit
    import api.routes_extraction
    import api.routes_workflow

    writes = sorted(
        (method, route.path)
        for module in (api.routes_audit, api.routes_workflow, api.routes_extraction)
        for route in module.router.routes
        for method in getattr(route, "methods", set()) - {"GET", "HEAD", "OPTIONS"}
        if "history" in route.path or "events" in route.path
    )

    # The only write is recording a new run; nothing updates or deletes a recorded one
    assert writes == [("POST", "/api/history/runs")]


def test_recorded_history_and_decisions_are_append_only(rig):

    assert act(rig, "bob", "dispute", "Fuel billed above the contract.").status_code == 200
    assert rig.client("alice").post("/api/history/runs", json={"label": "First"}).status_code == 201

    with sqlite3.connect(rig.locations.history_db) as connection:
        for table in ("shipment_snapshots", "snapshot_findings", "freight_audit_records", "audit_runs"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                connection.execute(f"DELETE FROM {table}")

    with sqlite3.connect(rig.locations.review_db) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE review_events SET note = 'changed'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM review_events")



# ============================================================
# IMPOSSIBLE TRANSITIONS
# ============================================================

def test_impossible_transitions_are_refused_and_change_nothing(rig):

    # Resolving needs a review first
    assert act(rig, "bob", "resolve", "Credit memo received.", ["CM-1"]).status_code == 422
    assert status_of(rig) == "open"

    assert act(rig, "alice", "waive", "Accepted below the write-off threshold.").status_code == 200

    for user, action in (("bob", "dispute"), ("alice", "waive"), ("alice", "dismiss"), ("bob", "start")):
        assert act(rig, user, action, "Trying again on a waived exception.").status_code == 422, action

    assert status_of(rig) == "waived"

    # A reason is never optional
    assert act(rig, "alice", "reopen", "Reopened after the carrier's new invoice.").status_code == 200
    assert act(rig, "bob", "dispute", "").status_code == 422
    assert act(rig, "alice", "waive", "   ").status_code == 422
    assert status_of(rig) == "open"
