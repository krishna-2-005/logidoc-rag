"""
TMS shipments in the audit: matching by reference, field-by-field comparison with
provenance, conflict resolution, and the protection of approved corrections
(docs/TMS_SHIPMENT_AUDIT.md).

The sandbox TMS on synthetic data, with one extra synthetic record whose
reference names the synthetic shipment SHJ-2026-101 (BOL: 48 pieces, delivered
October 3, 2026), in temporary folders; no network, no real data.

Run from the project root:
    python -m pytest tests/test_tms_audit.py
"""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import integration_store  # noqa: E402
import sandbox_tms  # noqa: E402
import tms_audit  # noqa: E402
from audit_history import HistoryStore  # noqa: E402
from integration_support import integration_rig  # noqa: E402
from test_freight_audit import SHIPMENT  # noqa: E402

KEY = "SHJ2026101"
RECORD = "SBX-SHP-9101"
REASON = "The TMS booking was amended to 50 pieces by the shipper on 30 Sept."


@pytest.fixture
def rig(tmp_path):

    return integration_rig(tmp_path)


def tms_item(rig, number=RECORD, reference=SHIPMENT, updated="2026-10-02T09:00:00Z", **values):
    """Puts one synthetic shipment into the sandbox TMS (what its partner system would hold)."""

    system = sandbox_tms.SandboxTMS(rig.sandbox)
    item = {"id": number, "reference": reference, "status": "delivered", "carrier_id": "SBX-CAR-001",
            "carrier_name": "Swift Lines", "origin": "Chicago, Illinois", "destination": "Atlanta, Georgia",
            "pickup_date": "2026-10-01", "delivery_date": "2026-10-03", "pieces": 50, "weight_kg": 900.0,
            "currency": "USD", "freight_charge": 1400.0, "updated_at": updated, **values}

    with closing(system.connect()) as connection, connection:
        connection.execute("INSERT OR REPLACE INTO items (entity, id, payload, updated_at) VALUES ('shipments', ?, ?, ?)",
                           (number, json.dumps(item), updated))


def connect(rig):

    reply = rig.client("alice").post("/api/integrations/connections", json={
        "name": "Sandbox TMS", "provider": "sandbox_tms", "settings": {"page_size": 50}})
    assert reply.status_code == 201, reply.text

    return reply.json()["id"]


def sync(rig, integration_id):

    services = rig.state.integrations.integrations
    run = services.execute(integration_id, services.claim(integration_id, "manual", "alice"))
    assert run["status"] in ("succeeded", "partial"), run

    return run


def linked(rig, **values):
    """The sandbox integration, synced with the record naming SHJ-2026-101."""

    tms_item(rig, **values)
    integration_id = connect(rig)
    sync(rig, integration_id)

    return integration_id


def view(rig, user="alice"):

    reply = rig.client(user).get(f"/api/shipments/{SHIPMENT}/tms")
    assert reply.status_code == 200, reply.text

    return reply.json()


def row(found, name):

    return next(item for item in found["comparisons"] if item["field"] == name)


def resolve(rig, name, choice, tms_value, document_value, user="alice", reason=REASON, record_id=None):

    record_id = record_id or view(rig)["match"]["record_id"]

    return rig.client(user).post(f"/api/shipments/{SHIPMENT}/tms/resolutions", json={
        "field": name, "choice": choice, "reason": reason, "record_id": record_id,
        "tms_value": tms_value, "document_value": document_value})


def quantity(rig):

    return rig.state.service.shipment(SHIPMENT)["result"]["quantity"]


def inputs_of(rig, name):

    return next(item for item in rig.state.service.shipment(SHIPMENT)["inputs"]["fields"] if item["field"] == name)


# ============================================================
# MATCHING AND COMPARING
# ============================================================

