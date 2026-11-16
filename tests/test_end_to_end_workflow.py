"""
One synthetic shipment from upload to report, through the real API, then
through a backup and a restore (Sprint 4, phase 2).

SHJ-2026-101 (tests/test_freight_audit.write_contracted_shipment): a bill of
lading, a proof of delivery, an invoice billing the fuel surcharge at $200
against the $180 of its rate confirmation, and the rate confirmation. Three
users: alice and dave (administrators), bob (reviewer).

The journey (test_one_shipment_from_upload_to_report):
     1. upload the four PDFs                    9. confirm and record the re-audit
     2. extract and classify them              10. the original audit is unchanged
     3. reconcile the shipment                 11. an exception's lifecycle and history
     4. run the freight audit (and record it)  12. the shipment's risk
     5. propose a line-item correction         13. the freight audit report
     6. self-approval refused                  14. its CSV and PDF
     7. approved by another administrator      15. provenance and permissions
     8. preview the controlled re-audit

Then (test_a_restored_backup_reproduces_the_audit): the whole environment is
backed up, verified and restored into a new folder, and the restored
application gives the same business content: audit, approved corrections,
history, exception states, risk, report and CSV.

Deterministic: stand-in embeddings, no model, no network; everything in
temporary folders, never the installation's data.
"""

import csv
import io
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import backup  # noqa: E402
import config  # noqa: E402
import freight_audit as fa  # noqa: E402
import ingestion_manager  # noqa: E402
from api.app import create_app  # noqa: E402
from api.core import CLIENT_HEADER, ApiSettings, ApiState  # noqa: E402
from audit_history import HistoryStore  # noqa: E402
from audit_service import Locations  # noqa: E402
from auth import UserStore  # noqa: E402
from authz import ADMIN, REVIEWER  # noqa: E402
from review_store import Policy, ReviewView  # noqa: E402
from test_freight_audit import SHIPMENT, write_contracted_shipment  # noqa: E402
from test_ingestion_manager import fake_embedding  # noqa: E402

PASSWORD = "correct horse battery"
KEY = "SHJ2026101"
INVOICE = "INVOICE_SHJ-2026-101.pdf"
REASON = "The carrier's credit note CN-17 reduces the fuel surcharge to the contracted amount."
DOCUMENT_TYPES = {"BOL_SHJ-2026-101.pdf": "BOL", "POD_SHJ-2026-101.pdf": "POD", INVOICE: "INVOICE",
                  "RATE_CONFIRMATION_SHJ-2026-101.pdf": "RATE_CONFIRMATION"}


class Environment:
    """An installation in a folder, laid out as a restore lays it out: data/ (documents, databases) and chroma_db/."""

    def __init__(self, root, create_users=True):

        self.root = Path(root)
        self.data = self.root / "data"
        (self.data / "raw").mkdir(parents=True, exist_ok=True)
        self.locations = Locations(
            self.data / "raw", self.root / "chroma_db", self.data / "audit_reviews.sqlite3",
            self.data / "audit_history.sqlite3", self.data / "ingestion_register.sqlite3",
            self.data / "quarantine", self.data / "archive",
        )
        self.auth_db = self.data / "auth.sqlite3"

        if create_users:
            users = UserStore(self.auth_db)
            for name, role in (("alice", ADMIN), ("dave", ADMIN), ("bob", REVIEWER)):
                users.create_user(name, PASSWORD, role)

        settings = ApiSettings(environ={"LOGIDOC_AUTH_MODE": "required"}, rate_limits=())
        self.state = ApiState(settings, self.locations, self.auth_db, Policy(), backup_dir=self.root / "backups")
        self.app = create_app(self.state)

    def client(self, user=None):

        client = TestClient(self.app, raise_server_exceptions=False, headers={CLIENT_HEADER: "web"})

        if user:
            reply = client.post("/api/auth/login", json={"username": user, "password": PASSWORD})
            assert reply.status_code == 200, reply.text

        return client


