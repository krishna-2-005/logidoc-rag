"""
LogiDoc-RAG Journal and Consistency Report Tests

Exercises src/ingestion_journal.py and src/consistency_report.py
against temporary folders and a real persistent ChromaDB in a
temporary folder. Hard crashes are simulated by terminating a child
process (os._exit) that works only on temporary folders.

Full-rebuild scenarios run twice: once with vectors written the way
vector_store.build_vector_database() writes them (no Ollama needed),
and once with the real build_vector_database() pointed at a temporary
folder (skipped, with the reason, without Ollama).

data/raw and chroma_db are never used; tests/conftest.py checks they
are unchanged after the session.

Run from the project root:
    python -m pytest tests/test_consistency_report.py
"""

import io
import json
import shutil
import sys
from contextlib import contextmanager, redirect_stdout
from pathlib import Path


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pytest

import document_loader
import vector_store
from consistency_report import check_destination
from ingestion_journal import (
    EVENT_BEGIN,
    JOURNAL_FILENAME,
    append_record,
    read_journal,
)
from ollama_check import requires_embeddings
from test_ingestion_manager import (
    Collection,
    add_legacy_vectors,
    check,
    collection,
    crash_during_ingestion,
    ingest,
    ingestion_manager,
    make_bol,
    make_invoice,
    make_pod,
    patched,
    section,
    workspace,
)


# ============================================================
# HELPERS
# ============================================================

def issues_of(report, issue_type):

    return [
        issue
        for issue in report["issues"]
        if issue["type"] == issue_type
    ]


def only_issue(report, issue_type):

    found = issues_of(report, issue_type)

    assert len(found) == 1, (
        f"FAIL: expected one {issue_type} issue, got {len(found)}: "
        f"{[issue['type'] for issue in report['issues']]}"
    )

    return found[0]


