"""
Carrier master data: identifier validation, duplicate detection, two-person
links of document names, the append-only event log, the permissions at the API,
and how approved links change the Carrier Intelligence grouping (and nothing
recorded).

Run from the project root:
    python -m pytest tests/test_carrier_registry.py
"""

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import carrier_intelligence as ci  # noqa: E402
from authz import PermissionDenied, Principal  # noqa: E402
from carrier_registry import CarrierRegistry, RegistryConflict, RegistryError, validate  # noqa: E402
from test_carrier_intelligence import complete, shipment  # noqa: E402

ALICE = Principal("alice", "admin", True, 1)
DAVE = Principal("dave", "admin", True, 4)
BOB = Principal("bob", "reviewer", True, 2)
REASON = "Same company: the invoices carry the same remit-to address and MC number."


@pytest.fixture
def registry(tmp_path):

    return CarrierRegistry(tmp_path / "carrier_registry.sqlite3")


# ============================================================
# IDENTIFIERS
# ============================================================

@pytest.mark.parametrize("field, raw, stored", [
    ("scac", "swft", "SWFT"),
    ("scac", " AB ", "AB"),
    ("mc_number", "MC-123456", "123456"),
    ("mc_number", "mc 0123456", "123456"),
    ("mc_number", "#987654", "987654"),
    ("dot_number", "USDOT 1234567", "1234567"),
    ("dot_number", "DOT: 12345678", "12345678"),
    ("contact_email", "ops@swiftlines.example", "ops@swiftlines.example"),
    ("contact_phone", "+1 (555) 010-2000", "+1 (555) 010-2000"),
    ("scac", "", None),
])
def test_valid_identifiers_are_stored_in_one_form(field, raw, stored):

    assert validate({field: raw}, partial=True)[field] == stored


@pytest.mark.parametrize("field, raw", [
    ("scac", "S"), ("scac", "SWIFT"), ("scac", "SW1F"),
    ("mc_number", "MC-123456789"), ("mc_number", "MC-12A45"),
    ("dot_number", "USDOT 123456789"), ("dot_number", "1234-567"),
    ("contact_email", "not an email"), ("contact_phone", "call me"),
    ("legal_name", "   "),
])
def test_malformed_identifiers_are_refused_not_cleaned(field, raw):

    with pytest.raises(RegistryError):
        validate({field: raw}, partial=True)


def test_an_identifier_belongs_to_one_carrier(registry):

    first = registry.create(ALICE, legal_name="Swift Lines Inc", scac="SWFT", mc_number="MC-123456", dot_number="1234567")

    for field, value in (("scac", "swft"), ("mc_number", "123456"), ("dot_number", "USDOT 1234567")):
        with pytest.raises(RegistryConflict, match=f"belongs to carrier {first['id']}"):
            registry.create(ALICE, legal_name=f"Other Carrier {field}", **{field: value})

    other = registry.create(ALICE, legal_name="Blue Ridge Haulers", scac="BRHL")
    with pytest.raises(RegistryConflict):
        registry.update(ALICE, other["id"], scac="SWFT")


def test_a_similar_name_is_a_possible_duplicate_unless_confirmed(registry):

    first = registry.create(ALICE, legal_name="Swift Lines, Inc.")

    with pytest.raises(RegistryConflict, match=f"carrier {first['id']}"):
        registry.create(ALICE, legal_name="SWIFT LINES INC")
    with pytest.raises(RegistryConflict):
        registry.create(ALICE, legal_name="Swift Transport", trading_name="Swift Lines Inc")

    assert registry.create(ALICE, legal_name="Swift Lines Inc", confirm_distinct=True)["id"] != first["id"]
    assert registry.create(ALICE, legal_name="Swift Logistics")["legal_name"] == "Swift Logistics"


def test_an_update_records_what_it_changed(registry):

    carrier = registry.create(ALICE, legal_name="Blue Ridge Haulers")
    updated = registry.update(ALICE, carrier["id"], scac="brhl", contact_email="ap@blueridge.example")

    assert (updated["scac"], updated["contact_email"], updated["legal_name"]) == ("BRHL", "ap@blueridge.example", "Blue Ridge Haulers")

    event = registry.events()[0]
    assert (event["action"], event["actor"], event["before"], event["after"]) == (
        "update", "alice", {"scac": None, "contact_email": None}, {"scac": "BRHL", "contact_email": "ap@blueridge.example"})