def charge(audit, name):

    return next(item for item in audit["charges"] if item["charge"] == name)


# ============================================================
# The journey
# ============================================================

@pytest.fixture(scope="module")
def journey(tmp_path_factory):
    """Runs steps 1 to 15 once, checking each as it goes; returns the environment and what was seen."""

    root = tmp_path_factory.mktemp("journey")
    saved = ingestion_manager.create_embedding
    ingestion_manager.create_embedding = fake_embedding

    try:
        env = Environment(root / "installation")
        seen = run_journey(env, root / "incoming")
    finally:
        ingestion_manager.create_embedding = saved

    return env, seen


def run_journey(env, incoming):

    alice, dave, bob, nobody = env.client("alice"), env.client("dave"), env.client("bob"), env.client()
    seen = {}

    # 1. Upload: an administrator's four PDFs are stored and indexed; a reviewer may not upload
    incoming.mkdir()
    write_contracted_shipment(incoming)
    files = [("files", (path.name, path.read_bytes(), "application/pdf")) for path in sorted(incoming.glob("*.pdf"))]

    assert bob.post("/api/ingestion/upload", files=files).status_code == 403
    uploaded = alice.post("/api/ingestion/upload", files=files)
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert (body["indexed"], body["failed"], body["corpus_changed"]) == (4, 0, True)
    assert {item["filename"]: item["document_type"] for item in body["files"]} == DOCUMENT_TYPES
    assert {item["shipment_id"] for item in body["files"]} == {SHIPMENT}

    # 2. Extraction and classification: every document typed and tied to the shipment; the invoice's fields extracted
    documents = {item["filename"]: item for item in alice.get("/api/documents").json()["files"]}
    assert {name: item["document_type"] for name, item in documents.items()} == DOCUMENT_TYPES
    assert all(item["status"] == "parsed" and item["shipment_key"] == KEY for item in documents.values())

    extracted = alice.post("/api/extractions/run", json={"document": INVOICE})
    assert extracted.status_code == 200, extracted.text
    extraction_id = extracted.json()["id"]
    detail = alice.get(f"/api/extractions/{extraction_id}").json()
    fuel = next(item for item in detail["line_items"] if item["extracted"]["description"] == "Fuel Surcharge")
    assert fuel["extracted"]["amount"] == 200.0 and fuel["approved"] is None
    seen["extraction_id"], seen["fuel_index"] = extraction_id, fuel["index"]

    # 3. Reconciliation: the four documents of one shipment, reconciled
    shipment = alice.get(f"/api/shipments/{SHIPMENT}").json()
    assert shipment["shipment_key"] == KEY and shipment["result"]["success"] is True
    assert shipment["result"]["final_status"] == "NEEDS REVIEW"

    # 4. The freight audit: the fuel surcharge billed above the rate confirmation; recorded as the month-end run
    audit = alice.get(f"/api/shipments/{SHIPMENT}/freight-audit").json()["freight_audit"]
    assert audit["outcome"] == fa.DISCREPANCY
    assert charge(audit, "Fuel surcharge")["outcome"] == fa.DISCREPANCY
    seen["risk_before"] = alice.get(f"/api/risk/shipments/{SHIPMENT}").json()

    first = alice.post("/api/history/runs", json={"label": "Month end"})
    assert first.status_code == 201, first.text
    seen["first_run"] = first.json()["run_id"]
    original = alice.get(f"/api/history/shipments/{SHIPMENT}").json()
    assert [entry["snapshot"]["run_id"] for entry in original] == [seen["first_run"]]

    # 5. A line-item correction, proposed by an administrator with its reason
    proposal = alice.post(f"/api/extractions/{extraction_id}/line-items/{fuel['index']}/corrections",
                          json={"amount": "180.00", "reason": REASON, "based_on": None})
    assert proposal.status_code == 200, proposal.text
    proposal = proposal.json()
    assert proposal["status"] == "pending" and proposal["corrected"]["amount"] == 180.0
    # Pending: the audit still reads the invoice as printed
    assert charge(alice.get(f"/api/shipments/{SHIPMENT}/freight-audit").json()["freight_audit"],
                  "Fuel surcharge")["outcome"] == fa.DISCREPANCY

    # 6. Self-approval refused, and a reviewer cannot decide at all
    own = alice.post(f"/api/extractions/corrections/{proposal['id']}/decision", json={"decision": "approve"})
    assert own.status_code == 403 and "own correction" in own.json()["detail"]
    assert bob.post(f"/api/extractions/corrections/{proposal['id']}/decision",
                    json={"decision": "approve"}).status_code == 403

    # 7. Approved by another administrator: no run is recorded by the approval itself
    approved = dave.post(f"/api/extractions/corrections/{proposal['id']}/decision",
                         json={"decision": "approve", "note": "Credit note CN-17 checked."})
    assert approved.status_code == 200, approved.text
    assert approved.json()["recompute"] == {"status": "re_audit_required", "shipments": [KEY]}
    seen["correction_id"] = proposal["id"]

    # 8. The controlled re-audit, previewed
    preview = alice.get(f"/api/shipments/{SHIPMENT}/re-audit").json()
    assert preview["needed"] is True and preview["recorded"]["run_id"] == seen["first_run"]
    assert preview["corrections"]["added"] == [proposal["id"]]

    # 9. Confirmed by an administrator, with a reason: recorded as a new run that supersedes the first
    assert bob.post(f"/api/shipments/{SHIPMENT}/re-audit",
                    json={"token": preview["token"], "reason": "Credit note CN-17 applied."}).status_code == 403
    recorded = alice.post(f"/api/shipments/{SHIPMENT}/re-audit",
                          json={"token": preview["token"], "reason": "Credit note CN-17 applied."})
    assert recorded.status_code == 201, recorded.text
    recorded = recorded.json()
    assert recorded["previous_run_id"] == seen["first_run"] and recorded["corrections_added"] == [proposal["id"]]
    seen["second_run"] = recorded["run_id"]

    after = alice.get(f"/api/shipments/{SHIPMENT}/freight-audit").json()["freight_audit"]
    assert charge(after, "Fuel surcharge")["outcome"] != fa.DISCREPANCY
    assert [item["correction_id"] for item in after["corrections_applied"]] == [proposal["id"]]

    # 10. The original audit is unchanged, superseded, and cannot be rewritten
    timeline = {entry["snapshot"]["run_id"]: entry["snapshot"] for entry in
                alice.get(f"/api/history/shipments/{SHIPMENT}").json()}
    assert sorted(timeline) == [seen["first_run"], seen["second_run"]]
    assert timeline[seen["first_run"]] == original[0]["snapshot"]
    assert timeline[seen["second_run"]]["digest"] != original[0]["snapshot"]["digest"]
    history = HistoryStore(env.locations.history_db)
    assert history.superseded_by(seen["first_run"], KEY) == seen["second_run"]

    with sqlite3.connect(env.locations.history_db) as connection:
        for statement in ("UPDATE shipment_snapshots SET digest = 'rewritten'", "DELETE FROM shipment_snapshots",
                          "UPDATE freight_audit_records SET outcome = 'PASS'"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                connection.execute(statement)

    # 11. An exception's lifecycle: shown as open, assigned, started, resolved with evidence, with its history
    exceptions = {row["category"]: row for row in alice.get("/api/exceptions").json()["exceptions"]}
    detention = exceptions["Freight audit: Detention Charge"]
    assert detention["status"] == "open" and detention["detected"] is True
    key = detention["exception_key"]

    def act(client, action, **body):
        reply = client.post(f"/api/exceptions/{key}/actions", json={"action": action, **body})
        return reply

    assert act(bob, "dismiss", note="Not a real problem at all.").status_code == 403     # a reviewer may not dismiss
    assert act(bob, "assign", assignee="bob").status_code == 200
    assert act(bob, "start").status_code == 200
    assert act(bob, "note", note="Detention of 2 hours is on the proof of delivery.").status_code == 200
    resolved = act(bob, "resolve", note="Detention confirmed by the consignee's sign-off.",
                   evidence=["POD_SHJ-2026-101.pdf p.1"])
    assert resolved.status_code == 200, resolved.text

    events = [event["event_type"] for event in alice.get(f"/api/exceptions/{key}/history").json()]
    assert events == ["detected", "assign", "start", "note", "resolve"]
    review = alice.get(f"/api/exceptions/{key}").json()["review"]
    assert (review["status"], review["assignee"], review["resolved_by"]) == ("resolved", "bob", "bob")
    verified = alice.post("/api/admin/verify-history")
    assert verified.status_code == 200 and verified.json()["ok"] is True

    # 12. Risk: an explained score whose factors add up, lower after the correction and the resolution
    risk = alice.get(f"/api/risk/shipments/{SHIPMENT}").json()
    assert risk["score"] == sum(factor["points"] for factor in risk["factors"])
    assert risk["score"] < seen["risk_before"]["score"]
    assert risk["model"]["version"] and risk["model"]["digest"]
    seen["risk"] = risk

    # 13. The freight audit report: the shipment as the audit holds it now, with its risk
    report = alice.get("/api/reporting/freight-audit-summary", params={"shipment": SHIPMENT}).json()
    [row] = report["rows"]
    assert (row["shipment_id"], row["invoice_total"]) == (SHIPMENT, 1675.0)
    assert (row["risk_score"], row["risk_category"]) == (risk["score"], risk["category"])
    seen["report"] = report

    # 14. CSV and PDF: the same rows, formula-protected text, a plain PDF
    exported = alice.get("/api/reporting/freight-audit-summary.csv", params={"shipment": SHIPMENT})
    assert exported.status_code == 200 and exported.headers["content-type"].startswith("text/csv")
    text = exported.content.decode("utf-8-sig")
    assert SHIPMENT in text and "1675.00" in text
    pdf = alice.get("/api/reporting/freight-audit-summary.pdf", params={"shipment": SHIPMENT})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert b"/JavaScript" not in pdf.content and b"/URI" not in pdf.content and b"/EmbeddedFile" not in pdf.content
    seen["csv"] = text

    # 15. Provenance and permissions: where the figures come from, who did what, who may see it
    provenance = json.dumps(report["provenance"])
    assert risk["model"]["version"] in provenance and str(env.root) not in provenance
    corrections = alice.get("/api/reporting/corrections", params={"shipment": SHIPMENT}).json()
    kinds = [(row["event"], row["actor"]) for row in corrections["rows"]]
    assert kinds == [("Correction proposed", "alice"), ("Correction approved", "dave"), ("Re-audit recorded", "alice")]

    assert bob.get("/api/reporting/freight-audit-summary.csv").status_code == 200       # reports.download
    assert bob.post("/api/history/runs", json={"label": "x"}).status_code == 403          # history.record
    assert nobody.get("/api/reporting/freight-audit-summary").status_code == 401
    assert nobody.get(f"/api/shipments/{SHIPMENT}").status_code == 401

    return seen


def test_one_shipment_from_upload_to_report(journey):

    env, seen = journey

    assert seen["second_run"] > seen["first_run"]
    assert seen["risk"]["category"] in ("low", "medium", "high", "critical")
    # The review store holds the exceptions the actions recorded, and its history chain verifies
    assert ReviewView(env.locations.review_db).verify_integrity()["ok"]


# ============================================================
# Backup and restore
# ============================================================

TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2})?")
STABLE_REVIEW = ("exception_key", "status", "assignee", "note", "resolved_by", "signature", "approval_state")
STABLE_RUN = ("id", "label", "shipments_checked", "snapshots_written")


