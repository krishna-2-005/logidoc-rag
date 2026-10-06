"""
LogiDoc-RAG Multi-Shipment Regression Test

Verifies that two shipments indexed together never mix documents,
metrics, exceptions or evidence, both in the intelligence engine and
in the Streamlit dashboard.

The test builds a temporary, isolated workspace containing:
    - the SHJ-2026-001 sample PDFs (copied from data/raw)
    - the SHJ-2026-002 fixture PDFs (tests/fixtures/shipment_SHJ-2026-002)
and a temporary ChromaDB index. The production data/raw folder and
chroma_db index are never written; the test checks this at the end.

Requires Ollama running with the embedding model from config.py,
because the temporary index is built with the real pipeline.

Run from the project root:
    python tests/test_multi_shipment.py
"""

import io
import shutil
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"

for path in (PROJECT_ROOT, SRC_DIR):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config
import document_loader
import rag
import vector_store
from intelligence_service import analyze_shipment
from shipment_retriever import get_shipment_documents
from shipment_selector import get_available_shipments
from streamlit.testing.v1 import AppTest


# ============================================================
# TEST DATA
# ============================================================

FIXTURE_DIR = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "shipment_SHJ-2026-002"
)

APP_FILE = PROJECT_ROOT / "app.py"

SHIPMENT_A = "SHJ-2026-001"
SHIPMENT_B = "SHJ-2026-002"

EXPECTED = {

    SHIPMENT_A: {
        "sources": {
            "BOL_SHJ-2026-001.pdf",
            "POD_SHJ-2026-001.pdf",
            "INVOICE_SHJ-2026-001.pdf",
        },
        "bol_pieces": 48,
        "delivered_pieces": 45,
        "difference": 3,
        "quantity_status": "SHORTAGE",
        "expected_delivery": "October 3, 2026",
        "actual_delivery": "October 5, 2026",
        "delay_days": 2,
        "delivery_status": "DELAYED",
        "invoice_total": 1550.00,
        "exception_categories": ["Delivery", "Quantity"],
        "shipment_status": "EXCEPTION",
    },

    SHIPMENT_B: {
        "sources": {
            "BOL_SHJ-2026-002.pdf",
            "POD_SHJ-2026-002.pdf",
            "INVOICE_SHJ-2026-002.pdf",
        },
        "bol_pieces": 60,
        "delivered_pieces": 60,
        "difference": 0,
        "quantity_status": "MATCH",
        "expected_delivery": "October 9, 2026",
        "actual_delivery": "October 8, 2026",
        "delay_days": 0,
        "delivery_status": "EARLY",
        "invoice_total": 2875.50,
        "exception_categories": [],
        "shipment_status": "CLEAR",
    },
}


# ============================================================
# HELPERS
# ============================================================

def check(label, actual, expected, display=None):
    """
    Assert one value and print a PASS line.
    """

    assert actual == expected, (
        f"FAIL: {label}: expected {expected!r}, got {actual!r}"
    )

    print(
        f"PASS: {label} = "
        f"{display if display is not None else actual}"
    )


def section(title):

    print()
    print(f"--- {title}")


def production_snapshot():
    """
    File names, sizes and modification times of the production
    sample PDFs and index, used to prove the test never wrote them.
    """

    paths = sorted(config.RAW_DATA_DIR.glob("*"))
    paths += sorted(config.CHROMA_DIR.rglob("*"))

    return {
        str(path): (path.stat().st_size, path.stat().st_mtime)
        for path in paths
        if path.is_file()
    }


