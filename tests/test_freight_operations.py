"""
Freight audit operations: approved corrections in the freight audit,
reproducible recorded audits, the disputed and waived exception states,
the Exception Center data, and the review store migration behind them.

Synthetic PDFs in a temporary folder, through the real API; no network, no
model.

Run from the project root:
    python -m pytest tests/test_freight_operations.py
"""

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import freight_audit as fa
import review_store
from auth import UserStore
from authz import ADMIN, PermissionDenied, Principal
from exception_workflow import ExceptionWorkflow
from review_store import DISPUTED, IN_REVIEW, OPEN, RESOLVED, WAIVED, Policy, ReviewError, ReviewStore
from test_api import PASSWORD, Rig
from test_freight_audit import SHIPMENT, charge, invoice, rate, write_contracted_shipment

RATE_FILE = "RATE_CONFIRMATION_SHJ-2026-101.pdf"
FUEL_KEY = "SHJ2026101|freight-fuel-surcharge"
REASON = "The carrier's amended rate confirmation of 30 Sept agrees a fuel surcharge of $200."


# ============================================================
# THE APPLICATION
# ============================================================

@pytest.fixture
def rig(tmp_path):

    result = Rig(tmp_path, "required")
    UserStore(tmp_path / "auth.sqlite3").create_user("dave", PASSWORD, ADMIN)
    write_contracted_shipment(result.raw)
    result.state.service.invalidate()

    return result


def extract(rig, name):

    reply = rig.client("alice").post("/api/extractions/run", json={"document": name})
    assert reply.status_code == 200, reply.text

    return reply.json()["id"]


def correct_fuel(rig, value="200.00"):
    """bob proposes the rate confirmation's fuel surcharge, dave approves it."""

    extraction_id = extract(rig, RATE_FILE)

    proposal = rig.client("bob").post(f"/api/extractions/{extraction_id}/corrections",
                                      json={"field": "fuel_surcharge", "value": value, "reason": REASON, "based_on": None})
    assert proposal.status_code == 200, proposal.text

    decision = rig.client("dave").post(f"/api/extractions/corrections/{proposal.json()['id']}/decision",
                                       json={"decision": "approve", "note": "Checked against the amended rate confirmation."})
    assert decision.status_code == 200, decision.text

    return extraction_id, proposal.json()["id"], decision.json()


def freight(rig):

    return rig.client("alice").get(f"/api/shipments/{SHIPMENT}").json()["freight_audit"]


def test_an_approved_correction_becomes_the_value_the_freight_audit_uses(rig):

    before = freight(rig)
    assert charge(before, "Fuel surcharge")["outcome"] == fa.DISCREPANCY
    assert before["corrections_applied"] == []

    extraction_id, correction_id, decision = correct_fuel(rig)

    after = freight(rig)
    fuel = charge(after, "Fuel surcharge")

    assert (fuel["expected"], fuel["billed"], fuel["outcome"]) == (200.0, 200.0, fa.MATCHED)
    assert fuel["expected_source"] == {"file": RATE_FILE, "page": fuel["expected_source"]["page"],
                                       "document_type": "RATE_CONFIRMATION", "corrected": True}

    applied = after["corrections_applied"]
    assert len(applied) == 1
    assert applied[0] | {"decided_at": None} == {
        "correction_id": correction_id, "file": RATE_FILE, "document_type": "RATE_CONFIRMATION",
        "field": "fuel_surcharge", "original_state": "EXTRACTED", "original_value": 180.0, "corrected_value": 200.0,
        "proposed_by": "bob", "approved_by": "dave", "decided_at": None,
    }
    assert any(f"Approved correction #{correction_id} used" in reason for reason in after["explanation"])

    # What the audit read says so too, and the stored extraction is unchanged
    rate_input = next(item for item in after["inputs"] if item["file"] == RATE_FILE)
    assert rate_input["fields"]["fuel_surcharge"] == {"state": "EXTRACTED", "value": 200.0, "page": fuel["expected_source"]["page"],
                                                      "corrected": True}
    stored = rig.client("alice").get(f"/api/extractions/{extraction_id}").json()
    assert stored["payload"]["fields"]["fuel_surcharge"]["value"] == 180.0

    # The approval of a field the freight audit reads is recalculated like the reconciliation's
    assert decision["recompute"]["status"] in ("recorded", "no_recorded_history")