def test_the_tms_record_is_matched_by_reference_and_compared_field_by_field(rig):

    integration_id = linked(rig)
    found = view(rig, user="bob")                                   # reviewers read it

    match = found["match"]
    assert (match["external_id"], match["reference"], match["integration_name"]) == (RECORD, SHIPMENT, "Sandbox TMS")
    assert match["integration_id"] == integration_id and match["source_updated_at"].startswith("2026-10-02T09:00")
    assert match["last_run_id"] >= 1 and found["configured"] is True and found["ambiguous"] is False

    pieces = row(found, "expected_pieces")
    assert (pieces["tms_value"], pieces["document_value"], pieces["state"], pieces["open"]) == (50, 48, "conflict", True)
    assert pieces["choices"] == ["document", "tms"] and pieces["consumed"] is True
    for name, value in (("actual_date", "2026-10-03"), ("pickup_date", "2026-10-01"), ("carrier", "swift lines"),
                        ("origin", "chicago, illinois"), ("destination", "atlanta, georgia"), ("agreed_rate", 1400.0)):
        assert (row(found, name)["state"], row(found, name)["tms_value"]) == ("agrees", value), name
    assert found["open_conflicts"] == 1

    # An open conflict changes no result: the audit reads the BOL, and says a TMS value differs
    assert quantity(rig)["bol"] == 48
    entry = inputs_of(rig, "expected_pieces")
    assert entry["source"] == "legacy_reader" and "tms_conflict" in entry["warnings"] and entry["validation"] == "warning"
    assert (entry["tms"]["external_id"], entry["tms"]["value"], entry["tms"]["state"]) == (RECORD, 50, "conflict")

    queue = rig.client("bob").get("/api/tms/conflicts").json()
    assert [(item["shipment_id"], item["field"], item["external_id"]) for item in queue["conflicts"]] == [(SHIPMENT, "expected_pieces", RECORD)]


def test_choosing_the_tms_value_changes_the_audit_and_a_controlled_re_audit_records_it(rig):

    linked(rig)
    alice = rig.client("alice")
    first = alice.post("/api/history/runs", json={"label": "Before the TMS decision"}).json()["run_id"]

    # Only an administrator decides
    assert resolve(rig, "expected_pieces", "tms", 50, 48, user="bob").status_code == 403

    reply = resolve(rig, "expected_pieces", "tms", 50, 48)
    assert reply.status_code == 201, reply.text
    body = reply.json()
    assert body["re_audit"] == "required" and body["resolution"]["choice"] == "tms" and body["resolution"]["actor"] == "alice"
    assert body["resolution"]["external_id"] == RECORD and body["resolution"]["tms_value"] == 50

    # The audit now expects 50 pieces from the TMS; the POD delivered 48
    assert (quantity(rig)["bol"], quantity(rig)["pod"]) == (50, 48)
    entry = inputs_of(rig, "expected_pieces")
    assert (entry["source"], entry["value"], entry["document_reading"]["value"]) == ("tms_resolution", 50, 48)
    assert entry["tms"]["resolution_id"] == body["resolution"]["id"]
    assert row(view(rig), "expected_pieces")["state"] == "resolved_tms" and view(rig)["open_conflicts"] == 0

    # Recorded only by the controlled re-audit, with the TMS provenance beside the value
    queue = alice.get("/api/re-audits").json()["re_audits"]
    assert queue[0]["tms_resolution_ids"] == [body["resolution"]["id"]] and queue[0]["recorded_run_id"] == first
    preview = alice.get(f"/api/shipments/{SHIPMENT}/re-audit").json()
    assert preview["needed"] and preview["tms_resolutions"] == [body["resolution"]["id"]]
    assert {"field": "bol_pieces", "label": "BOL pieces", "previous": 48, "current": 50} in preview["value_changes"]

    recorded = alice.post(f"/api/shipments/{SHIPMENT}/re-audit", json={"token": preview["token"], "reason": "TMS booking of 50 pieces accepted."})
    assert recorded.status_code == 201, recorded.text
    inputs = {item["field_name"]: item for item in HistoryStore(rig.locations.history_db).run_inputs(recorded.json()["run_id"], KEY)}
    assert inputs["expected_pieces"]["source"] == "tms_resolution" and inputs["expected_pieces"]["value"] == 50
    assert inputs["expected_pieces"]["tms"]["resolution_id"] == body["resolution"]["id"]
    assert alice.get("/api/re-audits").json()["re_audits"] == []


