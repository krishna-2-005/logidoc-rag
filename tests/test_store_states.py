"""
Extraction store states and GET safety (Step 0 of docs/SCHEMA_V5_ALIAS_DESIGN.md), now against the real schema 5.

A "valid v4 file with its upgrade pending" is a genuine schema 4 file (built with plain sqlite3 and the schema 4 code path,
holding one approved, one rejected and one pending correction) opened by the current code, which expects schema 5.
Synthetic temporary databases only.

    states      absent, older, migration pending, current, unavailable, migration failed: all distinguishable
    reading     a pending v4 file still gives its approvals, to the audit and to the API, read-only
    GET         no GET changes the file: bytes, version and tables stay identical
    writing     a write on a file awaiting an upgrade fails with 503 and changes nothing
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS.parent

for folder in (PROJECT_ROOT / "src", TESTS):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import document_extraction as de  # noqa: E402
import extraction_migration as migration  # noqa: E402
import extraction_store as store_module  # noqa: E402
from extraction_store import (  # noqa: E402
    ABSENT, CURRENT, MIGRATION_FAILED, MIGRATION_PENDING, OLDER, UNAVAILABLE, ExtractionStore, MigrationPending, StoreError, StoreUnavailable,
)
from migration_support import store_at, upgrade  # noqa: E402
from test_correction_aware_audit import (  # noqa: E402,F401
    ALICE, BOB, POD_A, REASON, approve, extract, propose, rig,
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def shape(path):
    connection = sqlite3.connect(path)

    try:
        return (connection.execute("SELECT version FROM schema_info").fetchone()[0],
                sorted(r[0] for r in connection.execute("SELECT name FROM sqlite_master")))
    finally:
        connection.close()


def current_with_approval(rig):
    """A current (schema 5) store holding one approved and one pending correction, made through the application."""

    pod = extract(rig, POD_A)
    approved = propose(rig, pod, "delivered_pieces", "48")
    approve(rig, approved["id"])
    pending = propose(rig, pod, "delivery_date", "2026-10-06")

    return pod, approved, pending, rig.locations.extraction_path()


def v4_file(path, monkeypatch, raw_folder=None):
    """
    A genuine schema 4 store. With raw_folder it holds the extraction of POD_A and three corrections: delivered_pieces approved,
    delivery_date rejected and then proposed again (pending). Returns (extraction id, approved, rejected, pending).
    """

    store_at(path, 4)

    if raw_folder is None:
        return None

    extraction = de.extract_document(Path(raw_folder) / POD_A)
    payload = json.dumps(store_module.persistable(extraction))
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, shipment_id, shipment_key, payload, "
        "payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'then')",
        (POD_A, extraction.sha256, extraction.extractor_version, extraction.document_type, extraction.status, extraction.shipment_id,
         "SHJ2026001", payload, store_module.sha256_text(payload)))
    connection.commit()
    connection.close()

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "SCHEMA_VERSION", 4)       # the schema 4 code path: no alias table yet
        store = ExtractionStore(path)
        approved = store.propose(BOB, 1, "delivered_pieces", "48", REASON, based_on=None)
        store.decide(ALICE, approved["id"], "approve", "Checked against the paper POD.")
        rejected = store.propose(BOB, 1, "delivery_date", "2026-10-09", REASON, based_on=None)
        store.decide(ALICE, rejected["id"], "reject", "The paper POD gives another date.")
        pending = store.propose(BOB, 1, "delivery_date", "2026-10-06", REASON, based_on=rejected["id"])

    return 1, approved, rejected, pending


@pytest.fixture
def v4_world(rig, monkeypatch):
    """The rig's extraction store is a real v4 file with approvals; the code under test expects schema 5."""

    made = v4_file(rig.locations.extraction_path(), monkeypatch, rig.raw)
    rig.state.service.invalidate()

    return (*made, rig.locations.extraction_path())


# =========================================================================
# the states
# =========================================================================

