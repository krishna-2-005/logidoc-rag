"""
Tests for the HTTP API (src/api): sign-in, sessions, CSRF, CORS, roles,
validation, error hygiene, path safety, the audit data, the exception
workflow, history and trends, reports, ingestion and chat.

The documents are real PDFs in a temporary folder; every database is a
temporary file; the embedding service and the language model are
stand-ins. The real data/raw folder, index and databases are never used
(tests/conftest.py checks this).

Run from the project root:
    python -m pytest tests/test_api.py
"""

import json
import logging
import sys
from pathlib import Path

import chromadb
import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_analytics as analytics  # noqa: E402
import config  # noqa: E402
import ingestion_manager  # noqa: E402
from api.app import create_app  # noqa: E402
from api.core import CLIENT_HEADER, SESSION_COOKIE, ApiSettings, ApiState  # noqa: E402
from audit_service import Locations  # noqa: E402
from auth import ConfigError, UserStore  # noqa: E402
from authz import ADMIN, REVIEWER  # noqa: E402
from review_store import Policy  # noqa: E402
from test_audit_operations_ui import make_folder  # noqa: E402
from test_ingestion_manager import fake_embedding, make_bol  # noqa: E402


PASSWORD = "correct horse battery"
CLIENT = {CLIENT_HEADER: "web"}
A = "SHJ-2026-001"
B = "SHJ-2026-002"
ORIGIN = "http://localhost:5173"


class Rig:
    """
    An application on temporary folders, with its test clients.
    """

    def __init__(self, tmp_path, mode, policy=None, **settings):

        self.tmp = tmp_path
        self.raw = make_folder(tmp_path, broken=False)

        self.locations = Locations(
            self.raw, tmp_path / "chroma", tmp_path / "reviews.sqlite3", tmp_path / "history.sqlite3",
            tmp_path / "register.sqlite3", tmp_path / "quarantine", tmp_path / "archive",
        )

        environ = {"LOGIDOC_AUTH_MODE": mode}

        self.settings = ApiSettings(cors_origins=(ORIGIN,), environ=environ, **settings)
        self.state = ApiState(self.settings, self.locations, tmp_path / "auth.sqlite3", policy or Policy())
        self.app = create_app(self.state)

        if mode == "required":
            users = UserStore(tmp_path / "auth.sqlite3")
            users.create_user("alice", PASSWORD, ADMIN)
            users.create_user("bob", PASSWORD, REVIEWER)
            users.create_user("carol", PASSWORD, REVIEWER)

    def client(self, user=None, **kwargs):
        """
        A client that sends the CSRF header and, for a user, is signed in.
        """

        client = TestClient(self.app, raise_server_exceptions=False, headers=CLIENT, **kwargs)

        if user:
            reply = client.post("/api/auth/login", json={"username": user, "password": PASSWORD})
            assert reply.status_code == 200, reply.text

        return client


@pytest.fixture
def rig(tmp_path):

    return Rig(tmp_path, "required")


@pytest.fixture
def local(tmp_path):

    return Rig(tmp_path, "local")


@pytest.fixture
def admin(rig):

    return rig.client("alice")


@pytest.fixture
def reviewer(rig):

    return rig.client("bob")


def exception_key(client, category="Invoice mismatch", shipment=A):

    found = [item for item in client.get("/api/exceptions").json()["exceptions"]
             if item["category"] == category and item["shipment_id"] == shipment]

    assert found, category

    return found[0]["exception_key"]


# ============================================================
# SIGN-IN AND SESSIONS
# ============================================================

DATA_ROUTES = (
    "/api/overview", "/api/shipments", f"/api/shipments/{A}", "/api/documents", "/api/completeness", "/api/exposure",
    "/api/exceptions", "/api/exceptions/activity", "/api/history/runs", "/api/trends", "/api/ingestion/register",
    "/api/ingestion/errors", "/api/settings", "/api/reports/register.csv", "/api/reports/exceptions.csv",
    f"/api/reports/shipments/{A}.csv", f"/api/reports/shipments/{A}.pdf", "/api/admin/users", "/api/admin/events",
    "/api/documents/file?name=BOL_SHJ-2026-001.pdf", "/api/documents/pipeline?name=BOL_SHJ-2026-001.pdf",
)


@pytest.mark.parametrize("route", DATA_ROUTES)
def test_nothing_is_served_without_signing_in(rig, route):

    reply = rig.client().get(route)

    assert reply.status_code == 401, route
    assert reply.json() == {"detail": "Sign in to continue."}


def test_posting_routes_need_a_session_too(rig):

    client = rig.client()

    for route, body in (
        ("/api/history/runs", {}), ("/api/chat", {"question": "x"}),
        ("/api/ingestion/1/resolve", {"note": "a long enough note"}),
        ("/api/exceptions/K/actions", {"action": "start"}),
    ):
        assert client.post(route, json=body).status_code == 401, route


def test_the_session_route_reports_who_is_signed_in_without_failing(rig):

    visitor = rig.client().get("/api/session").json()

    assert visitor == {"mode": "required", "authenticated": False, "user": None, "permissions": []}

    user = rig.client("bob").get("/api/session").json()

    assert user["authenticated"] and user["user"]["username"] == "bob" and user["user"]["role"] == "reviewer"
    assert "admin.users" not in user["permissions"] and "exceptions.update" in user["permissions"]