@contextmanager
def isolated_workspace():
    """
    Copy both shipments into a temporary raw folder, point every
    loaded module at it and at a temporary ChromaDB folder, and
    restore the production paths afterwards.
    """

    # On Windows ChromaDB keeps its files open until the process
    # exits, so a previous run's folder is removed here instead
    for stale in Path(tempfile.gettempdir()).glob("logidoc_multi_*"):
        shutil.rmtree(stale, ignore_errors=True)

    workspace = Path(
        tempfile.mkdtemp(prefix="logidoc_multi_")
    )

    raw_dir = workspace / "raw"
    chroma_dir = workspace / "chroma_db"

    raw_dir.mkdir()

    pdf_files = (
        sorted(config.RAW_DATA_DIR.glob("*.pdf"))
        + sorted(FIXTURE_DIR.glob("*.pdf"))
    )

    for pdf_file in pdf_files:

        shutil.copy2(
            pdf_file,
            raw_dir / pdf_file.name
        )

    # Modules bind these paths at import time with
    # "from config import ...", so each binding is replaced
    replacements = {
        "RAW_DATA_DIR": (config.RAW_DATA_DIR, raw_dir),
        "CHROMA_DIR": (config.CHROMA_DIR, chroma_dir),
    }

    patched = []

    for module in list(sys.modules.values()):

        for attribute, (old, new) in replacements.items():

            try:
                value = getattr(module, attribute, None)
            except Exception:
                continue

            if isinstance(value, Path) and value == old:

                setattr(module, attribute, new)

                patched.append(
                    (module, attribute, old)
                )

    try:

        yield raw_dir, chroma_dir

    finally:

        for module, attribute, old in patched:
            setattr(module, attribute, old)

        try:

            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()

        except Exception:
            pass

        shutil.rmtree(
            workspace,
            ignore_errors=True
        )


def ui_snapshot(at):
    """
    Collect what the Shipment Intelligence / Reconciliation
    sections rendered: metrics, evidence files and messages.
    """

    metrics = {}

    # Later metrics with the same label (e.g. "Documents")
    # belong to the analysis section and override the header
    for metric in at.metric:
        metrics[metric.label] = metric.value

    markdown = [
        str(item.value)
        for item in at.markdown
    ]

    evidence_sources = {
        text.replace("**", "").replace("📄", "").strip()
        for text in markdown
        if text.startswith("**📄 ")
    }

    return {
        "metrics": metrics,
        "markdown": markdown,
        "evidence_sources": evidence_sources,
        "warnings": [str(item.value) for item in at.warning],
        "success": [str(item.value) for item in at.success],
        "errors": [str(item.value) for item in at.error],
        "exceptions": [str(item.value) for item in at.exception],
        "dataframes": [item.value for item in at.dataframe],
    }


def run_app(shipment, button):
    """
    Open the dashboard, select a shipment in the sidebar
    and click one of the tab buttons.
    """

    at = AppTest.from_file(
        str(APP_FILE),
        default_timeout=240
    )

    at.run()

    selector = at.sidebar.selectbox[0]

    if shipment != selector.value:
        selector.select(shipment).run()

    [
        item
        for item in at.button
        if item.label == button
    ][0].click().run()

    return at


# ============================================================
# TESTS
# ============================================================

def test_shipment_detection():

    section("Shipment detection")

    shipments = {
        shipment["shipment_id"]: shipment
        for shipment in get_available_shipments()
    }

    check(
        "Shipments detected",
        sorted(shipments),
        [SHIPMENT_A, SHIPMENT_B],
        display=", ".join(sorted(shipments))
    )

    for shipment_id in (SHIPMENT_A, SHIPMENT_B):

        shipment = shipments[shipment_id]

        check(
            f"{shipment_id} documents",
            shipment["document_count"],
            3
        )

        check(
            f"{shipment_id} document types",
            sorted(shipment["document_types"]),
            ["BOL", "INVOICE", "POD"],
            display=", ".join(sorted(shipment["document_types"]))
        )

    # The dashboard groups by index metadata, so check it too
    _, metadatas = rag.get_all_documents()

    indexed = {}

    for metadata in metadatas:

        indexed.setdefault(
            metadata.get("shipment_id"),
            set()
        ).add(
            metadata.get("source")
        )

    for shipment_id in (SHIPMENT_A, SHIPMENT_B):

        check(
            f"{shipment_id} indexed sources",
            indexed.get(shipment_id),
            EXPECTED[shipment_id]["sources"],
            display=len(indexed.get(shipment_id, ()))
        )


