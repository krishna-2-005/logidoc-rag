"""
The integration boundary: the outbound audit-run feed (GET
/api/integrations/audit-runs) an ERP, TMS or webhook dispatcher polls, and the
security of the endpoints added for integrations (sign-in, CSRF, error text).

Synthetic PDFs in a temporary folder; no network, no model.

Run from the project root:
    python -m pytest tests/test_integration_feed.py
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_freight_operations import correct_fuel, rig  # noqa: E402,F401  (the fixture)

FEED = "/api/integrations/audit-runs"


def record(rig, label):

    reply = rig.client("alice").post("/api/history/runs", json={"label": label})
    assert reply.status_code == 201, reply.text

    return reply.json()["run_id"]


def test_the_feed_is_empty_before_anything_is_recorded(rig):

    assert rig.client("bob").get(FEED).json() == {"runs": [], "next_after": 0, "has_more": False}
    assert not rig.locations.history_db.exists()


def test_each_run_with_its_shipments_and_outcomes(rig):

    first = record(rig, "First")

    feed = rig.client("bob").get(FEED).json()

    assert [run["run_id"] for run in feed["runs"]] == [first] and feed["next_after"] == first and not feed["has_more"]
    shipments = {item["shipment_id"]: item for item in feed["runs"][0]["shipments"]}

    contracted = shipments["SHJ-2026-101"]
    assert contracted["changed"] is True and contracted["freight_outcome"] == "DISCREPANCY"
    assert contracted["carrier"] == "Swift Lines" and contracted["invoice_total"] == 1675.0
    # The sample shipment has no rate confirmation; its invoice arithmetic does not add up
    assert shipments["SHJ-2026-001"]["freight_outcome"] == "DISCREPANCY"


def test_the_cursor_moves_forward_and_marks_unchanged_shipments(rig):

    first = record(rig, "First")
    second = record(rig, "Nothing changed")
    correct_fuel(rig)                                         # records a recalculated run for the shipment
    client = rig.client("bob")

    page = client.get(FEED, params={"after": 0, "limit": 1}).json()
    assert [run["run_id"] for run in page["runs"]] == [first] and page["has_more"] is True

    page = client.get(FEED, params={"after": page["next_after"], "limit": 1}).json()
    assert [run["run_id"] for run in page["runs"]] == [second]
    assert all(item["changed"] is False for item in page["runs"][0]["shipments"])

    rest = client.get(FEED, params={"after": page["next_after"]}).json()
    assert rest["has_more"] is False and len(rest["runs"]) == 1
    recalculated = rest["runs"][0]["shipments"]
    assert [item["shipment_id"] for item in recalculated] == ["SHJ-2026-101"]
    # The fuel line now matches; the detention charge still waits for a decision
    assert recalculated[0]["changed"] is True and recalculated[0]["freight_outcome"] == "PENDING_REVIEW"

    # Nothing after the last run: the cursor stays where it was
    assert client.get(FEED, params={"after": rest["next_after"]}).json() == {"runs": [], "next_after": rest["next_after"], "has_more": False}


def test_reading_again_returns_the_same_runs(rig):

    record(rig, "First")
    client = rig.client("bob")

    assert client.get(FEED).json() == client.get(FEED).json() == client.get("/api/v1" + FEED.removeprefix("/api")).json()


@pytest.mark.parametrize("params", [{"after": -1}, {"limit": 0}, {"limit": 501}, {"after": "first"}])
def test_bad_cursors_are_refused(rig, params):

    reply = rig.client("bob").get(FEED, params=params)

    assert reply.status_code == 422 and isinstance(reply.json()["detail"], str)


def test_the_feed_needs_a_signed_in_user(rig):

    assert rig.client(None).get(FEED).status_code == 401
    assert rig.client(None).get("/api/v1/integrations/audit-runs").status_code == 401


# ============================================================
# SECURITY OF THE NEW WRITE ENDPOINTS
# ============================================================

@pytest.mark.parametrize("method, url, body", [
    ("post", "/api/carrier-registry", {"legal_name": "Swift Lines Inc"}),
    ("patch", "/api/carrier-registry/1", {"scac": "SWFT"}),
    ("post", "/api/carrier-registry/links", {"name": "Swift Lines", "carrier_id": 1, "reason": "x" * 20}),
    ("post", "/api/carrier-registry/links/1/decision", {"decision": "approve"}),
])
def test_a_change_without_the_csrf_header_is_refused(rig, method, url, body):

    client = rig.client("alice")
    client.headers.pop("X-LogiDoc-Client", None)

    reply = getattr(client, method)(url, json=body)

    assert reply.status_code == 403 and "X-LogiDoc-Client" in reply.json()["detail"]
    assert not rig.locations.carrier_registry_path().exists()


def test_errors_name_no_server_path(rig):

    client = rig.client("alice")
    replies = [
        client.patch("/api/carrier-registry/999", json={"scac": "SWFT"}),
        client.post("/api/carrier-registry/links/999/decision", json={"decision": "approve"}),
        client.post("/api/carrier-registry", json={"legal_name": "X", "mc_number": "MC-1234567890"}),
        client.get("/api/carriers/nobody"),
    ]

    for reply in replies:
        detail = reply.json()["detail"]
        assert reply.status_code in (404, 422)
        assert str(rig.tmp) not in detail and ".sqlite3" not in detail and "Traceback" not in detail