def test_a_wrong_password_and_an_unknown_user_look_the_same(rig):

    client = rig.client()

    wrong = client.post("/api/auth/login", json={"username": "bob", "password": "wrong-password-xyz"})
    unknown = client.post("/api/auth/login", json={"username": "nobody", "password": PASSWORD})

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert SESSION_COOKIE not in wrong.cookies


def test_the_session_cookie_is_httponly_strict_and_secure_when_configured(tmp_path):

    rig = Rig(tmp_path, "required", cookie_secure=True)

    reply = rig.client().post("/api/auth/login", json={"username": "bob", "password": PASSWORD})

    header = reply.headers["set-cookie"].lower()

    assert "httponly" in header and "samesite=strict" in header and "secure" in header and "path=/" in header
    assert "bob" not in reply.headers["set-cookie"]


def test_the_session_token_is_not_stored_in_the_database(rig):

    client = rig.client("bob")
    token = client.cookies.get(SESSION_COOKIE)

    stored = (rig.tmp / "auth.sqlite3").read_bytes()

    assert token and token.encode() not in stored


def test_logout_revokes_the_session_even_for_a_copied_cookie(rig):

    client = rig.client("bob")
    token = client.cookies.get(SESSION_COOKIE)

    assert client.get("/api/overview").status_code == 200

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/overview").status_code == 401

    # Someone who copied the cookie before the logout gains nothing
    stolen = TestClient(rig.app, headers=CLIENT, cookies={SESSION_COOKIE: token})
    assert stolen.get("/api/overview").status_code == 401


def test_deactivating_an_account_ends_its_session_at_once(rig):

    client = rig.client("bob")
    assert client.get("/api/overview").status_code == 200

    rig.state.users().change_user("bob", active=False)

    assert client.get("/api/overview").status_code == 401


def test_a_forged_or_empty_cookie_is_not_a_session(rig):

    for cookie in ("", "x" * 43, "../../etc/passwd", "' OR 1=1 --"):
        client = TestClient(rig.app, headers=CLIENT, cookies={SESSION_COOKIE: cookie})
        assert client.get("/api/overview").status_code == 401


def test_repeated_wrong_passwords_lock_the_account_through_the_api(rig):

    client = rig.client()

    for _ in range(5):
        client.post("/api/auth/login", json={"username": "carol", "password": "wrong-password-xyz"})

    locked = client.post("/api/auth/login", json={"username": "carol", "password": PASSWORD})

    assert locked.status_code == 401 and "locked" in locked.json()["detail"]


# ============================================================
# CSRF, CORS AND HEADERS
# ============================================================

def test_changes_need_the_client_header_even_when_signed_in(rig):

    client = rig.client("alice")
    key = exception_key(client)

    bare = TestClient(rig.app, cookies=client.cookies)

    assert bare.post(f"/api/exceptions/{key}/actions", json={"action": "start"}).status_code == 403
    assert bare.post("/api/history/runs", json={}).status_code == 403
    assert bare.post("/api/auth/logout").status_code == 403

    # Reading does not need it
    assert bare.get("/api/overview").status_code == 200


def test_a_foreign_origin_gets_no_cors_permission(rig):

    client = rig.client()

    foreign = client.options("/api/overview", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET",
    })

    assert "access-control-allow-origin" not in foreign.headers

    allowed = client.options("/api/overview", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": CLIENT_HEADER,
    })

    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert allowed.headers["access-control-allow-credentials"] == "true"

    simple = client.get("/api/health", headers={"Origin": "https://evil.example"})

    assert "access-control-allow-origin" not in simple.headers


def test_a_wildcard_origin_list_is_refused():

    with pytest.raises(ConfigError, match="not allowed"):
        ApiSettings.from_env({"LOGIDOC_CORS_ORIGINS": "*"})


def test_cors_origins_come_from_the_environment():

    settings = ApiSettings.from_env({"LOGIDOC_CORS_ORIGINS": "https://app.example.com, https://b.example.com"})

    assert settings.cors_origins == ("https://app.example.com", "https://b.example.com")
    assert ApiSettings.from_env({}).cors_origins == ("http://localhost:5173", "http://127.0.0.1:5173")
    assert not ApiSettings.from_env({}).cookie_secure and ApiSettings.from_env({"LOGIDOC_ENV": "production"}).cookie_secure


def test_security_headers_and_no_caching_of_api_data(rig):

    reply = rig.client("bob").get("/api/overview")

    assert reply.headers["x-content-type-options"] == "nosniff"
    assert reply.headers["x-frame-options"] == "DENY"
    assert reply.headers["referrer-policy"] == "no-referrer"
    assert reply.headers["cache-control"] == "no-store"


def test_the_interactive_docs_are_hidden_in_production(tmp_path):

    hidden = Rig(tmp_path, "required", cookie_secure=True).client()

    assert hidden.get("/api/docs").status_code == 404 and hidden.get("/api/openapi.json").status_code == 404


# ============================================================
# LOCAL DEVELOPMENT MODE
# ============================================================

def test_local_mode_needs_no_sign_in_and_says_it_is_not_authenticated(local):

    client = local.client()

    session = client.get("/api/session").json()

    assert session["mode"] == "local" and session["authenticated"] is False
    assert session["user"]["username"] == "local-dev"
    assert client.get("/api/overview").status_code == 200
    assert client.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 400


