"""
One read request looks the audit's state up once (audit_service.request_scope),
and never keeps it for the next request: a change is always seen.

Everything runs on temporary folders (tests/test_api.py's Rig).
"""

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_analytics  # noqa: E402
import audit_service  # noqa: E402
from audit_service import request_scope  # noqa: E402
from test_api import Rig  # noqa: E402
from test_freight_audit import SHIPMENT, write_contracted_shipment  # noqa: E402

READS = ("/api/overview", "/api/shipments", "/api/exceptions", "/api/carriers", "/api/risk/overview",
         "/api/risk/shipments", "/api/reporting/freight-audit-summary", "/api/reporting/freight-audit-summary.csv")


def count_fingerprints(monkeypatch):

    counted = []
    real = audit_analytics.source_fingerprint
    monkeypatch.setattr(audit_service, "source_fingerprint", lambda raw: counted.append(1) or real(raw))

    return counted


def test_the_fingerprint_is_what_it_always_was(tmp_path):
    """Name, size and modification time of every PDF, sorted by name: the cheaper listing changes nothing."""

    rig = Rig(tmp_path, "local")
    (rig.raw / "notes.txt").write_text("not a document")

    expected = tuple((path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in sorted(rig.raw.glob("*.pdf")))

    assert audit_analytics.source_fingerprint(rig.raw) == expected and expected
    assert audit_analytics.source_fingerprint(tmp_path / "missing") == ()
    assert audit_analytics.source_fingerprint(rig.raw / "notes.txt") == ()


def test_a_read_request_lists_the_document_folder_once(tmp_path, monkeypatch):

    rig = Rig(tmp_path, "local")
    client = rig.client()

    for route in READS:
        assert client.get(route).status_code == 200        # warm

    counted = count_fingerprints(monkeypatch)

    for route in READS:
        counted.clear()
        assert client.get(route).status_code == 200
        assert len(counted) == 1, route


def test_a_document_added_between_two_requests_is_seen_by_the_second(tmp_path):

    rig = Rig(tmp_path, "local")
    client = rig.client()
    before = {row["shipment_id"] for row in client.get("/api/shipments").json()["shipments"]}

    write_contracted_shipment(rig.raw)

    after = {row["shipment_id"] for row in client.get("/api/shipments").json()["shipments"]}
    assert after - before == {SHIPMENT}

    for path in rig.raw.glob("*SHJ-2026-101.pdf"):
        path.unlink()

    assert {row["shipment_id"] for row in client.get("/api/shipments").json()["shipments"]} == before


def test_the_state_is_kept_for_one_scope_only_and_a_change_through_the_service_clears_it(tmp_path, monkeypatch):

    rig = Rig(tmp_path, "local")
    service = rig.state.service
    counted = count_fingerprints(monkeypatch)

    # Outside a request: looked up every time
    service.state_key()
    service.state_key()
    assert len(counted) == 2

    with request_scope():
        first = service.state_key()
        assert service.state_key() == first and len(counted) == 3

        # A change made through the service inside the request is seen at once
        source = next(rig.raw.glob("*.pdf"))
        shutil.copy2(source, rig.raw / "copy_of_a_document.pdf")
        service.invalidate()
        assert service.state_key() != first and len(counted) == 4

    # Nothing outlives the scope
    service.state_key()
    assert len(counted) == 5


def test_a_change_request_is_never_memoised(tmp_path, monkeypatch):
    """Only GET opens the scope: a POST that changes documents and reads them again sees its own change."""

    rig = Rig(tmp_path, "local")
    client = rig.client()
    client.get("/api/exceptions")
    counted = count_fingerprints(monkeypatch)

    key = client.get("/api/exceptions").json()["exceptions"][0]["exception_key"]
    counted.clear()
    assert client.post(f"/api/exceptions/{key}/actions", json={"action": "note", "note": "Checked the documents."}).status_code == 200

    assert len(counted) > 1
