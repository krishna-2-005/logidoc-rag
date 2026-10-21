"""
Tests for src/review_store.py: the exception review database.

Every test uses its own temporary SQLite file (pytest tmp_path); the
real review database is never opened (tests/conftest.py checks it).

Run from the project root:
    python -m pytest tests/test_review_store.py
"""

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from review_store import (
    DISMISSED,
    IN_REVIEW,
    OPEN,
    RESOLVED,
    ReviewError,
    ReviewStore,
)


def exception(code="invoice", signature="s1", shipment="SHJ-2026-001"):

    return {
        "exception_key": f"SHJ2026001|{code}",
        "shipment_key": "SHJ2026001",
        "shipment_id": shipment,
        "category": "Invoice mismatch",
        "signature": signature,
        # Fields the store must never write
        "description": "Invoice line items add up to $1,430.00 but the total is $1,550.00.",
        "evidence": ["INVOICE_SHJ-2026-001.pdf p.1 (INVOICE): Total Invoice Amount: $1,550.00"],
    }


@pytest.fixture
def store(tmp_path):

    return ReviewStore(tmp_path / "reviews.sqlite3")


def test_new_exceptions_are_open(store):

    reviews = store.sync([exception()])

    assert reviews["SHJ2026001|invoice"]["status"] == OPEN
    assert [event["to_status"] for event in store.history("SHJ2026001|invoice")] == [OPEN]

    # Showing it again changes nothing
    store.sync([exception()])
    assert len(store.history("SHJ2026001|invoice")) == 1


def test_state_transitions(store):

    item = exception()
    store.sync([item])

    assert store.set_status(item, IN_REVIEW, reviewer="krishna")["status"] == IN_REVIEW

    resolved = store.set_status(item, RESOLVED, reviewer="krishna", note="Carrier credit note received")

    assert (resolved["status"], resolved["reviewer"], resolved["note"]) == (
        RESOLVED, "krishna", "Carrier credit note received",
    )

    assert store.set_status(item, OPEN)["status"] == OPEN

    assert [(event["from_status"], event["to_status"]) for event in store.history(item["exception_key"])] == [
        (None, OPEN), (OPEN, IN_REVIEW), (IN_REVIEW, RESOLVED), (RESOLVED, OPEN),
    ]


@pytest.mark.parametrize("note", [None, "", "   "])
def test_dismissal_needs_a_reason(store, note):

    item = exception()
    store.sync([item])

    with pytest.raises(ReviewError, match="needs a reason"):
        store.set_status(item, DISMISSED, note=note)

    assert store.states()[item["exception_key"]]["status"] == OPEN

    dismissed = store.set_status(item, DISMISSED, note="Duplicate of the corrected invoice")

    assert (dismissed["status"], dismissed["note"]) == (DISMISSED, "Duplicate of the corrected invoice")


def test_database_rejects_a_dismissal_without_reason_even_directly(store):

    store.sync([exception()])

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE exception_reviews SET status = 'dismissed', note = NULL"
            )


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda store, item: store.set_status(item, "approved"), "Unknown review status"),
        (lambda store, item: store.set_status(dict(item, exception_key="X|y"), RESOLVED), "not been recorded"),
        (lambda store, item: store.set_status(dict(item, signature="other"), RESOLVED), "has changed"),
    ],
)
def test_invalid_changes_are_refused(store, change, message):

    item = exception()
    store.sync([item])

    with pytest.raises(ReviewError, match=message):
        change(store, item)

    assert store.states()[item["exception_key"]]["status"] == OPEN


def test_resolved_cannot_be_dismissed_directly(store):

    item = exception()
    store.sync([item])
    store.set_status(item, RESOLVED)

    with pytest.raises(ReviewError, match="Cannot change Resolved to Dismissed"):
        store.set_status(item, DISMISSED, note="no")


def test_state_survives_reconnection(store):

    item = exception()
    store.sync([item])
    store.set_status(item, RESOLVED, reviewer="krishna", note="Checked with the carrier")

    reopened = ReviewStore(store.path)

    row = reopened.states()[item["exception_key"]]

    assert (row["status"], row["reviewer"], row["note"]) == (RESOLVED, "krishna", "Checked with the carrier")
    assert len(reopened.history(item["exception_key"])) == 2