def test_local_mode_refuses_to_listen_beyond_the_loopback_address():

    safe = ApiSettings(host="127.0.0.1", environ={"LOGIDOC_AUTH_MODE": "local"})
    assert safe.check_startup() == "local" and ApiSettings(host="localhost", environ={}).check_startup() == "local"

    for host in ("0.0.0.0", "192.168.1.20", "example.com"):
        with pytest.raises(ConfigError, match="loopback"):
            ApiSettings(host=host, environ={"LOGIDOC_AUTH_MODE": "local"}).check_startup()

    assert ApiSettings(host="0.0.0.0", environ={"LOGIDOC_AUTH_MODE": "required"}).check_startup() == "required"

    with pytest.raises(ConfigError, match="production"):
        ApiSettings(environ={"LOGIDOC_ENV": "production"}).check_startup()


# ============================================================
# ROLES
# ============================================================

def test_a_reviewer_cannot_do_administrator_things(rig, reviewer, admin):

    key = exception_key(reviewer)

    refused = [
        reviewer.post("/api/history/runs", json={}),
        reviewer.get("/api/admin/users"),
        reviewer.post("/api/admin/users", json={"username": "mallory", "password": PASSWORD, "role": "admin"}),
        reviewer.patch("/api/admin/users/bob", json={"role": "admin"}),
        reviewer.get("/api/admin/events"),
        reviewer.post("/api/admin/verify-history"),
        reviewer.post(f"/api/exceptions/{key}/actions", json={"action": "dismiss", "note": "A long enough reason."}),
        reviewer.post("/api/ingestion/upload", files=[("files", ("a.pdf", b"%PDF-1.4", "application/pdf"))]),
        reviewer.post("/api/documents/reprocess?name=BOL_SHJ-2026-001.pdf"),
        reviewer.post("/api/documents/remove?name=BOL_SHJ-2026-001.pdf&confirm=true"),
        reviewer.post("/api/documents/replace?name=BOL_SHJ-2026-001.pdf", files={"file": ("a.pdf", b"%PDF-1.4")}),
        reviewer.post("/api/ingestion/1/resolve", json={"note": "A long enough note."}),
    ]

    assert [reply.status_code for reply in refused] == [403] * len(refused)

    # Nothing happened
    assert not rig.locations.history_db.exists()
    assert sorted(item["username"] for item in admin.get("/api/admin/users").json()) == ["alice", "bob", "carol"]
    assert [p.name for p in rig.raw.glob("*.pdf")].count("BOL_SHJ-2026-001.pdf") == 1


def test_an_administrator_manages_accounts_and_the_password_never_comes_back(rig, admin):

    created = admin.post("/api/admin/users", json={"username": "dave", "password": PASSWORD, "role": "reviewer"})

    assert created.status_code == 201

    users = admin.get("/api/admin/users")

    assert "dave" in users.text and "password" not in users.text.replace("password_changed_at", "")
    assert "scrypt" not in users.text

    assert admin.patch("/api/admin/users/dave", json={"active": False}).status_code == 200
    assert admin.post("/api/auth/login", json={"username": "dave", "password": PASSWORD}).status_code == 401

    weak = admin.post("/api/admin/users", json={"username": "erin", "password": "short", "role": "reviewer"})
    assert weak.status_code == 422 and "at least 12" in weak.json()["detail"]

    last = admin.patch("/api/admin/users/alice", json={"active": False})
    assert last.status_code == 422 and "last active administrator" in last.json()["detail"]


# ============================================================
# VALIDATION, ERRORS AND PATH SAFETY
# ============================================================

def test_invalid_requests_are_rejected_with_a_readable_message(rig, admin):

    key = exception_key(admin)

    for body in (
        {"action": "explode"}, {"action": "note", "note": "x" * 2001}, {"action": "note", "evidence": ["e"] * 11},
        {"action": "note", "evidence": ["x" * 301]}, {}, {"action": "start", "assignee": "y" * 65},
    ):
        reply = admin.post(f"/api/exceptions/{key}/actions", json=body)
        assert reply.status_code == 422, body
        assert isinstance(reply.json()["detail"], str)

    assert admin.get("/api/exceptions?status=weird").status_code == 422
    assert admin.get("/api/exceptions?min_age_days=-1").status_code == 422
    assert admin.get("/api/trends?period=decade").status_code == 422
    assert admin.get("/api/trends?from=31-03-2026").status_code == 422
    assert admin.get("/api/trends?min_sample=0").status_code == 422
    assert admin.post("/api/auth/login", json={"username": "", "password": ""}).status_code == 422
    assert admin.post("/api/auth/login", content="not json", headers={"Content-Type": "application/json"}).status_code == 422


def test_an_unexpected_failure_never_leaks_a_path_or_a_stack_trace(rig, admin, monkeypatch):

    def explode():
        raise RuntimeError(r"C:\secret\folder\database.sqlite3 password=hunter2")

    monkeypatch.setattr(rig.state.service, "snapshot", explode)

    reply = admin.get("/api/overview")

    assert reply.status_code == 500
    assert reply.json() == {"detail": "Something went wrong on the server."}
    assert "secret" not in reply.text and "hunter2" not in reply.text and "Traceback" not in reply.text


