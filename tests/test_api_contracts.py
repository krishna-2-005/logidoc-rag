"""
API contracts: the typed endpoints return exactly what they returned before
their response models were added, the OpenAPI document publishes the models,
/api/v1 is the same API as /api, errors have one shape, and the carrier
shipment list pages correctly.

Synthetic PDFs in a temporary folder (the sample shipments plus a contracted
shipment), through the real application; no network, no model.

Run from the project root:
    python -m pytest tests/test_api_contracts.py
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from api import routes_audit, routes_workflow, schemas  # noqa: E402
from authz import Principal  # noqa: E402
from test_freight_operations import correct_fuel, rig  # noqa: E402,F401  (the fixture)

ALICE = Principal("alice", "admin", True, 1)
SHIPMENT = "SHJ-2026-101"
DETENTION = "SHJ2026101|freight-detention-charge"


def plain(value):
    """What the JSON encoder makes of a Python value."""

    return json.loads(json.dumps(value, default=str))


@pytest.fixture
def populated(rig):
    """Every optional part filled: a recorded history, an approved correction, a disputed exception."""

    client = rig.client("alice")
    assert client.post("/api/history/runs", json={"label": "Before"}).status_code == 201
    correct_fuel(rig)
    assert client.get("/api/exceptions").status_code == 200
    assert client.post(f"/api/exceptions/{DETENTION}/actions",
                       json={"action": "dispute", "note": "No detention receipt with the POD."}).status_code == 200

    return rig


# ============================================================
# THE MODELS CHANGE NOTHING
# ============================================================

def raw_responses(rig):
    """The route functions called directly (no response model): {url: raw value}."""

    state = rig.state

    return {
        "/api/carriers": routes_audit.carriers(principal=ALICE, state=state, start=None, end=None),
        "/api/carriers/swift lines": routes_audit.carrier("swift lines", principal=ALICE, state=state, start=None, end=None,
                                                          offset=0, limit=50),
        f"/api/shipments/{SHIPMENT}/freight-audit": routes_audit.shipment_freight_audit(SHIPMENT, principal=ALICE, state=state),
        f"/api/history/shipments/{SHIPMENT}": routes_workflow.shipment_history(SHIPMENT, principal=ALICE, state=state),
        "/api/exceptions": routes_workflow.exceptions(
            principal=ALICE, state=state, search="", assignee=None, unassigned=False, status=[], category=[],
            min_age_days=None, max_age_days=None, pending_approval=False, include_absent=False, severity=[]),
        f"/api/exceptions/{DETENTION}/history": routes_workflow.exception_history(DETENTION, principal=ALICE, state=state),
    }


def test_every_typed_endpoint_returns_what_it_returned_before(populated):

    client = populated.client("alice")

    for url, raw in raw_responses(populated).items():
        reply = client.get(url)
        assert reply.status_code == 200, (url, reply.text)
        assert reply.json() == plain(raw), url


def test_the_populated_data_reaches_every_optional_part(populated):

    client = populated.client("alice")

    audit = client.get(f"/api/shipments/{SHIPMENT}/freight-audit").json()["freight_audit"]
    assert audit["corrections_applied"] and audit["inputs"] and audit["audited_at"]
    assert any(c["expected_source"] and c["expected_source"].get("corrected") for c in audit["charges"])

    history = client.get(f"/api/history/shipments/{SHIPMENT}").json()
    assert len(history) == 2 and all(entry["snapshot"]["freight_audit"] for entry in history)

    exceptions = {row["exception_key"]: row for row in client.get("/api/exceptions").json()["exceptions"]}
    assert exceptions[DETENTION]["status"] == "disputed" and exceptions[DETENTION]["financial_impact"]


def test_an_action_returns_the_stored_review(populated):

    reply = populated.client("alice").post(f"/api/exceptions/{DETENTION}/actions",
                                           json={"action": "note", "note": "Asked the carrier for the driver log."})
    state = populated.state
    flow = routes_workflow.workflow_of(state)
    stored = state.service.review_store().states()[DETENTION]

    assert reply.json() == plain({"review": stored, "allowed_actions": flow.allowed_actions(ALICE, stored)})


def test_the_responses_validate_against_their_models(populated):

    client = populated.client("alice")

    schemas.CarrierIntelligenceResponse.model_validate(client.get("/api/carriers").json())
    schemas.CarrierDetailResponse.model_validate(client.get("/api/carriers/swift lines").json())
    schemas.FreightAuditResponse.model_validate(client.get(f"/api/shipments/{SHIPMENT}/freight-audit").json())
    schemas.ExceptionList.model_validate(client.get("/api/exceptions").json())

    for entry in client.get(f"/api/history/shipments/{SHIPMENT}").json():
        schemas.TimelineEntry.model_validate(entry)


# ============================================================
# THE PUBLISHED CONTRACT
# ============================================================

def test_the_openapi_document_publishes_the_models_and_the_error_shape(rig):

    document = rig.client("alice").get("/api/openapi.json").json()
    components = document["components"]["schemas"]

    for name in ("CarrierIntelligenceResponse", "CarrierDetailResponse", "FreightAuditResponse", "FreightAuditResult",
                 "TimelineEntry", "ExceptionList", "ReviewEvent", "ActionResult", "ErrorResponse"):
        assert name in components, name

    carriers = document["paths"]["/api/carriers"]["get"]["responses"]
    assert carriers["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/CarrierIntelligenceResponse")
    assert carriers["403"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    assert "text/csv" in document["paths"]["/api/reports/carriers.csv"]["get"]["responses"]["200"]["content"]

    # Optional fields are explicit, and an outcome is one of the documented values
    money = components["CarrierMoney"]["properties"]["invoiced_total"]
    assert {"type": "null"} in money["anyOf"]
    assert set(components["FreightCharge"]["properties"]["outcome"]["enum"]) == {
        "MATCHED", "MATCHED_WITH_TOLERANCE", "DISCREPANCY", "INSUFFICIENT_EVIDENCE", "DUPLICATE", "PENDING_REVIEW"}


# ============================================================
# VERSIONING
# ============================================================

def test_v1_is_the_same_api(populated):

    client = populated.client("alice")

    for url in ("/carriers", "/carriers/swift lines", f"/shipments/{SHIPMENT}/freight-audit", "/exceptions",
                f"/history/shipments/{SHIPMENT}"):
        current, pinned = client.get("/api" + url), client.get("/api/v1" + url)
        assert pinned.status_code == current.status_code == 200, url
        assert pinned.json() == current.json(), url
        assert pinned.headers["api-version"] == current.headers["api-version"] == "1"


def test_v1_keeps_every_guard(populated):

    assert populated.client(None).get("/api/v1/carriers").status_code == 401

    # A change still needs the CSRF header, whatever the version prefix
    bare = populated.client("alice")
    bare.headers.pop("X-LogiDoc-Client", None)
    assert bare.post(f"/api/v1/exceptions/{DETENTION}/actions", json={"action": "note", "note": "x" * 12}).status_code == 403

    # A reviewer still cannot waive
    assert populated.client("bob").post(f"/api/v1/exceptions/{DETENTION}/actions",
                                         json={"action": "waive", "note": "Accept the charge."}).status_code == 403


def test_an_unknown_version_is_not_found(rig):

    assert rig.client("alice").get("/api/v2/carriers").status_code == 404
    assert rig.client("alice").get("/api/v1/health").json() == {"status": "ok"}


# ============================================================
# ONE ERROR SHAPE
# ============================================================

@pytest.mark.parametrize("user, method, url, body, status", [
    (None, "get", "/api/carriers", None, 401),
    ("bob", "post", f"/api/exceptions/{DETENTION}/actions", {"action": "waive", "note": "Accept it as it is."}, 403),
    ("alice", "get", "/api/carriers/no such carrier", None, 404),
    ("alice", "get", "/api/shipments/NO-SUCH/freight-audit", None, 404),
    ("alice", "get", "/api/carriers?start=2026-11-01&end=2026-10-01", None, 422),
    ("alice", "get", "/api/carriers?start=yesterday", None, 422),
    ("alice", "get", "/api/carriers/swift lines?limit=0", None, 422),
])
def test_every_error_is_one_detail_message(populated, user, method, url, body, status):

    client = populated.client(user)
    reply = client.post(url, json=body) if method == "post" else client.get(url)

    assert reply.status_code == status
    assert set(reply.json()) == {"detail"} and isinstance(reply.json()["detail"], str) and reply.json()["detail"]


# ============================================================
# PAGINATION
# ============================================================

def test_the_carrier_shipments_page(populated):

    client = populated.client("alice")
    everything = client.get("/api/carriers/swift lines", params={"limit": 500}).json()
    total = everything["shipment_count"]
    ids = [row["shipment_id"] for row in everything["shipment_rows"]]

    assert total == len(ids) >= 1

    pages = []
    for offset in range(0, total, 1):
        page = client.get("/api/carriers/swift lines", params={"offset": offset, "limit": 1}).json()
        assert page["shipment_count"] == total and page["offset"] == offset and page["limit"] == 1
        pages += [row["shipment_id"] for row in page["shipment_rows"]]

    assert pages == ids                                     # every shipment once, in order

    beyond = client.get("/api/carriers/swift lines", params={"offset": total + 5}).json()
    assert beyond["shipment_rows"] == [] and beyond["shipment_count"] == total
    assert client.get("/api/carriers/swift lines", params={"limit": 501}).status_code == 422
    assert client.get("/api/carriers/swift lines", params={"offset": -1}).status_code == 422


def test_the_published_contract_is_up_to_date():
    """docs/api/openapi-v1.json is what the code serves: regenerate it with scripts/export_openapi.py."""

    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import export_openapi

    assert export_openapi.TARGET.read_text(encoding="utf-8") == export_openapi.contract()
