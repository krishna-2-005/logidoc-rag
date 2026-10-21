"""
Tests for src/audit_history.py: audit snapshots and change detection.

Snapshots are built from real reconcile_shipment() results on
synthetic document chunks (the helpers of
test_reconciliation_consistency and test_audit_analytics: no PDFs, no
index, no Ollama). Every test uses its own temporary SQLite file.

Run from the project root:
    python -m pytest tests/test_audit_history.py
"""

import sqlite3
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
import audit_history as history  # noqa: E402


@pytest.fixture
def store(tmp_path):

    return history.HistoryStore(tmp_path / "history.sqlite3")


def record(store, chunks, **kwargs):
    """
    Audit the chunks and record a run.
    """

    shots = snapshot(chunks)

    return store.record_run(shots, analytics.build_exceptions(shots), **kwargs)


def latest(store, shipment_key):

    return store.timeline(shipment_key)[0]


def keys(items):

    return sorted(item["finding_key"].split("|")[1] for item in items)


A_KEY = "SHJ2026001"
B_KEY = "SHJ2026002"


# ============================================================
# FIRST SNAPSHOT AND IDENTICAL AUDITS
# ============================================================

def test_first_snapshot(store):

    run = record(store, documents(shipment=A))

    assert (run["shipments_checked"], run["snapshots_written"], run["written"]) == (1, 1, [A])

    entries = store.timeline(A_KEY)

    assert len(entries) == 1

    shot, changes = entries[0]["snapshot"], entries[0]["changes"]

    assert (shot["shipment_id"], shot["reconciled"], shot["final_status"]) == (A, 1, "DISCREPANCIES DETECTED")
    assert (shot["quantity_status"], shot["delivery_status"], shot["invoice_status"]) == ("MISMATCH", "DELAYED", "MISMATCH")
    assert (shot["bol_pieces"], shot["pod_pieces"], shot["piece_difference"]) == (48, 45, 3)
    assert (shot["expected_date"], shot["actual_date"], shot["days_late"]) == ("October 3, 2026", "October 5, 2026", 2)
    assert (shot["invoice_total"], shot["line_item_sum"], shot["invoice_difference"]) == (1550.0, 1430.0, 120.0)
    assert (shot["discrepancy_count"], shot["review_count"]) == (3, 0)
    assert {name: state["state"] for name, state in shot["audit_states"].items()} == {
        "quantity": "verified", "delivery": "verified", "invoice": "verified",
    }
    assert len(shot["input_fingerprint"]) == 64

    assert changes["first"] and keys(changes["new"]) == ["delivery", "invoice", "quantity"]
    assert sorted(item["category"] for item in shot["findings"]) == [
        "Delivery delay", "Invoice mismatch", "Quantity shortage",
    ]


def test_consecutive_identical_audits_write_no_duplicate_snapshot(store):

    first = record(store, documents(shipment=A))
    second = record(store, documents(shipment=A))
    third = record(store, documents(shipment=A))

    assert (second["snapshots_written"], second["unchanged"], second["written"]) == (0, [A], [])
    assert third["snapshots_written"] == 0

    entries = store.timeline(A_KEY)

    # One snapshot, confirmed by all three runs
    assert len(entries) == 1
    assert [run["run_id"] for run in entries[0]["snapshot"]["runs"]] == [
        first["run_id"], second["run_id"], third["run_id"],
    ]

    runs = store.runs()

    assert [(run["shipments_checked"], run["snapshots_written"]) for run in runs] == [(1, 0), (1, 0), (1, 1)]


# ============================================================
# CHANGES BETWEEN SNAPSHOTS
# ============================================================

def test_changed_invoice_total(store):

    record(store, documents(shipment=A))
    record(store, documents(shipment=A, total="$1,600.00"))

    entries = store.timeline(A_KEY)

    assert len(entries) == 2

    changes = entries[0]["changes"]

    assert [(item["finding_key"].split("|")[1], item["previous_difference"], item["difference"]) for item in changes["changed"]] == [
        ("invoice", 120.0, 170.0),
    ]
    assert keys(changes["persistent"]) == ["delivery", "quantity"]
    assert changes["new"] == [] and changes["resolved"] == []

    amounts = {item["field"]: (item["previous"], item["current"]) for item in changes["value_changes"]}

    assert amounts == {"invoice_total": (1550.0, 1600.0), "invoice_difference": (120.0, 170.0)}
    assert changes["input_changed"]          # the invoice document changed
    assert changes["status_change"] is None


def test_newly_appearing_discrepancy(store):

    record(store, clean(B))
    record(store, documents(shipment=B, delivered=45, actual="October 3, 2026", total="$1,430.00"))

    changes = latest(store, B_KEY)["changes"]

    assert keys(changes["new"]) == ["quantity"]
    assert changes["status_change"] == ("NO DISCREPANCIES", "DISCREPANCIES DETECTED")
    assert {item["field"] for item in changes["value_changes"]} >= {"pod_pieces", "piece_difference", "quantity_status"}