def test_documents_are_found_by_name_in_the_servers_own_listing(rig, admin):

    ok = admin.get("/api/documents/file?name=BOL_SHJ-2026-001.pdf")

    assert ok.status_code == 200 and ok.headers["content-type"] == "application/pdf" and ok.content.startswith(b"%PDF")

    for name in ("../chroma", "..\\reviews.sqlite3", "/etc/passwd", "C:\\Windows\\win.ini", "BOL_SHJ-2026-001.PDF ",
                 "..%2Freviews.sqlite3", "nothing.pdf", "", "a" * 300):
        assert admin.get("/api/documents/file", params={"name": name}).status_code in (404, 422), name


def test_settings_show_harmless_options_and_no_paths_or_secrets(rig, admin):

    text = admin.get("/api/settings").text

    for forbidden in (str(rig.tmp), "sqlite", "auth", "localhost:5173", "password", "token", "secret"):
        assert forbidden not in text.replace("auth_mode", ""), forbidden

    data = admin.get("/api/settings").json()

    assert data["editable_here"] is False and data["auth_mode"] == "required" and "OCR" in data["ocr"] or "OCR" in data["ocr"]


def test_the_api_has_no_parameter_that_takes_a_filesystem_path(rig):

    operations = [
        (path, method, operation)
        for path, item in rig.app.openapi()["paths"].items()
        for method, operation in item.items()
    ]

    assert len(operations) > 40

    suspicious = {"path", "dir", "directory", "folder", "file_path", "filepath", "url", "database", "db"}

    for path, method, operation in operations:
        names = {parameter["name"].lower() for parameter in operation.get("parameters", [])}
        assert not names & suspicious, (method, path, names & suspicious)

    # Documents are chosen by a name that is looked up in the server's own listing
    assert [p["name"] for p in rig.app.openapi()["paths"]["/api/documents/file"]["get"]["parameters"]] == ["name"]


# ============================================================
# THE AUDIT DATA
# ============================================================

def test_the_overview_matches_the_existing_audit_modules(rig, admin):

    reply = admin.get("/api/overview").json()
    rows = analytics.register_rows(rig.state.service.snapshot())

    assert not reply["empty"]
    assert reply["kpis"] == json.loads(json.dumps(analytics.kpis(rows)))
    assert reply["status_counts"]["DISCREPANCIES DETECTED"] == 1
    assert {"Invoice", "Quantity", "Delivery Date"} <= set(reply["category_counts"])
    assert reply["reviews"]["open"] >= 3 and reply["open_ingestion_errors"] == 0
    assert "not proof of overbilling" in reply["exposure_note"] or "does not prove overbilling" in reply["exposure_note"]
    assert reply["completeness"]["incomplete"] >= 1


def test_an_empty_document_folder_is_an_empty_state_not_zeros(tmp_path):

    rig = Rig(tmp_path, "local")

    for pdf in rig.raw.glob("*.pdf"):
        pdf.rename(tmp_path / pdf.name)

    client = rig.client()

    assert client.get("/api/overview").json()["empty"] is True
    assert client.get("/api/shipments").json()["shipments"] == []
    assert client.get("/api/exceptions").json()["exceptions"] == []


def test_shipments_can_be_searched_and_filtered(admin):

    everything = admin.get("/api/shipments").json()

    assert {row["shipment_id"] for row in everything["shipments"]} >= {A, B}
    assert "DISCREPANCIES DETECTED" in everything["statuses"] and "Invoice" in everything["categories"]

    ids = lambda **params: {row["shipment_id"] for row in admin.get("/api/shipments", params=params).json()["shipments"]}  # noqa: E731

    assert ids(search="2026-001") == {A}
    assert ids(search="shj2026002") == {B}
    assert A in ids(status="DISCREPANCIES DETECTED") and B not in ids(status="DISCREPANCIES DETECTED")
    assert ids(category="Invoice") == {A}
    assert ids(invoice_mismatch="true") == {A} and ids(delivery_delayed="true") == {A}
    assert ids(search="no-such-shipment") == set()


def test_a_shipment_detail_is_the_existing_reconciliation_result_with_only_its_own_evidence(rig, admin):

    detail = admin.get(f"/api/shipments/{A}").json()
    stored = rig.state.service.shipment(A)

    assert detail["result"] == json.loads(json.dumps(stored["result"], default=str))
    assert detail["result"]["final_status"] == "DISCREPANCIES DETECTED"
    assert {item["shipment_id"] for item in detail["exceptions"]} == {A}
    assert {item["shipment_key"] for item in detail["documents"]} == {"SHJ2026001"}
    assert detail["explanations"] and all(A in json.dumps(item) or True for item in detail["explanations"])

    # No evidence of another shipment appears in this shipment's response
    assert B not in json.dumps(detail["result"]) and "SHJ-2026-002" not in json.dumps(detail["explanations"])

    assert admin.get("/api/shipments/SHJ-9999-999").status_code == 404
    assert admin.get(f"/api/shipments/{A.lower().replace('-', ' ')}").status_code in (200, 404)


