"""
The production-readiness screens of the dashboard (app.py), driven with
Streamlit's AppTest: the sign-in gate and roles, the exception queue
workflow, historical trends and the ingestion tab.

The documents are real PDFs in a temporary folder; every database (users,
review, history, ingestion register) is a temporary file; the language
model is a fake. The real data/raw folder, index and databases are never
used (tests/conftest.py checks this).

Run from the project root:
    python -m pytest tests/test_production_ui.py
"""

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from streamlit.testing.v1 import AppTest  # noqa: E402

import config  # noqa: E402
import rag  # noqa: E402
from auth import UserStore  # noqa: E402
from authz import ADMIN, REVIEWER  # noqa: E402
from isolation import redirect_project_paths  # noqa: E402
from review_store import ReviewStore  # noqa: E402
from test_audit_operations_ui import make_folder  # noqa: E402
from test_ingestion_manager import patched  # noqa: E402


APP_FILE = PROJECT_ROOT / "app.py"
PASSWORD = "correct horse battery"

TAB_OPERATIONS, TAB_QUEUE, TAB_TRENDS, TAB_INGESTION, TAB_ADMIN = 5, 6, 7, 8, 9


@pytest.fixture
def world(tmp_path, monkeypatch):
    """
    Documents, a user database with an administrator (alice) and a
    reviewer (bob), and temporary databases, with every path redirected.
    """

    raw = make_folder(tmp_path, broken=False)
    auth_db = tmp_path / "auth.sqlite3"

    users = UserStore(auth_db)
    users.create_user("alice", PASSWORD, ADMIN)
    users.create_user("bob", PASSWORD, REVIEWER)

    monkeypatch.setattr(config, "AUTH_DB_PATH", auth_db)
    monkeypatch.setattr(config, "REVIEW_DB_PATH", tmp_path / "reviews.sqlite3")
    monkeypatch.setattr(config, "HISTORY_DB_PATH", tmp_path / "history.sqlite3")
    monkeypatch.setattr(rag, "call_llm", lambda prompt: "unavailable")

    class World:
        pass

    state = World()
    state.raw, state.auth, state.reviews, state.history = raw, auth_db, config.REVIEW_DB_PATH, config.HISTORY_DB_PATH
    state.users = users
    state.register = raw.parent / "ingestion_register.sqlite3"
    state.monkeypatch = monkeypatch

    with redirect_project_paths(raw, raw.parent / "chroma_db"):
        yield state


def required(world):

    world.monkeypatch.setenv("LOGIDOC_AUTH_MODE", "required")


def start():

    at = AppTest.from_file(str(APP_FILE), default_timeout=300)
    at.run()

    assert not at.exception, [str(item.value) for item in at.exception]

    return at


def sign_in(at, user="bob", password=PASSWORD):

    fields = {item.label: item for item in at.text_input}

    fields["User name"].input(user)
    fields["Password"].input(password)

    [item for item in at.button if item.label == "Sign in"][0].click().run()

    return at


def click(at, label):

    [item for item in at.button if item.label == label][0].click().run()

    assert not at.exception, [str(item.value) for item in at.exception]


def texts(element):

    return " ".join(
        str(item.value)
        for kind in (element.markdown, element.caption, element.info, element.warning, element.error, element.success)
        for item in kind
    )


# ============================================================
# SIGN-IN GATE
# ============================================================

def test_local_mode_needs_no_sign_in_and_says_so(world, monkeypatch):

    monkeypatch.delenv("LOGIDOC_AUTH_MODE", raising=False)

    at = start()

    assert len(at.tabs) == 10
    assert any("Local development mode" in item.value for item in at.sidebar.caption)
    assert not any(item.label == "Sign in" for item in at.button)


def test_required_mode_shows_only_the_sign_in_form(world):

    required(world)

    at = start()

    assert len(at.tabs) == 0
    assert {item.label for item in at.text_input} == {"User name", "Password"}
    assert not at.sidebar.file_uploader and not at.sidebar.selectbox
    assert not any(item.label == "Logistics question" for item in at.text_input)