def test_a_correction_of_another_version_of_the_file_is_not_applied():

    corrected = rate()
    correction = {"correction_id": 7, "decision_id": 9, "filename": corrected.filename, "sha256": "another-version",
                  "field_name": "agreed_rate", "value": 1500.0, "approved_by": "dave"}

    extractions, applied, ignored = fa.apply_corrections([corrected], [correction])

    assert applied == [] and extractions[0] is corrected
    assert ignored == [{"correction_id": 7, "file": corrected.filename, "field": "agreed_rate",
                        "reason": "It was made on another version of the file; check the field again."}]


def test_the_latest_decision_of_a_field_wins_and_the_original_is_never_changed():

    original = rate()
    corrections = [
        {"correction_id": 1, "decision_id": 1, "filename": original.filename, "sha256": original.sha256,
         "field_name": "agreed_rate", "value": 1450.0, "approved_by": "dave"},
        {"correction_id": 2, "decision_id": 2, "filename": original.filename, "sha256": original.sha256,
         "field_name": "agreed_rate", "value": 1500.0, "approved_by": "dave"},
    ]

    extractions, applied, _ = fa.apply_corrections([original], corrections)

    assert extractions[0].value("agreed_rate") == 1500.0
    assert [item["correction_id"] for item in applied] == [2]
    assert original.value("agreed_rate") == 1400.0


def test_a_corrected_value_drives_charge_and_total_outcomes():

    documents = [rate(agreed_rate=1300.0), invoice()]
    correction = {"correction_id": 3, "decision_id": 3, "filename": "RATE_CONFIRMATION.pdf", "sha256": "RATE_CONFIRMATION.pdf",
                  "field_name": "agreed_rate", "value": 1400.0, "approved_by": "dave"}

    without = fa.audit_freight(documents, policy=fa.load_policy(""))
    with_it = fa.audit_freight(documents, policy=fa.load_policy(""), corrections=[correction])

    assert charge(without, "Base freight")["outcome"] == fa.DISCREPANCY
    assert charge(with_it, "Base freight")["outcome"] == fa.MATCHED
    assert charge(with_it, "Invoice total against the contract")["outcome"] == fa.MATCHED


# ============================================================
# REPRODUCIBLE RECORDED AUDITS
# ============================================================

def record(rig, label):

    reply = rig.client("alice").post("/api/history/runs", json={"label": label})
    assert reply.status_code == 201, reply.text

    return reply.json()


def recorded_freight(rig):

    timeline = rig.client("alice").get(f"/api/history/shipments/{SHIPMENT}").json()

    return [entry["snapshot"]["freight_audit"] for entry in reversed(timeline)]  # oldest first


def test_a_recorded_freight_audit_keeps_what_it_was_computed_from(rig):

    record(rig, "First run")

    (first,) = recorded_freight(rig)

    assert first["outcome"] == fa.DISCREPANCY
    assert first["audited_at"]
    assert {item["file"] for item in first["inputs"]} == {
        "BOL_SHJ-2026-101.pdf", "POD_SHJ-2026-101.pdf", "INVOICE_SHJ-2026-101.pdf", RATE_FILE}
    assert all(len(item["sha256"]) == 64 and item["extractor_version"] for item in first["inputs"])
    assert first["tolerances"] == {"default": {"absolute": 0.0, "percent": 0.0}}
    assert charge(first, "Fuel surcharge")["expected_source"]["file"] == RATE_FILE


def test_repeating_a_run_on_the_same_documents_writes_no_new_audit(rig):

    record(rig, "First run")
    second = record(rig, "Second run")

    assert SHIPMENT in second["unchanged"]
    assert len(recorded_freight(rig)) == 1


def test_a_new_audit_never_overwrites_the_one_recorded_before(rig):

    record(rig, "Before the correction")
    correct_fuel(rig)
    record(rig, "After the correction")

    first, second = recorded_freight(rig)

    # The earlier audit reopens exactly as it was
    assert charge(first, "Fuel surcharge")["outcome"] == fa.DISCREPANCY
    assert first["corrections_applied"] == []
    assert charge(second, "Fuel surcharge")["outcome"] == fa.MATCHED
    assert [item["field"] for item in second["corrections_applied"]] == ["fuel_surcharge"]


def test_recorded_freight_audits_cannot_be_changed(rig):

    record(rig, "First run")

    with sqlite3.connect(rig.locations.history_db) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE freight_audit_records SET outcome = 'MATCHED'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM freight_audit_records")


def test_shipments_without_a_contract_keep_their_recorded_history_unchanged(rig):

    record(rig, "First run")
    second = record(rig, "Second run")

    assert "SHJ-2026-001" in second["unchanged"]


