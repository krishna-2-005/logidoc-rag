"""
A PDF that cannot be used must not break the audit, must be reported
by name, and must never make a shipment look clean.

The documents are real PDFs in temporary folders: the sample shipments
SHJ-2026-001 (shortage, delay, invoice $120 over its line items) and
SHJ-2026-002 (no discrepancies), plus deliberately broken files (not a
PDF, and a page without text). The app tests use the temporary
two-shipment index of test_multi_shipment (stand-in embeddings, no
Ollama) and add the broken file to its raw folder afterwards, as if a
file had been damaged or copied in outside the upload workflow. The
real data/raw folder and index are never used.

Behaviour under test:

    - one unreadable PDF does not crash reconcile_shipment or the tabs
    - it is named in the result, the warnings and the review items
    - it is never attributed to a shipment, not even by its file name
    - no shipment is NO DISCREPANCIES while it cannot be ruled out
      (NEEDS REVIEW with a "Documents" review item); real discrepancies
      and every calculation are unchanged
    - a missing required document stays an explicit failure that names
      the unreadable files
    - shipment isolation is unchanged

Run from the project root:
    python -m pytest tests/test_unreadable_documents.py
"""

import io
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_analytics as analytics  # noqa: E402
import audit_explanations as explanations  # noqa: E402
import audit_history as history  # noqa: E402
import chunker  # noqa: E402
import financial_exposure as exposure  # noqa: E402
import reconciliation  # noqa: E402
from audit_export import audit_csv  # noqa: E402
from audit_pdf_export import audit_pdf  # noqa: E402
from isolation import SAMPLE_SHIPMENT_DIR, redirect_project_paths  # noqa: E402


A = "SHJ-2026-001"
B = "SHJ-2026-002"

SECOND_SHIPMENT_DIR = PROJECT_ROOT / "tests" / "fixtures" / "shipment_SHJ-2026-002"

BROKEN = "broken_scan.pdf"


# ============================================================
# FOLDERS
# ============================================================

def sample_folder(tmp_path, broken=True, blank=False):

    raw = tmp_path / "docs" / "raw"
    raw.mkdir(parents=True)

    for source in (SAMPLE_SHIPMENT_DIR, SECOND_SHIPMENT_DIR):
        for pdf in sorted(source.glob("*.pdf")):
            shutil.copy2(pdf, raw / pdf.name)

    if broken:
        (raw / BROKEN).write_bytes(b"this is not a pdf")

    if blank:
        document = pymupdf.open()
        document.new_page()
        document.save(str(raw / "blank_scan.pdf"))
        document.close()

    return raw


def reconcile(raw, tmp_path, shipment):
    """
    reconcile_shipment reading the folder itself, as the tabs do.
    """

    with redirect_project_paths(raw, tmp_path / "chroma_db"):
        return reconciliation.reconcile_shipment(shipment)


def documents_item(result):

    return [item for item in result["review_items"] if item["category"] == "Documents"]


# ============================================================
# THE LOADER
# ============================================================

def test_read_chunks_reports_every_file_that_gives_no_chunks(tmp_path):

    raw = sample_folder(tmp_path, blank=True)

    chunks, problems = chunker.read_chunks(raw)

    assert [(item["filename"], item["status"]) for item in problems] == [
        ("blank_scan.pdf", "no_text"), (BROKEN, "unreadable"),
    ]
    assert problems[0]["error"] is None and problems[1]["error"]

    assert {chunk["metadata"]["source"] for chunk in chunks} == {
        pdf.name for source in (SAMPLE_SHIPMENT_DIR, SECOND_SHIPMENT_DIR) for pdf in source.glob("*.pdf")
    }

    assert chunker.describe_problems(problems) == "blank_scan.pdf (no extractable text), broken_scan.pdf (unreadable)"


def test_read_chunks_equal_create_all_chunks_when_every_file_reads(tmp_path):

    raw = sample_folder(tmp_path, broken=False)

    with redirect_project_paths(raw, tmp_path / "chroma_db"):
        expected = chunker.create_all_chunks()

    chunks, problems = chunker.read_chunks(raw)

    assert problems == [] and chunks == expected

    # The project's own loader still fails loudly (index builders rely on it)
    (raw / BROKEN).write_bytes(b"this is not a pdf")

    with redirect_project_paths(raw, tmp_path / "chroma_db"):
        with pytest.raises(Exception):
            chunker.create_all_chunks()


# ============================================================
# RECONCILIATION
# ============================================================

