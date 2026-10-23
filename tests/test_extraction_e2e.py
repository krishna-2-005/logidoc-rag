"""
End-to-end QA of the extraction, correction-approval and recorded-run workflow,
driven through the HTTP API: the whole sequence, the permission and CSRF matrix,
status codes, the defects the QA pass found (large ids, duplicate concurrent
recomputation, an unreadable correction store), real multi-process concurrency,
older databases, and leaks.

Synthetic PDFs in a temporary folder; no network, no model.

Run: python -m pytest tests/test_extraction_e2e.py
"""

import json
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_history  # noqa: E402
from migration_support import upgrade  # noqa: E402
import extraction_store as store_module  # noqa: E402
from assistant import UNAVAILABLE_NOTE, Assistant  # noqa: E402
from auth import UserStore  # noqa: E402
from authz import ADMIN, Principal  # noqa: E402
from extraction_store import ExtractionStore  # noqa: E402
from test_api import A, B, PASSWORD, Rig  # noqa: E402
from test_correction_aware_audit import (  # noqa: E402
    ALICE, BOB, BOL_A, DAVE, INVOICE_A, POD_A, REASON, add_shipment, corpus_pdf, extract, propose, record_run, v3_store,
)

SENTINEL = "ZQX-SENTINEL-9031"


@pytest.fixture
def rig(tmp_path):
    result = Rig(tmp_path, "required")
    UserStore(tmp_path / "auth.sqlite3").create_user("dave", PASSWORD, ADMIN)

    return result


def post(client, url, **body):
    return client.post(url, json=body)


# ---- the whole sequence, over HTTP only ----------------------------------------------------------

def test_the_complete_workflow(rig):
    anon, alice, bob, dave = rig.client(), rig.client("alice"), rig.client("bob"), rig.client("dave")

    # 1. authenticate: the session is real, and the audit is closed to anyone else
    assert anon.get("/api/extractions").status_code == 401 and bob.get("/api/session").json()["user"]["username"] == "bob"
    record_run(rig)
    baseline = {s: bob.get(f"/api/shipments/{s}").json()["result"] for s in (A, B)}

    # 2. select a document and extract its fields (administrator)
    run = post(alice, "/api/extractions/run", document=POD_A)

    assert run.status_code == 200 and run.json()["created"] is True
    pod_id = run.json()["id"]

    # 3. original values and their provenance
    detail = bob.get(f"/api/extractions/{pod_id}").json()
    pieces = detail["payload"]["fields"]["delivered_pieces"]

    assert pieces["value"] == 45 and pieces["state"] == "EXTRACTED" and pieces["page"] == 1 and pieces["source_file"] == POD_A
    assert pieces["method"] and pieces["basis"] == "exact_label" and pieces["raw_text"] is None
    assert detail["effective"]["delivered_pieces"]["source"] == "extracted"

    # 4. propose, with a reason and the version it is based on
    url = f"/api/extractions/{pod_id}/corrections"
    proposal = post(bob, url, field="delivered_pieces", value="48", reason=REASON, based_on=None)

    assert proposal.status_code == 200 and proposal.json()["status"] == "pending"
    correction_id = proposal.json()["id"]
    assert post(bob, url, field="delivered_pieces", value="47", reason=REASON, based_on=None).status_code == 409

    # 5. the proposal has no effect on any audit
    assert {s: bob.get(f"/api/shipments/{s}").json()["result"] for s in (A, B)} == baseline
    assert bob.get(f"/api/extractions/{pod_id}").json()["effective"]["delivered_pieces"]["value"] == 45
    assert bob.get("/api/extractions/staleness").json()["stale"] == []

    # 6. self-approval is rejected; a reviewer cannot approve either
    decision = f"/api/extractions/corrections/{correction_id}/decision"
    own = post(alice, f"/api/extractions/{pod_id}/corrections", field="delivered_pieces", value="49", reason=REASON, based_on=None)
    assert own.status_code == 409                      # a proposal is already pending for the field
    assert post(bob, decision, decision="approve").status_code == 403

    # 7. a different administrator decides (the proposer here is a reviewer; the self-approval case is below)
    approved = post(dave, decision, decision="approve", note="Matches the paper POD.")

    assert approved.status_code == 200 and approved.json()["decision"] == "approved"
    assert post(alice, decision, decision="approve").status_code == 409

    # 8. approval records the corrected run for that shipment
    recompute = approved.json()["recompute"]

    assert recompute["status"] == "recorded" and recompute["shipments"] == ["SHJ2026001"]
    new_run = recompute["run_id"]
    inputs = bob.get(f"/api/history/runs/{new_run}/inputs?shipment_id={A}").json()
    field = next(i for i in inputs["inputs"] if i["field_name"] == "delivered_pieces")

    assert field["source"] == "approved_correction" and field["value"] == 48 and field["correction_id"] == correction_id and field["page"] is None

    # 9. the previous run is unchanged and marked superseded
    old = bob.get("/api/history/runs/1/inputs").json()

    assert old["superseded_by"] == {"SHJ2026001": new_run, "SHJ2026002": None, "SHJ2026701": None}      # only the corrected shipment
    assert next(i for i in old["inputs"] if i["field_name"] == "delivered_pieces")["source"] == "legacy_reader"
    assert next(i for i in old["inputs"] if i["field_name"] == "delivered_pieces")["value"] == 45

    # 10. other shipments are unaffected
    now = {s: bob.get(f"/api/shipments/{s}").json()["result"] for s in (A, B)}

    assert now[B] == baseline[B] and now[A]["quantity"]["pod"] == 48 and now[A]["quantity"]["status"] == "MATCH"
    assert {k: v for k, v in now[A].items() if k not in ("quantity", "discrepancies", "final_status")} ==         {k: v for k, v in baseline[A].items() if k not in ("quantity", "discrepancies", "final_status")}
    assert bob.get(f"/api/history/runs/{new_run}/inputs").json()["inputs"][0]["shipment_key"] == "SHJ2026001"
    assert {i["shipment_key"] for i in bob.get(f"/api/history/runs/{new_run}/inputs").json()["inputs"]} == {"SHJ2026001"}

    # 12. the assistant reports the effective value and its origin
    reply = Assistant(rig.state.service, BOB).ask("Do the BOL and POD piece counts match?", A)

    assert reply["status"] == "answered" and any("reviewer-approved correction" in u for u in reply["uncertainty"])
    assert any(c["value_origin"] == "approved_correction" and c["page"] is None for c in reply["citations"])