def business_content(env):
    """What the application says about the shipment, without times that change on every computation."""

    alice = env.client("alice")
    audit = alice.get(f"/api/shipments/{SHIPMENT}/freight-audit").json()["freight_audit"]
    detail = alice.get(f"/api/extractions/{alice.get('/api/extractions').json()['extractions'][0]['id']}").json()
    risk = alice.get(f"/api/risk/shipments/{SHIPMENT}").json()
    report = alice.get("/api/reporting/freight-audit-summary", params={"shipment": SHIPMENT}).json()
    csv_rows = list(csv.reader(io.StringIO(
        alice.get("/api/reporting/freight-audit-summary.csv", params={"shipment": SHIPMENT}).content.decode("utf-8-sig"))))
    history = HistoryStore(env.locations.history_db)

    return {
        "final_status": alice.get(f"/api/shipments/{SHIPMENT}").json()["result"]["final_status"],
        "freight_outcome": audit["outcome"],
        "charges": [(item["charge"], item.get("billed"), item.get("contracted"), item["outcome"]) for item in audit["charges"]],
        "corrections_applied": [item["correction_id"] for item in audit["corrections_applied"]],
        "approved_line_items": sorted((item["index"], json.dumps(item["approved"], sort_keys=True))
                                      for item in detail["line_items"] if item["approved"]),
        "runs": [{name: run[name] for name in STABLE_RUN} for run in history.runs(100)],
        "timeline": sorted((entry["snapshot"]["run_id"], entry["snapshot"]["digest"], entry["snapshot"]["final_status"],
                            entry["snapshot"]["invoice_total"])
                           for entry in alice.get(f"/api/history/shipments/{SHIPMENT}").json()),
        "superseded": history.superseded_by(1, KEY),
        "reviews": sorted(tuple(row.get(name) for name in STABLE_REVIEW)
                          for row in ReviewView(env.locations.review_db).states().values()),
        "review_events": [(event["exception_key"], event["event_type"], event["event_hash"])
                          for event in ReviewView(env.locations.review_db).all_events(1000)],
        "risk": (risk["score"], risk["category"], [(f["signal"], f["points"]) for f in risk["factors"]],
                 risk["model"]["version"], risk["model"]["digest"]),
        "report_rows": report["rows"],
        "report_totals": report["totals"],
        # The CSV with its times (generation, and when the audit was computed) masked: everything else must match
        "csv": [[TIME.sub("<time>", cell) for cell in row] for row in csv_rows],
    }