def test_a_broken_pdf_does_not_crash_reconciliation_or_change_its_calculations(tmp_path):

    clean = reconcile(sample_folder(tmp_path / "clean", broken=False), tmp_path / "clean", A)
    broken = reconcile(sample_folder(tmp_path / "broken"), tmp_path / "broken", A)

    assert clean["success"] and broken["success"]

    # Every calculated value, discrepancy and piece of evidence is identical
    for key in ("quantity", "delivery", "invoice", "discrepancies", "carrier", "route", "shipment_id"):
        assert broken[key] == clean[key], key

    assert broken["final_status"] == clean["final_status"] == "DISCREPANCIES DETECTED"

    # The broken file is reported, as a review item, and nowhere in the evidence
    assert broken["unreadable_files"] == [BROKEN] and clean["unreadable_files"] == []
    assert [item["reason"] for item in documents_item(broken)] == [
        "1 file(s) in the document folder could not be used (broken_scan.pdf (unreadable)). "
        "Their shipment cannot be established, so the audit cannot rule out that they hold "
        "a BOL, invoice or POD of this shipment."
    ]
    assert documents_item(clean) == []

    for category in ("quantity", "delivery", "invoice"):
        assert all(item["source"] != BROKEN for item in broken[category]["evidence"])


@pytest.mark.parametrize("blank", [False, True], ids=["unreadable", "no extractable text"])
def test_no_shipment_is_clean_while_an_unusable_file_cannot_be_ruled_out(tmp_path, blank):

    raw = sample_folder(tmp_path, broken=not blank, blank=blank)

    result = reconcile(raw, tmp_path, B)

    assert result["success"]
    assert result["final_status"] == "NEEDS REVIEW"          # was NO DISCREPANCIES
    assert result["discrepancies"] == []
    assert [item["category"] for item in result["review_items"]] == ["Documents"]

    # The three audits themselves are unchanged and verified
    assert (result["quantity"]["status"], result["delivery"]["status"], result["invoice"]["status"]) == (
        "MATCH", "EARLY", "MATCH",
    )

    expected_name = "blank_scan.pdf (no extractable text)" if blank else "broken_scan.pdf (unreadable)"

    assert expected_name in result["review_items"][0]["reason"]

    # A folder without it is clean, as before
    clean = reconcile(sample_folder(tmp_path / "clean", broken=False), tmp_path / "clean", B)

    assert clean["final_status"] == "NO DISCREPANCIES" and clean["review_items"] == []


def test_a_missing_required_document_stays_a_failure_that_names_the_file(tmp_path):

    raw = sample_folder(tmp_path)

    (raw / f"POD_{B}.pdf").unlink()

    result = reconcile(raw, tmp_path, B)

    assert result["success"] is False
    assert result["error"].startswith("Reconciliation requires BOL, Invoice and POD.")
    assert "broken_scan.pdf (unreadable)" in result["error"]
    assert "cannot be assigned to any shipment" in result["error"]

    # Without an unreadable file the message is the original one
    (raw / BROKEN).unlink()

    assert reconcile(raw, tmp_path, B)["error"] == "Reconciliation requires BOL, Invoice and POD."


def test_a_damaged_pdf_is_not_attributed_to_a_shipment_by_its_name(tmp_path):

    raw = sample_folder(tmp_path, broken=False)

    # The POD of SHJ-2026-001 is damaged: its name says whose it is,
    # but nothing readable does
    (raw / f"POD_{A}.pdf").write_bytes(b"damaged")

    failed = reconcile(raw, tmp_path, A)

    assert failed["success"] is False
    assert "requires BOL, Invoice and POD" in failed["error"]
    assert f"POD_{A}.pdf (unreadable)" in failed["error"]

    # The other shipment is unaffected except that it cannot be called clean
    other = reconcile(raw, tmp_path, B)

    assert other["final_status"] == "NEEDS REVIEW"
    assert {item["shipment_id"] for category in ("quantity", "delivery", "invoice") for item in other[category]["evidence"]} == {B}


def test_when_nothing_can_be_read_the_failure_is_explicit(tmp_path):

    raw = tmp_path / "docs" / "raw"
    raw.mkdir(parents=True)
    (raw / "a.pdf").write_bytes(b"x")
    (raw / "b.pdf").write_bytes(b"y")

    result = reconcile(raw, tmp_path, A)

    assert result["success"] is False
    assert result["error"].startswith("No document could be read.")
    assert "a.pdf (unreadable), b.pdf (unreadable)" in result["error"]
    assert result["unreadable_files"] == ["a.pdf", "b.pdf"]

    # An empty folder is still "nothing" (unchanged)
    empty = tmp_path / "empty" / "raw"
    empty.mkdir(parents=True)

    assert reconcile(empty, tmp_path / "empty", A) is None