def test_every_state_is_distinguishable(rig, tmp_path, monkeypatch):
    seen = {"absent": ExtractionStore.inspect(tmp_path / "none.sqlite3")}

    older = store_at(tmp_path / "older.sqlite3", 3)
    seen["older"] = ExtractionStore.inspect(older)

    seen["pending"] = ExtractionStore.inspect(v4_file(tmp_path / "v4.sqlite3", monkeypatch) or tmp_path / "v4.sqlite3")

    _, _, _, current = current_with_approval(rig)
    seen["current"] = ExtractionStore.inspect(current)

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a database" * 40)
    seen["corrupt"] = ExtractionStore.inspect(corrupt)

    newer = tmp_path / "newer.sqlite3"
    ExtractionStore(newer)
    with sqlite3.connect(newer) as connection:
        connection.execute("UPDATE schema_info SET version = 99")
    seen["newer"] = ExtractionStore.inspect(newer)

    broken = tmp_path / "broken.sqlite3"
    ExtractionStore(broken)
    with sqlite3.connect(broken) as connection:
        connection.execute("DROP TABLE extraction_aliases")
    seen["invalid"] = ExtractionStore.inspect(broken)

    stripped = tmp_path / "stripped.sqlite3"
    ExtractionStore(stripped)
    with sqlite3.connect(stripped) as connection:
        connection.execute("DROP TRIGGER extraction_aliases_no_delete")
    seen["no_trigger"] = ExtractionStore.inspect(stripped)

    assert {k: v.state for k, v in seen.items()} == {
        "absent": ABSENT, "older": OLDER, "pending": MIGRATION_PENDING, "current": CURRENT, "corrupt": UNAVAILABLE, "newer": UNAVAILABLE,
        "invalid": UNAVAILABLE, "no_trigger": UNAVAILABLE,
    }
    assert seen["pending"].version == 4 and seen["older"].version == 3 and seen["absent"].version is None and seen["current"].version == 5
    assert len({v.reason for v in seen.values()}) >= 6                       # the states say different things
    assert not any(str(tmp_path) in v.reason for v in seen.values())          # no path in a reason


def test_an_empty_file_is_a_new_database_not_a_damaged_one(tmp_path):
    path = tmp_path / "empty.sqlite3"
    path.write_bytes(b"")

    assert ExtractionStore.inspect(path).state == ABSENT
    assert ExtractionStore(path).version() == store_module.SCHEMA_VERSION


def test_a_missing_file_is_not_confused_with_an_older_or_a_damaged_one(tmp_path):
    older = store_at(tmp_path / "older.sqlite3", 3)
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"x" * 600)

    assert ExtractionStore.open_for_reading(tmp_path / "none.sqlite3") is None
    assert ExtractionStore.open_for_reading(older) is None                    # nothing approved can exist
    assert ExtractionStore.inspect(older).state != ExtractionStore.inspect(tmp_path / "none.sqlite3").state

    with pytest.raises(StoreUnavailable):
        ExtractionStore.open_for_reading(corrupt)                              # never None: that would read as "no corrections"


def test_open_if_current_keeps_working_for_a_current_store(rig):
    _, _, _, path = current_with_approval(rig)

    assert ExtractionStore.open_if_current(path).approved_corrections("SHJ2026001")


# =========================================================================
# reading a store whose upgrade is pending
# =========================================================================

def test_approvals_stay_visible_to_the_audit_and_the_api_while_the_upgrade_is_pending(rig, v4_world):
    pod, approved, rejected, pending, path = v4_world
    service = rig.state.service
    before = digest(path)

    store = ExtractionStore.open_for_reading(path)
    ids = [c["correction_id"] for c in store.approved_corrections("SHJ2026001")]
    field = next(f for f in service.shipment("SHJ-2026-001")["inputs"]["fields"] if f["field"] == "delivered_pieces")

    assert store is not None and store.readonly and ids == [approved["id"]]
    assert field["source"] == "approved_correction" and field["value"] == 48
    assert service.corrections_available() is True and service.store_state()["state"] == MIGRATION_PENDING
    assert store.verify_history()["ok"]                                        # history verifies without writing
    assert [c["status"] for c in store.corrections(pod)] == ["approved", "rejected", "pending"]
    assert store.aliases(pod) is None                                          # no alias table yet: said, not invented

    with pytest.raises(StoreError, match="reading only"):
        with store.write():
            pass

    with pytest.raises(sqlite3.OperationalError):                              # read-only at the SQLite level too
        connection = store.connect()
        try:
            connection.execute("DELETE FROM schema_info")
        finally:
            connection.close()

    assert digest(path) == before