def test_retrieval_isolation():

    section("Retrieval isolation")

    for shipment_id in (SHIPMENT_A, SHIPMENT_B):

        documents = get_shipment_documents(shipment_id)

        shipment_ids = {
            document["metadata"].get("shipment_id")
            for document in documents
        }

        sources = {
            document["metadata"].get("source")
            for document in documents
        }

        check(
            f"{shipment_id} retrieves only its own chunks",
            shipment_ids,
            {shipment_id},
            display=", ".join(sorted(shipment_ids))
        )

        check(
            f"{shipment_id} retrieves only its own files",
            sources,
            EXPECTED[shipment_id]["sources"],
            display=", ".join(sorted(sources))
        )


def check_intelligence(shipment_id, intelligence):

    expected = EXPECTED[shipment_id]

    assert intelligence.get("success"), (
        f"FAIL: analyze_shipment({shipment_id!r}) failed: "
        f"{intelligence.get('error')}"
    )

    documents = intelligence["documents"]
    quantity = intelligence["quantity"]
    delivery = intelligence["delivery"]

    check(
        f"{shipment_id} shipment ID",
        intelligence["shipment_id"],
        shipment_id
    )

    check(
        f"{shipment_id} documents",
        (documents["found"], documents["expected"]),
        (3, 3),
        display=f"{documents['found']}/{documents['expected']}"
    )

    check(
        f"{shipment_id} completeness",
        documents["completeness"],
        100,
        display=f"{documents['completeness']}%"
    )

    check(
        f"{shipment_id} BOL pieces",
        quantity["bol_pieces"],
        expected["bol_pieces"]
    )

    check(
        f"{shipment_id} delivered pieces",
        quantity["delivered_pieces"],
        expected["delivered_pieces"]
    )

    check(
        f"{shipment_id} quantity difference",
        quantity["difference"],
        expected["difference"]
    )

    check(
        f"{shipment_id} quantity status",
        quantity["status"],
        expected["quantity_status"]
    )

    check(
        f"{shipment_id} expected delivery",
        delivery["expected"],
        expected["expected_delivery"]
    )

    check(
        f"{shipment_id} actual delivery",
        delivery["actual"],
        expected["actual_delivery"]
    )

    check(
        f"{shipment_id} delivery delay",
        delivery["delay_days"],
        expected["delay_days"],
        display=f"{delivery['delay_days']} days"
    )

    check(
        f"{shipment_id} delivery status",
        delivery["status"],
        expected["delivery_status"]
    )

    check(
        f"{shipment_id} invoice total",
        round(intelligence["invoice_total"], 2),
        expected["invoice_total"],
        display=f"${intelligence['invoice_total']:,.2f}"
    )

    check(
        f"{shipment_id} exceptions",
        sorted(
            exception["category"]
            for exception in intelligence["exceptions"]
        ),
        expected["exception_categories"],
        display=(
            ", ".join(expected["exception_categories"])
            or "none"
        )
    )

    check(
        f"{shipment_id} shipment status",
        intelligence["shipment_status"],
        expected["shipment_status"]
    )


