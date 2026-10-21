"""
Audit Analytics cache invalidation, end to end.

The analytics tab caches its result per document folder and a
fingerprint of the folder's PDFs (names, sizes, modified times). These
tests run the real dashboard (app.py, AppTest) against temporary
document folders and temporary review databases, change the PDFs while
the dashboard is open, and check that stale results are never reused:

    - a PDF of a new shipment is reflected
    - a replaced PDF is re-read
    - a removed PDF is no longer treated as indexed
    - cached results do not leak between document folders
    - review state does not leak between review databases

The PDFs are the project's sample fixtures plus synthetic PDFs written
with the ingestion tests' make_pdf. The real data/raw folder, index
and review database are never used (tests/conftest.py checks this).

Run from the project root:
    python -m pytest tests/test_analytics_cache.py
"""

import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from streamlit.testing.v1 import AppTest  # noqa: E402

import audit_analytics  # noqa: E402
import config  # noqa: E402
from isolation import SAMPLE_SHIPMENT_DIR, redirect_project_paths  # noqa: E402
from review_store import ReviewStore  # noqa: E402
from test_ingestion_manager import make_pdf, patched  # noqa: E402


A = "SHJ-2026-001"
B = "SHJ-2026-002"

SECOND_SHIPMENT_DIR = PROJECT_ROOT / "tests" / "fixtures" / "shipment_SHJ-2026-002"
APP_FILE = PROJECT_ROOT / "app.py"


# ============================================================
# DOCUMENT FOLDERS
# ============================================================

def folder(tmp_path, name, *source_dirs):
    """
    A new raw folder holding copies of the sample PDFs of the given
    fixture folders.
    """

    raw = tmp_path / name / "raw"
    raw.mkdir(parents=True)

    for source in source_dirs:
        for pdf in sorted(source.glob("*.pdf")):
            shutil.copy2(pdf, raw / pdf.name)

    return raw


@contextmanager
def workspace(raw, database):
    """
    The dashboard reads documents from raw and keeps reviews in
    database; the search index is never opened by these tests.
    """

    with redirect_project_paths(raw, raw.parent / "chroma_db"):
        with patched(config, "REVIEW_DB_PATH", database):
            yield


def shipment_a_pod(delivered, date):
    """
    A synthetic POD that replaces SHJ-2026-001's sample POD.
    """

    return [
        f"Shipment Number: {A}", "",
        "Delivery Date:", date, "",
        "Number of Pieces Delivered:", str(delivered), "",
        "End of document",
    ]


def replace_pod(raw, delivered, date):

    make_pdf(raw / f"POD_{A}.pdf", "PROOF OF DELIVERY", shipment_a_pod(delivered, date))


# ============================================================
# THE DASHBOARD
# ============================================================

def open_analytics():

    at = AppTest.from_file(str(APP_FILE), default_timeout=240)
    at.run()

    [button for button in at.button if button.label == "Load audit analytics"][0].click().run()

    assert not at.exception, [str(item.value) for item in at.exception]

    return at


def refresh(at):
    """
    The dashboard rerun, as when the user interacts with it after a
    document changed.
    """

    at.run()

    assert not at.exception, [str(item.value) for item in at.exception]

    return at


def metrics(at):

    return {metric.label: metric.value for metric in at.tabs[4].metric}


def register(at):

    frame = [item.value for item in at.tabs[4].dataframe if "Final status" in item.value.columns][0]

    return {row["Shipment"]: row for _, row in frame.iterrows()}


def exception_categories(at):

    frames = [item.value for item in at.tabs[4].dataframe if "Review state" in item.value.columns]

    return sorted(zip(frames[0]["Shipment"], frames[0]["Category"])) if frames else []


# ============================================================
# TESTS
# ============================================================

