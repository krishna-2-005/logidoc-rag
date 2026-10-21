"""
The Audit Operations tab of the dashboard (app.py), driven with
Streamlit's AppTest.

The documents are real PDFs in a temporary folder: the two sample
shipments (SHJ-2026-001 has discrepancies, SHJ-2026-002 has none), an
incomplete shipment (a BOL and an invoice, no POD) and a file that is
not a PDF. The review and audit history databases are temporary files
and the language model is a fake. The real data/raw folder, index and
databases are never used (tests/conftest.py checks this).

Run from the project root:
    python -m pytest tests/test_audit_operations_ui.py
"""

import shutil
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
from isolation import SAMPLE_SHIPMENT_DIR, redirect_project_paths  # noqa: E402
from review_store import ReviewStore  # noqa: E402
from test_document_completeness import typed_pdf  # noqa: E402
from test_ingestion_manager import make_pdf, patched  # noqa: E402


A = "SHJ-2026-001"
B = "SHJ-2026-002"
X = "SHJ-2026-701"

SECOND_SHIPMENT_DIR = PROJECT_ROOT / "tests" / "fixtures" / "shipment_SHJ-2026-002"
APP_FILE = PROJECT_ROOT / "app.py"

TAB = 5     # Audit Operations


# ============================================================
# WORKSPACE
# ============================================================

def make_folder(tmp_path, broken=True):

    raw = tmp_path / "docs" / "raw"
    raw.mkdir(parents=True)

    for source in (SAMPLE_SHIPMENT_DIR, SECOND_SHIPMENT_DIR):
        for pdf in sorted(source.glob("*.pdf")):
            shutil.copy2(pdf, raw / pdf.name)

    typed_pdf(raw, "BOL", X)
    typed_pdf(raw, "INVOICE", X)

    if broken:
        (raw / "broken_scan.pdf").write_bytes(b"this is not a pdf")

    return raw


@contextmanager
def workspace(raw, review_db, history_db, model=None):

    with redirect_project_paths(raw, raw.parent / "chroma_db"):
        with patched(config, "REVIEW_DB_PATH", review_db):
            with patched(config, "HISTORY_DB_PATH", history_db):
                with patched(rag, "call_llm", model or (lambda prompt: "unavailable")):
                    yield


@pytest.fixture
def paths(tmp_path):

    return make_folder(tmp_path), tmp_path / "reviews.sqlite3", tmp_path / "history.sqlite3"


@pytest.fixture
def clean_paths(tmp_path):
    """
    The same documents without the broken file: a broken file makes
    every shipment NEEDS REVIEW (see test_unreadable_documents.py).
    """

    return make_folder(tmp_path, broken=False), tmp_path / "reviews.sqlite3", tmp_path / "history.sqlite3"


def open_operations():

    at = AppTest.from_file(str(APP_FILE), default_timeout=300)
    at.run()

    [button for button in at.button if button.label == "Load audit operations"][0].click().run()

    assert not at.exception, [str(item.value) for item in at.exception]

    return at


def refresh(at):

    at.run()

    assert not at.exception, [str(item.value) for item in at.exception]

    return at


def metrics(at):

    return {metric.label: metric.value for metric in at.tabs[TAB].metric}


def button(at, key):

    return at.button(key=key)


def expander_labels(at):

    return [item.label for item in at.tabs[TAB].expander]


def runs(history_db):

    with sqlite3.connect(history_db) as connection:
        return (
            connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM shipment_snapshots").fetchone()[0],
        )


# ============================================================
# THE TAB
# ============================================================

def test_the_tab_loads_on_request_and_creates_no_history(paths):

    raw, reviews, history = paths

    with workspace(raw, reviews, history):

        at = AppTest.from_file(str(APP_FILE), default_timeout=300)
        at.run()

        assert not at.exception
        assert at.tabs[TAB].label == "🧭 Audit Operations"
        assert metrics(at) == {}
        assert not history.exists() and not reviews.exists()

        at = open_operations()

        # Opening the view records nothing: history has no runs
        assert runs(history) == (0, 0)


def test_completeness_and_financial_exposure(paths):

    raw, reviews, history = paths

    with workspace(raw, reviews, history):

        at = open_operations()

        figures = metrics(at)

        # Documents: SHJ-2026-001, -002 complete; -701 has no POD; one file unusable
        assert figures["Shipments"] == "3"
        assert figures["Ready for reconciliation"] == "2"
        assert figures["Incomplete"] == "1"
        assert figures["Files that could not be used"] == "1"

        # Money: one confirmed mismatch ($120), one unverifiable invoice (no POD, no total)
        assert figures["Invoices"] == "3"
        assert figures["Confirmed mismatches"] == "1"
        assert figures["Unverifiable invoices"] == "1"
        assert figures["Verified invoices"] == "1"
        assert figures["Stated invoice total"] == "$4,425.50"
        assert figures["Absolute variance"] == "$120.00"
        assert figures["Net signed difference"] == "$120.00"
        assert figures["Amount under review (not a loss)"] == "NOT AVAILABLE"

        register = [frame.value for frame in at.tabs[TAB].dataframe if "What is needed" in frame.value.columns][0]
        row = register[register["Shipment"] == X].iloc[0]

        assert (row["Readiness"], row["POD"], row["BOL"]) == ("Incomplete", "MISSING", "present")
        assert row["What is needed"].startswith(f"Upload the POD for {X}")
        assert "broken_scan.pdf" in row["What is needed"]

        assert "Files that could not be used (1)" in expander_labels(at)

        unusable = [frame.value for frame in at.tabs[TAB].dataframe if "What happened" in frame.value.columns][0]

        assert list(unusable["File"]) == ["broken_scan.pdf"]
        assert list(unusable["Status"]) == ["unreadable"]