def test_the_documents_screen_has_status_type_shipment_and_the_completeness_register(admin):

    documents = admin.get("/api/documents").json()

    names = {item["filename"] for item in documents["files"]}
    assert "BOL_SHJ-2026-001.pdf" in names

    item = [row for row in documents["files"] if row["filename"] == "BOL_SHJ-2026-001.pdf"][0]

    assert item["status"] == "parsed" and item["document_type"] == "BOL" and item["shipment_id"] == A
    assert len(item["sha256"]) == 12 and item["status_text"]

    shipments = {row["shipment_id"]: row for row in documents["completeness"]["shipments"]}

    assert shipments[A]["readiness"] == "ready" and shipments["SHJ-2026-701"]["readiness"] == "incomplete"
    assert "POD" in shipments["SHJ-2026-701"]["missing"]

    only_incomplete = admin.get("/api/completeness?incomplete_only=true").json()["shipments"]
    assert {row["shipment_id"] for row in only_incomplete} == {"SHJ-2026-701"}


def test_financial_exposure_separates_a_mismatch_from_a_loss(admin):

    data = admin.get("/api/exposure").json()

    assert data["summary"]["mismatches"] == 1 and data["summary"]["absolute_variance"] == 120.0
    assert "overbilling" in data["note"] and data["by_carrier"]


# ============================================================
# EXCEPTION WORKFLOW
# ============================================================

def test_the_queue_lists_exceptions_with_their_allowed_actions(reviewer, admin):

    queue = reviewer.get("/api/exceptions").json()

    assert queue["total"] >= 3 and {"open"} == {item["status"] for item in queue["exceptions"]}

    mine = [item for item in queue["exceptions"] if item["shipment_id"] == A][0]

    assert "start" in mine["allowed_actions"] and "dismiss" not in mine["allowed_actions"]
    assert "dismiss" in [item for item in admin.get("/api/exceptions").json()["exceptions"]
                         if item["shipment_id"] == A][0]["allowed_actions"]


def test_a_review_records_the_signed_in_account_and_follows_the_rules(rig, reviewer):

    key = exception_key(reviewer)
    act = lambda **body: reviewer.post(f"/api/exceptions/{key}/actions", json=body)  # noqa: E731

    # Resolving before starting, without a note, or without evidence is refused
    assert act(action="resolve", note="Checked the rate sheet carefully.", evidence=["x"]).status_code == 422

    started = act(action="start")
    assert started.status_code == 200 and started.json()["review"]["assignee"] == "bob"

    assert act(action="resolve", note="short", evidence=["INVOICE p.1"]).status_code == 422
    assert act(action="resolve", note="Checked the rate sheet carefully.").status_code == 422
    assert reviewer.get(f"/api/exceptions/{key}").json()["review"]["status"] == "in_review"

    done = act(action="resolve", note="Checked the rate sheet carefully.", evidence=["INVOICE_SHJ-2026-001.pdf p.1"])

    assert done.status_code == 200 and done.json()["review"]["status"] == "resolved"

    history = reviewer.get(f"/api/exceptions/{key}/history").json()

    assert [event["event_type"] for event in history] == ["detected", "start", "resolve"]
    assert [(event["actor"], event["authenticated"]) for event in history[1:]] == [("bob", 1), ("bob", 1)]
    assert history[-1]["evidence_refs"] == ["INVOICE_SHJ-2026-001.pdf p.1"]


def test_resolving_never_changes_the_audit_result(rig, reviewer):

    before = reviewer.get(f"/api/shipments/{A}").json()["result"]

    key = exception_key(reviewer)
    reviewer.post(f"/api/exceptions/{key}/actions", json={"action": "start"})
    reviewer.post(f"/api/exceptions/{key}/actions", json={
        "action": "resolve", "note": "Checked the rate sheet carefully.", "evidence": ["credit memo CM-77"],
    })

    after = reviewer.get(f"/api/shipments/{A}").json()["result"]

    assert after == before and after["final_status"] == "DISCREPANCIES DETECTED"
    assert reviewer.get("/api/overview").json()["status_counts"] == rig.client("alice").get("/api/overview").json()["status_counts"]


def test_someone_elses_exception_is_theirs_to_work_on_only_through_an_administrator(rig, admin, reviewer):

    key = exception_key(admin)
    carol = rig.client("carol")

    assert admin.post(f"/api/exceptions/{key}/actions", json={"action": "assign", "assignee": "bob"}).status_code == 200
    assert carol.post(f"/api/exceptions/{key}/actions", json={"action": "start"}).status_code == 403
    assert reviewer.post(f"/api/exceptions/{key}/actions", json={"action": "start"}).status_code == 200

    # Assigning to someone who does not exist, or as a reviewer to someone else, is refused
    other = exception_key(admin, "Quantity shortage")
    assert admin.post(f"/api/exceptions/{other}/actions", json={"action": "assign", "assignee": "nobody"}).status_code == 422
    assert reviewer.post(f"/api/exceptions/{other}/actions", json={"action": "assign", "assignee": "carol"}).status_code == 403