def test_disappearing_discrepancy_is_resolved(store):

    record(store, documents(shipment=A))
    record(store, documents(shipment=A, delivered=48, actual="October 3, 2026"))

    changes = latest(store, A_KEY)["changes"]

    assert keys(changes["resolved"]) == ["delivery", "quantity"]
    assert keys(changes["persistent"]) == ["invoice"]
    assert changes["unverified"] == []


def test_returning_discrepancy(store):

    record(store, documents(shipment=A))
    record(store, documents(shipment=A, delivered=48))      # shortage gone
    record(store, documents(shipment=A))                     # and back

    entries = store.timeline(A_KEY)

    assert [keys(entry["changes"]["resolved"]) for entry in reversed(entries)] == [[], ["quantity"], []]
    assert keys(entries[0]["changes"]["reappeared"]) == ["quantity"]
    assert entries[0]["changes"]["new"] == []


# ============================================================
# MISSING AND CONFLICTING VALUES
# ============================================================

def test_a_discrepancy_that_becomes_unverifiable_is_not_resolved(store):

    record(store, documents(shipment=A))
    record(store, drop(documents(shipment=A), "POD", "Number of Pieces Delivered"))

    entry = latest(store, A_KEY)
    shot, changes = entry["snapshot"], entry["changes"]

    # The shortage cannot be checked any more: unverified, not resolved
    assert keys(changes["unverified"]) == ["quantity"]
    assert "quantity" not in keys(changes["resolved"])
    assert keys(changes["new"]) == ["review-quantity"]

    assert shot["audit_states"]["quantity"]["state"] == "unavailable"
    assert "was not found" in shot["audit_states"]["quantity"]["reason"]
    assert shot["quantity_status"] == "NEEDS REVIEW"

    # Missing is stored as NULL, never as 0
    assert (shot["bol_pieces"], shot["pod_pieces"], shot["piece_difference"]) == (48, None, None)
    assert {item["field"]: (item["previous"], item["current"]) for item in changes["value_changes"]}["pod_pieces"] == (45, None)
    assert changes["state_changes"] == [{"audit": "quantity", "previous": "verified", "current": "unavailable"}]


def test_conflicting_values_are_recorded_as_conflicting(store):

    conflicting = clean(B) + [
        dict(item, text=item["text"].replace("Number of Pieces:\n48", "Number of Pieces:\n50"),
             metadata=dict(item["metadata"], source=f"BOL_{B}_revised.pdf"))
        for item in clean(B) if item["metadata"]["document_type"] == "BOL"
    ]

    record(store, conflicting)

    shot = latest(store, B_KEY)["snapshot"]

    assert shot["audit_states"]["quantity"]["state"] == "conflicting"
    assert shot["audit_states"]["delivery"]["state"] == "verified"
    assert shot["final_status"] == "NEEDS REVIEW"
    assert (shot["bol_pieces"], shot["piece_difference"]) == (None, None)
    assert [item["category"] for item in shot["findings"]] == ["Quantity needs review"]


def test_unreadable_values_need_review(store):

    record(store, edit(documents(shipment=A), "POD", "October 5, 2026", "10/05/2026"))

    shot = latest(store, A_KEY)["snapshot"]

    assert shot["audit_states"]["delivery"]["state"] == "review_required"
    assert "could not be read" in shot["audit_states"]["delivery"]["reason"]
    assert (shot["actual_date"], shot["days_late"]) == (None, None)


def test_a_shipment_that_cannot_be_reconciled(store):

    record(store, documents(shipment=A))
    record(store, [item for item in documents(shipment=A) if item["metadata"]["document_type"] != "POD"])

    entry = latest(store, A_KEY)
    shot, changes = entry["snapshot"], entry["changes"]

    assert shot["reconciled"] == 0
    assert shot["error"] == "Reconciliation requires BOL, Invoice and POD."
    assert shot["final_status"] is None and shot["invoice_total"] is None
    assert {state["state"] for state in shot["audit_states"].values()} == {"not_assessed"}

    # Nothing is resolved by the audit failing
    assert changes["resolved"] == []
    assert keys(changes["unverified"]) == ["delivery", "invoice", "quantity"]


# ============================================================
# INPUT VERSION
# ============================================================

def test_a_changed_document_is_detected_even_when_the_result_is_the_same(store):

    record(store, documents(shipment=A))

    # A remark added to the BOL changes no audited value
    run = record(store, edit(documents(shipment=A), "BOL", "Shipment Number:", "Remarks:\nhandle with care\n\nShipment Number:"))

    assert run["snapshots_written"] == 1

    entries = store.timeline(A_KEY)

    assert entries[0]["changes"]["input_changed"]
    assert entries[0]["changes"]["value_changes"] == []
    assert entries[0]["changes"]["new"] == entries[0]["changes"]["resolved"] == []
    assert entries[0]["snapshot"]["input_fingerprint"] != entries[1]["snapshot"]["input_fingerprint"]