def test_a_damaged_store_does_not_silently_disable_correction_handling(rig):
    current_with_approval(rig)
    path = rig.locations.extraction_path()
    service = rig.state.service
    path.write_bytes(b"not a database at all" * 50)
    service.invalidate()

    assert service.corrections_available() is False and service.store_state()["state"] == UNAVAILABLE

    fields = service.shipment("SHJ-2026-001")["inputs"]["fields"]

    assert all("correction_store_unavailable" in f["warnings"] for f in fields)
    assert rig.client("alice").get("/api/extractions/staleness").json()["corrections_available"] is False


# =========================================================================
# GET requests never change the file
# =========================================================================

READS = ["/api/extractions", "/api/extractions/1", "/api/extractions/pending", "/api/extractions/integrity",
         "/api/extractions/staleness", "/api/extractions?shipment_id=SHJ-2026-001"]


def test_get_routes_do_not_change_a_file_awaiting_its_upgrade(rig, v4_world):
    _, _, _, _, path = v4_world
    before, shape_before = digest(path), shape(path)
    client = rig.client("alice")

    for url in READS:
        reply = client.get(url)

        assert reply.status_code == 200, (url, reply.text)

    listed = client.get("/api/extractions").json()["extractions"]

    assert listed and listed[0]["alias_count"] is None and listed[0]["has_type_conflict"] is None
    assert client.get("/api/extractions/1").json()["aliases_available"] is False
    assert client.get("/api/extractions/staleness").json()["store"]["state"] == MIGRATION_PENDING
    assert client.get("/api/extractions/pending").json()["pending"]
    assert digest(path) == before and shape(path) == shape_before and shape_before[0] == 4


def test_get_routes_do_not_change_a_current_file(rig):
    _, _, _, path = current_with_approval(rig)
    before = digest(path)
    client = rig.client("alice")

    for url in READS:
        assert client.get(url).status_code == 200

    assert digest(path) == before
    assert client.get("/api/extractions/staleness").json()["store"]["state"] == CURRENT
    assert client.get("/api/extractions/1").json()["aliases_available"] is True


def test_get_routes_do_not_upgrade_an_older_file_and_say_why(rig):
    path = store_at(rig.locations.extraction_path(), 3)
    before, shape_before = digest(path), shape(path)
    client = rig.client("alice")

    for url in ("/api/extractions", "/api/extractions/1", "/api/extractions/pending", "/api/extractions/integrity"):
        reply = client.get(url)

        assert reply.status_code == 503, (url, reply.text)
        assert "older than the first version" in reply.json()["detail"] and str(path.parent) not in reply.json()["detail"]

    assert digest(path) == before and shape(path) == shape_before and shape_before[0] == 3
    assert client.get("/api/extractions/staleness").json()["store"]["state"] == OLDER


def test_get_routes_on_a_damaged_file_answer_503_not_an_empty_list(rig):
    path = rig.locations.extraction_path()
    path.write_bytes(b"garbage" * 100)
    before = digest(path)
    client = rig.client("alice")

    for url in ("/api/extractions", "/api/extractions/pending", "/api/extractions/integrity"):
        assert client.get(url).status_code == 503

    assert digest(path) == before


def test_get_routes_without_a_store_still_answer_empty_and_create_nothing(rig):
    path = rig.locations.extraction_path()
    client = rig.client("alice")

    assert not path.exists()
    assert client.get("/api/extractions").json()["extractions"] == []
    assert client.get("/api/extractions/pending").json() == {"pending": []}
    assert client.get("/api/extractions/staleness").json()["store"]["state"] == ABSENT
    assert not path.exists()


def test_a_viewer_cannot_trigger_a_schema_change(rig):
    """Authorization is unchanged, and whatever a viewer requests, the file stays as it was."""

    path = store_at(rig.locations.extraction_path(), 3)
    before = digest(path)
    viewer = rig.client("bob")

    assert viewer.get("/api/extractions").status_code in (403, 503)
    assert rig.client().get("/api/extractions").status_code == 401
    assert digest(path) == before


# =========================================================================
# writes while an upgrade is pending
# =========================================================================

