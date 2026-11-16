"""
Authorization of every API operation (docs/API_PERMISSIONS.md).

The operations are read from the routes (scripts/api_permissions.py), so a new
route is covered the day it is added: every operation is called without a
session, as a reviewer and as an administrator, under /api and /api/v1, and
must answer 401, 403 or get past authorization exactly as the inventory says.
The published inventory must match the code, so a permission cannot be
dropped or loosened without the change showing in docs/API_PERMISSIONS.md.

Everything runs on temporary folders (tests/test_api.py's Rig).
"""

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests", PROJECT_ROOT / "scripts"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import api_permissions  # noqa: E402
import authz  # noqa: E402
from api.core import CLIENT_HEADER  # noqa: E402
from test_api import Rig  # noqa: E402

OPERATIONS = api_permissions.operations()
PREFIXES = ("/api", "/api/v1")

# Ends the caller's session: called last, on its own (test_logout_needs_nothing_and_ends_only_the_callers_session)
LOGOUT = ("POST", "/api/auth/logout")


def url(item, prefix="/api"):
    """The operation's path with every parameter filled with a value that names nothing."""

    return prefix + re.sub(r"\{[^}]+\}", "999999", item["path"])[len("/api"):]


def call(client, item, prefix="/api", **kwargs):

    if item["method"] == "GET":
        return client.get(url(item, prefix), **kwargs)

    return client.request(item["method"], url(item, prefix), json={}, **kwargs)


def label(item):

    return f"{item['method']} {item['path']}"


@pytest.fixture(scope="module")
def rig(tmp_path_factory):

    # Hundreds of calls in a few seconds: without rate limits (tests/test_rate_limit.py), so every answer here is
    # the authorization's own
    return Rig(tmp_path_factory.mktemp("authorization"), "required", rate_limits=())


# ============================================================
# The inventory
# ============================================================

def test_the_published_inventory_matches_the_code():
    """docs/API_PERMISSIONS.md is what the routes declare: regenerate it with scripts/api_permissions.py."""

    assert api_permissions.TARGET.read_text(encoding="utf-8") == api_permissions.document()


def test_every_operation_is_listed_once_and_the_contract_has_no_other():

    import json

    contract = json.loads((PROJECT_ROOT / "docs" / "api" / "openapi-v1.json").read_text(encoding="utf-8"))
    published = {(method.upper(), path) for path, item in contract["paths"].items() for method in item}
    listed = [(item["method"], item["path"]) for item in OPERATIONS]

    assert len(listed) == len(set(listed))
    assert set(listed) == published


def test_only_the_known_operations_are_open_without_a_session():

    public = {(item["method"], item["path"]) for item in OPERATIONS if not item["signed_in"]}

    assert public == set(api_permissions.PUBLIC)


def test_every_permission_named_is_a_known_one():

    known = set().union(*authz.PERMISSIONS.values())

    assert {item["permission"] for item in OPERATIONS if item["permission"]} <= known


def test_the_reviewer_role_holds_no_administrative_permission():
    """The role table itself: widening the reviewer role must be a deliberate change of this test."""

    assert authz.REVIEWER_PERMISSIONS == {
        authz.EXCEPTIONS_VIEW, authz.EXCEPTIONS_ASSIGN_SELF, authz.EXCEPTIONS_UPDATE, authz.EXCEPTIONS_REOPEN,
        authz.HISTORY_VIEW, authz.REPORTS_DOWNLOAD, authz.INGESTION_VIEW, authz.EXTRACTION_VIEW,
        authz.EXTRACTION_CORRECT,
    }
    assert authz.REVIEWER_PERMISSIONS < authz.ADMIN_PERMISSIONS


def test_every_change_to_data_is_administrator_only_or_a_reviewer_workflow_step():
    """The writes a reviewer may make: exception steps, value corrections, questions to the assistant."""

    reviewer_writes = {
        label(item) for item in OPERATIONS
        if item["method"] != "GET" and item["roles"] and authz.REVIEWER in item["roles"]
    }

    assert reviewer_writes == {
        "POST /api/exceptions/{key}/actions",
        "POST /api/extractions/{extraction_id}/corrections",
        "POST /api/extractions/{extraction_id}/line-items/{index}/corrections",
        "POST /api/chat",
        "POST /api/assistant",
    }


