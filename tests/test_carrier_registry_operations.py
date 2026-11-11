"""
Carrier registry management (schema version 2 and the paged routes the
management screens use): registration status and address, the upgrade of a
version 1 store, search, filters, sorting and paging, the detail with its
links, requests and history, the document names with their shipments, and
the permissions, duplicate rules and two-person approval through the API.

Synthetic PDFs in a temporary folder (the sample shipments plus a contracted
shipment); no network, no model.

Run from the project root:
    python -m pytest tests/test_carrier_registry_operations.py
"""

import json
import sqlite3
from contextlib import closing

import pytest

from integration_support import ALICE, DAVE, integration_rig

import carrier_registry  # noqa: E402
from carrier_registry import CarrierRegistry, RegistryError, validate  # noqa: E402

REASON = "Same company: the invoices carry the same remit-to address and MC number."


@pytest.fixture
def registry(tmp_path):

    return CarrierRegistry(tmp_path / "carrier_registry.sqlite3")


@pytest.fixture
def rig(tmp_path):

    return integration_rig(tmp_path, signing_key=False, sandbox=False)


# ============================================================
# SCHEMA VERSION 2
# ============================================================

def test_a_version_1_store_is_upgraded_in_place_keeping_every_row(tmp_path):

    path = tmp_path / "carrier_registry.sqlite3"

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(carrier_registry.SCHEMA)
        connection.execute("INSERT INTO schema_info (version) VALUES (1)")
        connection.execute("INSERT INTO carriers (legal_name, scac, created_by, created_at, updated_at) "
                           "VALUES ('Old Carrier Inc', 'OLDC', 'alice', '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')")
        connection.execute("INSERT INTO carrier_events (at, actor, action, carrier_id, after) "
                           "VALUES ('2026-10-01T00:00:00+00:00', 'alice', 'create', 1, '{}')")
        before = connection.execute("SELECT * FROM carriers").fetchall()

    store = CarrierRegistry(path)

    assert store.version() == carrier_registry.SCHEMA_VERSION == 2
    [carrier] = store.carriers()
    assert tuple(carrier[name] for name in ("id", "legal_name", "scac", "created_by", "created_at", "updated_at")) == \
        (before[0][0], "Old Carrier Inc", "OLDC", "alice", "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
    assert carrier["status"] == "active" and all(carrier[name] is None for name in carrier_registry.ADDRESS)
    assert len(store.events()) == 1

    # Opening it again changes nothing; the history is still append-only
    assert CarrierRegistry(path).version() == 2
    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE carrier_events SET actor = 'x'")


def test_a_failed_upgrade_leaves_version_1_untouched(tmp_path, monkeypatch):

    path = tmp_path / "carrier_registry.sqlite3"

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(carrier_registry.SCHEMA)
        connection.execute("INSERT INTO schema_info (version) VALUES (1)")

    monkeypatch.setattr(carrier_registry, "MIGRATION_V2", (*carrier_registry.MIGRATION_V2[:3], "THIS IS NOT SQL"))

    with pytest.raises(sqlite3.OperationalError):
        CarrierRegistry(path)

    with closing(sqlite3.connect(path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(carriers)")}
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 1 and "status" not in columns


def test_a_store_from_a_newer_version_is_refused(tmp_path, rig):

    path = rig.locations.carrier_registry_path()
    CarrierRegistry(path)

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE schema_info SET version = 99")

    with pytest.raises(carrier_registry.RegistryVersionError):
        CarrierRegistry(path)

    reply = rig.client("alice").get("/api/carrier-registry/carriers")
    assert reply.status_code == 503 and "schema version 99" in reply.json()["detail"]


@pytest.mark.parametrize("field, raw, stored", [
    ("status", "On_Hold", "on_hold"), ("postal_code", "sw1a 1aa", "SW1A 1AA"), ("postal_code", "60601-1234", "60601-1234"),
    ("country", "us", "US"), ("city", "  Chicago ", "Chicago"), ("address_line1", "100  Main St", "100 Main St"),
])
def test_status_and_address_are_validated_and_stored_in_one_form(field, raw, stored):

    assert validate({field: raw}, partial=True)[field] == stored


@pytest.mark.parametrize("field, raw", [
    ("status", "suspended"), ("status", None), ("postal_code", "#"), ("postal_code", "1234567890123"), ("country", "USA"),
    ("country", "U1"), ("city", "x" * 101),
])
def test_malformed_status_and_address_are_refused(field, raw):

    with pytest.raises(RegistryError):
        validate({field: raw}, partial=True)


# ============================================================
# SEARCH, FILTERS, SORTING, PAGING
# ============================================================

def populate(registry):

    registry.create(ALICE, legal_name="Swift Lines Inc", trading_name="Swift", scac="SWFT", mc_number="MC-123456",
                    dot_number="USDOT 1234567", city="Chicago", country="US")
    registry.create(ALICE, legal_name="Blue Ridge Haulers", scac="BRHL", mc_number="654321", status="on_hold")
    registry.create(ALICE, legal_name="Desert Sun Carriers", trading_name="Desert_Sun 100%", dot_number="7654321",
                    status="inactive")


@pytest.mark.parametrize("query, expected", [
    ("swift", ["Swift Lines Inc"]), ("RIDGE", ["Blue Ridge Haulers"]), ("brhl", ["Blue Ridge Haulers"]),
    ("MC-123456", ["Swift Lines Inc"]), ("123456", ["Swift Lines Inc"]), ("MC 654321", ["Blue Ridge Haulers"]),
    ("654", ["Blue Ridge Haulers", "Desert Sun Carriers"]),                       # part of an MC and of a USDOT number
    ("USDOT 7654321", ["Desert Sun Carriers"]), ("100%", ["Desert Sun Carriers"]), ("_", ["Desert Sun Carriers"]),
    ("nothing like it", []),
])
def test_search_matches_names_and_identifiers(registry, query, expected):

    populate(registry)

    assert [carrier["legal_name"] for carrier in registry.search(query)["carriers"]] == expected


def test_filters_sorting_and_paging(registry):

    populate(registry)

    assert [c["legal_name"] for c in registry.search(statuses=("on_hold", "inactive"))["carriers"]] == \
        ["Blue Ridge Haulers", "Desert Sun Carriers"]
    assert [c["scac"] for c in registry.search(sort="scac", order="desc")["carriers"]] == ["SWFT", "BRHL", None]   # empty last
    assert [c["status"] for c in registry.search(sort="status")["carriers"]] == ["active", "inactive", "on_hold"]

    pages = [registry.search(offset=offset, limit=1) for offset in range(3)]
    assert [page["total"] for page in pages] == [3, 3, 3]
    assert [page["carriers"][0]["legal_name"] for page in pages] == ["Blue Ridge Haulers", "Desert Sun Carriers", "Swift Lines Inc"]
    assert registry.search(offset=10)["carriers"] == []

    for bad in ({"sort": "contact_email; DROP TABLE carriers"}, {"order": "sideways"}, {"statuses": ("archived",)}):
        with pytest.raises(RegistryError):
            registry.search(**bad)


def test_the_paged_route(rig):

    alice, bob = rig.client("alice"), rig.client("bob")

    assert bob.get("/api/carrier-registry/carriers").json() == {"carriers": [], "total": 0, "offset": 0, "limit": 25}
    assert not rig.locations.carrier_registry_path().exists()                           # reading created nothing

    for body in ({"legal_name": "Swift Lines Inc", "scac": "SWFT", "city": "Chicago", "country": "us"},
                 {"legal_name": "Blue Ridge Haulers", "status": "on_hold"}):
        assert alice.post("/api/carrier-registry", json=body).status_code == 201

    page = bob.get("/api/carrier-registry/carriers", params={"search": "swft"}).json()
    assert page["total"] == 1 and page["carriers"][0]["country"] == "US" and page["carriers"][0]["pending_requests"] == 0
    assert bob.get("/api/carrier-registry/carriers", params={"status": "on_hold"}).json()["total"] == 1
    assert bob.get("/api/carrier-registry/carriers", params={"sort": "updated_at", "order": "desc", "limit": 1}).json()["limit"] == 1
    for params in ({"sort": "password"}, {"status": "deleted"}, {"limit": 0}, {"limit": 201}, {"offset": -1}):
        assert bob.get("/api/carrier-registry/carriers", params=params).status_code == 422, params

    # The same through the versioned API
    assert bob.get("/api/v1/carrier-registry/carriers").json() == bob.get("/api/carrier-registry/carriers").json()


# ============================================================
# CREATION, EDITING, DUPLICATES, PERMISSIONS
# ============================================================

def test_creation_and_editing_through_the_api(rig):

    alice = rig.client("alice")
    created = alice.post("/api/carrier-registry", json={
        "legal_name": "Swift Lines Inc", "scac": "swft", "mc_number": "MC-123456", "contact_name": "Pat Doe",
        "contact_email": "ops@swift.example", "contact_phone": "+1 (555) 010-2000", "address_line1": "100 Main St",
        "city": "Chicago", "region": "IL", "postal_code": "60601", "country": "US"}).json()

    assert (created["scac"], created["mc_number"], created["status"], created["city"]) == ("SWFT", "123456", "active", "Chicago")

    changed = alice.patch(f"/api/carrier-registry/{created['id']}", json={"status": "on_hold", "postal_code": "60602"}).json()
    assert changed["status"] == "on_hold" and changed["postal_code"] == "60602" and changed["legal_name"] == "Swift Lines Inc"

    assert alice.patch(f"/api/carrier-registry/{created['id']}", json={"status": "archived"}).status_code == 422
    assert alice.patch(f"/api/carrier-registry/{created['id']}", json={"country": "United States"}).status_code == 422
    assert alice.patch(f"/api/carrier-registry/{created['id']}", json={}).status_code == 422
    assert alice.patch("/api/carrier-registry/999", json={"city": "Austin"}).status_code == 404

    history = alice.get(f"/api/carrier-registry/carriers/{created['id']}/events").json()
    assert [event["action"] for event in history["events"]] == ["update", "create"]
    assert history["events"][0]["before"] == {"status": "active", "postal_code": "60601"}


def test_duplicates_are_explained_and_need_a_deliberate_confirmation(rig):

    alice = rig.client("alice")
    first = alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines, Inc.", "scac": "SWFT", "mc_number": "123456"}).json()

    for body, words in (({"legal_name": "Other Carrier", "scac": "swft"}, f"SCAC SWFT already belongs to carrier {first['id']}"),
                        ({"legal_name": "Other Carrier", "mc_number": "MC 123456"}, "MC number 123456 already belongs"),
                        ({"legal_name": "SWIFT LINES INC"}, "Possibly the same company")):
        reply = alice.post("/api/carrier-registry", json=body)
        assert reply.status_code == 409 and words in reply.json()["detail"] and "Swift Lines, Inc." in reply.json()["detail"]

    # A malformed identifier is refused with its rule, never cleaned into something else
    reply = alice.post("/api/carrier-registry", json={"legal_name": "Other Carrier", "scac": "SW1"})
    assert reply.status_code == 422 and "2 to 4 letters" in reply.json()["detail"]

    assert alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc", "confirm_distinct": True}).status_code == 201


def test_only_administrators_change_master_data_or_links_and_read_history(rig):

    alice, bob = rig.client("alice"), rig.client("bob")
    carrier = alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"}).json()
    request = alice.post("/api/carrier-registry/links", json={"name": "Swift Lines", "carrier_id": carrier["id"],
                                                              "reason": REASON}).json()

    for method, url, body in (("post", "/api/carrier-registry", {"legal_name": "Reviewer Carrier"}),
                              ("patch", f"/api/carrier-registry/{carrier['id']}", {"city": "Austin"}),
                              ("post", "/api/carrier-registry/links", {"name": "Swift", "carrier_id": carrier["id"], "reason": REASON}),
                              ("post", f"/api/carrier-registry/links/{request['id']}/decision", {"decision": "approve"}),
                              ("get", f"/api/carrier-registry/carriers/{carrier['id']}/events", None),
                              ("get", "/api/carrier-registry/events", None)):
        reply = bob.get(url) if method == "get" else getattr(bob, method)(url, json=body)
        assert reply.status_code == 403, url

    # Reviewers read the registry, but a carrier's history only reaches administrators
    assert bob.get(f"/api/carrier-registry/carriers/{carrier['id']}").json()["history"] is None
    assert alice.get(f"/api/carrier-registry/carriers/{carrier['id']}").json()["history"]["total"] == 2


# ============================================================
# TWO-PERSON LINKS
# ============================================================

def test_a_link_is_proposed_by_one_administrator_and_decided_by_another(rig):

    alice, dave = rig.client("alice"), rig.client("dave")
    carrier = alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"}).json()
    request = alice.post("/api/carrier-registry/links", json={"name": "Swift Lines", "carrier_id": carrier["id"],
                                                              "reason": REASON}).json()
    decision = f"/api/carrier-registry/links/{request['id']}/decision"

    queue = dave.get("/api/carrier-registry/links").json()
    assert queue["total"] == 1 and queue["requests"][0]["carrier_legal_name"] == "Swift Lines Inc"
    assert queue["requests"][0]["current_carrier_id"] is None

    refused = alice.post(decision, json={"decision": "approve"})
    assert refused.status_code == 403 and "another administrator" in refused.json()["detail"]
    assert dave.post(decision, json={"decision": "reject"}).status_code == 422                 # a rejection needs a note

    assert dave.post(decision, json={"decision": "approve", "note": "Checked the MC number."}).json()["decision"] == "approved"
    assert dave.post(decision, json={"decision": "reject", "note": "Too late."}).status_code == 409

    detail = alice.get(f"/api/carrier-registry/carriers/{carrier['id']}").json()
    assert [link["display_name"] for link in detail["name_links"]] == ["Swift Lines"]
    assert detail["name_links"][0]["proposed_by"] == "alice" and detail["name_links"][0]["decided_by"] == "dave"
    assert detail["pending_requests"] == [] and detail["recent_decisions"][0]["decision"] == "approved"
    assert [event["action"] for event in detail["history"]["events"]] == ["approved_link", "propose_link", "create"]
    assert detail["intelligence_key"] == f"registered:{carrier['id']}"

    # Carrier Intelligence now shows the documents' Swift Lines under the registered carrier
    figures = alice.get(f"/api/carriers/{detail['intelligence_key']}").json()
    assert figures["carrier"] == "Swift Lines Inc" and figures["shipment_count"] >= 1
    assert alice.get("/api/carrier-registry/links", params={"state": "decided"}).json()["total"] == 1


def test_a_rejected_link_changes_nothing_and_keeps_its_reason(rig):

    alice, dave = rig.client("alice"), rig.client("dave")
    carrier = alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"}).json()
    request = alice.post("/api/carrier-registry/links", json={"name": "Swift Lines", "carrier_id": carrier["id"],
                                                              "reason": REASON}).json()
    rejected = dave.post(f"/api/carrier-registry/links/{request['id']}/decision",
                         json={"decision": "reject", "note": "Different company: other MC number."}).json()

    assert rejected["decision"] == "rejected" and rejected["decision_note"] == "Different company: other MC number."
    detail = alice.get(f"/api/carrier-registry/carriers/{carrier['id']}").json()
    assert detail["name_links"] == [] and detail["intelligence_key"] is None
    assert detail["history"]["events"][0]["action"] == "rejected_link"


# ============================================================
# DOCUMENT NAMES (the link screen's context)
# ============================================================

def test_document_names_show_their_shipments_documents_and_link_state(rig):

    alice, dave = rig.client("alice"), rig.client("dave")
    page = alice.get("/api/carrier-registry/document-names").json()
    names = {row["name"]: row for row in page["names"]}

    assert page["total"] == len(page["names"]) >= 2 and page["counts"]["unlinked"] == page["total"]
    swift = names["Swift Lines"]
    assert swift["state"] == "unlinked" and swift["shipments"] == len(swift["shipment_ids"]) >= 1
    assert {document["document_type"] for document in swift["documents"]} <= {"BOL", "INVOICE", "POD", "RATE_CONFIRMATION"}
    assert all(document["shipment_id"] in swift["shipment_ids"] for document in swift["documents"])

    # A registered carrier with the same name is only a suggestion: nothing is linked by itself
    carrier = alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines", "confirm_distinct": True}).json()
    swift = next(row for row in alice.get("/api/carrier-registry/document-names").json()["names"] if row["name"] == "Swift Lines")
    assert swift["suggestions"] == [{"id": carrier["id"], "legal_name": "Swift Lines"}] and swift["state"] == "unlinked"
    assert CarrierRegistry(rig.locations.carrier_registry_path()).name_links() == []

    request = alice.post("/api/carrier-registry/links", json={"name": "Swift Lines", "carrier_id": carrier["id"],
                                                              "reason": REASON}).json()
    pending = alice.get("/api/carrier-registry/document-names", params={"state": "pending"}).json()
    assert [row["name"] for row in pending["names"]] == ["Swift Lines"]
    assert pending["names"][0]["pending_request"]["proposed_by"] == "alice"

    dave.post(f"/api/carrier-registry/links/{request['id']}/decision", json={"decision": "approve"})
    linked = alice.get("/api/carrier-registry/document-names", params={"state": "linked", "search": "swift"}).json()
    assert linked["names"][0]["linked_carrier"] == {"id": carrier["id"], "legal_name": "Swift Lines"}
    assert linked["names"][0]["suggestions"] == []


def test_document_names_page_and_search(rig):

    alice = rig.client("alice")
    everything = alice.get("/api/carrier-registry/document-names", params={"limit": 200}).json()
    pages = [alice.get("/api/carrier-registry/document-names", params={"offset": offset, "limit": 1}).json()["names"][0]["name_key"]
             for offset in range(everything["total"])]

    assert pages == [row["name_key"] for row in everything["names"]]
    assert alice.get("/api/carrier-registry/document-names", params={"search": "no such carrier"}).json()["names"] == []
    assert alice.get("/api/carrier-registry/document-names", params={"state": "merged"}).status_code == 422


def test_reading_the_registry_screens_never_changes_recorded_history(rig):

    alice = rig.client("alice")
    assert alice.post("/api/history/runs", json={"label": "Before"}).status_code == 201
    before = alice.get("/api/history/shipments/SHJ-2026-101").json()

    for url in ("/api/carrier-registry/carriers", "/api/carrier-registry/document-names", "/api/carrier-registry/links"):
        assert alice.get(url).status_code == 200

    assert alice.get("/api/history/shipments/SHJ-2026-101").json() == before
    assert json.dumps(before)


def test_the_services_check_the_permission_themselves(tmp_path):

    registry = CarrierRegistry(tmp_path / "carrier_registry.sqlite3")
    carrier = registry.create(ALICE, legal_name="Swift Lines Inc")
    request = registry.propose_link(ALICE, "Swift Lines", carrier["id"], REASON)

    from authz import Principal, PermissionDenied

    reviewer = Principal("bob", "reviewer", True, 2)
    for call in (lambda: registry.create(reviewer, legal_name="X"), lambda: registry.update(reviewer, carrier["id"], city="Austin"),
                 lambda: registry.propose_link(reviewer, "Swift", carrier["id"], REASON),
                 lambda: registry.decide_link(reviewer, request["id"], "approve")):
        with pytest.raises(PermissionDenied):
            call()

    assert registry.decide_link(DAVE, request["id"], "approve")["decision"] == "approved"