def test_shipment_isolation_is_unchanged_with_a_broken_file(tmp_path):

    raw = sample_folder(tmp_path)

    for shipment in (A, B):

        result = reconcile(raw, tmp_path, shipment)

        for category in ("quantity", "delivery", "invoice"):
            assert {item["shipment_id"] for item in result[category]["evidence"]} == {shipment}

    # Several shipments without a selection are still refused
    with redirect_project_paths(raw, tmp_path / "chroma_db"):
        assert "multiple shipments" in reconciliation.reconcile_shipment()["error"]


# ============================================================
# ANALYTICS, EXPLANATIONS, HISTORY, EXPORTS
# ============================================================

def snapshot_of(raw):

    chunks, problems = chunker.read_chunks(raw)

    return analytics.build_snapshot(chunks, reconciliation.reconcile_shipment, unreadable_files=problems)


def test_snapshot_lists_the_files_and_no_shipment_is_clean(tmp_path):

    shots = snapshot_of(sample_folder(tmp_path))

    assert [item["filename"] for item in shots["unreadable_files"]] == [BROKEN]

    rows = {row["shipment_id"]: row for row in analytics.register_rows(shots)}

    assert rows[A]["final_status"] == "DISCREPANCIES DETECTED"
    assert rows[B]["final_status"] == "NEEDS REVIEW" and rows[B]["needs_review"]

    figures = analytics.kpis(list(rows.values()))

    assert (figures["with_discrepancies"], figures["needs_review"], figures["no_discrepancies"]) == (1, 2, 0)

    # The same documents without the broken file
    clean = analytics.kpis(analytics.register_rows(snapshot_of(sample_folder(tmp_path / "ok", broken=False))))

    assert (clean["needs_review"], clean["no_discrepancies"]) == (0, 1)

    # The invoice figures are untouched
    assert {row["shipment_id"]: row["classification"] for row in exposure.exposure_rows(shots)} == {
        A: exposure.MISMATCH, B: exposure.VERIFIED,
    }


def test_the_documents_finding_is_explained_and_exported(tmp_path):

    shots = snapshot_of(sample_folder(tmp_path))
    exceptions = analytics.build_exceptions(shots)

    finding = next(item for item in exceptions if item["exception_key"] == "SHJ2026002|review-documents")

    assert (finding["kind"], finding["category"], finding["severity"]) == (
        "Needs review", "Documents needs review", "MEDIUM",
    )

    explained = next(item for item in explanations.explain_all(shots, exceptions) if item["exception_key"] == finding["exception_key"])

    assert "broken_scan.pdf (unreadable)" in explained["reason"]
    assert explained["sources"] == [] and explained["evidence_note"] == explanations.NO_EVIDENCE
    assert explained["next_step"].startswith("Replace the document with a readable copy")

    # Both reports still work and carry the finding
    result = shots["shipments"][1]["result"]

    csv_text = audit_csv(result, B).decode("utf-8-sig")

    assert "REVIEW" in csv_text and "broken_scan.pdf (unreadable)" in csv_text
    assert audit_pdf(result, B).startswith(b"%PDF-")


def test_history_records_the_finding_and_sees_it_resolved(tmp_path):

    store = history.HistoryStore(tmp_path / "history.sqlite3")

    broken, fixed = snapshot_of(sample_folder(tmp_path / "a")), snapshot_of(sample_folder(tmp_path / "b", broken=False))

    store.record_run(broken, analytics.build_exceptions(broken))
    store.record_run(fixed, analytics.build_exceptions(fixed))

    entries = store.timeline("SHJ2026002")

    assert [entry["snapshot"]["final_status"] for entry in reversed(entries)] == ["NEEDS REVIEW", "NO DISCREPANCIES"]

    first, latest = entries[1], entries[0]

    assert [item["category"] for item in first["snapshot"]["findings"]] == ["Documents needs review"]
    assert latest["changes"]["status_change"] == ("NEEDS REVIEW", "NO DISCREPANCIES")
    assert [item["category"] for item in latest["changes"]["resolved"]] == ["Documents needs review"]

    # The snapshot keeps the file name only in the finding's one-line summary, never document text
    assert b"this is not a pdf" not in store.path.read_bytes()


# ============================================================
# THE APP
# ============================================================

@pytest.fixture(scope="module")
def workspace():
    """
    Both sample shipments in a temporary raw folder, with a temporary
    index built from them (stand-in embeddings), before any broken
    file exists.
    """

    import document_loader
    import rag
    import vector_store
    from test_ingestion_manager import fake_embedding, patched
    from test_multi_shipment import isolated_workspace

    with isolated_workspace() as (raw_dir, chroma_dir):

        assert vector_store.CHROMA_DIR == chroma_dir and rag.CHROMA_DIR == chroma_dir
        assert document_loader.RAW_DATA_DIR == raw_dir

        with patched(vector_store, "create_embedding", fake_embedding):
            with redirect_stdout(io.StringIO()):
                vector_store.build_vector_database()

        yield raw_dir