def test_a_failed_recording_is_stale_and_the_retry_endpoint_records_it_once(rig, monkeypatch):
    """Step 11 of the workflow."""

    alice, dave = rig.client("alice"), rig.client("dave")
    record_run(rig)
    pod_id = extract(rig, POD_A)
    proposal = propose(rig, pod_id, "delivered_pieces", "48")

    def broken(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(audit_history.HistoryStore, "record_run", broken)
        approved = post(dave, f"/api/extractions/corrections/{proposal['id']}/decision", decision="approve")

    assert approved.status_code == 200 and approved.json()["recompute"]["status"] == "failed"
    assert "locked" not in approved.text and "sqlite" not in approved.text.lower()

    stale = alice.get("/api/extractions/staleness").json()

    assert [s["shipment_key"] for s in stale["stale"]] == ["SHJ2026001"] and stale["corrections_available"] is True
    assert rig.client("bob").get("/api/shipments/" + A).json()["result"]["quantity"]["pod"] == 48          # the audit already reads it

    runs_before = len(rig.state.service.history_store().runs())
    retry = post(alice, "/api/extractions/recompute")

    assert retry.status_code == 200 and retry.json()["recorded"] is True
    assert len(rig.state.service.history_store().runs()) == runs_before + 1
    assert post(alice, "/api/extractions/recompute").json() == {"recorded": False, "run_id": None, "shipments": []}
    assert alice.get("/api/extractions/staleness").json()["stale"] == []


def test_an_administrators_own_proposal_cannot_be_approved_by_them(rig):
    alice, dave = rig.client("alice"), rig.client("dave")
    pod_id = extract(rig, POD_A)
    proposal = post(alice, f"/api/extractions/{pod_id}/corrections", field="delivered_pieces", value="48", reason=REASON, based_on=None).json()
    url = f"/api/extractions/corrections/{proposal['id']}/decision"

    assert post(alice, url, decision="approve").status_code == 403
    assert post(dave, url, decision="reject", note="The paper POD says 45 pieces.").status_code == 200
    assert rig.client("bob").get("/api/shipments/" + A).json()["result"]["quantity"]["pod"] == 45


# ---- permissions and CSRF on every endpoint -------------------------------------------------------

def endpoints(pod_id, correction_id, run_id):
    body = {"field": "tax", "value": "1", "reason": REASON, "based_on": None}

    return [
        ("get", "/api/extractions", None, "reviewer"), ("get", f"/api/extractions/{pod_id}", None, "reviewer"),
        ("get", "/api/extractions/pending", None, "reviewer"), ("get", "/api/extractions/staleness", None, "reviewer"),
        ("get", f"/api/history/runs/{run_id}/inputs", None, "reviewer"),
        ("get", "/api/extractions/integrity", None, "admin"),
        ("post", "/api/extractions/run", {"document": POD_A}, "admin"),
        ("post", "/api/extractions/recompute", {}, "admin"),
        ("post", f"/api/extractions/corrections/{correction_id}/decision", {"decision": "reject", "note": "Not a real correction."}, "admin"),
        ("post", f"/api/extractions/{pod_id}/corrections", body, "reviewer"),
    ]


def test_every_endpoint_enforces_sign_in_and_role(rig):
    record_run(rig)
    pod_id = extract(rig, POD_A)
    pending = propose(rig, pod_id, "delivered_pieces", "48", who=ALICE)

    for method, url, body, minimum in endpoints(pod_id, pending["id"], 1):
        kwargs = {"json": body} if body is not None else {}

        assert getattr(rig.client(), method)(url, **kwargs).status_code == 401, (method, url)

        reviewer = getattr(rig.client("bob"), method)(url, **kwargs).status_code
        admin = getattr(rig.client("dave"), method)(url, **kwargs).status_code

        if minimum == "admin":
            assert reviewer == 403, (method, url, reviewer)

        assert admin not in (401, 403) and (minimum == "admin" or reviewer not in (401, 403)), (method, url, reviewer, admin)


def test_every_state_changing_endpoint_requires_the_client_header(rig):
    pod_id = extract(rig, POD_A)
    pending = propose(rig, pod_id, "delivered_pieces", "48")

    for method, url, body, _ in endpoints(pod_id, pending["id"], 1):
        if method != "post":
            continue

        client = rig.client("alice")
        client.headers.pop("X-LogiDoc-Client")
        reply = client.post(url, json=body)

        assert reply.status_code == 403 and "X-LogiDoc-Client" in reply.json()["detail"], url

    assert rig.state.service.shipment(A)["result"]["quantity"]["pod"] == 45


def test_local_mode_cannot_approve_anything(tmp_path):
    local = Rig(tmp_path, "local")
    client = local.client()
    pod_id = client.post("/api/extractions/run", json={"document": POD_A}).json()["id"]
    proposal = post(client, f"/api/extractions/{pod_id}/corrections", field="delivered_pieces", value="48", reason=REASON, based_on=None).json()
    reply = post(client, f"/api/extractions/corrections/{proposal['id']}/decision", decision="approve")

    assert proposal["authenticated"] is False and reply.status_code == 403
    assert client.get("/api/shipments/" + A).json()["result"]["quantity"]["pod"] == 45


# ---- status codes ------------------------------------------------------------------------------------

@pytest.mark.parametrize("method, url, body, status", [
    ("get", "/api/extractions/99999999999999999999", None, 422),
    ("get", "/api/extractions/0", None, 422),
    ("get", "/api/extractions/-1", None, 422),
    ("get", "/api/extractions/abc", None, 422),
    ("get", "/api/extractions/4242", None, 404),
    ("post", "/api/extractions/99999999999999999999/corrections", {"field": "tax", "value": "1", "reason": REASON, "based_on": None}, 422),
    ("post", "/api/extractions/4242/corrections", {"field": "tax", "value": "1", "reason": REASON, "based_on": None}, 404),
    ("post", "/api/extractions/corrections/99999999999999999999/decision", {"decision": "approve"}, 422),
    ("post", "/api/extractions/corrections/4242/decision", {"decision": "approve"}, 404),
    ("post", "/api/extractions/corrections/1/decision", {"decision": "maybe"}, 422),
    ("post", "/api/extractions/corrections/1/decision", {}, 422),
    ("post", "/api/extractions/run", {"document": "nothing.pdf"}, 404),
    ("post", "/api/extractions/run", {}, 422),
    ("post", "/api/extractions/recompute", {"shipment_id": "NOPE-1"}, 404),
    ("get", "/api/history/runs/99999999999999999999/inputs", None, 422),
    ("get", "/api/history/runs/4242/inputs", None, 404),
])
def test_invalid_requests_get_the_right_status_and_never_a_500(rig, method, url, body, status):
    record_run(rig)
    extract(rig, POD_A)
    kwargs = {"json": body} if body is not None else {}
    reply = getattr(rig.client("alice"), method)(url, **kwargs)

    assert reply.status_code == status, reply.text
    assert "Traceback" not in reply.text and str(rig.raw) not in reply.text


def test_bad_correction_bodies_are_422_and_a_huge_base_is_refused(rig):
    pod_id = extract(rig, POD_A)
    bob = rig.client("bob")
    url = f"/api/extractions/{pod_id}/corrections"
    good = {"field": "delivered_pieces", "value": "48", "reason": REASON, "based_on": None}

    for change in ({"value": "lots"}, {"field": "nonsense"}, {"field": "shipment_id", "value": "SHJ-2026-002"}, {"reason": "no"},
                   {"based_on": 99999999999999999999}, {"based_on": 0}, {"based_on": "x"}, {"value": ""}, {"field": "x" * 100}):
        reply = post(bob, url, **{**good, **change})

        assert reply.status_code == 422, (change, reply.text)

    assert bob.post(url, content="not json", headers={"Content-Type": "application/json"}).status_code == 422
    assert rig.state.service.shipment(A)["inputs"]["applied_correction_ids"] == []


# ---- defects found by this pass ----------------------------------------------------------------------------

def test_concurrent_recomputation_records_one_run(rig):
    record_run(rig)
    proposal = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")
    ExtractionStore(rig.locations.extraction_path()).decide(DAVE, proposal["id"], "approve")
    rig.state.service.invalidate()
    before = len(rig.state.service.history_store().runs())
    results = []

    def attempt():
        results.append(rig.state.service.record_corrected_run(ALICE))

    threads = [threading.Thread(target=attempt) for _ in range(6)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    runs = rig.state.service.history_store().runs()

    assert len(runs) == before + 1 and [r["recorded"] for r in results].count(True) == 1
    assert sum(len(r["supersedes"]) for r in runs) == 1 and rig.state.service.staleness() == []


def test_a_damaged_correction_store_is_reported_not_silently_ignored(rig):
    alice, bob = rig.client("alice"), rig.client("bob")
    record_run(rig)
    approved = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")
    rig.state.service.decide_correction(DAVE, approved["id"], "approve")

    assert bob.get("/api/shipments/" + A).json()["result"]["quantity"]["pod"] == 48

    path = rig.locations.extraction_path()
    good = path.read_bytes()
    path.write_bytes(b"this is not a database" * 50)
    rig.state.service.invalidate()

    # The audit still answers (from the documents) but says that corrections could not be read
    shipment = bob.get("/api/shipments/" + A).json()
    inputs = rig.state.service.shipment(A)["inputs"]

    assert shipment["result"]["quantity"]["pod"] == 45
    assert inputs["corrections_unavailable"] is True and {"correction_id": None, "reason": "correction_store_unavailable"} in inputs["ignored"]
    assert all("correction_store_unavailable" in f["warnings"] for f in inputs["fields"])
    assert alice.get("/api/extractions/staleness").json()["corrections_available"] is False

    reply = Assistant(rig.state.service, BOB).ask("Do the BOL and POD piece counts match?", A)

    assert UNAVAILABLE_NOTE in reply["uncertainty"]

    # Extraction endpoints fail closed (503, never an empty list), and nothing leaks: no path, no document text
    for url in ("/api/extractions", "/api/extractions/pending", "/api/extractions/integrity"):
        failed = alice.get(url)

        assert failed.status_code == 503 and failed.json() == {"detail": "The extraction store cannot be read."}

    # A run recorded now says so
    recorded = audit_history.record_run_as(ALICE, rig.state.service.history_store(), rig.state.service.snapshot(),
                                           __import__("audit_analytics").build_exceptions(rig.state.service.snapshot()), label="while damaged")
    inputs_row = rig.state.service.history_store()
    note = json.loads(sqlite3.connect(rig.locations.history_db).execute(
        "SELECT ignored FROM run_input_sets WHERE run_id = ? AND shipment_key = 'SHJ2026001'", (recorded["run_id"],)).fetchone()[0])

    assert {"correction_id": None, "reason": "correction_store_unavailable"} in note and inputs_row

    # Recovery: the same file restored, the approval applies again
    path.write_bytes(good)
    rig.state.service.invalidate()

    assert bob.get("/api/shipments/" + A).json()["result"]["quantity"]["pod"] == 48
    assert alice.get("/api/extractions/staleness").json()["corrections_available"] is True


def test_a_store_from_a_newer_version_is_unavailable_not_ignored(rig):
    extract(rig, POD_A)
    path = rig.locations.extraction_path()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_info SET version = 99")
    connection.commit()
    connection.close()
    rig.state.service.invalidate()

    assert rig.state.service.corrections_available() is False
    assert rig.state.service.shipment(A)["inputs"]["corrections_unavailable"] is True


def test_an_older_store_without_approvals_is_not_an_error(tmp_path):
    rig = Rig(tmp_path, "required")
    path = rig.locations.extraction_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    v3_store(path)
    rig.state.service.invalidate()

    assert rig.state.service.corrections_available() is True
    assert "corrections_unavailable" not in rig.state.service.shipment(A)["inputs"]


# ---- real processes -------------------------------------------------------------------------------------------

DECIDER = """
import sys, time
sys.path[:0] = [{src!r}]
from authz import Principal
from extraction_store import ExtractionStore, InvalidTransition
who = Principal(sys.argv[2], "admin", True, 1)
while time.time() < float(sys.argv[3]):
    time.sleep(0.001)
try:
    ExtractionStore(sys.argv[1]).decide(who, int(sys.argv[4]), "approve", "approved by a separate process")
    print("ok")
except InvalidTransition:
    print("refused")
"""


def test_two_processes_deciding_the_same_correction_record_one_decision(rig, tmp_path):
    proposal = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")
    script = tmp_path / "decider.py"
    script.write_text(DECIDER.format(src=str(PROJECT_ROOT / "src")), encoding="utf-8")
    go = time.time() + 3
    procs = [subprocess.Popen([sys.executable, str(script), str(rig.locations.extraction_path()), name, str(go), str(proposal["id"])],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ("carol-admin", "dave")]
    outputs = [p.communicate(timeout=60) for p in procs]

    assert sorted(o[0].strip() for o in outputs) == ["ok", "refused"], outputs
    assert all(p.returncode == 0 for p in procs)
    assert ExtractionStore(rig.locations.extraction_path()).verify_history()["decisions_checked"] == 1


def test_a_reader_during_a_write_gets_a_consistent_audit(rig):
    """A decision committed from another thread while audits are read is either seen whole or not at all."""

    proposal = propose(rig, extract(rig, POD_A), "delivered_pieces", "48")
    seen, stop = set(), threading.Event()

    def reader():
        while not stop.is_set():
            shipment = rig.state.service.shipment(A)
            seen.add((shipment["result"]["quantity"]["pod"], tuple(shipment["inputs"]["applied_correction_ids"])))

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.2)
    ExtractionStore(rig.locations.extraction_path()).decide(DAVE, proposal["id"], "approve")
    time.sleep(0.5)
    stop.set()
    thread.join()

    assert seen <= {(45, ()), (48, (proposal["id"],))} and (48, (proposal["id"],)) in seen


# ---- older databases with real records -----------------------------------------------------------------------------

def test_a_version_1_store_with_records_upgrades_to_the_current_one(tmp_path):
    path = tmp_path / "v1.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")

    for statement in store_module.statements(store_module.MIGRATIONS[1]):
        connection.execute(statement)

    connection.execute("INSERT INTO schema_info VALUES (1)")
    connection.execute("INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, shipment_key, payload, created_at) "
                       "VALUES ('a.pdf', 'h', '1.0', 'POD', 'extracted', 'SHJ2026001', '{\"fields\": {}}', 'then')")
    connection.commit()
    connection.close()

    upgrade(path)
    store = ExtractionStore(path)

    assert store.version() == store_module.SCHEMA_VERSION and [r["filename"] for r in store.list()] == ["a.pdf"]
    assert store.verify_history()["unchained_legacy"] == 1 and store.pending() == []


def test_a_version_2_store_keeps_its_corrections_as_legacy(tmp_path):
    path = tmp_path / "v2.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")

    for version in (1, 2):
        for statement in store_module.statements(store_module.MIGRATIONS[version]):
            connection.execute(statement)

    connection.execute("INSERT INTO schema_info VALUES (2)")
    connection.execute("INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, shipment_key, payload, created_at) "
                       "VALUES ('a.pdf', 'h', '1.0', 'POD', 'extracted', 'SHJ2026001', '{\"fields\": {}}', 'then')")
    connection.execute("INSERT INTO field_corrections (extraction_id, field_name, previous, corrected, reason, actor, authenticated, at) "
                       "VALUES (1, 'delivered_pieces', '{}', '48', 'an old correction', 'bob', 1, 'then')")
    connection.commit()
    connection.close()

    upgrade(path)
    store = ExtractionStore(path)
    legacy = store.corrections(1)[0]

    assert store.version() == store_module.SCHEMA_VERSION and legacy["status"] == "legacy" and legacy["legacy"] is True
    assert store.approved_corrections("SHJ2026001") == [] and store.verify_history()["ok"]


