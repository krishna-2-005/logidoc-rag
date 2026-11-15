"""
Read requests never write to the review database (Sprint 4, phase 1).

Every GET operation of the API (read from the routes, scripts/api_permissions.py)
is called on three review databases: none at all, a current one holding
reviews, and one at schema version 2 awaiting its upgrade. The file must come
out byte for byte as it went in, and never be created or upgraded, while the
answers still show every detected exception with its review state. Changes
(an exception action, the server's background loop) still record detections,
exactly as before.

Everything runs on temporary folders (tests/test_api.py's Rig).
"""

import re
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests", PROJECT_ROOT / "scripts"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import api_permissions  # noqa: E402
import review_store  # noqa: E402
from review_store import ReviewError, ReviewStore, ReviewView  # noqa: E402
from test_api import A, Rig, exception_key  # noqa: E402
from test_freight_operations import version_2_database  # noqa: E402

READS = [item for item in api_permissions.operations() if item["method"] == "GET"]


def read_everything(client, shipment, key):
    """Every GET operation, with real identifiers where a route takes one. Returns {route: status}."""

    answers = {}

    for item in READS:

        path = (item["path"].replace("{shipment_id}", shipment).replace("{key}", key)
                .replace("{report}", "freight-audit-summary"))
        path = re.sub(r"\{[^}]+\}", "999999", path)
        answers[path] = client.get(path).status_code

    return answers


def fingerprint(path):

    path = Path(path)

    return path.read_bytes() if path.is_file() else None


def siblings(path):

    return sorted(item.name for item in Path(path).parent.glob(Path(path).stem + "*"))


@pytest.fixture
def rig(tmp_path):

    return Rig(tmp_path, "required")


# ============================================================
# Reads write nothing
# ============================================================

def test_reads_never_create_the_review_database(rig):

    admin = rig.client("alice")
    review_db = Path(rig.locations.review_db)
    assert not review_db.exists()

    detected = admin.get("/api/exceptions").json()["exceptions"]
    answers = read_everything(admin, A, detected[0]["exception_key"])

    assert not review_db.exists(), "a read created the review database"
    assert siblings(review_db) == []
    # The reads still answer, and show every detected exception as open
    assert answers["/api/exceptions"] == answers["/api/overview"] == answers[f"/api/shipments/{A}"] == 200
    assert detected and {item["status"] for item in detected} == {"open"}


def test_reads_leave_a_current_review_database_byte_for_byte_unchanged(rig):

    admin = rig.client("alice")
    key = exception_key(admin)
    reply = admin.post(f"/api/exceptions/{key}/actions",
                       json={"action": "dismiss", "note": "Checked with the carrier: a duplicate."})
    assert reply.status_code == 200, reply.text

    review_db = Path(rig.locations.review_db)
    before = fingerprint(review_db)
    rows_before = ReviewView(review_db).states()

    answers = read_everything(admin, A, key)

    assert fingerprint(review_db) == before, "a read changed the review database"
    assert ReviewView(review_db).states() == rows_before
    assert answers[f"/api/exceptions/{key}"] == 200
    assert admin.get(f"/api/exceptions/{key}").json()["review"]["status"] == "dismissed"


def test_reads_never_upgrade_a_version_2_review_database(rig):

    review_db = Path(rig.locations.review_db)
    version_2_database(review_db)
    before = fingerprint(review_db)

    admin = rig.client("alice")
    detected = admin.get("/api/exceptions").json()["exceptions"]
    read_everything(admin, A, detected[0]["exception_key"])

    assert fingerprint(review_db) == before
    assert not (review_db.parent / "audit_reviews.before-v3.sqlite3").exists()
    assert not review_db.with_name(review_db.stem + ".before-v3" + review_db.suffix).exists()

    with sqlite3.connect(review_db) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 2

    # Read through the upgrade made in memory: the old row is there, the detected exceptions are open
    assert ReviewView(review_db).states()["K|invoice"]["status"] == "dismissed"
    assert {item["status"] for item in detected} == {"open"}


def test_a_newly_detected_exception_keeps_one_creation_time_across_reads(rig):

    admin = rig.client("alice")
    first = {row["exception_key"]: row["created_at"] for row in admin.get("/api/exceptions").json()["exceptions"]}
    again = {row["exception_key"]: row["created_at"] for row in admin.get("/api/exceptions").json()["exceptions"]}

    assert first and first == again
    assert all(first.values())


# ============================================================
# Changes still record what the audit detects
# ============================================================

def test_an_action_on_an_exception_never_recorded_records_the_audit_first(rig):

    admin = rig.client("alice")
    listed = {row["exception_key"]: row for row in admin.get("/api/exceptions").json()["exceptions"]}
    key = exception_key(admin)

    reply = admin.post(f"/api/exceptions/{key}/actions", json={"action": "note", "note": "Asked the carrier."})
    assert reply.status_code == 200, reply.text

    store = ReviewStore(rig.locations.review_db)
    states = store.states()
    # Every detected exception was recorded, dated from when it was first shown
    assert set(states) == set(listed)
    assert all(states[name]["created_at"] == row["created_at"] for name, row in listed.items())
    assert [event["event_type"] for event in store.history(key)] == ["detected", "note"]
    assert store.verify_integrity()["ok"]


def test_the_background_loop_records_detections_once(rig):

    service = rig.state.service
    review_db = Path(rig.locations.review_db)

    assert service.record_detections() > 0
    written = fingerprint(review_db)
    events = ReviewView(review_db).all_events(1000)
    assert events and {event["event_type"] for event in events} == {"detected"}

    # Nothing new: nothing written, not even a lock taken on the file
    assert service.record_detections() == 0
    assert fingerprint(review_db) == written

    # Through the operations loop of the server
    rig.state.operations.record_detections()
    assert fingerprint(review_db) == written


def test_a_reopening_shows_on_a_read_before_it_is_recorded(rig, monkeypatch):
    """A dismissed exception whose audited values changed reads as reopened, and the read writes nothing."""

    admin = rig.client("alice")
    key = exception_key(admin)
    assert admin.post(f"/api/exceptions/{key}/actions",
                      json={"action": "dismiss", "note": "Checked with the carrier: a duplicate."}).status_code == 200

    original = review_store.ReviewStore.record_detected

    def changed_signature(self, connection, detected, at, detected_at):
        detected = {name: dict(item, signature="changed") if name == key else item for name, item in detected.items()}
        return original(self, connection, detected, at, detected_at)

    monkeypatch.setattr(review_store.ReviewStore, "record_detected", changed_signature)
    review_db = Path(rig.locations.review_db)
    before = fingerprint(review_db)

    row = admin.get(f"/api/exceptions/{key}").json()["review"]

    assert (row["status"], row["needs_recheck"]) == ("open", 1)
    assert fingerprint(review_db) == before


# ============================================================
# The view itself
# ============================================================

def test_the_view_refuses_every_write(tmp_path):

    view = ReviewView(tmp_path / "reviews.sqlite3")

    with pytest.raises(ReviewError):
        view.sync([])

    with pytest.raises(ReviewError):
        view.initialize()

    assert not (tmp_path / "reviews.sqlite3").exists()


def test_the_view_refuses_a_newer_schema_like_the_store(tmp_path):

    path = tmp_path / "reviews.sqlite3"
    ReviewStore(path)

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_info SET version = 99")

    with pytest.raises(ReviewError):
        ReviewView(path)