def test_filters_reset_and_financial_filter(paths):

    raw, reviews, history = paths

    with workspace(raw, reviews, history):

        at = open_operations()

        at.checkbox(key="op_dc_incomplete").check().run()

        register = [frame.value for frame in at.tabs[TAB].dataframe if "What is needed" in frame.value.columns][0]
        assert list(register["Shipment"]) == [X]

        button(at, "op_dc_reset").click().run()
        assert not at.checkbox(key="op_dc_incomplete").value

        # The financial figures follow their filters
        at.multiselect(key="op_fx_classes").select("mismatch").run()

        figures = metrics(at)
        assert (figures["Invoices"], figures["Confirmed mismatches"], figures["Stated invoice total"]) == (
            "1", "1", "$1,550.00",
        )

        button(at, "op_fx_reset").click().run()
        assert metrics(at)["Invoices"] == "3"


# ============================================================
# ONE SHIPMENT
# ============================================================

def test_findings_with_evidence_for_the_selected_shipment_only(clean_paths):

    raw, reviews, history = clean_paths

    with workspace(raw, reviews, history):

        at = open_operations()

        labels = expander_labels(at)

        assert sorted(label for label in labels if "· HIGH ·" in label) == [
            "Delivery delay · HIGH · open",
            "Invoice mismatch · HIGH · open",
            "Quantity shortage · HIGH · open",
        ]

        invoice = next(item for item in at.tabs[TAB].expander if item.label.startswith("Invoice mismatch"))

        frames = [frame.value for frame in invoice.dataframe]
        evidence = next(frame for frame in frames if "Field text" in frame.columns)

        assert set(evidence["Shipment"]) == {A}
        assert "INVOICE_SHJ-2026-001.pdf" in set(evidence["Source"])
        assert "Total Invoice Amount: $1,550.00" in set(evidence["Field text"])

        texts = [str(item.value) for item in invoice.markdown]
        assert any(text.startswith("**Suggested next step:**") for text in texts)

        # The other shipment has its own findings and evidence
        at.selectbox(key="op_shipment").select(B).run()

        assert not at.exception
        assert not [label for label in expander_labels(at) if "· HIGH ·" in label]
        assert any(item.value == f"{B}: NO DISCREPANCIES" for item in at.tabs[TAB].success)

        # The incomplete shipment cannot be reconciled and says what is needed
        at.selectbox(key="op_shipment").select(X).run()

        assert any(f"{X} could not be reconciled" in item.value for item in at.tabs[TAB].error)
        assert any(item.value.startswith(f"Upload the POD for {X}") for item in at.tabs[TAB].warning)
        assert any("Reconciliation incomplete" in label for label in expander_labels(at))


def test_recording_audits_writes_no_duplicates(paths):

    raw, reviews, history = paths

    with workspace(raw, reviews, history):

        at = open_operations()

        button(at, "op_record_one").click().run()

        assert any(item.value.startswith("Recorded audit run #1: 1 new snapshot(s)") for item in at.tabs[TAB].success)
        assert runs(history) == (1, 1)

        # Streamlit reruns (widget changes, refreshes) record nothing
        refresh(at)
        at.checkbox(key="op_dc_incomplete").check().run()
        at.checkbox(key="op_dc_incomplete").uncheck().run()
        refresh(at)

        assert runs(history) == (1, 1)

        # An unchanged audit recorded again stores no new snapshot
        button(at, "op_record_one").click().run()

        assert any("nothing changed since the last snapshot" in item.value for item in at.tabs[TAB].info)
        assert runs(history) == (2, 1)

        # All shipments: the two new ones are stored, the first is unchanged
        button(at, "op_record_all").click().run()

        assert any(
            "2 new snapshot(s)" in item.value and "Unchanged since their last snapshot: SHJ-2026-001" in item.value
            for item in at.tabs[TAB].success
        )
        assert runs(history) == (3, 3)