def test_a_restored_backup_reproduces_the_audit(journey, tmp_path):

    env, _ = journey
    original = business_content(env)

    # A verified backup of the whole environment
    sources = backup.Sources.from_locations(env.locations, env.auth_db)
    manifest, folder = backup.create_backup(sources, tmp_path / "backups")
    assert manifest["status"] == "complete"
    assert backup.verify_backup(folder)["ok"] is True

    # Restored into a new folder: never a data folder, the environment's or the installation's
    target = tmp_path / "restored"
    with pytest.raises(backup.RestoreRefused):
        backup.restore_backup(folder, env.root, protected=(env.root,))
    report = backup.restore_backup(folder, target, protected=(env.root, config.DATA_DIR, config.CHROMA_DIR))
    assert report["status"] == "restored", report.get("problems")

    restored = Environment(target, create_users=False)
    copy = business_content(restored)

    for name in original:
        assert copy[name] == original[name], name

    # What the journey established survived the round trip
    assert original["corrections_applied"] and original["approved_line_items"]
    assert [run["id"] for run in copy["runs"]] == [2, 1] and copy["superseded"] == 2
    assert ("resolved", "bob") in {(row[1], row[2]) for row in copy["reviews"]}
    assert ReviewView(restored.locations.review_db).verify_integrity()["ok"]