def test_added_replaced_and_removed_documents_are_reflected(tmp_path):

    raw = folder(tmp_path, "docs", SAMPLE_SHIPMENT_DIR)

    with workspace(raw, tmp_path / "reviews.sqlite3"):

        at = open_analytics()

        # 1. Only SHJ-2026-001: shortage of 3, 2 days late, invoice $120 over
        assert metrics(at)["Identified shipments"] == "1"
        assert metrics(at)["With discrepancies"] == "1"
        assert set(register(at)) == {A}
        assert register(at)[A]["Quantity"] == "MISMATCH"
        assert exception_categories(at) == [
            (A, "Delivery delay"), (A, "Invoice mismatch"), (A, "Quantity shortage"),
        ]

        # An unchanged folder is served from the cache, not re-read
        before = audit_analytics.build_snapshot

        def forbidden(*args, **kwargs):
            raise AssertionError("the analytics were recomputed for an unchanged folder")

        with patched(audit_analytics, "build_snapshot", forbidden):
            assert metrics(refresh(at)) == metrics(at)

        assert audit_analytics.build_snapshot is before

        # 2. A new shipment appears
        for pdf in sorted(SECOND_SHIPMENT_DIR.glob("*.pdf")):
            shutil.copy2(pdf, raw / pdf.name)

        refresh(at)

        assert metrics(at)["Identified shipments"] == "2"
        assert metrics(at)["No discrepancies"] == "1"
        assert set(register(at)) == {A, B}
        assert register(at)[B]["Final status"] == "NO DISCREPANCIES"

        # 3. SHJ-2026-001's POD is replaced by one delivering all 48
        # pieces on the expected date: the shortage and the delay go
        replace_pod(raw, delivered=48, date="October 3, 2026")

        refresh(at)

        assert register(at)[A]["Quantity"] == "MATCH"
        assert register(at)[A]["Delivery"] == "ON TIME"
        assert exception_categories(at) == [(A, "Invoice mismatch")]
        assert metrics(at)["With discrepancies"] == "1"

        # ... and a replacement with no readable piece count needs review
        replace_pod(raw, delivered="", date="October 3, 2026")

        refresh(at)

        assert register(at)[A]["Quantity"] == "NEEDS REVIEW"
        assert (A, "Quantity needs review") in exception_categories(at)

        # 4. SHJ-2026-002's invoice is removed: it can no longer be
        # reconciled and is not shown as clean from the old result
        (raw / f"INVOICE_{B}.pdf").unlink()

        refresh(at)

        assert register(at)[B]["Final status"] == "NOT RECONCILED"
        assert metrics(at)["No discrepancies"] == "0"
        assert metrics(at)["Need manual review"] == "2"
        assert (B, "Reconciliation incomplete") in exception_categories(at)

        # 5. All of SHJ-2026-002's documents are removed
        for pdf in raw.glob(f"*{B}*.pdf"):
            pdf.unlink()

        refresh(at)

        assert set(register(at)) == {A}
        assert metrics(at)["Identified shipments"] == "1"
        assert all(shipment == A for shipment, _ in exception_categories(at))

        # 6. The folder is emptied
        for pdf in raw.glob("*.pdf"):
            pdf.unlink()

        refresh(at)

        assert metrics(at) == {}
        assert any("No PDFs in the document folder" in item.value for item in at.tabs[4].info)


def test_cached_results_do_not_leak_between_document_folders(tmp_path):

    only_a = folder(tmp_path, "only_a", SAMPLE_SHIPMENT_DIR)
    both = folder(tmp_path, "both", SAMPLE_SHIPMENT_DIR, SECOND_SHIPMENT_DIR)

    # Same file names, sizes and times in the shared shipment's PDFs:
    # only the folder differs
    def identified(raw):

        with workspace(raw, raw.parent / "reviews.sqlite3"):
            return metrics(open_analytics())["Identified shipments"]

    assert identified(only_a) == "1"
    assert identified(both) == "2"
    assert identified(only_a) == "1"
    assert identified(both) == "2"


def test_review_state_does_not_leak_between_review_databases(tmp_path):

    raw = folder(tmp_path, "docs", SAMPLE_SHIPMENT_DIR)

    first, second = tmp_path / "first.sqlite3", tmp_path / "second.sqlite3"

    def review_states():

        frame = [item.value for item in at.tabs[4].dataframe if "Review state" in item.value.columns][0]

        return set(frame["Review state"])

    with workspace(raw, first):

        at = open_analytics()

        assert review_states() == {"Open"}

        store = ReviewStore(first)
        invoice = next(row for row in store.states().values() if row["category"] == "Invoice mismatch")
        store.set_status(
            {"exception_key": invoice["exception_key"], "signature": invoice["signature"]},
            "in_review", reviewer="krishna",
        )

        refresh(at)

        assert "In review" in review_states()

    # The same documents with another database: nothing carries over,
    # although the audit result itself is served from the cache
    with workspace(raw, second):

        at = open_analytics()

        assert review_states() == {"Open"}
        assert {row["status"] for row in ReviewStore(second).states().values()} == {"open"}

    # The first database kept its state
    assert {row["status"] for row in ReviewStore(first).states().values()} == {"open", "in_review"}


def test_a_changed_fingerprint_is_what_invalidates_the_cache(tmp_path):

    raw = folder(tmp_path, "docs", SAMPLE_SHIPMENT_DIR)

    with workspace(raw, tmp_path / "reviews.sqlite3"):

        at = open_analytics()
        assert metrics(at)["Identified shipments"] == "1"

        # With the fingerprint frozen the cached result is (wrongly)
        # reused: this is what the fingerprint protects against
        frozen = audit_analytics.source_fingerprint(raw)

        for pdf in sorted(SECOND_SHIPMENT_DIR.glob("*.pdf")):
            shutil.copy2(pdf, raw / pdf.name)

        with patched(audit_analytics, "source_fingerprint", lambda directory: frozen):
            assert metrics(refresh(at))["Identified shipments"] == "1"

        # The real fingerprint sees the new documents
        assert metrics(refresh(at))["Identified shipments"] == "2"