# ============================================================
# TWO-PERSON LINKS
# ============================================================

def test_a_link_needs_a_different_administrator(registry):

    carrier = registry.create(ALICE, legal_name="Swift Lines Inc")
    request = registry.propose_link(ALICE, "Swift Lines", carrier["id"], REASON)

    assert registry.resolver() == {}                         # no effect until approved

    with pytest.raises(PermissionDenied, match="another administrator must"):
        registry.decide_link(ALICE, request["id"], "approve")
    with pytest.raises(PermissionDenied):
        registry.decide_link(BOB, request["id"], "approve")

    decided = registry.decide_link(DAVE, request["id"], "approve", "Checked the MC number on both invoices.")

    assert (decided["decision"], decided["decided_by"]) == ("approved", "dave")
    assert registry.resolver()["swift lines"]["id"] == carrier["id"]
    with pytest.raises(RegistryConflict, match="already approved"):
        registry.decide_link(DAVE, request["id"], "reject", "Too late.")


def test_link_rules(registry):

    carrier = registry.create(ALICE, legal_name="Swift Lines Inc")

    with pytest.raises(RegistryError, match="at least 10"):
        registry.propose_link(ALICE, "Swift Lines", carrier["id"], "same")
    with pytest.raises(LookupError):
        registry.propose_link(ALICE, "Swift Lines", 999, REASON)
    with pytest.raises(RegistryError, match="not linked"):
        registry.propose_link(ALICE, "Swift Lines", None, REASON)

    registry.propose_link(ALICE, "Swift Lines", carrier["id"], REASON)
    with pytest.raises(RegistryConflict, match="already waiting"):
        registry.propose_link(ALICE, "swift  lines", carrier["id"], REASON)

    request = registry.requests(pending_only=True)[0]
    with pytest.raises(RegistryError, match="needs a note"):
        registry.decide_link(DAVE, request["id"], "reject")
    assert registry.decide_link(DAVE, request["id"], "reject", "Different remit-to address.")["decision"] == "rejected"
    assert registry.resolver() == {}


def test_a_linked_name_moves_or_unlinks_only_by_an_approved_request(registry):

    first = registry.create(ALICE, legal_name="Swift Lines Inc")
    second = registry.create(ALICE, legal_name="Swift Transport LLC")
    registry.decide_link(DAVE, registry.propose_link(ALICE, "Swift Lines", first["id"], REASON)["id"], "approve")

    with pytest.raises(RegistryError, match="already linked"):
        registry.propose_link(ALICE, "Swift Lines", first["id"], REASON)

    move = registry.propose_link(ALICE, "Swift Lines", second["id"], REASON)
    assert registry.resolver()["swift lines"]["id"] == first["id"]          # still where it was
    registry.decide_link(DAVE, move["id"], "approve")
    assert registry.resolver()["swift lines"]["id"] == second["id"]

    registry.decide_link(DAVE, registry.propose_link(ALICE, "Swift Lines", None, REASON)["id"], "approve")
    assert registry.resolver() == {}
    assert [e["action"] for e in reversed(registry.events())][-2:] == ["propose_unlink", "approved_link"]