def test_timeline_shows_what_changed_after_a_document_changes(clean_paths):

    raw, reviews, history = clean_paths

    with workspace(raw, reviews, history):

        at = open_operations()

        assert any("No audit has been recorded" in item.value for item in at.tabs[TAB].info)

        button(at, "op_record_one").click().run()

        first = [label for label in expander_labels(at) if "First recorded audit" in label]
        assert len(first) == 1 and "DISCREPANCIES DETECTED" in first[0]

        # The POD is replaced: all 48 pieces, on the expected date
        make_pdf(raw / f"POD_{A}.pdf", "PROOF OF DELIVERY", [
            f"Shipment Number: {A}", "", "Delivery Date:", "October 3, 2026", "",
            "Number of Pieces Delivered:", "48", "", "End of document",
        ])

        refresh(at)

        # The cached audit was not reused: the shortage and the delay are gone
        labels = expander_labels(at)
        assert [label for label in labels if "· HIGH ·" in label] == ["Invoice mismatch · HIGH · open"]

        button(at, "op_record_one").click().run()

        assert runs(history) == (2, 2)

        latest = next(item for item in at.tabs[TAB].expander if "No longer detected: 2" in item.label)

        assert "Persistent: 1" in latest.label and "DISCREPANCIES DETECTED" in latest.label

        texts = " ".join(
            str(item.value)
            for elements in (latest.markdown, latest.caption, latest.warning)
            for item in elements
        )

        assert "The shipment's documents changed" in texts
        assert "Quantity shortage" in texts and "Delivery delay" in texts


# ============================================================
# REVIEW, COMMENTARY, EXPORT
# ============================================================

def test_review_from_the_operations_tab_and_both_tabs_together(paths):

    raw, reviews, history = paths

    with workspace(raw, reviews, history):

        at = open_operations()

        # The Audit Analytics tab shows its own review form at the same time
        [button_ for button_ in at.button if button_.label == "Load audit analytics"][0].click().run()

        assert not at.exception, [str(item.value) for item in at.exception]

        tab = at.tabs[TAB]

        state = [item for item in tab.selectbox if item.label == "Review state"][0]
        state.select("in_review").run()

        reviewer = [item for item in at.tabs[TAB].text_input if item.label == "Reviewer (optional)"][0]
        reviewer.input("krishna").run()

        [item for item in at.tabs[TAB].button if item.label == "Save review state"][0].click().run()

        assert not at.exception

        rows = ReviewStore(reviews).states()

        assert sorted((row["status"], row["reviewer"]) for row in rows.values()).count(("in_review", "krishna")) == 1

        assert any("in review" in label for label in expander_labels(at))


def test_commentary_is_optional_separate_and_checked(paths):

    raw, reviews, history = paths

    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return "The total is $1,550.00 and the carrier owes $9,999.00."

    with workspace(raw, reviews, history, model=model):

        at = open_operations()

        # Nothing is sent to the model until asked
        assert prompts == []

        key = "op_comment_SHJ2026001|invoice"

        button(at, key).click().run()

        assert not at.exception
        assert len(prompts) == 1 and "ONLY the facts below" in prompts[0]

        texts = " ".join(str(item.value) for item in at.tabs[TAB].markdown)

        assert "Generated commentary (language model)" in texts
        assert "not part of the audit" in texts
        assert any("figures that are not in the audit: 9999.00" in item.value for item in at.tabs[TAB].warning)

        # The audit itself is unchanged
        assert any(item.value == f"{A}: DISCREPANCIES DETECTED" for item in at.tabs[TAB].error)
        assert metrics(at)["Absolute variance"] == "$120.00"


def test_exports_do_not_rerun_the_app(paths):

    raw, reviews, history = paths

    with workspace(raw, reviews, history):

        at = open_operations()

        downloads = {item.label: item for item in at.get("download_button")}

        assert {"Shipment audit (CSV)", "Shipment audit (PDF)", "Download completeness register (CSV)",
                "Download financial exposure (CSV)"} <= set(downloads)
        assert all(item.proto.ignore_rerun for item in downloads.values())

        # A shipment that cannot be reconciled has no report
        at.selectbox(key="op_shipment").select(X).run()

        assert any("A report needs a successful reconciliation" in item.value for item in at.tabs[TAB].caption)
        assert "Shipment audit (PDF)" not in {item.label for item in at.get("download_button")}


# ============================================================
# FAILURES AND EMPTY STATES
# ============================================================

def test_unavailable_databases_are_reported_not_fatal(paths, tmp_path):

    raw, _, _ = paths

    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so nothing can be created below it")

    with workspace(raw, blocker / "reviews.sqlite3", blocker / "history.sqlite3"):

        at = open_operations()

        warnings = " ".join(item.value for item in at.tabs[TAB].warning)

        assert "Audit history unavailable" in warnings
        assert "Review database unavailable" in warnings

        # The audit itself still works
        assert metrics(at)["Confirmed mismatches"] == "1"


def test_an_empty_document_folder(tmp_path):

    raw = tmp_path / "docs" / "raw"
    raw.mkdir(parents=True)

    with workspace(raw, tmp_path / "reviews.sqlite3", tmp_path / "history.sqlite3"):

        at = open_operations()

        assert any("No PDFs in the document folder yet" in item.value for item in at.tabs[TAB].info)
        assert metrics(at) == {}