def test_a_failed_change_is_rolled_back(store, monkeypatch):

    item = exception()
    store.sync([item])

    def fail(*args, **kwargs):
        raise RuntimeError("disk full")

    # The status is updated first, then the event is written: a
    # failure in between must leave nothing behind
    monkeypatch.setattr(store, "record_event", fail)

    with pytest.raises(RuntimeError):
        store.set_status(item, RESOLVED, note="should not stay")

    row = store.states()[item["exception_key"]]

    assert (row["status"], row["note"]) == (OPEN, None)
    assert len(store.history(item["exception_key"])) == 1

    # A sync that fails half-way records nothing either
    with pytest.raises(RuntimeError):
        store.sync([item, exception(code="delivery")])

    assert set(store.states()) == {item["exception_key"]}


def test_resolved_exception_is_reopened_when_the_audit_changes(store):

    item = exception(signature="s1")
    store.sync([item])
    store.set_status(item, RESOLVED, note="Accepted")

    # The same exception with the same values: still resolved
    assert store.sync([item])[item["exception_key"]]["status"] == RESOLVED

    # Other audited values: reopened and flagged
    changed = exception(signature="s2")
    row = store.sync([changed])[item["exception_key"]]

    assert (row["status"], row["needs_recheck"], row["signature"]) == (OPEN, 1, "s2")
    assert "audited values changed" in store.history(item["exception_key"])[-1]["note"]

    # Handling it again clears the flag
    assert store.set_status(changed, IN_REVIEW)["needs_recheck"] == 0


def test_exception_that_comes_back_after_disappearing_is_reopened(store):

    item = exception()
    store.sync([item])
    store.set_status(item, DISMISSED, note="Not relevant")

    # No longer detected: kept as dismissed, marked absent
    store.sync([])

    row = store.states()[item["exception_key"]]
    assert row["status"] == DISMISSED and row["absent_since"]

    # Detected again: reopened, not silently still dismissed
    row = store.sync([item])[item["exception_key"]]

    assert (row["status"], row["needs_recheck"], row["absent_since"]) == (OPEN, 1, None)
    assert "detected again" in store.history(item["exception_key"])[-1]["note"]


def test_an_exception_in_review_keeps_its_state_but_is_flagged(store):

    item = exception(signature="s1")
    store.sync([item])
    store.set_status(item, IN_REVIEW, reviewer="krishna")

    row = store.sync([exception(signature="s2")])[item["exception_key"]]

    assert (row["status"], row["needs_recheck"]) == (IN_REVIEW, 1)


def test_no_document_text_is_stored(store):

    item = exception()
    store.sync([item])
    store.set_status(item, RESOLVED, note="ok")

    with sqlite3.connect(store.path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(exception_reviews)")}

    assert not columns & {"description", "evidence", "text"}

    content = store.path.read_bytes()

    assert b"Total Invoice Amount" not in content
    assert b"$1,550.00" not in content


def test_keys_are_passed_as_parameters(store):

    item = dict(exception(), exception_key="SHJ2026001|x'); DROP TABLE exception_reviews; --")

    store.sync([item])
    store.set_status(item, RESOLVED)

    assert store.states()[item["exception_key"]]["status"] == RESOLVED


def test_a_database_of_another_schema_version_is_refused(store):

    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE schema_info SET version = 99")

    with pytest.raises(ReviewError, match="schema version 99"):
        ReviewStore(store.path)


@pytest.mark.parametrize("handled", [OPEN, IN_REVIEW, RESOLVED, DISMISSED])
def test_a_disappearing_exception_is_never_resolved_by_disappearing(store, handled):

    item = exception()
    store.sync([item])

    if handled != OPEN:
        store.set_status(item, handled, note="why")

    # Not detected for several runs
    store.sync([])
    store.sync([])

    row = store.states()[item["exception_key"]]

    assert row["status"] == handled
    assert row["absent_since"]
    assert not [event for event in store.history(item["exception_key"]) if event["note"] and "Reopened" in event["note"]]


@pytest.mark.parametrize(
    "handled, reopened",
    [(RESOLVED, True), (DISMISSED, True), (IN_REVIEW, False), (OPEN, False)],
)
def test_a_returning_exception_is_reopened_only_when_it_was_closed(store, handled, reopened):

    item = exception()
    store.sync([item])

    if handled != OPEN:
        store.set_status(item, handled, note="why")

    store.sync([])

    row = store.sync([item])[item["exception_key"]]

    assert row["absent_since"] is None

    if reopened:
        assert (row["status"], row["needs_recheck"]) == (OPEN, 1)
    else:
        assert (row["status"], row["needs_recheck"]) == (handled, 0)