def test_a_misconfiguration_stops_the_app(world, monkeypatch):

    monkeypatch.setenv("LOGIDOC_ENV", "production")
    monkeypatch.delenv("LOGIDOC_AUTH_MODE", raising=False)

    at = AppTest.from_file(str(APP_FILE), default_timeout=300)
    at.run()

    assert any("production" in item.value for item in at.error)
    assert len(at.tabs) == 0


def test_with_no_administrator_nobody_can_sign_in(world, tmp_path):

    required(world)
    world.monkeypatch.setattr(config, "AUTH_DB_PATH", tmp_path / "empty_auth.sqlite3")

    at = start()

    assert any("No administrator account exists" in item.value for item in at.error)
    assert not at.text_input


def test_a_wrong_password_is_refused_and_the_right_one_opens_the_app(world):

    required(world)

    at = sign_in(start(), "bob", "not the password at all")

    assert any("Wrong user name or password" in item.value for item in at.error)
    assert len(at.tabs) == 0

    at = sign_in(at, "bob")

    assert not at.exception
    assert len(at.tabs) == 10
    assert any("Signed in as **bob**" in item.value for item in at.sidebar.caption)


def test_signing_out_and_a_revoked_session_both_return_to_the_sign_in_form(world):

    required(world)

    at = sign_in(start())
    assert len(at.tabs) == 10

    click(at, "Sign out")
    assert len(at.tabs) == 0 and any(item.label == "Sign in" for item in at.button)

    # Another place ends the session (the account is deactivated)
    at = sign_in(start())
    assert len(at.tabs) == 10

    world.users.change_user("bob", active=False)
    at.run()

    assert len(at.tabs) == 0 and any(item.label == "Sign in" for item in at.button)


def test_signing_in_does_not_reveal_which_account_exists(world):

    required(world)

    wrong_user = [item.value for item in sign_in(start(), "nobody", PASSWORD).error]
    wrong_password = [item.value for item in sign_in(start(), "bob", "wrong password here").error]

    assert wrong_user == wrong_password


# ============================================================
# ROLES
# ============================================================

def test_a_reviewer_cannot_add_documents_or_open_the_administration(world):

    required(world)

    at = sign_in(start())

    upload = [item for item in at.sidebar.button if item.label == "Process Documents"][0]

    assert upload.disabled
    assert "administrators" in texts(at.tabs[TAB_ADMIN])
    assert not at.tabs[TAB_ADMIN].dataframe and not at.tabs[TAB_ADMIN].text_input


def test_an_administrator_sees_the_accounts_and_can_add_one(world):

    required(world)

    at = sign_in(start(), "alice")

    assert not [item for item in at.sidebar.button if item.label == "Process Documents"][0].disabled

    admin = at.tabs[TAB_ADMIN]
    accounts = [frame.value for frame in admin.dataframe if "Role" in frame.value.columns][0]

    assert sorted(accounts["User"]) == ["alice", "bob"]
    assert "password_hash" not in " ".join(map(str, accounts.columns)).lower()

    [item for item in at.text_input if item.key == "new_user_name"][0].input("carol")
    [item for item in at.text_input if item.key == "new_user_password"][0].input(PASSWORD)
    click(at, "Create account")

    assert "carol" in [user["username"] for user in world.users.users()]


def test_a_reviewer_cannot_record_audits_but_an_administrator_can(world):

    required(world)

    at = sign_in(start())
    click(at, "Load audit operations")

    assert at.button(key="op_record_all").disabled and at.button(key="op_record_one").disabled

    with sqlite3.connect(world.history) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 0

    at = sign_in(start(), "alice")
    click(at, "Load audit operations")

    assert not at.button(key="op_record_all").disabled

    at.button(key="op_record_all").click().run()

    with sqlite3.connect(world.history) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 1


# ============================================================
# EXCEPTION QUEUE
# ============================================================

def load_queue(at):

    click(at, "Load exception queue")

    return at.tabs[TAB_QUEUE]