def test_fingerprint_depends_on_the_shipments_documents_only(store):

    record(store, documents(shipment=A) + clean(B))

    before = {key: store.snapshots(key)[0]["input_fingerprint"] for key in (A_KEY, B_KEY)}

    run = record(store, documents(shipment=A) + edit(clean(B), "INVOICE", "Swift Lines", "Other Lines"))

    assert (run["written"], run["unchanged"]) == ([B], [A])
    assert store.snapshots(A_KEY)[0]["input_fingerprint"] == before[A_KEY]
    assert store.snapshots(B_KEY)[-1]["input_fingerprint"] != before[B_KEY]


# ============================================================
# ISOLATION
# ============================================================

def test_shipments_have_separate_histories(store):

    record(store, documents(shipment=A) + clean(B))
    record(store, documents(shipment=A, total="$1,600.00") + clean(B))

    assert len(store.snapshots(A_KEY)) == 2
    assert len(store.snapshots(B_KEY)) == 1
    assert store.shipments() == {A_KEY: A, B_KEY: B}

    for key, shipment in ((A_KEY, A), (B_KEY, B)):
        for entry in store.timeline(key):
            assert entry["snapshot"]["shipment_id"] == shipment
            assert all(item["finding_key"].startswith(key + "|") for item in entry["snapshot"]["findings"])

    assert store.snapshots("SHJ2026999") == []


# ============================================================
# PERSISTENCE, TRANSACTIONS, SAFETY
# ============================================================

def test_history_survives_reconnection(store):

    record(store, documents(shipment=A), label="before the carrier call")
    record(store, documents(shipment=A, total="$1,600.00"))

    reopened = history.HistoryStore(store.path)

    entries = reopened.timeline(A_KEY)

    assert len(entries) == 2
    assert entries[0]["snapshot"]["invoice_total"] == 1600.0
    assert entries[1]["snapshot"]["runs"][0]["label"] == "before the carrier call"
    assert [run["snapshots_written"] for run in reopened.runs()] == [1, 1]


def test_a_failed_run_is_rolled_back_completely(store, monkeypatch):

    record(store, documents(shipment=A))

    original = history.HistoryStore.insert_snapshot
    calls = []

    def fail_on_the_second(self, connection, run_id, rec, at):
        calls.append(rec["shipment_id"])
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return original(self, connection, run_id, rec, at)

    monkeypatch.setattr(history.HistoryStore, "insert_snapshot", fail_on_the_second)

    with pytest.raises(RuntimeError):
        # Both shipments changed: the first insert succeeds, the second fails
        record(store, documents(shipment=A, total="$1,600.00") + clean(B))

    monkeypatch.undo()

    assert len(store.runs()) == 1
    assert len(store.snapshots(A_KEY)) == 1
    assert store.snapshots(B_KEY) == []

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_shipments").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM snapshot_findings").fetchone()[0] == 3


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE shipment_snapshots SET final_status = 'NO DISCREPANCIES'",
        "DELETE FROM shipment_snapshots",
        "UPDATE snapshot_findings SET summary = 'x'",
        "DELETE FROM snapshot_findings",
        "UPDATE audit_runs SET label = 'x'",
        "DELETE FROM audit_runs",
        "UPDATE run_shipments SET snapshot_id = 1",
        "DELETE FROM run_shipments",
    ],
)
def test_old_snapshots_cannot_be_modified_or_deleted(store, statement):

    record(store, documents(shipment=A))

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)

    assert len(store.snapshots(A_KEY)) == 1


def test_no_document_text_is_stored(store):

    record(store, documents(shipment=A))

    content = store.path.read_bytes()

    for text in (b"BILL OF LADING", b"Shipment Number:", b"Chicago, Illinois", b"Swift Lines",
                 b"Number of Pieces:", b"BOL_SHJ-2026-001.pdf"):
        assert text not in content, text

    with sqlite3.connect(store.path) as connection:
        columns = {
            row[1]
            for table in ("shipment_snapshots", "snapshot_findings", "audit_runs")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }

    assert not columns & {"text", "evidence", "document", "content", "source", "pages"}


def test_schema_version_is_checked(store):

    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE schema_info SET version = 99")

    with pytest.raises(history.HistoryError, match="schema version 99"):
        history.HistoryStore(store.path)


def test_digest_covers_every_stored_value():

    shots = snapshot(documents(shipment=A))
    exceptions = analytics.build_exceptions(shots)

    first = history.snapshot_record(shots["shipments"][0], exceptions)
    again = history.snapshot_record(shots["shipments"][0], exceptions)

    assert first["digest"] == again["digest"]

    changed = dict(first, invoice_total=1600.0)

    assert history.digest(changed) != first["digest"]
    assert history.digest(dict(first, input_fingerprint="other")) != first["digest"]
    assert history.digest(dict(first, audit_states={})) != first["digest"]