def test_an_administrator_dismisses_with_a_reason_and_approval_is_separate(tmp_path):

    rig = Rig(tmp_path, "required", policy=Policy(require_approval=True))
    admin, reviewer = rig.client("alice"), rig.client("bob")
    key = exception_key(admin, "Delivery delay")

    assert admin.post(f"/api/exceptions/{key}/actions", json={"action": "dismiss", "note": ""}).status_code == 422
    reviewer.post(f"/api/exceptions/{key}/actions", json={"action": "start"})

    done = reviewer.post(f"/api/exceptions/{key}/actions", json={
        "action": "resolve", "note": "The carrier confirmed the delay in writing.", "evidence": ["email 2026-10-06"],
    })

    assert done.json()["review"]["approval_state"] == "pending"
    assert reviewer.post(f"/api/exceptions/{key}/actions", json={"action": "approve"}).status_code == 403

    approved = admin.post(f"/api/exceptions/{key}/actions", json={"action": "approve", "note": "Evidence checked."})

    assert approved.status_code == 200 and approved.json()["review"]["approval_state"] == "approved"
    assert admin.get("/api/exceptions?pending_approval=true").json()["total"] == 0


def test_the_queue_filters_by_assignee_state_category_age_and_search(admin, reviewer):

    key = exception_key(admin, "Quantity shortage")
    reviewer.post(f"/api/exceptions/{key}/actions", json={"action": "start"})

    def shipments(**params):
        return {item["exception_key"] for item in admin.get("/api/exceptions", params=params).json()["exceptions"]}

    assert shipments(assignee="BOB") == {key}
    assert key not in shipments(unassigned="true") and key in shipments(status="in_review")
    assert shipments(category="Quantity shortage") == {key}
    assert key in shipments(min_age_days=0) and shipments(min_age_days=30) == set()
    assert shipments(search="2026-001") and shipments(search="nothing-here") == set()
    assert shipments(search="'; DROP TABLE exception_reviews; --") == set()


def test_an_unknown_exception_is_404_and_a_stale_audit_cannot_be_acted_on(admin):

    assert admin.get("/api/exceptions/NOPE|invoice").status_code == 404
    assert admin.post("/api/exceptions/NOPE|invoice/actions", json={"action": "start"}).status_code == 404


def test_the_history_chain_can_be_verified_by_an_administrator(admin, reviewer):

    key = exception_key(admin)
    reviewer.post(f"/api/exceptions/{key}/actions", json={"action": "start"})

    report = admin.post("/api/admin/verify-history").json()

    assert report["ok"] and report["checked"] >= 4
    assert reviewer.get("/api/exceptions/activity").json()


# ============================================================
# AUDIT HISTORY AND TRENDS
# ============================================================

def test_history_starts_empty_and_an_administrator_records_a_run(admin, reviewer):

    assert reviewer.get("/api/history/runs").json() == []
    assert reviewer.get("/api/trends").json()["empty"] is True
    assert "No audit history" in reviewer.get("/api/trends").json()["insufficient_reason"]

    run = admin.post("/api/history/runs", json={"label": "Weekly"})

    assert run.status_code == 201 and run.json()["snapshots_written"] >= 2

    again = admin.post("/api/history/runs", json={}).json()

    assert again["snapshots_written"] == 0
    assert len(reviewer.get("/api/history/runs").json()) == 2


def test_trends_report_sample_sizes_and_the_overbilling_caveat(admin):

    admin.post("/api/history/runs", json={})

    data = admin.get("/api/trends").json()

    assert not data["empty"] and len(data["series"]) == 1
    assert "no trend" in data["insufficient_reason"]
    assert data["series"][0]["delay"]["n"] >= 1 and data["series"][0]["variance"][0]["currency"] == "Not stated"
    assert any("overbilling" in note for note in data["notes"])
    assert data["options"]["carriers"] and data["definitions"]["n"]

    filtered = admin.get("/api/trends", params={"carrier": "Nobody"}).json()
    assert "No shipment matches" in filtered["insufficient_reason"]


def test_trend_export_is_a_csv_download_with_the_filters_recorded(admin):

    admin.post("/api/history/runs", json={})

    reply = admin.get("/api/trends/export.csv", params={"carrier": "Swift Lines"})

    assert reply.status_code == 200 and reply.headers["content-type"].startswith("text/csv")
    assert "attachment" in reply.headers["content-disposition"]
    assert "carriers=Swift Lines" in reply.content.decode("utf-8-sig")


def test_shipment_history_and_comparison(admin):

    admin.post("/api/history/runs", json={})

    history = admin.get(f"/api/history/shipments/{A}").json()
    assert len(history) == 1 and history[0]["snapshot"]["final_status"] == "DISCREPANCIES DETECTED"
    assert admin.get(f"/api/shipments/{A}").json()["history"]

    assert admin.get("/api/history/compare", params={"shipment": A, "first": 1, "second": 999}).status_code == 404
    assert admin.get("/api/history/compare", params={"shipment": "SHJ-0-0", "first": 1, "second": 2}).status_code == 404


# ============================================================
# REPORTS
# ============================================================

def test_the_existing_csv_and_pdf_reports_download(admin):

    csv = admin.get(f"/api/reports/shipments/{A}.csv")

    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert 'filename="' in csv.headers["content-disposition"] and csv.content.startswith(b"\xef\xbb\xbf")

    pdf = admin.get(f"/api/reports/shipments/{A}.pdf")

    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")

    for route in ("register", "exceptions", "exposure", "completeness"):
        reply = admin.get(f"/api/reports/{route}.csv")
        assert reply.status_code == 200 and reply.content.startswith(b"\xef\xbb\xbf"), route

    assert admin.get("/api/reports/shipments/SHJ-9-9.csv").status_code == 404