# ============================================================
# DISPUTED AND WAIVED
# ============================================================

REVIEWER = Principal("bob", "reviewer", True, 2)
OTHER_REVIEWER = Principal("carol", "reviewer", True, 3)
ADMIN_ALICE = Principal("alice", "admin", True, 1)
ADMIN_DAVE = Principal("dave", "admin", True, 4)


def exception(key="SHJ2026101|freight-fuel-surcharge", signature="sig-1"):

    return {"exception_key": key, "shipment_key": "SHJ2026101", "shipment_id": SHIPMENT,
            "category": "Freight audit: Fuel surcharge", "signature": signature}


@pytest.fixture
def flow(tmp_path):

    store = ReviewStore(tmp_path / "reviews.sqlite3")
    store.sync([exception()])

    return ExceptionWorkflow(store)


def test_a_reviewer_disputes_with_a_reason_and_the_dispute_is_later_resolved(flow):

    item = exception()
    flow.start(REVIEWER, item)

    with pytest.raises(ReviewError, match="Disputing needs a note"):
        flow.dispute(REVIEWER, item, "")

    row = flow.dispute(REVIEWER, item, "Fuel billed $20 above the rate confirmation; credit requested.")

    assert row["status"] == DISPUTED and row["resolved_by"] is None
    assert "resolve" in flow.allowed_actions(REVIEWER, row)
    assert "dispute" not in flow.allowed_actions(REVIEWER, row)

    row = flow.resolve(REVIEWER, item, "Carrier issued credit memo CM-881 for $20.", ["Credit memo CM-881"])

    assert row["status"] == RESOLVED
    assert [event["event_type"] for event in flow.history(REVIEWER, item["exception_key"])] == [
        "detected", "start", "dispute", "resolve"]


def test_only_the_assignee_disputes_an_assigned_exception(flow):

    item = exception()
    flow.start(REVIEWER, item)

    with pytest.raises(PermissionDenied, match="assigned to bob"):
        flow.dispute(OTHER_REVIEWER, item, "Fuel billed above the contract.")


def test_waiving_is_an_administrators_financial_decision_with_its_reason(flow):

    item = exception()

    with pytest.raises(PermissionDenied):
        flow.waive(REVIEWER, item, "Accept the $20 difference.")
    assert "waive" not in flow.allowed_actions(REVIEWER, flow.current(item))

    with pytest.raises(ReviewError, match="Waiving needs a note"):
        flow.waive(ADMIN_ALICE, item, "")

    row = flow.waive(ADMIN_ALICE, item, "Accepted: below the $25 write-off threshold agreed with finance.")

    assert row["status"] == WAIVED
    assert row["resolved_by"] == "alice" and row["resolved_at"]
    assert row["note"].startswith("Accepted")
    assert "reopen" in flow.allowed_actions(ADMIN_ALICE, row)


def test_a_disputed_exception_can_be_waived(flow):

    item = exception()
    flow.dispute(REVIEWER, item, "Fuel billed above the contract; credit requested.")

    assert flow.waive(ADMIN_ALICE, item, "Carrier refused the credit; the amount is accepted.")["status"] == WAIVED


def test_a_waiver_waits_for_approval_when_approval_is_on(tmp_path):

    store = ReviewStore(tmp_path / "reviews.sqlite3", policy=Policy(require_approval=True))
    store.sync([exception()])
    flow = ExceptionWorkflow(store)

    row = flow.waive(ADMIN_ALICE, exception(), "Accepted below the write-off threshold.")

    assert row["approval_state"] == "pending"
    with pytest.raises(ReviewError, match="cannot approve"):
        flow.approve(ADMIN_ALICE, exception())
    assert flow.approve(ADMIN_DAVE, exception())["approval_state"] == "approved"


def test_impossible_decisions_are_refused(flow):

    item = exception()
    flow.start(REVIEWER, item)
    flow.resolve(REVIEWER, item, "Credit memo received.", ["Credit memo CM-1"])

    with pytest.raises(ReviewError, match="cannot be disputed"):
        flow.dispute(REVIEWER, item, "Too late to dispute.")
    with pytest.raises(ReviewError, match="cannot be waived"):
        flow.waive(ADMIN_ALICE, item, "Too late to waive.")