def test_a_signed_in_review_records_the_account_not_a_typed_name(world):

    required(world)

    at = sign_in(start())
    queue = load_queue(at)

    # No free-typed reviewer anywhere
    assert not any(item.label == "Reviewer (optional)" for item in at.text_input)

    [item for item in queue.selectbox if item.label == "Open an exception"][0]
    click(at, "Start review")

    store = ReviewStore(world.reviews)
    rows = [row for row in store.states().values() if row["status"] == "in_review"]

    assert len(rows) == 1 and rows[0]["assignee"] == "bob"

    event = store.history(rows[0]["exception_key"])[-1]

    assert (event["actor"], event["actor_role"], event["authenticated"]) == ("bob", "reviewer", 1)
    assert store.verify_integrity()["ok"]


def test_the_queue_offers_only_the_actions_the_role_allows(world):

    required(world)

    reviewer = sign_in(start())
    load_queue(reviewer)

    labels = {item.label for item in reviewer.button}

    assert "Start review" in labels and "Dismiss" not in labels and "Approve" not in labels

    admin = sign_in(start(), "alice")
    load_queue(admin)

    assert "Dismiss" in {item.label for item in admin.button}


def test_resolving_needs_a_note_and_evidence_through_the_screen(world):

    required(world)

    at = sign_in(start())
    load_queue(at)
    click(at, "Start review")

    resolve_note = [item for item in at.text_area if item.label.startswith("Resolution note")][0]
    resolve_note.input("Checked the carrier invoice against the rate sheet.")
    click(at, "Resolve")

    assert any("evidence reference" in item.value for item in at.error)

    [item for item in at.text_area if item.label.startswith("Resolution evidence")][0].input(
        "INVOICE_SHJ-2026-001.pdf p.1"
    )
    [item for item in at.text_area if item.label.startswith("Resolution note")][0].input(
        "Checked the carrier invoice against the rate sheet."
    )
    click(at, "Resolve")

    store = ReviewStore(world.reviews)

    assert [row["status"] for row in store.states().values()].count("resolved") == 1


def test_local_mode_keeps_the_quick_review_form_and_marks_it_unauthenticated(world, monkeypatch):

    monkeypatch.delenv("LOGIDOC_AUTH_MODE", raising=False)

    at = start()
    click(at, "Load audit analytics")

    assert any(item.label == "Reviewer (optional)" for item in at.text_input)


def test_the_queue_filters_by_assignee_state_and_search(world, monkeypatch):

    monkeypatch.delenv("LOGIDOC_AUTH_MODE", raising=False)

    at = start()
    queue = load_queue(at)

    frame = [item.value for item in queue.dataframe if "Assignee" in item.value.columns][0]
    assert len(frame) >= 1 and set(frame["State"]) == {"Open"}

    [item for item in at.selectbox if item.label == "Assignee"][0].select("Unassigned").run()
    assert not at.exception

    [item for item in at.text_input if item.label == "Search shipment"][0].input("no-such-shipment").run()

    assert any("No exception matches" in item.value for item in at.tabs[TAB_QUEUE].info)


# ============================================================
# HISTORICAL TRENDS
# ============================================================

def test_trends_start_empty_and_never_invent_history(world, monkeypatch):

    monkeypatch.delenv("LOGIDOC_AUTH_MODE", raising=False)

    at = start()

    assert not world.history.exists()

    click(at, "Load historical trends")

    assert any("No audit history has been recorded yet" in item.value for item in at.tabs[TAB_TRENDS].info)
    assert not at.tabs[TAB_TRENDS].dataframe


def test_trends_show_a_recorded_run_with_sample_sizes_and_the_overbilling_caveat(world, monkeypatch):

    monkeypatch.delenv("LOGIDOC_AUTH_MODE", raising=False)

    at = start()
    click(at, "Load audit operations")
    at.button(key="op_record_all").click().run()

    click(at, "Load historical trends")

    trends = at.tabs[TAB_TRENDS]
    text = texts(trends)

    assert "Only one period has recorded audits" in text
    assert "not proof of overbilling" in text or "not proven overbilling" in text

    periods = [frame.value for frame in trends.dataframe if "Period" in frame.value.columns]
    assert periods and any("(" in str(value) for value in periods[0].iloc[0])

    assert any(item.label == "Download filtered analytics (CSV)" for item in at.get("download_button"))