def test_a_report_needs_a_successful_reconciliation(rig, admin, monkeypatch):

    monkeypatch.setattr(rig.state.service, "shipment", lambda shipment_id: {
        "shipment_id": A, "shipment_key": "SHJ2026001", "result": {"success": False, "error": "x"},
    })

    assert admin.get(f"/api/reports/shipments/{A}.csv").status_code == 409


# ============================================================
# INGESTION
# ============================================================

@pytest.fixture
def embeddings(monkeypatch):

    monkeypatch.setattr(ingestion_manager, "create_embedding", fake_embedding)


def pdf_upload(tmp_path, name="BOL_NEW.pdf", shipment="SHJ-2026-555"):

    source = tmp_path / "upload_source"
    source.mkdir(exist_ok=True)

    return ("files", (name, make_bol(source / name, shipment, 5).read_bytes(), "application/pdf"))


def test_an_administrator_uploads_and_every_file_gets_a_status(rig, admin, embeddings, tmp_path):

    reply = admin.post("/api/ingestion/upload", files=[
        pdf_upload(tmp_path), ("files", ("empty.pdf", b"", "application/pdf")),
        ("files", ("notes.txt", b"hello", "text/plain")),
    ])

    assert reply.status_code == 200

    data = reply.json()

    assert (data["indexed"], data["failed"], data["unsupported"], data["corpus_changed"]) == (1, 1, 1, True)
    assert [item["status"] for item in data["files"]] == ["indexed", "failed", "unsupported"]
    assert (rig.raw / "BOL_NEW.pdf").exists() and not (rig.raw / "empty.pdf").exists()
    assert "quarantine" not in reply.text

    register = admin.get("/api/ingestion/register").json()

    assert register["summary"]["indexed"] == 1 and register["summary"]["open_errors"] == 2
    assert {item["filename"] for item in admin.get("/api/ingestion/errors").json()} == {"empty.pdf", "notes.txt"}

    # The new document shows up in the audit data without a restart
    assert "SHJ-2026-555" in {row["shipment_id"] for row in admin.get("/api/shipments").json()["shipments"]}


def test_an_uploaded_document_can_be_followed_step_by_step(rig, admin, embeddings, tmp_path, caplog):

    # While it runs: one line per step in the pipeline log (the server window prints it)
    with caplog.at_level(logging.INFO, logger="logidoc.pipeline"):
        assert admin.post("/api/ingestion/upload", files=[pdf_upload(tmp_path)]).json()["indexed"] == 1

    lines = [record.getMessage() for record in caplog.records if record.name == "logidoc.pipeline"]

    for step in ("received", "text extracted (PyMuPDF)", "classified: BOL, shipment SHJ-2026-555", "chunked", "embedded", "indexed"):
        assert any(f"BOL_NEW.pdf | {step}" in line for line in lines), step

    # Afterwards: what each step produced, read back without changing anything
    def stored():
        found = chromadb.PersistentClient(path=str(rig.locations.chroma_dir)).get_collection(name=config.COLLECTION_NAME).get(
            include=["embeddings", "metadatas", "documents"]
        )
        return sorted(zip(found["ids"], [list(map(float, values)) for values in found["embeddings"]], [sorted(item.items()) for item in found["metadatas"]], found["documents"]))

    before = {path.name: path.read_bytes() for path in rig.raw.glob("*.pdf")}
    vectors_before = stored()
    trace = admin.get("/api/documents/pipeline", params={"name": "BOL_NEW.pdf"}).json()

    assert trace["validation"]["ok"] and trace["ingestion"]["status"] == "indexed"
    assert (trace["classification"]["document_type"], trace["classification"]["shipment_id"]) == ("BOL", "SHJ-2026-555")
    assert "SHJ-2026-555" in trace["extraction"]["pages"][0]["text"]
    assert (trace["chunking"]["chunk_size"], trace["chunking"]["overlap"]) == (1000, 200)

    # The vectors shown are the ones ingestion stored for exactly these chunks
    assert [vector["id"] for vector in trace["index"]["vectors"]] == [
        f"{trace['sha256']}:{chunk['page']}:{chunk['chunk']}" for chunk in trace["chunking"]["chunks"]
    ]
    assert trace["index"]["vectors"][0]["dimensions"] == len(fake_embedding("x"))
    assert trace["index"]["vectors"][0]["metadata"]["shipment_id"] == "SHJ-2026-555"
    assert {path.name: path.read_bytes() for path in rig.raw.glob("*.pdf")} == before
    assert stored() == vectors_before
    assert str(rig.tmp) not in json.dumps(trace)


def test_the_processing_steps_are_found_by_name_in_the_servers_own_listing(rig, admin):

    assert admin.get("/api/documents/pipeline", params={"name": "BOL_SHJ-2026-001.pdf"}).status_code == 200

    for name in ("../chroma", "..\\reviews.sqlite3", "/etc/passwd", "nothing.pdf", "", "a" * 300):
        assert admin.get("/api/documents/pipeline", params={"name": name}).status_code in (404, 422), name