def test_a_changed_audit_reopens_a_waived_exception_but_not_a_disputed_one(tmp_path):

    store = ReviewStore(tmp_path / "reviews.sqlite3")
    flow = ExceptionWorkflow(store)
    waived, disputed = exception("A|freight-fuel-surcharge"), exception("B|freight-detention-charge")
    store.sync([waived, disputed])

    flow.waive(ADMIN_ALICE, waived, "Accepted below the write-off threshold.")
    flow.dispute(REVIEWER, disputed, "Detention not supported by the POD.")

    rows = store.sync([exception("A|freight-fuel-surcharge", "sig-2"), exception("B|freight-detention-charge", "sig-2")])

    assert rows["A|freight-fuel-surcharge"]["status"] == OPEN
    assert rows["B|freight-detention-charge"]["status"] == DISPUTED
    assert rows["B|freight-detention-charge"]["needs_recheck"] == 1


def test_the_same_unresolved_issue_is_one_exception(flow):

    for _ in range(3):
        flow.store.sync([exception()])

    assert list(flow.store.states()) == ["SHJ2026101|freight-fuel-surcharge"]
    assert [event["event_type"] for event in flow.history(REVIEWER, "SHJ2026101|freight-fuel-surcharge")] == ["detected"]


# ============================================================
# THE REVIEW STORE MIGRATION (version 2 -> 3)
# ============================================================

def version_2_database(path):
    """A review database as version 2 wrote it, with one dismissed exception."""

    with sqlite3.connect(path) as connection:
        connection.executescript(review_store.SCHEMA_V1)
        connection.execute("INSERT INTO schema_info (version) VALUES (2)")

        for statement in review_store.MIGRATION_V2:
            connection.execute(statement)

        connection.execute(
            "INSERT INTO exception_reviews (exception_key, shipment_key, shipment_id, category, status, signature, "
            "note, created_at, updated_at, assignee, evidence_refs) VALUES ('K|invoice', 'K', 'K-1', 'Invoice mismatch', "
            "'dismissed', 'sig', 'Duplicate of a corrected invoice.', '2026-10-01T00:00:00+00:00', "
            "'2026-10-02T00:00:00+00:00', 'bob', '[\"memo 1\"]')"
        )
        connection.execute(
            "INSERT INTO review_events (exception_key, from_status, to_status, note, signature, created_at, event_type, actor) "
            "VALUES ('K|invoice', 'open', 'dismissed', 'Duplicate of a corrected invoice.', 'sig', '2026-10-02T00:00:00+00:00', 'dismiss', 'bob')"
        )


def test_a_version_2_database_is_rebuilt_keeping_every_row_and_backed_up_first(tmp_path):

    path = tmp_path / "audit_reviews.sqlite3"
    version_2_database(path)

    store = ReviewStore(path)

    row = store.states()["K|invoice"]
    assert (row["status"], row["note"], row["assignee"], row["evidence_refs"]) == (
        "dismissed", "Duplicate of a corrected invoice.", "bob", ["memo 1"])
    assert [event["event_type"] for event in store.history("K|invoice")] == ["dismiss"]

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 3
        # The new states are allowed, and still need their reason
        connection.execute("UPDATE exception_reviews SET status = 'waived' WHERE exception_key = 'K|invoice'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE exception_reviews SET status = 'disputed', note = '' WHERE exception_key = 'K|invoice'")

    backup = tmp_path / "audit_reviews.before-v3.sqlite3"
    assert backup.is_file()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 2
        assert connection.execute("SELECT status FROM exception_reviews").fetchone()[0] == "dismissed"


def test_a_new_database_needs_no_backup(tmp_path):

    ReviewStore(tmp_path / "audit_reviews.sqlite3")

    assert not (tmp_path / "audit_reviews.before-v3.sqlite3").exists()


# ============================================================
# EXCEPTION CENTER DATA
# ============================================================

def queue(rig, user="alice", **params):

    reply = rig.client(user).get("/api/exceptions", params=params)
    assert reply.status_code == 200, reply.text

    return {row["exception_key"]: row for row in reply.json()["exceptions"]}


def test_the_queue_carries_impact_action_and_evidence(rig):

    rows = queue(rig)
    fuel = rows[FUEL_KEY]

    assert fuel["severity"] == "HIGH" and fuel["category"] == "Freight audit: Fuel surcharge"
    assert fuel["financial_impact"] == {"amount": 20.0, "currency": None, "basis": "billed above the contract"}
    assert fuel["recommended_action"].startswith("Dispute or short-pay")
    assert fuel["audit_outcome"] == fa.DISCREPANCY
    assert any(RATE_FILE in reference for reference in fuel["evidence"])
    assert fuel["invoice_numbers"] == ["INV-101"]

    # The sample invoice mismatch: the amount in question is the arithmetic difference
    sample = rows["SHJ2026001|invoice"]
    assert sample["financial_impact"]["amount"] == pytest.approx(abs(sample_difference(rig)))
    assert sample["recommended_action"]

    # A quantity shortage has no price: no impact is invented
    assert rows["SHJ2026001|quantity"]["financial_impact"] is None


