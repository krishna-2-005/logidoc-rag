"""
When a detected exception is recorded, and what that means for the webhooks
(docs/EXCEPTION_RECORDING.md).

A read, in the API or in the Streamlit app, shows a newly detected exception as
open and writes nothing. It is recorded (its "detected" event written, which
becomes an exception.created webhook event) by:
- the API server's background loop (operations.Operations.tick), when the
  server runs its background jobs;
- the first change to an exception (an API action, or a review saved in the
  Streamlit app).

Everything runs on temporary folders.
"""

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from integration_support import integration_rig, register  # noqa: E402
from review_store import ReviewView  # noqa: E402
from test_freight_operations import version_2_database  # noqa: E402

EXCEPTION_EVENTS = ("exception.created", "exception.status_changed")


def created_events(rig):
    """The exception events the webhook collector creates now, by type."""

    service = rig.state.integrations.webhooks
    service.collect(only=["review_events"])
    service.deliver_due(("pending",), limit=500)
    types = [json.loads(request["body"])["type"] for request in rig.receiver.requests]
    rig.receiver.requests.clear()

    return {name: types.count(name) for name in EXCEPTION_EVENTS}


# ============================================================
# The API: with and without the background loop
# ============================================================

def test_without_the_background_loop_reads_create_no_exception_events(tmp_path):

    rig = integration_rig(tmp_path)
    alice = rig.client("alice")
    register(alice, event_types=EXCEPTION_EVENTS)

    shown = alice.get("/api/exceptions").json()["exceptions"]
    assert shown and {row["status"] for row in shown} == {"open"}

    # Shown as open, recorded nowhere: no exception.created yet (the eventual consistency documented)
    assert not Path(rig.locations.review_db).exists()
    assert created_events(rig) == {"exception.created": 0, "exception.status_changed": 0}

    # The first change records every detection, then itself
    key = shown[0]["exception_key"]
    assert alice.post(f"/api/exceptions/{key}/actions", json={"action": "dispute", "note": "Raised with the carrier."}).status_code == 200

    assert created_events(rig) == {"exception.created": len(shown), "exception.status_changed": 1}


def test_with_the_background_loop_every_detection_becomes_an_event_once(tmp_path):

    rig = integration_rig(tmp_path)
    alice = rig.client("alice")
    register(alice, event_types=EXCEPTION_EVENTS)
    shown = alice.get("/api/exceptions").json()["exceptions"]

    # One tick of the server's operations loop (it runs every 30 s with the background jobs)
    rig.state.operations.record_detections()

    assert created_events(rig) == {"exception.created": len(shown), "exception.status_changed": 0}

    # Recorded with the creation time the reads showed
    stored = ReviewView(rig.locations.review_db).states()
    assert {row["exception_key"]: row["created_at"] for row in shown} == {
        key: row["created_at"] for key, row in stored.items()}

    # Nothing new: the next ticks record nothing and create no event
    rig.state.operations.record_detections()
    rig.state.operations.record_detections()
    assert created_events(rig) == {"exception.created": 0, "exception.status_changed": 0}

    # A change after that writes only itself
    key = shown[0]["exception_key"]
    assert alice.post(f"/api/exceptions/{key}/actions", json={"action": "dispute", "note": "Raised with the carrier."}).status_code == 200
    assert created_events(rig) == {"exception.created": 0, "exception.status_changed": 1}


# ============================================================
# The Streamlit app: showing writes nothing
# ============================================================

def open_every_review_view(tmp_path, review_db):
    """The analytics, audit operations and exception queue tabs on a temporary workspace."""

    from test_analytics_cache import open_analytics
    from test_audit_operations_ui import make_folder, open_operations, workspace

    raw = make_folder(tmp_path, broken=False)

    with workspace(raw, review_db, tmp_path / "history.sqlite3"):

        analytics = open_analytics()
        operations = open_operations()

        queue = analytics
        [button for button in queue.button if button.label == "Load exception queue"][0].click().run()

    return analytics, operations, queue


def test_streamlit_views_never_create_the_review_database(tmp_path):

    review_db = tmp_path / "reviews.sqlite3"
    analytics, operations, queue = open_every_review_view(tmp_path, review_db)

    for at in (analytics, operations, queue):
        assert not at.exception, [str(item.value) for item in at.exception]

    # The exceptions were shown, as open
    register = [frame.value for frame in analytics.tabs[4].dataframe if "Review state" in frame.value.columns][0]
    assert len(register) and set(register["Review state"]) == {"Open"}

    assert not review_db.exists()


def test_streamlit_views_leave_an_old_review_database_untouched(tmp_path):

    review_db = tmp_path / "reviews.sqlite3"
    version_2_database(review_db)
    before = review_db.read_bytes()

    open_every_review_view(tmp_path, review_db)

    assert review_db.read_bytes() == before
    assert not (tmp_path / "reviews.before-v3.sqlite3").exists()

    with sqlite3.connect(review_db) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 2