def test_writes_fail_safely_while_the_upgrade_is_pending(rig, v4_world):
    pod, approved, rejected, pending, path = v4_world
    before, shape_before = digest(path), shape(path)
    client = rig.client("alice")

    run = client.post("/api/extractions/run", json={"document": POD_A})
    proposal = client.post(f"/api/extractions/{pod}/corrections",
                           json={"field": "delivered_pieces", "value": "47", "reason": "The paper POD shows 47 pieces.", "based_on": approved["id"]})
    decision = client.post(f"/api/extractions/corrections/{pending['id']}/decision", json={"decision": "approve", "note": "Checked."})

    for reply in (run, proposal, decision):
        assert reply.status_code == 503, reply.text
        assert "needs a schema upgrade" in reply.json()["detail"] and str(path.parent) not in reply.json()["detail"]

    assert digest(path) == before and shape(path) == shape_before


def test_the_service_refuses_a_decision_while_the_upgrade_is_pending_and_changes_nothing(rig, v4_world):
    _, _, _, pending, path = v4_world
    before = digest(path)

    with pytest.raises(MigrationPending):
        rig.state.service.decide_correction(ALICE, pending["id"], "approve", "Checked against the paper POD.")

    assert digest(path) == before


def test_a_write_to_a_damaged_file_is_refused_not_recreated(rig):
    path = rig.locations.extraction_path()
    path.write_bytes(b"garbage" * 100)
    before = digest(path)

    with pytest.raises(StoreUnavailable):
        ExtractionStore.open_for_write(path, create=True)

    assert rig.client("alice").post("/api/extractions/run", json={"document": POD_A}).status_code == 503
    assert digest(path) == before


def test_a_write_to_an_older_file_is_refused_and_never_upgrades_it(rig):
    path = store_at(rig.locations.extraction_path(), 3)
    before = digest(path)
    client = rig.client("alice")

    reply = client.post("/api/extractions/run", json={"document": POD_A})

    assert reply.status_code == 503 and "older than the first version" in reply.json()["detail"]
    assert digest(path) == before and shape(path)[0] == 3

    with pytest.raises(MigrationPending):
        ExtractionStore(path)

    with pytest.raises(MigrationPending):
        ExtractionStore.open_for_write(path)

    assert digest(path) == before


def test_a_failed_explicit_migration_shows_as_migration_failed_and_the_next_one_clears_it(rig, monkeypatch):
    path = store_at(rig.locations.extraction_path(), 4)
    before = digest(path)
    good = dict(store_module.MIGRATIONS)
    monkeypatch.setattr(store_module, "MIGRATIONS", {**good, 5: good[5] + "\nTHIS IS NOT SQL;"})

    with pytest.raises(migration.MigrationAborted):
        upgrade(path)

    client = rig.client("alice")

    assert ExtractionStore.inspect(path).state == MIGRATION_FAILED and digest(path) == before
    assert client.get("/api/extractions/staleness").json()["store"]["state"] == MIGRATION_FAILED
    assert client.post("/api/extractions/run", json={"document": POD_A}).status_code == 503
    assert client.get("/api/extractions").status_code == 200                   # still readable: approvals are not hidden

    monkeypatch.setattr(store_module, "MIGRATIONS", good)
    upgrade(path)

    assert client.get("/api/extractions/staleness").json()["store"]["state"] == CURRENT


# =========================================================================
# the current schema behaves as before
# =========================================================================

def test_a_current_store_is_written_without_an_upgrade_attempt(rig, monkeypatch):
    _, approved, _, path = current_with_approval(rig)
    calls = []
    original = ExtractionStore.initialize
    monkeypatch.setattr(ExtractionStore, "initialize", lambda self: calls.append(1) or original(self))

    store = ExtractionStore.open_for_write(path)
    proposal = store.propose(BOB, 1, "delivered_pieces", "47", "The paper POD shows 47 pieces.", based_on=approved["id"])

    assert calls == [] and proposal["status"] == "pending" and store.verify_history()["ok"]


def test_absent_store_is_created_only_when_a_write_asks_for_it(tmp_path):
    path = tmp_path / "new.sqlite3"

    assert ExtractionStore.open_for_write(path) is None and not path.exists()
    assert ExtractionStore.open_for_write(path, create=True).version() == store_module.SCHEMA_VERSION and path.exists()