def sample_difference(rig):

    return rig.client("alice").get("/api/shipments/SHJ-2026-001").json()["result"]["invoice"]["difference"]


def test_the_queue_filters_by_severity_and_finds_invoice_numbers(rig):

    assert set(queue(rig, severity=["MEDIUM"])) and all(
        row["severity"] == "MEDIUM" for row in queue(rig, severity=["MEDIUM"]).values())
    assert {row["shipment_id"] for row in queue(rig, search="inv-101").values()} == {SHIPMENT}
    assert queue(rig, search="no-such-invoice") == {}


def test_disputing_and_waiving_through_the_api(rig):

    rows = queue(rig, "bob")
    assert "dispute" in rows[FUEL_KEY]["allowed_actions"]
    assert "waive" not in rows[FUEL_KEY]["allowed_actions"]

    url = f"/api/exceptions/{FUEL_KEY}/actions"

    assert rig.client("bob").post(url, json={"action": "waive", "note": "Accept the difference."}).status_code == 403
    assert rig.client("bob").post(url, json={"action": "dispute", "note": ""}).status_code == 422

    disputed = rig.client("bob").post(url, json={"action": "dispute", "note": "Fuel above contract; credit requested.",
                                                 "evidence": ["Rate confirmation p.1"]})
    assert disputed.status_code == 200 and disputed.json()["review"]["status"] == DISPUTED
    assert queue(rig, status=["disputed"]).keys() == {FUEL_KEY}

    waived = rig.client("alice").post(url, json={"action": "waive", "note": "Carrier declined; amount accepted by finance."})
    assert waived.status_code == 200 and waived.json()["review"]["status"] == WAIVED

    history = rig.client("alice").get(f"/api/exceptions/{FUEL_KEY}/history").json()
    assert [event["event_type"] for event in history] == ["detected", "dispute", "waive"]
    assert history[-1]["actor"] == "alice" and history[-1]["note"].startswith("Carrier declined")


def test_the_legacy_quick_form_asks_for_a_reason_before_disputing_or_waiving(tmp_path):
    """Streamlit's local-mode form uses set_status: a missing reason is a message, never a database error."""

    store = ReviewStore(tmp_path / "reviews.sqlite3")
    store.sync([exception()])

    for status, message in ((DISPUTED, "A dispute needs a reason"), (WAIVED, "A waiver needs a reason")):
        with pytest.raises(ReviewError, match=message):
            store.set_status(exception(), status, reviewer="Pat", note="  ")

    assert store.set_status(exception(), DISPUTED, reviewer="Pat", note="Raised with the carrier.")["status"] == DISPUTED
    assert store.set_status(exception(), WAIVED, reviewer="Pat", note="Accepted by finance.")["status"] == WAIVED


def test_one_audit_reads_each_shipments_corrections_once(rig, monkeypatch):
    """
    A decision committed while an audit is computed must reach both the
    reconciliation and the freight audit, or neither: the corrections are read
    once per shipment. Here every read after the first sees a newly approved
    correction, as if it were approved in between.
    """

    import extraction_store

    correction = {"correction_id": 99, "decision_id": 99, "extraction_id": 1, "extractor_version": "x",
                  "sha256": "x", "filename": RATE_FILE, "document_type": "RATE_CONFIRMATION",
                  "field_name": "fuel_surcharge", "value": 200.0, "proposed_by": "bob", "approved_by": "dave",
                  "decided_at": "now"}
    calls = {}

    def approved_corrections(self, key):
        calls[key] = calls.get(key, 0) + 1
        return [] if calls[key] == 1 else [correction]

    extract(rig, RATE_FILE)      # the extraction store exists
    monkeypatch.setattr(extraction_store.ExtractionStore, "approved_corrections", approved_corrections)
    rig.state.service.invalidate()

    shipment = rig.state.service.shipment(SHIPMENT)

    assert set(calls.values()) == {1}
    assert shipment["freight_audit"]["corrections_applied"] == []
    assert shipment["freight_audit"]["corrections_ignored"] == []
    assert shipment["inputs"]["applied_correction_ids"] == []