@pytest.fixture
def broken_file(workspace):
    """
    A file that cannot be read appears in the raw folder (damaged, or
    copied in outside the upload workflow), and goes away afterwards.
    """

    path = workspace / BROKEN
    path.write_bytes(b"this is not a pdf")

    yield path

    path.unlink(missing_ok=True)


def texts(at, kind, tab=None):

    elements = getattr(tab if tab is not None else at, kind)

    return [str(item.value) for item in elements]


def test_the_reconciliation_tab_reports_the_file_and_is_not_clean(workspace, broken_file):

    from test_multi_shipment import run_app

    at = run_app(B, "Run Reconciliation")

    assert not at.exception, [str(item.value) for item in at.exception]

    warnings = texts(at, "warning")

    assert any("broken_scan.pdf" in text and "belong to no shipment" in text for text in warnings)
    assert any(text.startswith("**Documents** — needs review") for text in warnings)
    assert any("NEEDS REVIEW" in text for text in warnings)

    # Never the clean result
    assert not any("NO DISCREPANCIES" in text for text in texts(at, "success"))

    # The other shipment keeps its real discrepancies
    at = run_app(A, "Run Reconciliation")

    assert not at.exception
    assert any("DISCREPANCIES DETECTED" in text for text in texts(at, "error"))
    assert any("Quantity" in text and "48 pieces" in text for text in texts(at, "warning"))


def test_the_reconciliation_tab_names_the_file_when_a_document_is_missing(workspace, broken_file):

    from test_multi_shipment import run_app

    pod = workspace / f"POD_{B}.pdf"
    original = pod.read_bytes()

    pod.write_bytes(b"damaged")

    try:

        at = run_app(B, "Run Reconciliation")

        assert not at.exception, [str(item.value) for item in at.exception]

        errors = texts(at, "error")

        assert any(
            "requires BOL, Invoice and POD" in text and f"POD_{B}.pdf (unreadable)" in text
            and "broken_scan.pdf (unreadable)" in text
            for text in errors
        ), errors

    finally:
        pod.write_bytes(original)


def test_shipment_intelligence_is_not_clear(workspace, broken_file):

    from test_multi_shipment import run_app

    at = run_app(B, "Run Shipment Intelligence")

    assert not at.exception, [str(item.value) for item in at.exception]

    statuses = [
        text for kind in ("error", "warning", "success")
        for text in texts(at, kind) if "Shipment Status" in text
    ]

    assert statuses and all("NEEDS REVIEW" in text for text in statuses), statuses
    assert not any("CLEAR" in text for text in statuses)


def test_analytics_and_operations_show_the_warning_and_no_clean_shipment(workspace, broken_file, tmp_path):

    import config
    import rag
    from streamlit.testing.v1 import AppTest
    from test_ingestion_manager import patched

    app_file = PROJECT_ROOT / "app.py"

    with patched(config, "REVIEW_DB_PATH", tmp_path / "reviews.sqlite3"):
        with patched(config, "HISTORY_DB_PATH", tmp_path / "history.sqlite3"):

            at = AppTest.from_file(str(app_file), default_timeout=300)
            at.run()

            assert not at.exception

            [button for button in at.button if button.label == "Load audit analytics"][0].click().run()
            [button for button in at.button if button.label == "Load audit operations"][0].click().run()

            assert not at.exception, [str(item.value) for item in at.exception]

            analytics_tab, operations_tab = at.tabs[4], at.tabs[5]

            for tab in (analytics_tab, operations_tab):
                assert any(
                    "could not be used and belong to no shipment" in text and "broken_scan.pdf (unreadable)" in text
                    for text in texts(at, "warning", tab)
                )

            metrics = {item.label: item.value for item in analytics_tab.metric}

            assert metrics["Need manual review"] == "2" and metrics["No discrepancies"] == "0"

            # The completeness register lists the file too
            assert "Files that could not be used (1)" in [item.label for item in operations_tab.expander]

            # The shipment workflow shows the Documents finding for the clean one
            at.selectbox(key="op_shipment").select(B).run()

            assert not at.exception
            assert any("Documents needs review" in item.label for item in at.tabs[5].expander)


# ============================================================
# NOTHING CHANGES WITHOUT A BROKEN FILE
# ============================================================

def test_without_a_broken_file_the_result_has_no_documents_item(workspace):

    from test_multi_shipment import run_app

    at = run_app(B, "Run Reconciliation")

    assert not at.exception
    assert any("NO DISCREPANCIES" in text for text in texts(at, "success"))
    assert not any("could not be used" in text for text in texts(at, "warning"))