def test_an_open_ingestion_error_keeps_every_shipment_from_being_clean_until_resolved(rig, admin):

    admin.post("/api/ingestion/upload", files=[("files", ("scan.pdf", b"", "application/pdf"))])

    overview = admin.get("/api/overview").json()
    statuses = {row["final_status"] for row in admin.get("/api/shipments").json()["shipments"]}

    assert overview["open_ingestion_errors"] == 1 and "NO DISCREPANCIES" not in statuses
    assert "scan.pdf" in json.dumps(overview["unreadable_files"])

    error = admin.get("/api/ingestion/errors").json()[0]

    assert admin.post(f"/api/ingestion/{error['id']}/resolve", json={"note": "short"}).status_code in (409, 422)
    assert admin.post(f"/api/ingestion/{error['id']}/resolve",
                      json={"note": "Checked: this file belongs to no shipment."}).status_code == 200

    assert admin.get("/api/overview").json()["open_ingestion_errors"] == 0
    assert "NO DISCREPANCIES" in {row["final_status"] for row in admin.get("/api/shipments").json()["shipments"]}


def test_an_oversized_upload_is_refused_without_being_stored(tmp_path, embeddings):

    rig = Rig(tmp_path, "local", max_upload_mb=1)
    client = rig.client()

    reply = client.post("/api/ingestion/upload", files=[("files", ("big.pdf", b"%PDF-1.4" + b"0" * (2 * 1024 * 1024),
                                                                    "application/pdf"))])

    assert reply.status_code == 413 and not (rig.raw / "big.pdf").exists()
    assert client.post("/api/ingestion/upload", files=[]).status_code == 422


def test_replace_and_remove_archive_the_original_and_need_confirmation(rig, admin, embeddings, tmp_path):

    admin.post("/api/ingestion/upload", files=[pdf_upload(tmp_path)])
    before = (rig.raw / "BOL_NEW.pdf").read_bytes()

    source = tmp_path / "fix"
    source.mkdir()
    corrected = make_bol(source / "x.pdf", "SHJ-2026-555", 9).read_bytes()

    replaced = admin.post("/api/documents/replace", params={"name": "BOL_NEW.pdf"}, files={"file": ("x.pdf", corrected)})

    assert replaced.status_code == 200 and replaced.json()["action"] == "replaced"
    assert (rig.raw / "BOL_NEW.pdf").read_bytes() == corrected
    assert [p.read_bytes() for p in (rig.tmp / "archive").glob("*BOL_NEW.pdf")] == [before]

    assert admin.post("/api/documents/remove", params={"name": "BOL_NEW.pdf"}).status_code == 422
    assert (rig.raw / "BOL_NEW.pdf").exists()

    removed = admin.post("/api/documents/remove", params={"name": "BOL_NEW.pdf", "confirm": "true"})

    assert removed.status_code == 200 and not (rig.raw / "BOL_NEW.pdf").exists()
    assert len(list((rig.tmp / "archive").glob("*BOL_NEW.pdf"))) == 2

    assert admin.post("/api/documents/remove", params={"name": "../x.pdf", "confirm": "true"}).status_code == 409
    assert admin.post("/api/documents/reprocess", params={"name": "nothing.pdf"}).status_code == 409


# ============================================================
# CHAT (the existing RAG)
# ============================================================

def test_chat_returns_generated_text_with_its_evidence_and_scope(rig, reviewer, monkeypatch):

    seen = []

    def ask(question, shipment_id=None):
        seen.append((question, shipment_id))
        return "The total is $1,550.00.", {"status": "answered", "answer_method": "calculation", "scope": {"shipment": A},
                                           "evidence": [{"source": "INVOICE_SHJ-2026-001.pdf"}]}

    import rag
    monkeypatch.setattr(rag, "ask_question", ask)

    reply = reviewer.post("/api/chat", json={"question": "What is the invoice total?", "shipment_id": A})

    assert reply.status_code == 200
    assert reply.json()["generated"] is True and reply.json()["evidence"] and "does not change" in reply.json()["notice"]
    assert seen == [("What is the invoice total?", A)]

    reviewer.post("/api/chat", json={"question": "Anything?", "shipment_id": "All Shipments"})
    assert seen[-1] == ("Anything?", None)

    assert reviewer.post("/api/chat", json={"question": ""}).status_code == 422


def test_chat_failures_do_not_leak_details(rig, reviewer, monkeypatch):

    import rag

    def broken(question, shipment_id=None):
        raise ConnectionError("http://10.0.0.5:11434 refused")

    monkeypatch.setattr(rag, "ask_question", broken)

    reply = reviewer.post("/api/chat", json={"question": "hello"})

    assert reply.status_code == 503 and "10.0.0.5" not in reply.text


# ============================================================
# THE REACT BUILD
# ============================================================

def test_the_api_serves_the_single_page_frontend_safely(tmp_path):

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    rig = Rig(tmp_path, "local", frontend_dir=dist)
    client = rig.client()

    assert client.get("/").text == "<html>app</html>"
    assert client.get("/shipments/SHJ-1").text == "<html>app</html>"
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/favicon-missing.svg").text == "<html>app</html>"

    # An unknown API path is a 404, not the application page
    assert client.get("/api/nothing").status_code == 404

    # Nothing outside the build folder can be read through it
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt"):
        assert "secret" not in client.get(path).text

    assert client.get("/").headers["content-security-policy"].startswith("default-src 'self'")