def test_keeping_the_documents_value_closes_the_conflict_without_changing_the_audit(rig):

    linked(rig)
    reply = resolve(rig, "expected_pieces", "document", 50, 48)

    assert reply.status_code == 201 and reply.json()["re_audit"] == "not_needed"
    pieces = row(view(rig), "expected_pieces")
    assert pieces["state"] == "resolved_document" and pieces["open"] is False and pieces["resolution"]["actor"] == "alice"
    assert quantity(rig)["bol"] == 48 and "tms_conflict" not in inputs_of(rig, "expected_pieces")["warnings"]
    assert rig.client("alice").get("/api/tms/conflicts").json()["conflicts"] == []


def test_a_decision_on_values_that_changed_or_was_already_made_is_refused(rig):

    linked(rig)

    stale = resolve(rig, "expected_pieces", "tms", 51, 48)
    assert stale.status_code == 409 and "changed since" in stale.json()["detail"]
    assert resolve(rig, "expected_pieces", "document", 50, 48).status_code == 201
    twice = resolve(rig, "expected_pieces", "tms", 50, 48)
    assert twice.status_code == 409 and "Nothing to resolve" in twice.json()["detail"]

    agrees = resolve(rig, "carrier", "document", "swift lines", "swift lines")
    assert agrees.status_code == 409
    assert resolve(rig, "nothing", "document", 1, 1).status_code == 422
    assert resolve(rig, "expected_pieces", "maybe", 50, 48).status_code == 422
    assert resolve(rig, "expected_pieces", "tms", 50, 48, reason="short").status_code == 422


def test_a_cross_check_field_can_be_noted_but_its_tms_value_never_chosen(rig):

    linked(rig, carrier_name="Swift Lines Inc")
    carrier = row(view(rig), "carrier")
    assert carrier["state"] == "conflict" and carrier["choices"] == ["document"] and carrier["consumed"] is False

    refused = resolve(rig, "carrier", "tms", "swift lines inc", "swift lines")
    assert refused.status_code == 422 and "No calculation reads this field" in refused.json()["detail"]
    assert resolve(rig, "carrier", "document", "swift lines inc", "swift lines").status_code == 201


def test_a_sync_bringing_another_value_reopens_the_conflict(rig):

    integration_id = linked(rig)
    assert resolve(rig, "expected_pieces", "tms", 50, 48).status_code == 201
    assert quantity(rig)["bol"] == 50

    # The TMS changes the booking again: the decision was made on 50, not on 52
    tms_item(rig, pieces=52, updated="2026-10-04T09:00:00Z")
    sync(rig, integration_id)

    pieces = row(view(rig), "expected_pieces")
    assert (pieces["state"], pieces["tms_value"], pieces["resolution"]) == ("conflict", 52, None)
    assert quantity(rig)["bol"] == 48                                    # back to the documents until someone decides
    assert [item["tms_value"] for item in view(rig)["history"]] == [50]  # the old decision stays in the history


def test_an_approved_correction_protects_the_field_from_the_tms(rig):

    linked(rig)
    assert resolve(rig, "expected_pieces", "tms", 50, 48).status_code == 201

    # A reviewer's correction of the BOL, approved by another administrator, wins over the TMS value
    extraction_id = rig.client("alice").post("/api/extractions/run", json={"document": "BOL_SHJ-2026-101.pdf"}).json()["id"]
    proposal = rig.client("bob").post(f"/api/extractions/{extraction_id}/corrections", json={
        "field": "piece_count", "value": "49", "reason": "The BOL was reissued with 49 pieces.", "based_on": None}).json()
    assert rig.client("dave").post(f"/api/extractions/corrections/{proposal['id']}/decision",
                                   json={"decision": "approve"}).status_code == 200

    assert quantity(rig)["bol"] == 49 and inputs_of(rig, "expected_pieces")["source"] == "approved_correction"
    pieces = row(view(rig), "expected_pieces")
    assert pieces["state"] == "protected" and pieces["protected_by"] == proposal["id"] and pieces["open"] is False
    refused = resolve(rig, "expected_pieces", "tms", 50, 48)
    assert refused.status_code == 409 and "approved correction is in force" in refused.json()["detail"]