def test_the_event_log_is_append_only(registry):

    registry.create(ALICE, legal_name="Swift Lines Inc")

    with sqlite3.connect(registry.path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE carrier_events SET actor = 'someone else'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM carrier_events")


def test_a_reviewer_cannot_change_master_data(registry):

    with pytest.raises(PermissionDenied):
        registry.create(BOB, legal_name="Swift Lines Inc")


# ============================================================
# WHAT A LINK CHANGES IN CARRIER INTELLIGENCE
# ============================================================

def test_names_linked_to_one_carrier_are_one_carrier(registry):

    carrier = registry.create(ALICE, legal_name="Swift Lines Incorporated", scac="SWFT")
    for name in ("Swift Lines", "Swift Lines, Inc."):
        registry.decide_link(DAVE, registry.propose_link(ALICE, name, carrier["id"], REASON)["id"], "approve")

    items = [shipment("S-1", carrier="Swift Lines"), shipment("S-2", carrier="Swift Lines, Inc."),
             shipment("S-3", carriers={"INVOICE": "Swift Lines, Inc."}),            # documents print both names
             shipment("S-4", carrier="Swift Logistics")]                            # similar, not linked
    rows = [complete(item) for item in items]

    before = ci.analyze(items, [], rows)
    after = ci.analyze(items, [], rows, resolver=registry.resolver())

    assert sorted(c["carrier"] for c in before["carriers"]) == ["Swift Lines", "Swift Lines, Inc.", "Swift Logistics"]
    assert [c["shipment_id"] for c in before["unattributed"]["conflicting"]] == ["S-3"]

    registered = next(c for c in after["carriers"] if c["registered"])
    assert registered["carrier"] == "Swift Lines Incorporated" and registered["shipments"] == 3
    assert registered["names"] == ["Swift Lines", "Swift Lines, Inc."] and registered["registered"]["scac"] == "SWFT"
    assert after["unattributed"]["conflicting"] == []
    assert [c["carrier"] for c in after["carriers"] if not c["registered"]] == ["Swift Logistics"]

    detail = ci.carrier_detail(items, [], rows, registered["carrier_key"], resolver=registry.resolver())
    assert detail["identifier"] == "SCAC SWFT" and "registered carrier" in detail["identity_note"].lower()


# ============================================================
# THE API
# ============================================================

@pytest.fixture
def rig(tmp_path):

    from auth import UserStore
    from authz import ADMIN
    from test_api import PASSWORD, Rig
    from test_freight_audit import write_contracted_shipment

    result = Rig(tmp_path, "required")
    UserStore(tmp_path / "auth.sqlite3").create_user("dave", PASSWORD, ADMIN)
    write_contracted_shipment(result.raw)
    result.state.service.invalidate()

    return result


def test_the_api_enforces_permissions_and_rules(rig):

    alice, dave, bob = rig.client("alice"), rig.client("dave"), rig.client("bob")

    assert rig.client(None).get("/api/carrier-registry").status_code == 401
    assert bob.get("/api/carrier-registry").json() == {"carriers": [], "requests": [], "pending": 0}
    assert not rig.locations.carrier_registry_path().exists()                  # reading created nothing

    assert bob.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"}).status_code == 403
    assert alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc", "scac": "SW1"}).status_code == 422

    created = alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc", "scac": "swft", "mc_number": "MC-123456"})
    assert created.status_code == 201 and created.json()["scac"] == "SWFT" and created.json()["mc_number"] == "123456"
    carrier_id = created.json()["id"]

    assert alice.post("/api/carrier-registry", json={"legal_name": "SWIFT LINES, INC."}).status_code == 409
    assert alice.patch(f"/api/carrier-registry/{carrier_id}", json={"dot_number": "USDOT 7654321"}).json()["dot_number"] == "7654321"

    request = alice.post("/api/carrier-registry/links", json={"name": "Swift Lines", "carrier_id": carrier_id, "reason": REASON})
    assert request.status_code == 201
    url = f"/api/carrier-registry/links/{request.json()['id']}/decision"

    assert alice.post(url, json={"decision": "approve"}).status_code == 403
    assert bob.post(url, json={"decision": "approve"}).status_code == 403
    assert dave.post(url, json={"decision": "approve", "note": "Checked."}).json()["decision"] == "approved"

    card = next(c for c in bob.get("/api/carriers").json()["carriers"] if c["registered"])
    assert card["carrier"] == "Swift Lines Inc" and card["registered"]["dot_number"] == "7654321"
    assert "SWFT" in bob.get("/api/reports/carriers.csv").content.decode("utf-8-sig")

    assert bob.get("/api/carrier-registry/events").status_code == 403
    assert [e["action"] for e in alice.get("/api/carrier-registry/events").json()][:2] == ["approved_link", "propose_link"]


def test_a_link_does_not_touch_recorded_history(rig):

    alice, dave = rig.client("alice"), rig.client("dave")
    assert alice.post("/api/history/runs", json={"label": "Before the link"}).status_code == 201
    before = alice.get("/api/history/shipments/SHJ-2026-101").json()

    carrier_id = alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"}).json()["id"]
    request_id = alice.post("/api/carrier-registry/links",
                            json={"name": "Swift Lines", "carrier_id": carrier_id, "reason": REASON}).json()["id"]
    dave.post(f"/api/carrier-registry/links/{request_id}/decision", json={"decision": "approve"})

    assert alice.get("/api/history/shipments/SHJ-2026-101").json() == before
    assert before[0]["snapshot"]["carrier"] == "Swift Lines"
