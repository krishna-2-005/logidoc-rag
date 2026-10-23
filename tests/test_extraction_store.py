"""
Tests for src/extraction_store.py and the extraction API routes: idempotent
storage, no document text, append-only reviewer corrections, permissions,
migrations with rollback, and the audit being unaffected.

Run: python -m pytest tests/test_extraction_store.py
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_extraction as de  # noqa: E402
from migration_support import upgrade  # noqa: E402
import extraction_store as store_module  # noqa: E402
from authz import PermissionDenied, Principal  # noqa: E402
from extraction_store import ExtractionStore, StoreError  # noqa: E402
from test_api import Rig  # noqa: E402

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
ADMIN = Principal("alice", "admin", True, 1)
ADMIN2 = Principal("dave", "admin", True, 4)
REVIEWER = Principal("bob", "reviewer", True, 2)
REASON = "The scanned total was misread."


@pytest.fixture
def store(tmp_path):
    return ExtractionStore(tmp_path / "x.sqlite3")


def invoice_a():
    return de.extract_document(FIXTURES / "shipment_SHJ-2026-001" / "INVOICE_SHJ-2026-001.pdf")


def saved(store):
    return store.save(invoice_a())[0]


# ---- storage ---------------------------------------------------------------------------

def test_the_same_file_and_extractor_version_is_stored_once(store):
    first = store.save(invoice_a())
    second = store.save(invoice_a())

    assert first[1] is True and second == (first[0], False) and len(store.list()) == 1


def test_a_new_extractor_version_adds_a_row_and_keeps_the_old_one(store):
    extraction = invoice_a()
    store.save(extraction)
    extraction.extractor_version = "9.9"
    _, created = store.save(extraction)

    assert created and {r["extractor_version"] for r in store.list()} == {de.EXTRACTOR_VERSION, "9.9"}


def test_the_stored_payload_has_typed_values_and_no_raw_text(store):
    record = store.row(saved(store))
    fields = record["payload"]["fields"]
    dumped = json.dumps(record["payload"])

    assert fields["invoice_total"]["value"] == 1550.0 and fields["invoice_total"]["page"] == 1
    assert all(f["raw_text"] is None for f in fields.values()) and all(i["raw_text"] is None for i in record["payload"]["line_items"])
    assert "INV-SHJ-2026-001" in dumps_ok(dumped) and "Base Freight:" not in dumped
    assert record["shipment_id"] == "SHJ-2026-001"


def dumps_ok(text):
    return text


def test_a_file_that_was_never_read_is_not_stored_but_a_damaged_one_is_recorded(store, tmp_path):
    broken = tmp_path / "b.pdf"
    broken.write_bytes(b"nope")

    with pytest.raises(StoreError):
        store.save(de.extract_document(tmp_path / "missing.pdf"))

    assert store.save(de.extract_document(broken))[1] is True
    assert store.list()[0]["status"] == "unreadable"


def test_list_filters_by_shipment_key(store):
    saved(store)
    store.save(de.extract_document(FIXTURES / "shipment_SHJ-2026-002" / "BOL_SHJ-2026-002.pdf"))

    assert len(store.list()) == 2 and [r["shipment_id"] for r in store.list("SHJ2026001")] == ["SHJ-2026-001"]


# ---- corrections -------------------------------------------------------------------------

def test_a_correction_is_typed_attributed_and_layered_over_the_extracted_value(store):
    extraction_id = saved(store)
    correction = store.propose(REVIEWER, extraction_id, "invoice_total", "$1,430.00", REASON, based_on=None)

    assert correction["corrected"] == 1430.0
    assert correction["previous"] == {"value": 1550.0, "state": "EXTRACTED", "source": "extracted"}
    assert correction["actor"] == "bob" and correction["authenticated"] is True and correction["reason"] == REASON

    assert correction["status"] == "pending"

    pending = store.effective(extraction_id)["invoice_total"]

    # A proposal changes nothing: the value is still the extracted one
    assert pending["value"] == 1550.0 and pending["source"] == "extracted" and pending["proposed"]["corrected"] == 1430.0

    store.decide(ADMIN, correction["id"], "approve")
    effective = store.effective(extraction_id)

    assert effective["invoice_total"]["value"] == 1430.0 and effective["invoice_total"]["source"] == "approved_correction"
    assert effective["invoice_total"]["page"] is None and effective["invoice_number"]["source"] == "extracted"
    assert effective["invoice_total"]["extracted"]["value"] == 1550.0 and effective["invoice_total"]["proposed"] is None
    assert store.row(extraction_id)["payload"]["fields"]["invoice_total"]["value"] == 1550.0


def test_a_missing_field_can_be_corrected_and_the_previous_state_is_kept(store):
    extraction_id = saved(store)
    correction = store.propose(ADMIN, extraction_id, "currency", "usd", REASON, based_on=None)

    assert correction["corrected"] == "USD"
    assert correction["previous"]["state"] == "MISSING" and correction["previous"]["value"] is None


def test_the_second_correction_records_the_first_as_previous(store):
    extraction_id = saved(store)
    first = store.propose(REVIEWER, extraction_id, "tax", "10.00", REASON, based_on=None)
    store.decide(ADMIN, first["id"], "approve")
    second = store.propose(REVIEWER, extraction_id, "tax", "12.50", REASON, based_on=first["id"])

    assert second["previous"]["value"] == 10.0 and second["previous"]["source"] == "approved_correction"
    assert store.effective(extraction_id)["tax"]["value"] == 10.0          # the second is only proposed
    store.decide(ADMIN, second["id"], "approve")
    assert store.effective(extraction_id)["tax"]["value"] == 12.5 and len(store.corrections(extraction_id)) == 2


@pytest.mark.parametrize("field, text", [("invoice_total", "lots"), ("invoice_date", "February 30, 2026"), ("currency", "ZZZ"),
                                         ("invoice_total", " "), ("nonsense", "1"), ("shipment_id", "")])
def test_an_invalid_correction_is_refused_and_nothing_is_recorded(store, field, text):
    extraction_id = saved(store)

    with pytest.raises(StoreError):
        store.propose(REVIEWER, extraction_id, field, text, REASON, based_on=None)

    assert store.corrections(extraction_id) == []


def test_reason_is_required_and_a_no_op_is_refused(store):
    extraction_id = saved(store)

    for reason in (None, "", "short", "x" * 1001):
        with pytest.raises(StoreError):
            store.propose(REVIEWER, extraction_id, "tax", "5", reason, based_on=None)

    with pytest.raises(StoreError):
        store.propose(REVIEWER, extraction_id, "invoice_total", "1550", REASON, based_on=None)

    with pytest.raises(StoreError):
        store.propose(REVIEWER, 999, "tax", "5", REASON, based_on=None)


def test_an_unsupported_field_cannot_be_corrected(store):
    pod = store.save(de.extract_document(FIXTURES / "shipment_SHJ-2026-001" / "POD_SHJ-2026-001.pdf"))[0]

    with pytest.raises(StoreError, match="not supported"):
        store.propose(REVIEWER, pod, "signature_image", "signed", REASON, based_on=None)


def test_only_a_signed_in_role_with_the_permission_can_correct(store):
    extraction_id = saved(store)

    for who in (None, Principal("eve", "root"), "bob"):
        with pytest.raises(PermissionDenied):
            store.propose(who, extraction_id, "tax", "5", REASON, based_on=None)

    assert store.corrections(extraction_id) == []


def test_corrections_are_append_only(store):
    extraction_id = saved(store)
    store.propose(REVIEWER, extraction_id, "tax", "5", REASON, based_on=None)

    with sqlite3.connect(store.path) as connection:
        for statement in ("UPDATE field_corrections SET reason = 'x'", "DELETE FROM field_corrections"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                connection.execute(statement)


# ---- schema versions ------------------------------------------------------------------------

def test_a_new_database_is_at_the_current_version(store):
    assert store.version() == store_module.SCHEMA_VERSION


def test_a_version_1_database_is_upgraded_without_losing_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")

    for statement in filter(None, (s.strip() for s in store_module.MIGRATIONS[1].split(";\n"))):
        connection.execute(statement)

    connection.execute("INSERT INTO schema_info VALUES (1)")
    connection.execute("INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, payload, created_at) "
                       "VALUES ('a.pdf', 'h', '0.1', 'BOL', 'extracted', '{}', 'then')")
    connection.commit()
    connection.close()

    with pytest.raises(store_module.MigrationPending):                       # a request never upgrades a file
        ExtractionStore(path)

    upgrade(path)
    upgraded = ExtractionStore(path)

    assert upgraded.version() == store_module.SCHEMA_VERSION and [r["filename"] for r in upgraded.list()] == ["a.pdf"] and upgraded.corrections(1) == []
    assert [a["relation"] for a in upgraded.aliases(1)] == ["original"]


def test_a_failed_creation_rolls_back_completely_and_the_next_start_retries(tmp_path, monkeypatch):
    path = tmp_path / "m.sqlite3"
    good = dict(store_module.MIGRATIONS)
    monkeypatch.setattr(store_module, "MIGRATIONS", {**good, 2: good[2] + "\nTHIS IS NOT SQL;"})

    with pytest.raises(sqlite3.DatabaseError):
        ExtractionStore(path)

    connection = sqlite3.connect(path)

    assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []       # one transaction: nothing was left behind
    connection.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", good)

    assert ExtractionStore(path).version() == store_module.SCHEMA_VERSION


def test_a_newer_database_is_refused(tmp_path):
    path = tmp_path / "n.sqlite3"
    ExtractionStore(path)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_info SET version = 99")
    connection.commit()
    connection.close()

    with pytest.raises(StoreError, match="newer"):
        ExtractionStore(path)


# ---- the API ----------------------------------------------------------------------------------

@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path, "required")


def first_document(rig):
    return sorted(rig.raw.glob("INVOICE_*.pdf"))[0].name


def test_the_api_runs_extraction_for_an_administrator_only(rig):
    name = first_document(rig)

    assert rig.client().post("/api/extractions/run", json={"document": name}).status_code == 401
    assert rig.client("bob").post("/api/extractions/run", json={"document": name}).status_code == 403

    reply = rig.client("alice").post("/api/extractions/run", json={"document": name})

    assert reply.status_code == 200 and reply.json()["created"] is True
    assert rig.client("alice").post("/api/extractions/run", json={"document": name}).json()["created"] is False


@pytest.mark.parametrize("name", ["../secrets.pdf", "nothing.pdf", "..\\x.pdf", "C:/Windows/win.ini"])
def test_the_api_takes_a_name_never_a_path(rig, name):
    assert rig.client("alice").post("/api/extractions/run", json={"document": name}).status_code == 404


def test_a_reviewer_reads_results_and_records_a_correction(rig):
    rig.client("alice").post("/api/extractions/run", json={"document": first_document(rig)})
    bob = rig.client("bob")

    listing = bob.get("/api/extractions").json()["extractions"]
    extraction_id = listing[0]["id"]

    assert listing[0]["correction_count"] == 0 and bob.get("/api/extractions?shipment_id=NOPE-1").json()["extractions"] == []

    reply = bob.post(f"/api/extractions/{extraction_id}/corrections", json={"field": "tax", "value": "9.50", "reason": REASON, "based_on": None})

    assert reply.status_code == 200 and reply.json()["actor"] == "bob"

    detail = bob.get(f"/api/extractions/{extraction_id}").json()

    assert detail["effective"]["tax"]["value"] is None and detail["effective"]["tax"]["proposed"]["corrected"] == 9.5
    assert detail["payload"]["fields"]["tax"]["state"] == "MISSING"
    assert detail["corrections"][0]["reason"] == REASON and detail["limitations"]


def test_the_api_refuses_bad_corrections_and_missing_sign_in_or_csrf(rig):
    rig.client("alice").post("/api/extractions/run", json={"document": first_document(rig)})
    body = {"field": "tax", "value": "9.50", "reason": REASON, "based_on": None}

    assert rig.client().post("/api/extractions/1/corrections", json=body).status_code == 401
    assert rig.client().get("/api/extractions").status_code == 401
    assert rig.client("bob").post("/api/extractions/1/corrections", json={**body, "value": "lots"}).status_code == 422
    assert rig.client("bob").post("/api/extractions/1/corrections", json={**body, "reason": "no"}).status_code == 422
    assert rig.client("bob").post("/api/extractions/77/corrections", json=body).status_code == 404

    bob = rig.client("bob")
    bob.headers.pop("X-LogiDoc-Client")

    assert bob.post("/api/extractions/1/corrections", json=body).status_code == 403


def test_corrections_do_not_change_the_audit(rig):
    rig.client("alice").post("/api/extractions/run", json={"document": first_document(rig)})
    bob = rig.client("bob")
    before = [bob.get("/api/shipments").json(), bob.get("/api/overview").json(), bob.get("/api/exposure").json()]
    files = {p.name: p.read_bytes() for p in (rig.locations.review_db, rig.locations.history_db) if p.exists()}

    bob.post("/api/extractions/1/corrections", json={"field": "invoice_total", "value": "1.00", "reason": REASON, "based_on": None})

    assert [bob.get("/api/shipments").json(), bob.get("/api/overview").json(), bob.get("/api/exposure").json()] == before
    assert files == {p.name: p.read_bytes() for p in (rig.locations.review_db, rig.locations.history_db) if p.exists()}
