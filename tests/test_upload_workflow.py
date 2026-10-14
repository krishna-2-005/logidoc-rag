"""
LogiDoc-RAG Streamlit Upload Workflow Test

Drives the real dashboard (app.py) with Streamlit's AppTest against
a temporary copy of the production setup:

    1. The SHJ-2026-001 sample PDFs (tests/fixtures/shipment_SHJ-2026-001)
       are copied into a temporary raw folder and indexed with the
       real full rebuild, like the original production index (no
       content hashes). The live data/raw folder is never read.
    2. The SHJ-2026-002 fixture PDFs are uploaded through the sidebar
       and processed with "Process Documents".

It checks that uploads are incremental and non-destructive, that the
dashboard refreshes, that duplicates, changed and broken files are
reported without success messages, that shipment intelligence stays
isolated, and that the Diagnostics tab changes nothing.

Every loaded module is pointed at the temporary folders, and the
test checks at the end that data/raw and chroma_db are unchanged.

The workflow runs twice: with deterministic stand-in embeddings (no
Ollama needed) and with real Ollama embeddings (skipped, with the
reason, when Ollama or the embedding model is unavailable).

Run from the project root:
    python -m pytest tests/test_upload_workflow.py
"""

import io
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager, nullcontext, redirect_stdout
from pathlib import Path


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import chromadb
import pytest
from streamlit.testing.v1 import AppTest

import config
import document_loader
import ingestion_manager
import rag
import vector_store
from consistency_report import check_destination
from ingestion_journal import JOURNAL_FILENAME, read_journal
from ingestion_manager import HASH_METADATA_KEY, calculate_sha256
from intelligence_service import analyze_shipment
from isolation import sample_workspace
from ollama_check import requires_embeddings
from test_ingestion_manager import (
    check,
    fake_embedding,
    make_bol,
    patched,
    section,
)


# ============================================================
# TEST DATA
# ============================================================

APP_FILE = PROJECT_ROOT / "app.py"

FIXTURE_DIR = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "shipment_SHJ-2026-002"
)

SHIPMENT_A = "SHJ-2026-001"
SHIPMENT_B = "SHJ-2026-002"

TEMP_PREFIX = "logidoc_uploadtest_"


# ============================================================
# HELPERS
# ============================================================

def pdf_state(raw_dir):
    """
    Every PDF in the raw folder: name -> (sha256, mtime).
    """

    return {
        path.name: (calculate_sha256(path), path.stat().st_mtime)
        for path in sorted(raw_dir.glob("*.pdf"))
    }


def vector_state(chroma_dir):
    """
    Every vector: id -> (source, content hash, shipment, text).
    """

    data = chromadb.PersistentClient(
        path=str(chroma_dir)
    ).get_or_create_collection(
        name=config.COLLECTION_NAME
    ).get(
        include=["documents", "metadatas"]
    )

    return {
        vector_id: (
            metadata.get("source"),
            metadata.get(HASH_METADATA_KEY),
            metadata.get("shipment_id"),
            document,
        )
        for vector_id, metadata, document in zip(
            data["ids"],
            data["metadatas"],
            data["documents"],
        )
    }


def full_state(raw_dir, chroma_dir):
    """
    Bytes of every file in the raw folder (journal included) and
    every stored vector with its embedding.
    """

    files = {
        path.name: path.read_bytes()
        for path in sorted(raw_dir.iterdir())
        if path.is_file()
    }

    data = chromadb.PersistentClient(
        path=str(chroma_dir)
    ).get_or_create_collection(
        name=config.COLLECTION_NAME
    ).get(
        include=["documents", "metadatas", "embeddings"]
    )

    vectors = sorted(
        (
            vector_id,
            document,
            json.dumps(metadata, sort_keys=True),
            [float(value) for value in embedding],
        )
        for vector_id, document, metadata, embedding in zip(
            data["ids"],
            data["documents"],
            data["metadatas"],
            data["embeddings"],
        )
    )

    return files, vectors


def open_app():

    at = AppTest.from_file(
        str(APP_FILE),
        default_timeout=600
    )

    at.run()

    return at


def upload(at, files):
    """
    Put files into the sidebar uploader and click Process Documents.
    files: [(name, bytes)]
    """

    at.sidebar.file_uploader[0].set_value(
        [
            (name, content, "application/pdf")
            for name, content in files
        ]
    )

    [
        button
        for button in at.sidebar.button
        if button.label == "Process Documents"
    ][0].click().run()

    return at


def sidebar_text(at):

    return {
        "success": [str(item.value) for item in at.sidebar.success],
        "error": [str(item.value) for item in at.sidebar.error],
        "warning": [str(item.value) for item in at.sidebar.warning],
        "markdown": [str(item.value) for item in at.sidebar.markdown],
    }


def click(at, label):

    [
        button
        for button in at.button
        if button.label == label
    ][0].click().run()

    return at


# ============================================================
# TEST
# ============================================================