def test_a_tms_value_corrected_by_hand_in_logidoc_cannot_be_chosen(rig):

    integration_id = linked(rig)
    record_id = view(rig)["match"]["record_id"]
    override = rig.client("alice").post(f"/api/integrations/connections/{integration_id}/records/{record_id}/corrections",
                                        json={"field": "pieces", "value": 47, "reason": "Typed from the phone call."})
    assert override.status_code == 200, override.text

    pieces = row(view(rig), "expected_pieces")
    assert (pieces["tms_value"], pieces["tms_corrected_by_hand"], pieces["choices"]) == (47, True, ["document"])
    refused = resolve(rig, "expected_pieces", "tms", 47, 48)
    assert refused.status_code == 409 and "corrected by hand" in refused.json()["detail"]
    assert quantity(rig)["bol"] == 48


def test_a_change_in_the_same_second_still_refreshes_the_audit(rig):
    """Change times have whole seconds: the audit's cache key follows the record versions, not the clock."""

    integration_id = linked(rig)
    assert row(view(rig), "expected_pieces")["tms_value"] == 50
    source = rig.state.service.tms_source
    before = source.token()
    services = rig.state.integrations.integrations
    store = services.records_store(services.integration(integration_id), create=False)
    record = store.record(view(rig)["match"]["record_id"])

    store.override("alice", record["id"], "pieces", 47, "Typed from the phone call.", record["updated_at"])

    assert store.record(record["id"])["updated_at"] == record["updated_at"]       # the same second
    assert source.token() != before
    assert row(view(rig), "expected_pieces")["tms_value"] == 47


def test_two_tms_records_naming_one_shipment_are_not_compared(rig):

    tms_item(rig, number="SBX-SHP-9102", pieces=48)
    linked(rig)

    found = view(rig)
    assert found["ambiguous"] is True and found["match"] is None and found["comparisons"] == []
    assert sorted(item["external_id"] for item in found["matches"]) == ["SBX-SHP-9101", "SBX-SHP-9102"]
    refused = resolve(rig, "expected_pieces", "tms", 50, 48, record_id=found["matches"][0]["record_id"])
    assert refused.status_code == 409 and "Several TMS records" in refused.json()["detail"]
    assert rig.client("alice").get("/api/tms/conflicts").json()["conflicts"][0]["state"] == "ambiguous"


def test_a_disabled_integration_is_not_read_and_a_delivery_date_waits_for_delivery(rig):

    integration_id = linked(rig, status="in_transit", delivery_date=None)
    assert row(view(rig), "actual_date")["state"] == "not_compared"

    assert rig.client("alice").patch(f"/api/integrations/connections/{integration_id}", json={"enabled": False}).status_code == 200
    rig.state.service.invalidate()
    assert view(rig)["match"] is None and view(rig)["comparisons"] == []


def test_without_an_integration_store_nothing_is_read_or_created(tmp_path):

    from test_api import Rig

    plain = Rig(tmp_path, "required")
    found = plain.client("bob").get("/api/shipments/SHJ-2026-001/tms").json()

    assert found["configured"] is False and found["match"] is None and found["history"] == []
    assert not plain.locations.integration_path().exists()
    assert plain.client().get("/api/shipments/SHJ-2026-001/tms").status_code == 401
    assert plain.client("bob").get("/api/shipments/NOPE-1/tms").status_code == 404


def test_resolutions_are_append_only_and_the_store_upgrades_to_version_3(rig):

    linked(rig)
    assert resolve(rig, "expected_pieces", "document", 50, 48).status_code == 201
    path = rig.locations.integration_path()

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == integration_store.SCHEMA_VERSION == 3
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE tms_resolutions SET choice = 'tms'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM tms_resolutions")


def test_values_are_compared_in_one_form():

    assert tms_audit.normalized("text", "  Swift   LINES ") == "swift lines"
    assert tms_audit.normalized("date", "2026-10-03T00:00:00") == "2026-10-03"
    assert tms_audit.normalized("money", "1400") == 1400.0 and tms_audit.normalized("count", 48.0) == 48
    assert tms_audit.normalized("count", None) is None