def destination_state(ws):
    """
    Everything the report must leave untouched: every file in the
    raw folder (bytes) and every stored vector.
    """

    files = {
        path.name: path.read_bytes()
        for path in sorted(ws.raw.iterdir())
        if path.is_file()
    } if ws.raw.exists() else {}

    data = collection(ws).get(
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


def report(ws):

    return check_destination(ws.raw, ws.chroma)


@contextmanager
def full_rebuild_into(ws):
    """
    Point the real full rebuild at the workspace.
    """

    with patched(vector_store, "CHROMA_DIR", ws.chroma):
        with patched(document_loader, "RAW_DATA_DIR", ws.raw):
            yield


def legacy_index(ws, real):
    """
    Index every PDF in the raw folder the way a full rebuild does.
    """

    if real:

        with full_rebuild_into(ws), redirect_stdout(io.StringIO()):
            vector_store.build_vector_database()

        return

    for index, pdf in enumerate(sorted(ws.raw.glob("*.pdf"))):

        text = document_loader.load_document(pdf)["pages"][0]["text"]

        add_legacy_vectors(ws, pdf.name, "LEGACY", text, index=index)


# ============================================================
# TESTS
# ============================================================

def test_healthy_destination():

    section("1. A healthy destination")

    with workspace() as ws:

        ingest(ws, [
            make_bol(ws.inputs / "BOL_SHJ-2026-2101.pdf", "SHJ-2026-2101", 1),
            make_pod(ws.inputs / "POD_SHJ-2026-2101.pdf", "SHJ-2026-2101", 1),
        ])

        result = report(ws)

        check("Healthy", result["summary"]["healthy"], True)
        check("Issues", result["issues"], [], display="none")
        check("Verified documents", result["summary"]["verified_documents"], 2)
        check("Read-only flag", result["read_only"], True)


def test_journal_reading():

    section("2. The journal is read defensively")

    with workspace() as ws:

        check(
            "Missing journal",
            read_journal(ws.raw),
            {
                "path": str(ws.raw / JOURNAL_FILENAME),
                "exists": False,
                "records": [],
                "problems": [],
            },
            display="no records, no problems"
        )

        ingest(ws, [make_bol(ws.inputs / "BOL_SHJ-2026-2201.pdf", "SHJ-2026-2201", 2)])

        journal_file = ws.raw / JOURNAL_FILENAME

        with open(journal_file, "a", encoding="utf-8") as journal:
            journal.write("not json at all\n")
            journal.write('{"schema": 1, "event": "explode", "operation_id": "x"}\n')
            journal.write('{"schema": 1, "event": "commit", "operat')

        journal = read_journal(ws.raw)

        check(
            "Valid records kept",
            [record["event"] for record in journal["records"]],
            ["begin", "commit"]
        )

        check(
            "Malformed, unknown and truncated lines reported",
            [(problem["line"], problem["reason"].split(" ")[0]) for problem in journal["problems"]],
            [(3, "Not"), (4, "Unknown"), (5, "Not")],
            display="lines 3, 4 and 5"
        )

        # A new record after the truncated line stays readable
        ingest(ws, [make_pod(ws.inputs / "POD_SHJ-2026-2201.pdf", "SHJ-2026-2201", 2)])

        journal = read_journal(ws.raw)

        check(
            "Records appended after a truncated line",
            [record["event"] for record in journal["records"]],
            ["begin", "commit", "begin", "commit"]
        )

        check(
            "Truncated line still reported, not merged",
            len(journal["problems"]),
            3
        )

        result = report(ws)

        check(
            "Report lists unreadable journal lines",
            len(issues_of(result, "journal_unreadable_line")),
            3
        )

        # Non-UTF-8 bytes must not crash the reader either
        with open(journal_file, "ab") as journal:
            journal.write(b"\n\xff\xfe\x00 broken bytes\n")

        check(
            "Undecodable bytes reported",
            len(read_journal(ws.raw)["problems"]),
            4
        )


def test_interrupted_operations():

    section("3. Crashes between writing vectors and storing the PDF")

    # Vectors written, PDF never stored
    with workspace() as ws:

        pdf = make_invoice(ws.inputs / "INVOICE_SHJ-2026-2301.pdf", "SHJ-2026-2301", "$1")

        crash_during_ingestion(ws, pdf, "after_upsert")

        result = report(ws)
        issue = only_issue(result, "interrupted_operation")
        begin = read_journal(ws.raw)["records"][0]

        check(
            "Crash after upsert",
            (issue["details"]["state"], issue["severity"], issue["certainty"]),
            ("orphan_vectors", "error", "confirmed"),
            display=issue["explanation"]
        )

        check(
            "Affected vector IDs and hash",
            (issue["vector_ids"], issue["sha256"], issue["operation_id"]),
            (sorted(begin["vector_ids"]), begin["sha256"], begin["operation_id"]),
            display=f"{len(issue['vector_ids'])} vector(s), hash and operation ID"
        )

        check(
            "Not reported a second time as an unexplained orphan",
            issues_of(result, "orphan_vectors"),
            [],
            display="yes"
        )

    # Complete .part copy written, rename never happened
    with workspace() as ws:

        pdf = make_bol(ws.inputs / "BOL_SHJ-2026-2302.pdf", "SHJ-2026-2302", 3)

        crash_during_ingestion(ws, pdf, "before_rename")

        result = report(ws)
        interrupted = only_issue(result, "interrupted_operation")
        partial = only_issue(result, "leftover_partial_file")

        check(
            "Crash before rename",
            (interrupted["details"]["state"], "complete copy" in interrupted["explanation"]),
            ("orphan_vectors", True),
            display=interrupted["explanation"]
        )

        check(
            "Leftover .part file linked to the operation",
            (partial["certainty"], partial["operation_id"], partial["details"]["original"]),
            ("confirmed", interrupted["operation_id"], pdf.name),
            display=partial["explanation"]
        )

    # PDF and vectors stored, commit record never written
    with workspace() as ws:

        pdf = make_pod(ws.inputs / "POD_SHJ-2026-2303.pdf", "SHJ-2026-2303", 4)

        crash_during_ingestion(ws, pdf, "before_commit")

        result = report(ws)
        issue = only_issue(result, "interrupted_operation")

        check(
            "Crash before commit record",
            (issue["details"]["state"], issue["severity"], issue["certainty"]),
            ("completed_without_commit", "warning", "uncertain"),
            display=issue["explanation"]
        )

        check(
            "Its PDF still counts as verified",
            result["summary"]["verified_documents"],
            1
        )

    # Only the begin record survived
    with workspace() as ws:

        ws.raw.mkdir()

        append_record(
            ws.raw,
            EVENT_BEGIN,
            "op-without-anything",
            filename="lost.pdf",
            sha256="0" * 64,
            vector_ids=["0" * 64 + ":1:0"]
        )

        collection(ws)

        issue = only_issue(report(ws), "interrupted_operation")

        check(
            "Crash before anything was kept",
            (issue["details"]["state"], issue["severity"]),
            ("nothing_remaining", "info"),
            display=issue["explanation"]
        )


def test_index_and_folder_mismatches():

    section("4. Mismatches between the raw folder and the index")

    # Orphan vectors with no journal explanation
    with workspace() as ws:

        pdf = make_bol(ws.inputs / "BOL_SHJ-2026-2401.pdf", "SHJ-2026-2401", 5)

        ingest(ws, [pdf])

        (ws.raw / pdf.name).unlink()

        issue = only_issue(report(ws), "orphan_vectors")

        check(
            "Vectors whose PDF was removed",
            (issue["severity"], issue["details"]["cause"], issue["file"]),
            ("error", "unknown", pdf.name),
            display=issue["explanation"]
        )

    # PDF without vectors
    with workspace() as ws:

        ws.raw.mkdir()

        pdf = make_pod(ws.raw / "POD_SHJ-2026-2402.pdf", "SHJ-2026-2402", 6)

        collection(ws)

        issue = only_issue(report(ws), "pdf_not_indexed")

        check(
            "PDF without vectors",
            (issue["file"], issue["severity"]),
            (pdf.name, "error"),
            display=issue["explanation"]
        )

    # PDF replaced by hand after indexing
    with workspace() as ws:

        pdf = make_invoice(ws.inputs / "INVOICE_SHJ-2026-2403.pdf", "SHJ-2026-2403", "$7")

        ingest(ws, [pdf])

        indexed_ids = sorted(collection(ws).get()["ids"])

        make_invoice(ws.raw / pdf.name, "SHJ-2026-2403", "$8")

        issue = only_issue(report(ws), "content_mismatch")

        check(
            "Content differs from the indexed version",
            (issue["file"], issue["vector_ids"]),
            (pdf.name, indexed_ids),
            display=issue["explanation"]
        )

    # The same content under two names, one of them indexed
    with workspace() as ws:

        pdf = make_bol(ws.inputs / "A.pdf", "SHJ-2026-2404", 9)

        ingest(ws, [pdf])

        shutil.copy2(pdf, ws.raw / "B.pdf")

        result = report(ws)
        duplicate = only_issue(result, "duplicate_content")
        unindexed = only_issue(result, "pdf_not_indexed")

        check(
            "Identical content under two filenames",
            [
                (item["filename"], item["index_status"])
                for item in duplicate["details"]["files"]
            ],
            [("A.pdf", "indexed"), ("B.pdf", "no_vectors")],
            display=duplicate["explanation"]
        )

        check(
            "The unindexed copy points to the indexed one",
            unindexed["details"]["indexed_as"],
            ["A.pdf"],
            display=unindexed["explanation"]
        )


def test_full_rebuild_vectors_simulated():

    check_full_rebuild_vectors(real=False)


@pytest.mark.ollama
@requires_embeddings
def test_full_rebuild_vectors_real():

    check_full_rebuild_vectors(real=True)


def check_full_rebuild_vectors(real):

    section(
        "5. Vectors from a full rebuild "
        + ("(real build_vector_database)" if real else
           "(written the way build_vector_database writes them)")
    )

    # Two identical PDFs indexed by a full rebuild
    with workspace() as ws:

        ws.raw.mkdir()

        pdf = make_bol(ws.raw / "copy_1.pdf", "SHJ-2026-2501", 10)
        shutil.copy2(pdf, ws.raw / "copy_2.pdf")

        legacy_index(ws, real)

        result = report(ws)
        vector_sets = only_issue(result, "duplicate_vector_sets")

        check(
            "Duplicate vector sets",
            (vector_sets["certainty"], vector_sets["details"]["sources"]),
            ("confirmed", ["copy_1.pdf", "copy_2.pdf"]),
            display=vector_sets["explanation"]
        )

        check(
            "Affected vector IDs",
            vector_sets["vector_ids"],
            sorted(collection(ws).get()["ids"]),
            display=", ".join(vector_sets["vector_ids"])
        )

        check(
            "Both marked unverified, not verified",
            (
                len(issues_of(result, "unverified_vectors")),
                result["summary"]["verified_documents"],
                result["summary"]["unverified_documents"],
            ),
            (2, 0, 2),
            display="2 unverified, 0 verified"
        )

        check(
            "Unverified is uncertain, not an error",
            {
                (issue["severity"], issue["certainty"])
                for issue in issues_of(result, "unverified_vectors")
            },
            {("info", "uncertain")}
        )

    # Full-rebuild vectors whose PDF is gone
    with workspace() as ws:

        ws.raw.mkdir()

        pdf = make_pod(ws.raw / "POD_SHJ-2026-2502.pdf", "SHJ-2026-2502", 11)

        legacy_index(ws, real)

        pdf.unlink()

        issue = only_issue(report(ws), "unverified_vectors_without_pdf")

        check(
            "Full-rebuild vectors without their PDF",
            (issue["severity"], issue["certainty"], issue["file"]),
            ("warning", "uncertain", pdf.name),
            display=issue["explanation"]
        )


def test_failed_rollback_and_missing_index():

    section("6. Failed rollbacks and a missing index")

    with workspace() as ws:

        pdf = make_pod(ws.inputs / "POD_SHJ-2026-2601.pdf", "SHJ-2026-2601", 12)

        def failing_store_file(*args):
            raise OSError("Simulated disk full")

        def failing_delete(self, *args, **kwargs):
            raise RuntimeError("Simulated index outage")

        with patched(ingestion_manager, "store_file", failing_store_file):
            with patched(Collection, "delete", failing_delete):
                ingest(ws, [pdf])

        issue = only_issue(report(ws), "rollback_failed")

        check(
            "Vectors left by a failed rollback",
            (issue["severity"], issue["vector_ids"]),
            ("error", sorted(collection(ws).get()["ids"])),
            display=issue["explanation"]
        )

    with workspace() as ws:

        ws.raw.mkdir()

        make_bol(ws.raw / "BOL_SHJ-2026-2602.pdf", "SHJ-2026-2602", 1)

        result = report(ws)

        check(
            "No index: reported, not created",
            (
                [issue["type"] for issue in result["issues"]],
                ws.chroma.exists(),
            ),
            (["index_unavailable"], False),
            display="index_unavailable, folder not created"
        )


def test_report_is_read_only():

    section("7. The report changes nothing")

    with workspace() as ws:

        # A destination with many problems at once
        ingest(ws, [make_bol(ws.inputs / "BOL_SHJ-2026-2701.pdf", "SHJ-2026-2701", 1)])

        crash_during_ingestion(
            ws,
            make_pod(ws.inputs / "POD_SHJ-2026-2701.pdf", "SHJ-2026-2701", 1),
            "before_rename"
        )

        shutil.copy2(ws.raw / "BOL_SHJ-2026-2701.pdf", ws.raw / "copy.pdf")

        with open(ws.raw / JOURNAL_FILENAME, "a", encoding="utf-8") as journal:
            journal.write('{"truncated": ')

        before = destination_state(ws)

        first = report(ws)
        second = report(ws)

        check(
            "Raw folder, journal and vectors unchanged",
            destination_state(ws) == before,
            True,
            display=f"{len(before[0])} files, {len(before[1])} vectors"
        )

        check(
            "Same report twice (deterministic)",
            first["issues"],
            second["issues"],
            display=f"{len(first['issues'])} identical issues"
        )

        check(
            "Issues found",
            sorted({issue["type"] for issue in first["issues"]}),
            sorted({
                "duplicate_content",
                "interrupted_operation",
                "journal_unreadable_line",
                "leftover_partial_file",
                "pdf_not_indexed",
            }),
            display=", ".join(sorted({issue["type"] for issue in first["issues"]}))
        )

        check(
            "Every issue is complete",
            all(
                issue["severity"] in ("error", "warning", "info")
                and issue["certainty"] in ("confirmed", "uncertain")
                and issue["explanation"]
                and isinstance(issue["vector_ids"], list)
                for issue in first["issues"]
            ),
            True
        )


# Staging-folder cleanup (crash children now use a temp folder inside
# their workspace) and the production-data check run for every
# session in tests/conftest.py.

if __name__ == "__main__":

    sys.exit(pytest.main([__file__, "-q"]))