@contextmanager
def stand_in_embeddings():
    """
    Replace the Ollama embedder in the full rebuild and in the
    ingestion manager (which looks it up when called).
    """

    with patched(vector_store, "create_embedding", fake_embedding):
        with patched(ingestion_manager, "create_embedding", fake_embedding):
            yield


@pytest.mark.parametrize(
    "embeddings",
    [
        "stand-in",
        pytest.param(
            "ollama",
            marks=[pytest.mark.ollama, requires_embeddings],
        ),
    ],
)
def test_upload_workflow(embeddings):

    backend = (
        stand_in_embeddings()
        if embeddings == "stand-in"
        else nullcontext()
    )

    with backend:
        run_upload_workflow()


def run_upload_workflow():

    with sample_workspace(TEMP_PREFIX) as (raw_dir, chroma_dir):

        # ----------------------------------------------------
        # Production-like start: SHJ-2026-001 from a full rebuild
        # ----------------------------------------------------

        section("Start: SHJ-2026-001 indexed by the full rebuild")

        assert vector_store.CHROMA_DIR == chroma_dir
        assert rag.CHROMA_DIR == chroma_dir
        assert document_loader.RAW_DATA_DIR == raw_dir

        with redirect_stdout(io.StringIO()):
            vector_store.build_vector_database()

        a_files = pdf_state(raw_dir)
        a_vectors = vector_state(chroma_dir)

        check(
            "SHJ-2026-001 PDFs and vectors",
            (len(a_files), {value[2] for value in a_vectors.values()}),
            (3, {SHIPMENT_A}),
            display=f"3 PDFs, {len(a_vectors)} vectors without hashes"
        )

        # ----------------------------------------------------
        # Upload SHJ-2026-002
        # ----------------------------------------------------

        section("Upload SHJ-2026-002 through the sidebar")

        fixtures = [
            (path.name, path.read_bytes())
            for path in sorted(FIXTURE_DIR.glob("*.pdf"))
        ]

        at = upload(open_app(), fixtures)
        text = sidebar_text(at)
        result = at.session_state["ingestion_result"]

        check("App errors", [item.value for item in at.exception], [], display="none")

        check(
            "Result",
            (result["files_added"], result["files_failed"]),
            (3, 0),
            display="3 added, 0 failed"
        )

        check(
            "Success shown only for stored files",
            text["success"],
            ["3 file(s) indexed and stored."]
        )

        check(
            "Per-file outcomes shown",
            sorted(
                line
                for line in text["markdown"]
                if line.startswith("✅")
            ),
            sorted(f"✅ **{name}**: Added" for name, _ in fixtures),
            display="3 x Added"
        )

        check(
            "Dashboard refreshed: selector",
            list(at.sidebar.selectbox[0].options),
            ["All Shipments", SHIPMENT_A, SHIPMENT_B],
            display="All Shipments, SHJ-2026-001, SHJ-2026-002"
        )

        check(
            "Existing SHJ-2026-001 PDFs untouched",
            {name: pdf_state(raw_dir)[name] for name in a_files},
            a_files,
            display="content and mtime unchanged"
        )

        vectors = vector_state(chroma_dir)

        check(
            "Existing SHJ-2026-001 vectors untouched",
            {vector_id: vectors[vector_id] for vector_id in a_vectors},
            a_vectors,
            display=f"{len(a_vectors)} unchanged"
        )

        new_vectors = {
            vector_id: value
            for vector_id, value in vectors.items()
            if vector_id not in a_vectors
        }

        fixture_hashes = {
            name: calculate_sha256(FIXTURE_DIR / name)
            for name, _ in fixtures
        }

        check(
            "New vectors: source, content hash, shipment, ID",
            sorted(
                (
                    source,
                    content_hash == fixture_hashes.get(source),
                    shipment,
                    vector_id.startswith(f"{content_hash}:"),
                )
                for vector_id, (source, content_hash, shipment, _) in new_vectors.items()
            ),
            sorted(
                (name, True, SHIPMENT_B, True)
                for name, _ in fixtures
            ),
            display=f"{len(new_vectors)} vectors for the 3 fixture files"
        )

        check(
            "Raw folder",
            sorted(pdf_state(raw_dir)),
            sorted(list(a_files) + [name for name, _ in fixtures]),
            display="6 PDFs"
        )

        check(
            "Journal",
            sorted(
                (record["event"], record["filename"])
                for record in read_journal(raw_dir)["records"]
            ),
            sorted(
                (event, name)
                for name, _ in fixtures
                for event in ("begin", "commit")
            ),
            display="begin + commit for each upload"
        )

        # ----------------------------------------------------
        # Same files again, plus a SHJ-2026-001 file
        # ----------------------------------------------------

        section("Upload the same files again")

        state_before = (pdf_state(raw_dir), vector_state(chroma_dir))

        repeat = fixtures + [
            ("BOL_SHJ-2026-001.pdf", (raw_dir / "BOL_SHJ-2026-001.pdf").read_bytes())
        ]

        at = upload(open_app(), repeat)
        text = sidebar_text(at)
        result = at.session_state["ingestion_result"]

        check(
            "All duplicates",
            (result["duplicates_skipped"], result["files_added"]),
            (4, 0),
            display="4 duplicates, 0 added"
        )

        check(
            "Full-rebuild document recognised as a duplicate",
            [
                outcome["matched_files"][0]["index_status"]
                for outcome in result["files"]
                if outcome["filename"] == "BOL_SHJ-2026-001.pdf"
            ],
            ["indexed_unverified"]
        )

        check("No success message", text["success"], [], display="none")

        check(
            "Nothing written",
            (pdf_state(raw_dir), vector_state(chroma_dir)),
            state_before,
            display="PDFs and vectors unchanged"
        )

        # ----------------------------------------------------
        # Changed and broken files
        # ----------------------------------------------------

        section("Upload a changed file and a broken file")

        workdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX + "input_"))

        try:

            changed = make_bol(
                workdir / "BOL_SHJ-2026-001.pdf",
                SHIPMENT_A,
                50
            ).read_bytes()

        finally:

            shutil.rmtree(workdir, ignore_errors=True)

        at = upload(
            open_app(),
            [
                ("BOL_SHJ-2026-001.pdf", changed),
                ("broken.pdf", b"%PDF-1.4 broken"),
            ]
        )

        text = sidebar_text(at)
        result = at.session_state["ingestion_result"]

        check(
            "Statuses",
            [outcome["status"] for outcome in result["files"]],
            ["changed", "failed"]
        )

        check("No success message", text["success"], [], display="none")

        check(
            "Failure and attention shown",
            (
                any("1 file(s) failed" in line for line in text["error"]),
                any("need attention" in line for line in text["warning"]),
            ),
            (True, True),
            display="error and warning shown"
        )

        check(
            "Original SHJ-2026-001 BOL kept",
            pdf_state(raw_dir)["BOL_SHJ-2026-001.pdf"],
            a_files["BOL_SHJ-2026-001.pdf"],
            display="unchanged"
        )

        check(
            "Nothing written",
            (pdf_state(raw_dir), vector_state(chroma_dir)),
            state_before,
            display="PDFs and vectors unchanged"
        )

        # ----------------------------------------------------
        # Shipment intelligence after incremental ingestion
        # ----------------------------------------------------

        section("Shipment intelligence stays isolated")

        expected = {
            SHIPMENT_A: (48, 45, "SHORTAGE", "DELAYED", 1550.0, "EXCEPTION"),
            SHIPMENT_B: (60, 60, "MATCH", "EARLY", 2875.5, "CLEAR"),
        }

        for shipment_id, values in expected.items():

            intelligence = analyze_shipment(shipment_id)

            check(
                f"{shipment_id}",
                (
                    intelligence["quantity"]["bol_pieces"],
                    intelligence["quantity"]["delivered_pieces"],
                    intelligence["quantity"]["status"],
                    intelligence["delivery"]["status"],
                    intelligence["invoice_total"],
                    intelligence["shipment_status"],
                ),
                values
            )

        at = open_app()

        at.sidebar.selectbox[0].select(SHIPMENT_B).run()

        click(at, "Run Shipment Intelligence")

        metrics = {metric.label: metric.value for metric in at.metric}

        check(
            "Dashboard for SHJ-2026-002",
            (
                metrics.get("Invoice Total"),
                metrics.get("Quantity Status"),
                metrics.get("Documents"),
            ),
            ("$2,875.50", "MATCH", "3/3")
        )

        # ----------------------------------------------------
        # Diagnostics tab
        # ----------------------------------------------------

        section("Diagnostics tab is read-only")

        before = full_state(raw_dir, chroma_dir)

        at = click(open_app(), "Run Consistency Check")

        check("App errors", [item.value for item in at.exception], [], display="none")

        metrics = {metric.label: metric.value for metric in at.metric}

        check(
            "Verified and unverified documents",
            (metrics.get("Verified Documents"), metrics.get("Unverified Documents")),
            ("3", "3"),
            display="3 verified (uploads), 3 unverified (full rebuild)"
        )

        check(
            "Healthy, with notes for unverified documents",
            any("No problems found" in str(item.value) for item in at.success),
            True
        )

        check(
            "Files, journal and vectors unchanged",
            full_state(raw_dir, chroma_dir) == before,
            True,
            display=f"{len(before[0])} files, {len(before[1])} vectors"
        )

        report = check_destination(raw_dir, chroma_dir)

        check(
            "Report: only notes for the full-rebuild documents",
            sorted(
                (issue["type"], issue["file"])
                for issue in report["issues"]
            ),
            sorted(
                ("unverified_vectors", name)
                for name in a_files
            ),
            display="3 unverified_vectors notes"
        )


# The production-data check runs for every session in
# tests/conftest.py.

if __name__ == "__main__":

    sys.exit(pytest.main([__file__, "-q"]))