# ============================================================
# Every operation, called
# ============================================================

@pytest.mark.parametrize("prefix", PREFIXES)
def test_without_a_session_every_operation_but_the_public_ones_answers_401(rig, prefix):

    client = rig.client()
    wrong = []

    for item in OPERATIONS:

        if (item["method"], item["path"]) == LOGOUT:
            continue

        status = call(client, item, prefix).status_code

        if item["signed_in"] and status != 401:
            wrong.append(f"{label(item)}: {status}, expected 401")
        elif not item["signed_in"] and status in (401, 403):
            wrong.append(f"{label(item)}: {status}, expected to be open")

    assert not wrong, "\n".join(wrong)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_reviewer_is_refused_every_administrator_operation_and_admitted_to_the_others(rig, prefix):

    client = rig.client("bob")
    wrong = []

    for item in OPERATIONS:

        if (item["method"], item["path"]) == LOGOUT or not item["signed_in"]:
            continue

        status = call(client, item, prefix).status_code

        if authz.REVIEWER not in item["roles"] and status != 403:
            wrong.append(f"{label(item)}: {status}, expected 403")
        elif authz.REVIEWER in item["roles"] and status in (401, 403):
            wrong.append(f"{label(item)}: {status}, expected to get past authorization")

    assert not wrong, "\n".join(wrong)
    # Still signed in: no refusal ended the session
    assert client.get("/api/session").json()["authenticated"] is True


def test_an_administrator_gets_past_authorization_everywhere(rig):

    client = rig.client("alice")
    wrong = []

    for item in OPERATIONS:

        if (item["method"], item["path"]) == LOGOUT or not item["signed_in"]:
            continue

        status = call(client, item).status_code

        if status in (401, 403):
            wrong.append(f"{label(item)}: {status}")

    assert not wrong, "\n".join(wrong)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_every_change_without_the_client_header_is_refused(rig, prefix):
    """CSRF: a POST or PATCH without X-LogiDoc-Client is refused, signed in or not, before anything else."""

    signed_in = rig.client("alice")
    signed_in.headers.pop(CLIENT_HEADER)
    anonymous = rig.client()
    anonymous.headers.pop(CLIENT_HEADER)
    wrong = []

    for item in OPERATIONS:

        if item["method"] == "GET" or (item["method"], item["path"]) == LOGOUT:
            continue

        for client in (signed_in, anonymous):

            reply = call(client, item, prefix)

            if reply.status_code != 403 or CLIENT_HEADER not in reply.json().get("detail", ""):
                wrong.append(f"{label(item)}: {reply.status_code}")

    assert not wrong, "\n".join(wrong)


def test_logout_needs_nothing_and_ends_only_the_callers_session(rig):

    assert rig.client().post("/api/auth/logout").status_code in (200, 204)

    first, second = rig.client("carol"), rig.client("carol")
    assert first.post("/api/v1/auth/logout").status_code in (200, 204)

    assert first.get("/api/exceptions").status_code == 401
    assert second.get("/api/exceptions").status_code == 200


# ============================================================
# A refusal opens nothing
# ============================================================

def test_a_refused_document_change_creates_or_opens_no_store(tmp_path):
    """reprocess and remove used to create the ingestion register before the service refused a reviewer."""

    rig = Rig(tmp_path, "required")
    reviewer = rig.client("bob")
    register = Path(rig.locations.register_db)
    before = register.read_bytes() if register.exists() else None

    for path in ("/api/documents/reprocess?name=a.pdf", "/api/documents/remove?name=a.pdf&confirm=true",
                 "/api/documents/replace?name=a.pdf", "/api/ingestion/upload", "/api/ingestion/1/retry",
                 "/api/ingestion/1/resolve"):
        assert reviewer.post(path, json={}).status_code == 403, path

    assert (register.read_bytes() if register.exists() else None) == before