def test_intelligence_isolation():

    section("Intelligence isolation")

    intelligence = {
        shipment_id: analyze_shipment(shipment_id)
        for shipment_id in (SHIPMENT_A, SHIPMENT_B)
    }

    for shipment_id, result in intelligence.items():
        check_intelligence(shipment_id, result)

    a = intelligence[SHIPMENT_A]
    b = intelligence[SHIPMENT_B]

    section("Cross-shipment leak checks")

    assert a["invoice_total"] != b["invoice_total"]
    print(
        "PASS: Invoice totals do not leak "
        f"(A ${a['invoice_total']:,.2f}, B ${b['invoice_total']:,.2f})"
    )

    assert (
        a["quantity"]["bol_pieces"],
        a["quantity"]["delivered_pieces"],
    ) != (
        b["quantity"]["bol_pieces"],
        b["quantity"]["delivered_pieces"],
    )
    print(
        "PASS: Quantities do not leak "
        f"(A {a['quantity']['bol_pieces']}/"
        f"{a['quantity']['delivered_pieces']}, "
        f"B {b['quantity']['bol_pieces']}/"
        f"{b['quantity']['delivered_pieces']})"
    )

    assert not (
        {a["delivery"]["expected"], a["delivery"]["actual"]}
        & {b["delivery"]["expected"], b["delivery"]["actual"]}
    )
    print(
        "PASS: Delivery dates do not leak "
        f"(A {a['delivery']['expected']} -> {a['delivery']['actual']}, "
        f"B {b['delivery']['expected']} -> {b['delivery']['actual']})"
    )

    assert a["exception_count"] == 2 and b["exception_count"] == 0
    print(
        "PASS: Exceptions belong to the correct shipment "
        "(A 2, B 0)"
    )

    section("Mixed-shipment safety")

    unscoped = analyze_shipment()

    check(
        "Unscoped analysis refused with 2 shipments indexed",
        unscoped.get("success"),
        False,
        display=unscoped.get("error")
    )

    unknown = analyze_shipment("SHJ-2026-999")

    check(
        "Unknown shipment refused",
        unknown.get("success"),
        False,
        display=unknown.get("error")
    )


