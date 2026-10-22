"""
Tests for the exception workflow: src/review_store.py (schema version 2,
lifecycle rules, approval, migration, history integrity) and
src/exception_workflow.py (roles).

Every test uses its own temporary SQLite file; the real review database
is never opened (tests/conftest.py checks it).

Run from the project root:
    python -m pytest tests/test_exception_workflow.py
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import review_store
from authz import ADMIN, REVIEWER, PermissionDenied, Principal
from exception_workflow import ExceptionWorkflow
from review_store import (
    DISMISSED, IN_REVIEW, OPEN, RESOLVED, Policy, ReviewError, ReviewStore,
)


ALICE = Principal("alice", ADMIN, True, 1)
BOB = Principal("bob", REVIEWER, True, 2)
CAROL = Principal("carol", REVIEWER, True, 3)
LOCAL = Principal("local-dev", ADMIN, False)

NOTE = "Checked the carrier invoice against the rate sheet."
EVIDENCE = ["INVOICE_SHJ-2026-001.pdf p.1"]


def exception(code="invoice", signature="s1", shipment="SHJ-2026-001", category="Invoice mismatch"):

    return {
        "exception_key": f"{shipment.replace('-', '')}|{code}",
        "shipment_key": shipment.replace("-", ""),
        "shipment_id": shipment,
        "category": category,
        "signature": signature,
    }


@pytest.fixture
def store(tmp_path):

    return ReviewStore(tmp_path / "reviews.sqlite3")


@pytest.fixture
def flow(store):

    store.sync([exception()])

    return ExceptionWorkflow(store)


def row(store, item=None):

    return store.states()[(item or exception())["exception_key"]]


def start_review(flow, principal=BOB):

    return flow.start(principal, exception())


# ------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------

def test_full_lifecycle_records_every_actor(flow, store):

    flow.assign(ALICE, exception(), "bob")
    flow.start(BOB, exception())
    flow.add_note(BOB, exception(), "Called the carrier.", ["call log 12"])
    flow.resolve(BOB, exception(), NOTE, EVIDENCE)

    final = row(store)

    assert final["status"] == RESOLVED
    assert final["assignee"] == "bob"
    assert final["resolved_by"] == "bob"
    assert final["evidence_refs"] == ["call log 12"] + EVIDENCE

    events = store.history(exception()["exception_key"])

    assert [event["event_type"] for event in events] == [
        "detected", "assign", "start", "note", "resolve",
    ]
    assert [event["actor"] for event in events] == ["system", "alice", "bob", "bob", "bob"]
    assert all(event["authenticated"] == 1 for event in events[1:])
    assert events[1]["actor_role"] == ADMIN and events[2]["actor_role"] == REVIEWER
    assert all(event["created_at"] for event in events)


def test_starting_assigns_the_exception_to_the_person_starting(flow, store):

    flow.start(BOB, exception())

    assert row(store)["assignee"] == "bob"
    assert row(store)["status"] == IN_REVIEW


def test_reopening_returns_to_open_and_the_history_keeps_everything(flow, store):

    start_review(flow)
    flow.resolve(BOB, exception(), NOTE, EVIDENCE)
    flow.reopen(CAROL, exception(), "The carrier re-billed this shipment.")

    assert row(store)["status"] == OPEN
    assert row(store)["resolved_by"] is None
    assert len(store.history(exception()["exception_key"])) == 4


def test_a_dismissal_by_an_administrator_needs_a_reason(flow, store):

    with pytest.raises(ReviewError, match="note of at least"):
        flow.dismiss(ALICE, exception(), "")

    with pytest.raises(ReviewError, match="note of at least"):
        flow.dismiss(ALICE, exception(), "too short")

    flow.dismiss(ALICE, exception(), "Duplicate of the already corrected invoice.")

    assert row(store)["status"] == DISMISSED


# ------------------------------------------------------------
# Rules
# ------------------------------------------------------------

def test_resolution_needs_a_note_and_evidence(flow, store):

    start_review(flow)

    with pytest.raises(ReviewError, match="note"):
        flow.resolve(BOB, exception(), "", EVIDENCE)

    with pytest.raises(ReviewError, match="evidence"):
        flow.resolve(BOB, exception(), NOTE, [])

    with pytest.raises(ReviewError, match="evidence"):
        flow.resolve(BOB, exception(), NOTE, ["   ", ""])

    assert row(store)["status"] == IN_REVIEW


def test_evidence_added_earlier_counts_for_the_resolution(flow, store):

    start_review(flow)
    flow.add_note(BOB, exception(), evidence=["credit memo CM-77"])
    flow.resolve(BOB, exception(), NOTE, [])

    assert row(store)["evidence_refs"] == ["credit memo CM-77"]


def test_evidence_can_be_made_optional_by_the_policy(tmp_path):

    store = ReviewStore(tmp_path / "r.sqlite3", Policy(resolve_needs_evidence=False))
    store.sync([exception()])
    flow = ExceptionWorkflow(store)

    flow.start(BOB, exception())
    flow.resolve(BOB, exception(), NOTE, [])

    assert row(store)["status"] == RESOLVED


def test_only_an_exception_in_review_can_be_resolved(flow, store):

    with pytest.raises(ReviewError, match="start the review first"):
        flow.resolve(BOB, exception(), NOTE, EVIDENCE)


@pytest.mark.parametrize("call", ["start_twice", "reopen_open", "approve_unpending", "assign_resolved"])
def test_invalid_transitions_are_refused_and_change_nothing(flow, store, call):

    start_review(flow)

    if call == "start_twice":
        action = lambda: flow.start(BOB, exception())
    elif call == "reopen_open":
        action = lambda: flow.reopen(BOB, exception(), "Reopening something that is open.")
    elif call == "approve_unpending":
        action = lambda: flow.approve(ALICE, exception())
    else:
        flow.resolve(BOB, exception(), NOTE, EVIDENCE)
        action = lambda: flow.assign(ALICE, exception(), "carol")

    before = (row(store), len(store.history(exception()["exception_key"])))

    with pytest.raises(ReviewError):
        action()

    assert (row(store), len(store.history(exception()["exception_key"]))) == before


def test_a_note_needs_text_or_evidence(flow):

    with pytest.raises(ReviewError, match="text or an evidence"):
        flow.add_note(BOB, exception(), "  ", [])


def test_long_notes_and_bad_evidence_are_refused(flow):

    with pytest.raises(ReviewError, match="too long"):
        flow.add_note(BOB, exception(), "x" * 2001)

    with pytest.raises(ReviewError, match="evidence"):
        flow.add_note(BOB, exception(), evidence=["y" * 301])

    with pytest.raises(ReviewError, match="control"):
        flow.add_note(BOB, exception(), evidence=["line\x00break"])

    with pytest.raises(ReviewError, match="At most"):
        flow.add_note(BOB, exception(), evidence=[f"ref {n}" for n in range(11)])


def test_a_changed_audit_blocks_a_stale_change(flow, store):

    stale = exception(signature="old")

    with pytest.raises(ReviewError, match="changed"):
        flow.start(BOB, stale)

    assert row(store)["status"] == OPEN


def test_an_unrecorded_exception_cannot_be_changed(flow):

    with pytest.raises(ReviewError, match="not been recorded"):
        flow.start(BOB, exception("quantity"))


# ------------------------------------------------------------
# Permissions
# ------------------------------------------------------------

def test_a_reviewer_cannot_dismiss_approve_or_assign_to_others(flow, store):

    with pytest.raises(PermissionDenied):
        flow.dismiss(BOB, exception(), "A reason that is long enough.")

    with pytest.raises(PermissionDenied):
        flow.assign(BOB, exception(), "carol")

    with pytest.raises(PermissionDenied):
        flow.approve(BOB, exception())

    with pytest.raises(PermissionDenied):
        flow.reject(BOB, exception(), "Not convincing enough.")

    with pytest.raises(PermissionDenied):
        flow.verify_history(BOB)

    assert row(store)["status"] == OPEN
    assert len(store.history(exception()["exception_key"])) == 1


def test_a_reviewer_can_assign_to_themself(flow, store):

    flow.assign(BOB, exception(), "bob")

    assert row(store)["assignee"] == "bob"


def test_someone_elses_exception_is_not_theirs_to_start_or_resolve(flow, store):

    flow.assign(ALICE, exception(), "bob")

    with pytest.raises(PermissionDenied, match="assigned to bob"):
        flow.start(CAROL, exception())

    flow.start(BOB, exception())

    with pytest.raises(PermissionDenied, match="assigned to bob"):
        flow.resolve(CAROL, exception(), NOTE, EVIDENCE)

    # An administrator may take over
    flow.resolve(ALICE, exception(), NOTE, EVIDENCE)
    assert row(store)["resolved_by"] == "alice"


def test_nobody_without_a_principal_can_change_anything(flow):

    for action in (
        lambda: flow.start(None, exception()),
        lambda: flow.resolve(None, exception(), NOTE, EVIDENCE),
        lambda: flow.queue(None),
    ):
        with pytest.raises(PermissionDenied):
            action()


def test_assignment_can_be_limited_to_active_users(store):

    store.sync([exception()])
    flow = ExceptionWorkflow(store, assignable=lambda: ["alice", "bob"])

    with pytest.raises(ReviewError, match="not an active user"):
        flow.assign(ALICE, exception(), "mallory")

    flow.assign(ALICE, exception(), "bob")


def test_allowed_actions_follow_the_state_and_the_role(flow, store):

    assert "dismiss" not in flow.allowed_actions(BOB, row(store))
    assert "dismiss" in flow.allowed_actions(ALICE, row(store))
    assert "start" in flow.allowed_actions(BOB, row(store))
    assert "resolve" not in flow.allowed_actions(BOB, row(store))

    start_review(flow)

    assert "resolve" in flow.allowed_actions(BOB, row(store))
    assert "resolve" not in flow.allowed_actions(CAROL, row(store))
    assert flow.allowed_actions(None, row(store)) == []


# ------------------------------------------------------------
# Approval
# ------------------------------------------------------------

@pytest.fixture
def approving(tmp_path):

    store = ReviewStore(tmp_path / "r.sqlite3", Policy(require_approval=True))
    store.sync([exception()])

    return ExceptionWorkflow(store), store


def test_a_resolution_waits_for_approval_and_an_administrator_approves(approving):

    flow, store = approving
    flow.start(BOB, exception())
    flow.resolve(BOB, exception(), NOTE, EVIDENCE)

    assert row(store)["approval_state"] == "pending"
    assert [r["exception_key"] for r in flow.queue(ALICE, pending_approval=True)]

    flow.approve(ALICE, exception(), "Evidence checked.")

    assert row(store)["approval_state"] == "approved"
    assert row(store)["approval_by"] == "alice"
    assert row(store)["status"] == RESOLVED


def test_a_rejection_returns_the_exception_to_review(approving):

    flow, store = approving
    flow.start(BOB, exception())
    flow.resolve(BOB, exception(), NOTE, EVIDENCE)

    with pytest.raises(ReviewError, match="note"):
        flow.reject(ALICE, exception(), "")

    flow.reject(ALICE, exception(), "The credit memo does not match the invoice.")

    assert row(store)["status"] == IN_REVIEW
    assert row(store)["approval_state"] == "rejected"
    assert row(store)["resolved_by"] is None


def test_the_resolver_cannot_approve_their_own_resolution(approving):

    flow, store = approving
    admin_two = Principal("dana", ADMIN, True, 4)

    flow.start(ALICE, exception())
    flow.resolve(ALICE, exception(), NOTE, EVIDENCE)

    with pytest.raises(ReviewError, match="cannot approve"):
        flow.approve(ALICE, exception())

    flow.approve(admin_two, exception())
    assert row(store)["approval_state"] == "approved"


def test_a_local_development_user_can_approve_their_own_work(approving):

    flow, store = approving
    flow.start(LOCAL, exception())
    flow.resolve(LOCAL, exception(), NOTE, EVIDENCE)
    flow.approve(LOCAL, exception())

    event = store.history(exception()["exception_key"])[-1]

    assert row(store)["approval_state"] == "approved"
    assert event["actor"] == "local-dev" and event["authenticated"] == 0


def test_a_dismissal_also_waits_for_approval(approving):

    flow, store = approving
    flow.dismiss(ALICE, exception(), "Duplicate of an already corrected invoice.")

    assert row(store)["approval_state"] == "pending"


# ------------------------------------------------------------
# Separation from the audit
# ------------------------------------------------------------

def test_the_workflow_never_touches_the_audit_values(flow, store):

    before = {k: row(store)[k] for k in ("shipment_id", "category", "signature")}

    start_review(flow)
    flow.resolve(BOB, exception(), NOTE, EVIDENCE)

    assert {k: row(store)[k] for k in before} == before


def test_a_resolved_exception_is_reopened_when_the_audit_changes(flow, store):

    start_review(flow)
    flow.resolve(BOB, exception(), NOTE, EVIDENCE)

    store.sync([exception(signature="s2")])

    assert row(store)["status"] == OPEN
    assert row(store)["needs_recheck"] == 1
    assert store.history(exception()["exception_key"])[-1]["event_type"] == "audit_reopened"


# ------------------------------------------------------------
# Search and filters
# ------------------------------------------------------------

def test_filters_by_assignee_status_category_age_and_search(store):

    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    old = (now - timedelta(days=40)).isoformat(timespec="seconds")
    new = (now - timedelta(days=2)).isoformat(timespec="seconds")

    store.sync([exception("invoice", shipment="SHJ-2026-001")], at=old)
    store.sync([exception("quantity", shipment="SHJ-2026-002", category="Quantity shortage")], at=new)
    flow = ExceptionWorkflow(store)

    flow.start(BOB, exception("quantity", shipment="SHJ-2026-002", category="Quantity shortage"))

    def keys(**filters):
        return {r["shipment_id"] for r in flow.queue(ALICE, now=now, **filters)}

    assert keys() == {"SHJ-2026-001", "SHJ-2026-002"}
    assert keys(assignee="BOB") == {"SHJ-2026-002"}
    assert keys(unassigned=True) == {"SHJ-2026-001"}
    assert keys(statuses=[IN_REVIEW]) == {"SHJ-2026-002"}
    assert keys(categories=["Invoice mismatch"]) == {"SHJ-2026-001"}
    assert keys(min_age_days=30) == {"SHJ-2026-001"}
    assert keys(max_age_days=7) == {"SHJ-2026-002"}
    assert keys(search="2026-002") == {"SHJ-2026-002"}
    assert keys(search="nothing") == set()
    assert keys(search="%") == set()

    ages = {r["shipment_id"]: r["age_days"] for r in flow.queue(ALICE, now=now)}
    assert ages == {"SHJ-2026-001": 40, "SHJ-2026-002": 2}


def test_a_filter_value_is_never_executed_as_sql(flow):

    assert flow.queue(ALICE, assignee="x' OR '1'='1") == []
    assert flow.queue(ALICE, search="'; DROP TABLE exception_reviews; --") == []
    assert flow.queue(ALICE)


# ------------------------------------------------------------
# Transactions and persistence
# ------------------------------------------------------------

def test_a_failed_change_rolls_back_the_state_and_the_event(flow, store, monkeypatch):

    start_review(flow)

    original = ReviewStore.record_event

    def failing(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ReviewStore, "record_event", failing)

    with pytest.raises(sqlite3.OperationalError):
        flow.resolve(BOB, exception(), NOTE, EVIDENCE)

    monkeypatch.setattr(ReviewStore, "record_event", original)

    assert row(store)["status"] == IN_REVIEW
    assert row(store)["resolved_by"] is None
    assert len(store.history(exception()["exception_key"])) == 2
    assert store.verify_integrity()["ok"]


def test_state_and_reviewer_identity_persist_across_stores(tmp_path):

    path = tmp_path / "r.sqlite3"
    first = ReviewStore(path)
    first.sync([exception()])
    ExceptionWorkflow(first).start(BOB, exception())

    second = ReviewStore(path)
    stored = second.states()[exception()["exception_key"]]

    assert stored["status"] == IN_REVIEW and stored["assignee"] == "bob"
    assert second.history(exception()["exception_key"])[-1]["actor"] == "bob"


def test_sql_in_names_and_notes_is_stored_as_text(flow, store):

    start_review(flow)
    flow.add_note(BOB, exception(), "x'); DROP TABLE exception_reviews; --")

    assert row(store)["note"].startswith("x')")
    assert store.states()


# ------------------------------------------------------------
# History integrity
# ------------------------------------------------------------

def test_history_cannot_be_updated_or_deleted(flow, store):

    start_review(flow)

    with sqlite3.connect(store.path) as connection:

        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE review_events SET note = 'edited'")

        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM review_events")


def test_the_hash_chain_detects_an_edit_made_by_removing_the_trigger(flow, store):

    start_review(flow)
    flow.add_note(BOB, exception(), NOTE)

    assert store.verify_integrity() == {"ok": True, "checked": 3, "legacy_events": 0, "problems": []}

    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER review_events_no_update")
        connection.execute("UPDATE review_events SET actor = 'mallory' WHERE id = 2")

    report = ExceptionWorkflow(store).verify_history(ALICE)

    assert not report["ok"]
    assert any("Event 2" in problem for problem in report["problems"])


def test_the_hash_chain_detects_a_removed_event(flow, store):

    start_review(flow)
    flow.add_note(BOB, exception(), NOTE)

    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER review_events_no_delete")
        connection.execute("DELETE FROM review_events WHERE id = 2")

    assert not store.verify_integrity()["ok"]


# ------------------------------------------------------------
# Migration from schema version 1
# ------------------------------------------------------------

def make_v1_database(path):

    connection = sqlite3.connect(path)
    connection.executescript(review_store.SCHEMA_V1)
    connection.execute("INSERT INTO schema_info (version) VALUES (1)")

    connection.execute(
        "INSERT INTO exception_reviews (exception_key, shipment_key, shipment_id, category, "
        "status, signature, reviewer, note, created_at, updated_at) "
        "VALUES ('K|invoice', 'K', 'SHJ-K', 'Invoice mismatch', 'resolved', 'sig', 'Pat', 'done', "
        "'2025-01-01T00:00:00+00:00', '2025-01-02T00:00:00+00:00')"
    )

    connection.execute(
        "INSERT INTO review_events (exception_key, from_status, to_status, reviewer, note, signature, "
        "created_at) VALUES ('K|invoice', 'open', 'resolved', 'Pat', 'done', 'sig', '2025-01-02T00:00:00+00:00')"
    )

    connection.commit()
    connection.close()


def test_a_version_1_database_is_migrated_and_keeps_its_data(tmp_path):

    path = tmp_path / "old.sqlite3"
    make_v1_database(path)

    store = ReviewStore(path)

    stored = store.states()["K|invoice"]
    event = store.history("K|invoice")[0]

    assert stored["status"] == "resolved" and stored["reviewer"] == "Pat" and stored["evidence_refs"] == []
    assert stored["assignee"] is None
    # The old free-typed name is kept, but it is not an authenticated identity
    assert event["actor"] == "Pat" and event["authenticated"] == 0 and event["event_hash"] is None

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 2

    # The migration is idempotent
    assert ReviewStore(path).states() == store.states()

    # New events chain after the legacy ones
    store.sync([exception()])
    assert store.verify_integrity()["ok"]
    assert store.verify_integrity()["legacy_events"] == 1


def test_a_failed_migration_changes_nothing(tmp_path, monkeypatch):

    path = tmp_path / "old.sqlite3"
    make_v1_database(path)

    broken = review_store.MIGRATION_V2 + ("ALTER TABLE no_such_table ADD COLUMN x TEXT",)
    monkeypatch.setattr(review_store, "MIGRATION_V2", broken)

    with pytest.raises(sqlite3.OperationalError):
        ReviewStore(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(exception_reviews)")}
        version = connection.execute("SELECT version FROM schema_info").fetchone()[0]

    assert version == 1 and "assignee" not in columns

    monkeypatch.undo()
    assert ReviewStore(path).states()["K|invoice"]["status"] == "resolved"


def test_a_newer_schema_is_refused(tmp_path):

    store = ReviewStore(tmp_path / "r.sqlite3")

    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE schema_info SET version = 3")

    with pytest.raises(ReviewError, match="schema version 3"):
        ReviewStore(store.path)


def test_the_legacy_set_status_is_attributed_to_an_unauthenticated_name(store):

    item = exception()
    store.sync([item])
    store.set_status(item, RESOLVED, reviewer="Pat", note="ok")

    event = store.history(item["exception_key"])[-1]

    assert (event["actor"], event["actor_role"], event["authenticated"]) == ("Pat", "legacy", 0)
    assert json.loads(json.dumps(event["evidence_refs"])) in (None, [])