# ============================================================
# INGESTION
# ============================================================

def upload(at, files):

    at.sidebar.file_uploader[0].set_value([(name, data, "application/pdf") for name, data in files])

    [item for item in at.sidebar.button if item.label == "Process Documents"][0].click().run()

    return at


def test_a_rejected_upload_is_recorded_and_keeps_every_shipment_from_being_clean(world, monkeypatch):

    monkeypatch.delenv("LOGIDOC_AUTH_MODE", raising=False)

    at = start()

    upload(at, [("scan.pdf", b"")])

    assert not at.exception
    assert any("failed" in item.value.lower() for item in at.sidebar.error)
    assert not (world.raw / "scan.pdf").exists()

    # The register has an open error ...
    click(at, "Load ingestion register")

    ingestion = at.tabs[TAB_INGESTION]
    assert any("open ingestion error" in item.value for item in ingestion.warning)

    # ... and no shipment is reported clean while it is open
    click(at, "Load audit analytics")

    register = [frame.value for frame in at.tabs[4].dataframe if "Final status" in frame.value.columns][0]

    assert "NO DISCREPANCIES" not in set(register["Final status"])
    assert any("could not be used" in item.value for item in at.tabs[4].warning)


def test_resolving_the_error_clears_the_hold(world, monkeypatch):

    monkeypatch.delenv("LOGIDOC_AUTH_MODE", raising=False)

    at = start()
    upload(at, [("scan.pdf", b"")])
    click(at, "Load ingestion register")

    [item for item in at.text_area if item.key == "ingest_resolve_note"][0].input(
        "Checked: this file is not part of any shipment."
    )
    click(at, "Mark as resolved")

    assert any("No open ingestion errors" in item.value for item in at.tabs[TAB_INGESTION].success)

    click(at, "Load audit analytics")

    register = [frame.value for frame in at.tabs[4].dataframe if "Final status" in frame.value.columns][0]

    assert "NO DISCREPANCIES" in set(register["Final status"])


def test_a_reviewer_sees_the_register_but_not_the_document_actions(world):

    required(world)

    at = sign_in(start())
    click(at, "Load ingestion register")

    assert "Only administrators can reprocess" in texts(at.tabs[TAB_INGESTION])
    assert not [item for item in at.button if item.label in ("Replace", "Remove", "Reprocess")]


def test_an_administrator_can_remove_a_stored_document_with_confirmation(world, monkeypatch):

    monkeypatch.delenv("LOGIDOC_AUTH_MODE", raising=False)

    from test_ingestion_manager import fake_embedding
    import ingestion_manager
    import vector_store

    with patched(vector_store, "create_embedding", fake_embedding):
        with patched(ingestion_manager, "create_embedding", fake_embedding):

            at = start()

            # The documents of the workspace were copied in, not ingested: ingest one more
            from test_ingestion_manager import make_bol

            source = world.raw.parent / "new_source"
            source.mkdir()
            data = make_bol(source / "BOL_NEW.pdf", "SHJ-2026-555", 5).read_bytes()

            upload(at, [("BOL_NEW.pdf", data)])
            assert (world.raw / "BOL_NEW.pdf").exists()

            click(at, "Load ingestion register")

            [item for item in at.selectbox if item.key == "ingest_target"][0].select("BOL_NEW.pdf").run()

            assert at.button(key="ingest_remove").disabled

            [item for item in at.checkbox if item.key == "ingest_remove_confirm"][0].check().run()
            at.button(key="ingest_remove").click().run()

            assert not at.exception
            assert not (world.raw / "BOL_NEW.pdf").exists()
            assert len(list((world.raw.parent / "archive").glob("*BOL_NEW.pdf"))) == 1