def test_dashboard():

    section("Dashboard: shipment selector")

    at = AppTest.from_file(
        str(APP_FILE),
        default_timeout=240
    )

    at.run()

    check(
        "Selector options",
        list(at.sidebar.selectbox[0].options),
        ["All Shipments", SHIPMENT_A, SHIPMENT_B],
        display=", ".join(at.sidebar.selectbox[0].options)
    )

    # --------------------------------------------------------
    # ALL SHIPMENTS
    # --------------------------------------------------------

    section("Dashboard: All Shipments")

    ui = ui_snapshot(
        run_app("All Shipments", "Run Shipment Intelligence")
    )

    check("App errors", ui["exceptions"], [], display="none")

    assert ui["dataframes"], "FAIL: no aggregate table rendered"

    table = ui["dataframes"][0]

    check(
        "Aggregate rows",
        sorted(table["Shipment"].tolist()),
        [SHIPMENT_A, SHIPMENT_B],
        display=", ".join(sorted(table["Shipment"].tolist()))
    )

    rows = {
        row["Shipment"]: row
        for row in table.to_dict("records")
    }

    for shipment_id in (SHIPMENT_A, SHIPMENT_B):

        check(
            f"Aggregate {shipment_id} status",
            rows[shipment_id]["Status"],
            EXPECTED[shipment_id]["shipment_status"]
        )

        check(
            f"Aggregate {shipment_id} invoice total",
            rows[shipment_id]["Invoice Total"],
            f"${EXPECTED[shipment_id]['invoice_total']:,.2f}"
        )

    check(
        "No single-shipment detail shown for All Shipments",
        (
            "Quantity Status" in ui["metrics"],
            any("Exception Details" in text for text in ui["markdown"]),
        ),
        (False, False),
        display="none"
    )

    # --------------------------------------------------------
    # EACH SHIPMENT
    # --------------------------------------------------------

    for shipment_id, other_id in (
        (SHIPMENT_A, SHIPMENT_B),
        (SHIPMENT_B, SHIPMENT_A),
    ):

        section(f"Dashboard: {shipment_id} selected")

        expected = EXPECTED[shipment_id]

        ui = ui_snapshot(
            run_app(shipment_id, "Run Shipment Intelligence")
        )

        metrics = ui["metrics"]

        check("App errors", ui["exceptions"], [], display="none")

        check(
            "Exceptions",
            metrics.get("Exceptions"),
            str(len(expected["exception_categories"]))
        )

        check("Documents", metrics.get("Documents"), "3/3")

        check("Completeness", metrics.get("Completeness"), "100%")

        check(
            "Invoice Total",
            metrics.get("Invoice Total"),
            f"${expected['invoice_total']:,.2f}"
        )

        check(
            "Quantity Status",
            metrics.get("Quantity Status"),
            expected["quantity_status"]
        )

        check(
            "Quantity Difference",
            metrics.get("Quantity Difference"),
            str(expected["difference"])
        )

        check(
            "Delivery Status",
            metrics.get("Delivery Status"),
            expected["delivery_status"]
        )

        delay_warnings = [
            text
            for text in ui["warnings"]
            if text.startswith("Delivery delayed")
        ]

        check(
            "Delivery delay",
            delay_warnings,
            (
                [f"Delivery delayed by {expected['delay_days']} day(s)"]
                if expected["delay_days"]
                else []
            ),
            display=f"{expected['delay_days']} days"
        )

        if expected["exception_categories"]:

            check(
                "Evidence files",
                ui["evidence_sources"],
                expected["sources"] - {
                    f"INVOICE_{shipment_id}.pdf"
                },
                display=", ".join(sorted(ui["evidence_sources"]))
            )

        else:

            check(
                "Evidence files",
                ui["evidence_sources"],
                set(),
                display="none (no exceptions)"
            )

            check(
                "No-exception message",
                "No exceptions detected for this shipment."
                in ui["success"],
                True
            )

        leaked = [
            text
            for text in (
                ui["markdown"]
                + ui["warnings"]
                + ui["errors"]
            )
            if other_id in text
        ]

        check(
            f"No {other_id} content shown",
            leaked,
            [],
            display="none"
        )

    # --------------------------------------------------------
    # RECONCILIATION TAB
    # --------------------------------------------------------

    section("Dashboard: Reconciliation tab")

    for shipment_id, other_id in (
        (SHIPMENT_A, SHIPMENT_B),
        (SHIPMENT_B, SHIPMENT_A),
    ):

        ui = ui_snapshot(
            run_app(shipment_id, "Run Reconciliation")
        )

        # st.error is also used for real discrepancies
        # (e.g. the SHJ-2026-001 shortage), so only app
        # crashes count as errors here
        check(
            f"{shipment_id} reconciliation app errors",
            ui["exceptions"],
            [],
            display="none"
        )

        check(
            f"{shipment_id} reconciliation shows no {other_id} content",
            [
                text
                for text in (
                    ui["markdown"]
                    + ui["warnings"]
                    + ui["errors"]
                    + ui["success"]
                )
                if other_id in text
            ],
            [],
            display="none"
        )

        check(
            f"{shipment_id} reconciliation evidence files",
            ui["evidence_sources"],
            EXPECTED[shipment_id]["sources"],
            display=", ".join(sorted(ui["evidence_sources"]))
        )

    ui = ui_snapshot(
        run_app("All Shipments", "Run Reconciliation")
    )

    check(
        "All Shipments reconciliation refuses to mix",
        (
            ui["evidence_sources"],
            any("multiple shipments" in text for text in ui["errors"]),
        ),
        (set(), True),
        display="asks to select a shipment"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # Windows consoles may not support every character in app text
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    print("=" * 70)
    print("MULTI-SHIPMENT REGRESSION TEST")
    print("=" * 70)

    before = production_snapshot()

    with isolated_workspace() as (raw_dir, chroma_dir):

        # Never build into the production index
        assert vector_store.CHROMA_DIR == chroma_dir
        assert rag.CHROMA_DIR == chroma_dir
        assert document_loader.RAW_DATA_DIR == raw_dir

        print(f"Temporary workspace: {raw_dir.parent}")

        # Keep the index build output quiet
        with redirect_stdout(io.StringIO()):
            vector_store.build_vector_database()

        test_shipment_detection()
        test_retrieval_isolation()
        test_intelligence_isolation()
        test_dashboard()

    section("Production data")

    check(
        "data/raw and chroma_db unchanged",
        production_snapshot() == before,
        True
    )


if __name__ == "__main__":

    try:

        main()

    except AssertionError as error:

        print(error)
        print()
        print("MULTI-SHIPMENT REGRESSION TEST FAILED")

        sys.exit(1)

    print()
    print("MULTI-SHIPMENT REGRESSION TEST PASSED")