# ---- leaks and size ---------------------------------------------------------------------------------------------------

def test_no_document_text_or_path_reaches_extraction_responses_or_logs(rig, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    corpus_pdf(rig.raw, "INVOICE_SHJ-2026-990.pdf", "FREIGHT INVOICE", "SHJ-2026-990",
               [("Carrier", "Test Lines"), ("Base Freight", "$100.00"), ("Total Invoice Amount", f"SEE {SENTINEL}"),
                ("Invoice Date", SENTINEL), ("Payment Terms", f"Net 30\n{SENTINEL} footer")])
    alice, bob = rig.client("alice"), rig.client("bob")
    record_run(rig)
    replies = []

    run = alice.post("/api/extractions/run", json={"document": "INVOICE_SHJ-2026-990.pdf"})
    replies.append(run)
    extraction_id = run.json()["id"]

    for url in ("/api/extractions", f"/api/extractions/{extraction_id}", "/api/extractions/pending", "/api/extractions/staleness",
                "/api/extractions/integrity", "/api/history/runs/1/inputs"):
        replies.append(alice.get(url))

    replies.append(post(bob, f"/api/extractions/{extraction_id}/corrections", field="invoice_total", value="100", reason=REASON, based_on=None))
    replies.append(post(bob, f"/api/extractions/{extraction_id}/corrections", field="invoice_total", value=SENTINEL, reason=REASON, based_on=None))
    replies.append(post(alice, "/api/extractions/recompute"))

    for reply in replies:
        assert SENTINEL not in reply.text and str(tmp_path) not in reply.text and "Traceback" not in reply.text, reply.request.url

    assert SENTINEL not in caplog.text
    assert SENTINEL.encode() not in b"".join(p.read_bytes() for p in tmp_path.rglob("*.sqlite3*"))


def test_a_large_pdf_is_extracted_in_bounded_time(rig):
    import pymupdf

    document = pymupdf.open()

    for number in range(400):
        page = document.new_page()
        page.insert_text((40, 60), f"BILL OF LADING page {number}\nShipment Number: SHJ-2026-991\nNumber of Pieces:\n5", fontsize=9)

    document.save(str(rig.raw / "BOL_SHJ-2026-991.pdf"))
    document.close()
    started = time.time()
    reply = rig.client("alice").post("/api/extractions/run", json={"document": "BOL_SHJ-2026-991.pdf"})

    assert reply.status_code == 200 and time.time() - started < 30
    assert rig.client("bob").get(f"/api/extractions/{reply.json()['id']}").json()["payload"]["pages"] == 400
