"""
The Audit Analytics tab of the dashboard (app.py), driven with
Streamlit's AppTest.

Both sample shipments are indexed in a temporary workspace like
test_multi_shipment (stand-in embeddings, no Ollama), and the review
database is a temporary file. SHJ-2026-001 has discrepancies (shortage
of 3, 2 days late, invoice $120 over its line items); SHJ-2026-002 has
none (early delivery, invoice adds up).

Run from the project root:
    python -m pytest tests/test_analytics_dashboard.py
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from streamlit.testing.v1 import AppTest  # noqa: E402

import config  # noqa: E402
from review_store import ReviewStore  # noqa: E402


A = "SHJ-2026-001"
B = "SHJ-2026-002"

APP_FILE = PROJECT_ROOT / "app.py"


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory):
    """
    Both shipments in a temporary workspace and index, and a
    temporary review database.
    """

    import document_loader
    import rag
    import vector_store
    from test_ingestion_manager import fake_embedding, patched
    from test_multi_shipment import isolated_workspace

    database = tmp_path_factory.mktemp("reviews") / "reviews.sqlite3"

    with isolated_workspace() as (raw_dir, chroma_dir):

        assert vector_store.CHROMA_DIR == chroma_dir
        assert rag.CHROMA_DIR == chroma_dir
        assert document_loader.RAW_DATA_DIR == raw_dir

        with patched(vector_store, "create_embedding", fake_embedding):
            with redirect_stdout(io.StringIO()):
                vector_store.build_vector_database()

        with patched(config, "REVIEW_DB_PATH", database):
            yield database


def open_analytics():

    at = AppTest.from_file(str(APP_FILE), default_timeout=240)
    at.run()

    [button for button in at.button if button.label == "Load audit analytics"][0].click().run()

    assert not at.exception, [str(item.value) for item in at.exception]

    return at


def metrics(at):

    return {metric.label: metric.value for metric in at.tabs[4].metric}


def button(at, label):

    return [item for item in at.button if item.label == label][0]


def test_existing_tabs_are_kept_and_analytics_loads_on_request(dashboard):

    at = AppTest.from_file(str(APP_FILE), default_timeout=240)
    at.run()

    assert not at.exception
    assert [tab.label for tab in at.tabs] == [
        "💬 Logistics Assistant",
        "📊 Shipment Reconciliation",
        "🧠 Shipment Intelligence",
        "🩺 Diagnostics",
        "📈 Audit Analytics",
        "🧭 Audit Operations",
    ]

    # Nothing is reconciled until asked for
    assert metrics(at) == {}
    assert not dashboard.exists()


def test_kpis(dashboard):

    at = open_analytics()

    assert metrics(at) == {
        "Identified shipments": "2",
        "Reconciled": "2",
        "With discrepancies": "1",
        "Need manual review": "0",
        "No discrepancies": "1",
        "Total invoice amount": "$4,425.50",
        "Invoice variance": "$120.00",
        "% reconciled with discrepancies": "50.0%",
    }


def test_register_filters_and_reset(dashboard):

    at = open_analytics()

    register = at.tabs[4].dataframe[0].value
    assert list(register["Shipment"]) == [A, B]
    assert list(register["Final status"]) == ["DISCREPANCIES DETECTED", "NO DISCREPANCIES"]

    at.text_input(key="an_search").input("shj 2026 002").run()
    assert list(at.tabs[4].dataframe[0].value["Shipment"]) == [B]

    at.text_input(key="an_search").input("").run()
    at.checkbox(key="an_invoice").check().run()
    assert list(at.tabs[4].dataframe[0].value["Shipment"]) == [A]

    button(at, "Reset filters").click().run()
    assert not at.checkbox(key="an_invoice").value
    assert list(at.tabs[4].dataframe[0].value["Shipment"]) == [A, B]

    # The register export button respects the filters (and does not rerun)
    downloads = {item.label: item for item in at.get("download_button")}
    assert downloads["Download register (CSV)"].proto.ignore_rerun


def test_drill_down_and_navigation_to_reconciliation(dashboard):

    at = open_analytics()

    at.selectbox(key="an_detail").select(A).run()

    tab = at.tabs[4]

    assert any(f"{A}: DISCREPANCIES DETECTED" in item.value for item in tab.error)
    assert {item.label for item in at.get("download_button")} >= {
        "Shipment audit (CSV)", "Shipment audit (PDF)",
    }

    # Evidence lists source, page, type and shipment
    evidence = [frame.value for frame in tab.dataframe if "Field text" in frame.value.columns][0]
    assert set(evidence["Shipment"]) == {A}
    assert "INVOICE_SHJ-2026-001.pdf" in set(evidence["Source"])

    # Selecting it for the Reconciliation workflow selects it in the sidebar
    button(at, "Select for Reconciliation").click().run()
    assert at.sidebar.selectbox[0].value == A

    button(at, "Run Reconciliation").click().run()
    assert not at.exception
    assert any(item.value == f"Shipment: {A}" for item in at.subheader)
    assert {item.label for item in at.get("download_button")} >= {
        "Download freight audit (CSV)", "Download freight audit (PDF)",
    }


def test_exception_register_and_review_workflow(dashboard):

    at = open_analytics()

    exceptions = [frame.value for frame in at.tabs[4].dataframe if "Review state" in frame.value.columns][0]

    assert set(exceptions["Shipment"]) == {A}
    assert sorted(exceptions["Category"]) == ["Delivery delay", "Invoice mismatch", "Quantity shortage"]
    assert set(exceptions["Review state"]) == {"Open"}

    store = ReviewStore(dashboard)
    assert {row["status"] for row in store.states().values()} == {"open"}

    label = f"{A} · Invoice mismatch (Discrepancy)"
    at.selectbox(key="an_exception").select(label).run()

    form = at.tabs[4]

    # Dismissing without a reason is refused and nothing changes
    review_state = [item for item in form.selectbox if item.label == "Review state"][0]
    review_state.select("dismissed").run()
    button(at, "Save review state").click().run()

    assert any("needs a reason" in item.value for item in at.error)
    key = "SHJ2026001|invoice"
    assert ReviewStore(dashboard).states()[key]["status"] == "open"

    # In review, with a reviewer
    [item for item in at.tabs[4].selectbox if item.label == "Review state"][0].select("in_review").run()
    [item for item in at.tabs[4].text_input if item.label == "Reviewer (optional)"][0].input("krishna").run()
    button(at, "Save review state").click().run()

    assert not at.exception
    row = ReviewStore(dashboard).states()[key]
    assert (row["status"], row["reviewer"]) == ("in_review", "krishna")

    exceptions = [frame.value for frame in at.tabs[4].dataframe if "Review state" in frame.value.columns][0]
    assert "In review" in set(exceptions["Review state"])


def test_review_states_never_change_the_audit_or_the_kpis(dashboard):

    before = open_analytics()

    kpis = metrics(before)
    register = [frame.value for frame in before.tabs[4].dataframe if "Final status" in frame.value.columns][0]

    # Resolve every exception of SHJ-2026-001 directly in the database
    store = ReviewStore(dashboard)

    for row in store.states().values():
        store.set_status(
            {"exception_key": row["exception_key"], "signature": row["signature"]},
            "resolved", reviewer="krishna", note="Handled",
        )

    after = open_analytics()

    assert metrics(after) == kpis
    assert metrics(after)["With discrepancies"] == "1"

    frame = [item.value for item in after.tabs[4].dataframe if "Final status" in item.value.columns][0]
    assert frame.equals(register)

    # The exceptions are still listed, now as resolved: showing or
    # resolving does not remove a discrepancy from the audit
    exceptions = [item.value for item in after.tabs[4].dataframe if "Review state" in item.value.columns][0]
    assert set(exceptions["Review state"]) == {"Resolved"}
    assert len(exceptions) == 3


def test_no_historical_trends_are_claimed(dashboard):

    at = open_analytics()

    text = " ".join(
        str(item.value) for kind in (at.tabs[4].markdown, at.tabs[4].caption, at.tabs[4].subheader)
        for item in kind
    ).lower()

    assert "trend" not in text and "over time" not in text and "history of audit" not in text
