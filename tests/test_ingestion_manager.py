"""
LogiDoc-RAG Ingestion Manager Tests

Exercises src/ingestion_manager.py and its journal against temporary
folders and synthetic PDFs. The real document loader, classifier,
chunker and a real persistent ChromaDB (in a temporary folder) are
used throughout. Hard crashes are simulated by terminating a separate
child process (os._exit) that works only on temporary folders.

Embeddings: the behaviour tests pass a deterministic stand-in for
the Ollama embedder so they run without Ollama and can simulate
failures. test_real_embedding runs the real Ollama embedder on the
SHJ-2026-002 fixture and is skipped (with the reason) when Ollama or
the embedding model is unavailable.

data/raw and chroma_db are never used; tests/conftest.py checks they
are unchanged after the session.

Run from the project root:
    python -m pytest tests/test_ingestion_manager.py
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
TESTS_DIR = PROJECT_ROOT / "tests"

for path in (PROJECT_ROOT, SRC_DIR, TESTS_DIR):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import chromadb
import pymupdf
import pytest
from chromadb.api.models.Collection import Collection
from streamlit.runtime.scriptrunner_utils.exceptions import (
    RerunException,
    StopException,
)
from streamlit.runtime.scriptrunner_utils.script_requests import RerunData

import config
import ingestion_manager
from ingestion_journal import JOURNAL_FILENAME, read_journal
from ingestion_manager import (
    HASH_METADATA_KEY,
    INDEX_MISSING,
    INDEX_UNVERIFIED,
    INDEX_VERIFIED,
    STATUS_ADDED,
    STATUS_CHANGED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_INCONSISTENT,
    calculate_sha256,
    ingest_files,
    ingest_uploads,
)
from ollama_check import requires_embeddings
from vector_store import create_embedding


# ============================================================
# TEST SETTINGS
# ============================================================

TEMP_PREFIX = "logidoc_ingesttest_"
STAGING_PREFIX = "logidoc_staging_"
UPLOAD_PREFIX = "logidoc_upload_"

FIXTURE_DIR = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "shipment_SHJ-2026-002"
)

# Exit code the crash child uses, to prove it died at the crash point
CRASH_EXIT_CODE = 3


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


def fake_embedding(text):
    """
    Deterministic 8-number stand-in for the Ollama embedding.

    "EMBED-FAIL" in the text simulates the embedding service
    failing; "DIM-FAIL" returns a vector of the wrong size,
    which ChromaDB itself rejects when indexing.
    """

    if "EMBED-FAIL" in text:
        raise RuntimeError("Simulated embedding service failure")

    if "DIM-FAIL" in text:
        return [0.5] * 4

    digest = hashlib.sha256(text.encode("utf-8")).digest()

    return [byte / 255 for byte in digest[:8]]


def make_pdf(path, title, lines):
    """
    Write a synthetic one-page logistics PDF.
    """

    document = pymupdf.open()
    page = document.new_page()

    page.insert_text((50, 50), title, fontsize=20)

    for index, line in enumerate(lines):
        page.insert_text(
            (50, 90 + index * 20),
            line,
            fontsize=11
        )

    document.save(str(path))
    document.close()

    return Path(path)


def make_bol(path, shipment_id, pieces, note=""):

    return make_pdf(
        path,
        "BILL OF LADING",
        [
            f"Shipment Number: {shipment_id}",
            "",
            "Carrier:",
            "Test Freight Lines",
            "",
            "Number of Pieces:",
            str(pieces),
            "",
            note,
        ]
    )


def make_pod(path, shipment_id, pieces, note=""):

    return make_pdf(
        path,
        "PROOF OF DELIVERY",
        [
            f"Shipment Number: {shipment_id}",
            "",
            "Number of Pieces Delivered:",
            str(pieces),
            "",
            note,
        ]
    )


def make_invoice(path, shipment_id, total):

    return make_pdf(
        path,
        "FREIGHT INVOICE",
        [
            f"Invoice Number: INV-{shipment_id}",
            "",
            f"Shipment Number: {shipment_id}",
            "",
            "Total Invoice Amount:",
            total,
        ]
    )


def add_legacy_vectors(ws, source, shipment_id, text, index=0):
    """
    Write one vector the way vector_store.build_vector_database()
    does: positional ID "<shipment>_<index>" and no content hash.
    """

    collection(ws).add(
        ids=[f"{shipment_id}_{index}"],
        embeddings=[fake_embedding(text)],
        documents=[text],
        metadatas=[{
            "source": source,
            "document_type": "",
            "shipment_id": shipment_id,
            "page": 1,
            "chunk": 0,
        }],
    )


@contextmanager
def workspace():
    """
    A temporary destination (raw + chroma_db) and input folder.
    """

    root = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))

    ws = SimpleNamespace(
        raw=root / "raw",
        chroma=root / "chroma_db",
        inputs=root / "inputs",
    )

    ws.inputs.mkdir()

    try:

        yield ws

    finally:

        try:

            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()

        except Exception:
            pass

        shutil.rmtree(root, ignore_errors=True)


def ingest(ws, files, embed_text=fake_embedding):
    """
    Run the real ingestion and check its counts every time.
    """

    files = list(files)

    result = ingest_files(
        files,
        ws.raw,
        ws.chroma,
        embed_text=embed_text
    )

    assert_counts_match(result, len(files))

    return result


def assert_counts_match(result, expected_files):
    """
    The summary counts must agree with the per-file outcomes.
    """

    outcomes = result["files"]

    def tally(status):

        return sum(
            1
            for outcome in outcomes
            if outcome["status"] == status
        )

    assert result["files_discovered"] == expected_files == len(outcomes)
    assert result["files_added"] == tally(STATUS_ADDED)
    assert result["files_processed"] == tally(STATUS_ADDED)
    assert result["duplicates_skipped"] == tally(STATUS_DUPLICATE)
    assert result["changed_detected"] == tally(STATUS_CHANGED)
    assert result["inconsistent_detected"] == tally(STATUS_INCONSISTENT)
    assert result["files_failed"] == tally(STATUS_FAILED)

    assert (
        result["files_added"]
        + result["duplicates_skipped"]
        + result["changed_detected"]
        + result["inconsistent_detected"]
        + result["files_failed"]
    ) == result["files_discovered"]

    assert len(result["errors"]) == result["files_failed"]

    assert len(result["rollback_errors"]) == sum(
        1
        for outcome in outcomes
        if outcome["rollback_error"]
    )

    assert result["success"] == (result["files_failed"] == 0)

    # Only added files may report stored paths or vectors
    for outcome in outcomes:

        if outcome["status"] != STATUS_ADDED:
            assert outcome["stored_path"] is None
            assert outcome["vector_ids"] == []


def outcome_for(result, filename):

    matches = [
        outcome
        for outcome in result["files"]
        if outcome["filename"] == filename
    ]

    assert len(matches) == 1, f"FAIL: no single outcome for {filename}"

    return matches[0]


def collection(ws):

    return chromadb.PersistentClient(
        path=str(ws.chroma)
    ).get_or_create_collection(
        name=config.COLLECTION_NAME
    )


def read_vectors(ws):
    """
    Every vector in the workspace index: id -> (document, metadata).
    """

    data = collection(ws).get(
        include=["documents", "metadatas"]
    )

    return {
        vector_id: (document, metadata)
        for vector_id, document, metadata in zip(
            data["ids"],
            data["documents"],
            data["metadatas"],
        )
    }


def sources_in(vectors):

    return {
        metadata.get("source")
        for _, metadata in vectors.values()
    }


def raw_files(ws):
    """
    Every PDF and leftover file in the raw folder:
    name -> (sha256, mtime). The journal is checked separately.
    """

    if not ws.raw.exists():
        return {}

    return {
        path.name: (calculate_sha256(path), path.stat().st_mtime)
        for path in sorted(ws.raw.iterdir())
        if path.is_file() and path.name != JOURNAL_FILENAME
    }


def journal_records(ws):
    """
    Journal records, asserting the journal itself is readable.
    """

    journal = read_journal(ws.raw)

    assert journal["problems"] == [], (
        f"FAIL: unreadable journal lines: {journal['problems']}"
    )

    return journal["records"]


def events_for(ws, filename):

    return [
        record["event"]
        for record in journal_records(ws)
        if record["filename"] == filename
    ]


def snapshot(folders):
    """
    File names, sizes and modification times under folders.
    """

    state = {}

    for folder in folders:

        for path in sorted(Path(folder).rglob("*")):

            if path.is_file():

                state[str(path)] = (
                    path.stat().st_size,
                    path.stat().st_mtime,
                )

    return state


def temp_folders(prefix):

    return set(
        Path(tempfile.gettempdir()).glob(f"{prefix}*")
    )


@contextmanager
def patched(target, name, replacement):
    """
    Temporarily replace an attribute (module function or class
    method) and always restore it.
    """

    original = getattr(target, name)

    setattr(target, name, replacement)

    try:
        yield original
    finally:
        setattr(target, name, original)


CRASH_SCRIPT = textwrap.dedent(
    """
    import os
    import sys

    sys.path[:0] = [{tests!r}, {src!r}, {root!r}]

    import ingestion_journal
    import ingestion_manager
    from test_ingestion_manager import fake_embedding

    raw_dir, chroma_dir, pdf, point = sys.argv[1:5]

    def crash(*args, **kwargs):
        os._exit({code})

    if point == "after_upsert":
        # Vectors written and verified, PDF not stored yet
        ingestion_manager.store_file = crash

    elif point == "before_rename":
        # .part copy written and verified, not renamed yet
        ingestion_manager.os.replace = crash

    elif point == "before_commit":
        # Vectors and PDF stored, commit record not written yet
        real_append = ingestion_journal.append_record

        def append(raw, event, *args, **kwargs):
            if event == "commit":
                crash()
            return real_append(raw, event, *args, **kwargs)

        ingestion_journal.append_record = append

    ingestion_manager.ingest_files(
        [pdf],
        raw_dir,
        chroma_dir,
        embed_text=fake_embedding
    )
    """
).format(
    tests=str(TESTS_DIR),
    src=str(SRC_DIR),
    root=str(PROJECT_ROOT),
    code=CRASH_EXIT_CODE,
)


def crash_during_ingestion(ws, pdf, point):
    """
    Ingest pdf into the workspace in a child process that is
    terminated with os._exit at the given point, like a hard crash
    or power loss. Only temporary folders are involved; the child's
    own temp folder (where its staging folder is left behind, as
    with a real crash) is inside the workspace.
    """

    child_temp = ws.raw.parent / "child_temp"
    child_temp.mkdir(exist_ok=True)

    environment = dict(os.environ)

    for variable in ("TMPDIR", "TEMP", "TMP"):
        environment[variable] = str(child_temp)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            CRASH_SCRIPT,
            str(ws.raw),
            str(ws.chroma),
            str(pdf),
            point,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
    )

    assert completed.returncode == CRASH_EXIT_CODE, (
        f"FAIL: crash child for {point} exited with "
        f"{completed.returncode}: {completed.stderr[-500:]}"
    )

    # ChromaDB caches open indexes per process; one opened here
    # before the child wrote to it would be stale (reads can fail
    # with "Error finding id"). Read it fresh, as a restarted
    # process would after a real crash.
    from chromadb.api.client import SharedSystemClient

    SharedSystemClient.clear_system_cache()


class FakeUpload:
    """
    Minimal stand-in for Streamlit's UploadedFile.
    """

    def __init__(self, name, data):

        self.name = name
        self._data = data

    def getbuffer(self):

        return memoryview(self._data)


# ============================================================
# TESTS
# ============================================================

def test_new_pdf_accepted():

    section("1. A new PDF is accepted")

    with workspace() as ws:

        bol = make_bol(
            ws.inputs / "BOL_SHJ-2026-101.pdf",
            "SHJ-2026-101",
            12
        )

        result = ingest(ws, [bol])
        outcome = outcome_for(result, bol.name)
        vectors = read_vectors(ws)

        check("Status", outcome["status"], STATUS_ADDED)
        check("Files added", result["files_added"], 1)

        check(
            "Stored file content matches input",
            calculate_sha256(ws.raw / bol.name),
            calculate_sha256(bol),
            display="same SHA-256"
        )

        check("Input file left in place", bol.exists(), True)

        check(
            "Vectors indexed",
            len(vectors),
            outcome["chunks_indexed"]
        )

        check(
            "Vector IDs reported",
            sorted(vectors),
            sorted(outcome["vector_ids"]),
            display=", ".join(sorted(vectors))[:40] + "..."
        )

        check(
            "Vector IDs built from the content hash",
            all(
                vector_id.startswith(outcome["sha256"] + ":")
                for vector_id in vectors
            ),
            True,
            display="<sha256>:<page>:<chunk>"
        )

        metadata = next(iter(vectors.values()))[1]

        check("Indexed source", metadata["source"], bol.name)

        check(
            "Document type (existing classifier)",
            metadata["document_type"],
            "BOL"
        )

        check(
            "Shipment ID (existing identifier extraction)",
            metadata["shipment_id"],
            "SHJ-2026-101"
        )

        check(
            "Content hash recorded on vectors",
            metadata[HASH_METADATA_KEY],
            outcome["sha256"],
            display=outcome["sha256"][:12] + "..."
        )

        records = journal_records(ws)

        check(
            "Journal events",
            [record["event"] for record in records],
            ["begin", "commit"]
        )

        begin, commit = records

        check(
            "Journal records share the operation ID",
            (begin["operation_id"], commit["operation_id"]),
            (outcome["operation_id"], outcome["operation_id"]),
            display=outcome["operation_id"][:12] + "..."
        )

        check(
            "Journal begin lists the vector IDs",
            sorted(begin["vector_ids"]),
            sorted(outcome["vector_ids"]),
            display=len(begin["vector_ids"])
        )

        check(
            "Journal records hash and filename",
            (begin["sha256"], commit["filename"]),
            (outcome["sha256"], bol.name),
            display="yes"
        )


def test_duplicate_with_different_name():

    section("2. An identical file with a different filename is a duplicate")

    with workspace() as ws:

        original = make_pod(
            ws.inputs / "POD_SHJ-2026-111.pdf",
            "SHJ-2026-111",
            30
        )

        ingest(ws, [original])

        vectors_before = read_vectors(ws)

        renamed_dir = ws.inputs / "renamed"
        renamed_dir.mkdir()

        renamed = renamed_dir / "scan_0001.pdf"

        shutil.copy2(original, renamed)

        result = ingest(ws, [renamed])
        outcome = outcome_for(result, renamed.name)

        check("Status", outcome["status"], STATUS_DUPLICATE)
        check("Duplicate of", outcome["duplicate_of"], original.name)

        check(
            "Matched file and its index status",
            outcome["matched_files"],
            [{"filename": original.name, "index_status": INDEX_VERIFIED}],
            display=f"{original.name} (indexed)"
        )

        provenance = journal_records(ws)[-1]

        check(
            "Provenance recorded in the journal",
            (
                provenance["event"],
                provenance["filename"],
                provenance["matched_files"],
                provenance["sha256"],
            ),
            ("duplicate", renamed.name, [original.name], outcome["sha256"]),
            display=f"{renamed.name} -> {original.name}"
        )

        check(
            "Raw folder unchanged",
            sorted(raw_files(ws)),
            [original.name]
        )

        check(
            "No new vectors",
            sorted(read_vectors(ws)),
            sorted(vectors_before),
            display=len(vectors_before)
        )

        # Duplicates inside one batch
        first = make_invoice(
            ws.inputs / "INVOICE_SHJ-2026-112.pdf",
            "SHJ-2026-112",
            "$900.00"
        )

        second = renamed_dir / "invoice_copy.pdf"

        shutil.copy2(first, second)

        result = ingest(ws, [first, second])

        check(
            "Same-batch copies",
            (
                outcome_for(result, first.name)["status"],
                outcome_for(result, second.name)["status"],
            ),
            (STATUS_ADDED, STATUS_DUPLICATE),
            display="first added, second duplicate"
        )


def test_existing_unrelated_untouched():

    section("3. An existing unrelated PDF remains untouched")

    with workspace() as ws:

        unrelated = make_pod(
            ws.inputs / "POD_SHJ-2026-201.pdf",
            "SHJ-2026-201",
            40
        )

        ingest(ws, [unrelated])

        file_before = raw_files(ws)[unrelated.name]

        vectors_before = {
            vector_id: value
            for vector_id, value in read_vectors(ws).items()
            if value[1]["source"] == unrelated.name
        }

        new_file = make_invoice(
            ws.inputs / "INVOICE_SHJ-2026-202.pdf",
            "SHJ-2026-202",
            "$1,200.00"
        )

        result = ingest(ws, [new_file])

        check(
            "New file added",
            outcome_for(result, new_file.name)["status"],
            STATUS_ADDED
        )

        check(
            "Unrelated file content and mtime",
            raw_files(ws)[unrelated.name],
            file_before,
            display="unchanged"
        )

        vectors_after = {
            vector_id: value
            for vector_id, value in read_vectors(ws).items()
            if value[1]["source"] == unrelated.name
        }

        check(
            "Unrelated file vectors",
            vectors_after,
            vectors_before,
            display=f"{len(vectors_before)} unchanged"
        )


def test_changed_file_detected():

    section("4. A changed file is detected by content hash")

    with workspace() as ws:

        original = make_bol(
            ws.inputs / "BOL_SHJ-2026-301.pdf",
            "SHJ-2026-301",
            20
        )

        ingest(ws, [original])

        stored_before = raw_files(ws)[original.name]
        vectors_before = read_vectors(ws)

        updated_dir = ws.inputs / "updated"
        updated_dir.mkdir()

        updated = make_bol(
            updated_dir / original.name,
            "SHJ-2026-301",
            25
        )

        result = ingest(ws, [updated])
        outcome = outcome_for(result, updated.name)

        check(
            "Same filename, different hash",
            outcome["sha256"] != stored_before[0],
            True
        )

        check("Status", outcome["status"], STATUS_CHANGED)
        check("Not treated as duplicate", result["duplicates_skipped"], 0)

        check(
            "Explicit reindex required",
            "full rebuild" in outcome["action"],
            True,
            display=outcome["action"]
        )

        check(
            "Existing file kept",
            raw_files(ws)[original.name],
            stored_before,
            display="unchanged"
        )

        check(
            "Index unchanged",
            read_vectors(ws),
            vectors_before,
            display=f"{len(vectors_before)} vectors"
        )

        # Vectors from a full rebuild (no content hash) under a name
        # whose PDF is missing: the upload cannot be compared with
        # them. Previously reported as "changed"; that claimed a
        # difference that cannot be established, so it is now
        # "inconsistent" (vectors without their PDF).
        add_legacy_vectors(
            ws,
            "POD_SHJ-2026-302.pdf",
            "SHJ-2026-302",
            "PROOF OF DELIVERY legacy text"
        )

        legacy_vectors = read_vectors(ws)

        legacy_upload = make_pod(
            ws.inputs / "POD_SHJ-2026-302.pdf",
            "SHJ-2026-302",
            10
        )

        result = ingest(ws, [legacy_upload])
        outcome = outcome_for(result, legacy_upload.name)

        check(
            "Full-rebuild vectors without their PDF",
            outcome["status"],
            STATUS_INCONSISTENT,
            display=f"{outcome['status']} ({outcome['message']})"
        )

        check(
            "Reason says the content cannot be compared",
            "cannot be compared" in outcome["message"],
            True
        )

        check(
            "No duplicate vectors added",
            read_vectors(ws),
            legacy_vectors,
            display=f"{len(legacy_vectors)} vectors"
        )


def test_failed_file_reported():

    section("5. A failed file is reported without deleting other files")

    with workspace() as ws:

        existing = make_pod(
            ws.inputs / "POD_SHJ-2026-401.pdf",
            "SHJ-2026-401",
            15
        )

        ingest(ws, [existing])

        files_before = raw_files(ws)
        vectors_before = read_vectors(ws)

        corrupt = ws.inputs / "corrupt.pdf"
        corrupt.write_bytes(b"this is not a pdf")

        not_pdf = ws.inputs / "notes.txt"
        not_pdf.write_text("not a pdf")

        missing = ws.inputs / "missing.pdf"

        blank = ws.inputs / "blank.pdf"
        document = pymupdf.open()
        document.new_page()
        document.save(str(blank))
        document.close()

        valid = make_bol(
            ws.inputs / "BOL_SHJ-2026-402.pdf",
            "SHJ-2026-402",
            8
        )

        result = ingest(
            ws,
            [corrupt, not_pdf, missing, blank, valid]
        )

        expected = {
            corrupt.name: (STATUS_FAILED, "load"),
            not_pdf.name: (STATUS_FAILED, "validate"),
            missing.name: (STATUS_FAILED, "validate"),
            blank.name: (STATUS_FAILED, "load"),
            valid.name: (STATUS_ADDED, "done"),
        }

        for filename, (status, stage) in expected.items():

            outcome = outcome_for(result, filename)

            check(
                f"{filename}",
                (outcome["status"], outcome["stage"]),
                (status, stage),
                display=(
                    f"{status} at {stage}"
                    + (
                        f" ({outcome['error']})"
                        if outcome["error"]
                        else ""
                    )
                )
            )

        check("Files failed", result["files_failed"], 4)
        check("Errors reported", len(result["errors"]), 4)

        check(
            "Existing file kept",
            raw_files(ws)[existing.name],
            files_before[existing.name],
            display="unchanged"
        )

        check(
            "Raw folder holds only good files",
            sorted(raw_files(ws)),
            sorted([existing.name, valid.name])
        )

        vectors_after = read_vectors(ws)

        check(
            "Existing vectors kept",
            {
                vector_id: value
                for vector_id, value in vectors_after.items()
                if vector_id in vectors_before
            },
            vectors_before,
            display=f"{len(vectors_before)} unchanged"
        )

        check(
            "Indexed sources",
            sources_in(vectors_after),
            {existing.name, valid.name},
            display=", ".join(sorted(sources_in(vectors_after)))
        )

        check(
            "No journal records for files that failed before writing",
            [
                record["filename"]
                for record in journal_records(ws)
                if record["filename"] not in (existing.name, valid.name)
            ],
            [],
            display="none"
        )


def test_run_twice():

    section("6. Running ingestion twice does not duplicate processing")

    with workspace() as ws:

        files = [
            make_bol(ws.inputs / "BOL_SHJ-2026-501.pdf", "SHJ-2026-501", 5),
            make_pod(ws.inputs / "POD_SHJ-2026-501.pdf", "SHJ-2026-501", 5),
        ]

        first = ingest(ws, files)

        files_after_first = raw_files(ws)
        vectors_after_first = read_vectors(ws)
        records_after_first = len(journal_records(ws))

        second = ingest(ws, files)

        check("First run added", first["files_added"], 2)
        check("Second run processed", second["files_processed"], 0)
        check("Second run duplicates", second["duplicates_skipped"], 2)

        check(
            "Raw folder after second run",
            raw_files(ws),
            files_after_first,
            display=f"{len(files_after_first)} files unchanged"
        )

        check(
            "Index after second run",
            read_vectors(ws),
            vectors_after_first,
            display=f"{len(vectors_after_first)} vectors, no duplicates"
        )

        check(
            "Second run journal: provenance only, no new operations",
            [
                record["event"]
                for record in journal_records(ws)[records_after_first:]
            ],
            ["duplicate", "duplicate"]
        )


def test_partial_failure():

    section("7. A partial failure does not mark every file successful")

    with workspace() as ws:

        good_1 = make_bol(ws.inputs / "BOL_SHJ-2026-601.pdf", "SHJ-2026-601", 9)

        embed_fail = make_pod(
            ws.inputs / "POD_SHJ-2026-601.pdf",
            "SHJ-2026-601",
            9,
            note="EMBED-FAIL"
        )

        index_fail = make_invoice(
            ws.inputs / "INVOICE_SHJ-2026-601.pdf",
            "SHJ-2026-601",
            "DIM-FAIL"
        )

        good_2 = make_bol(ws.inputs / "BOL_SHJ-2026-602.pdf", "SHJ-2026-602", 3)

        result = ingest(ws, [good_1, embed_fail, index_fail, good_2])

        check(
            "Per-file outcomes",
            [
                (outcome["filename"], outcome["status"], outcome["stage"])
                for outcome in result["files"]
            ],
            [
                (good_1.name, STATUS_ADDED, "done"),
                (embed_fail.name, STATUS_FAILED, "embed"),
                (index_fail.name, STATUS_FAILED, "index"),
                (good_2.name, STATUS_ADDED, "done"),
            ],
            display="added, failed at embed, failed at index, added"
        )

        check("Overall success", result["success"], False)
        check("Files processed", result["files_processed"], 2)
        check("Files failed", result["files_failed"], 2)

        check(
            "Failed files not stored",
            sorted(raw_files(ws)),
            sorted([good_1.name, good_2.name])
        )

        check(
            "Failed files not indexed",
            sources_in(read_vectors(ws)),
            {good_1.name, good_2.name},
            display="only the two good files"
        )

        check(
            "Journal per file",
            [
                events_for(ws, name)
                for name in (
                    good_1.name,
                    embed_fail.name,
                    index_fail.name,
                    good_2.name,
                )
            ],
            [
                ["begin", "commit"],
                [],
                ["begin", "rolled_back"],
                ["begin", "commit"],
            ],
            display=(
                "commit / nothing (failed before writing) / "
                "rolled_back / commit"
            )
        )

        # Indexing succeeds but saving the PDF fails (e.g. disk
        # full): the vectors just written must be rolled back
        files_before = raw_files(ws)
        vectors_before = read_vectors(ws)

        store_fail = make_pod(
            ws.inputs / "POD_SHJ-2026-602.pdf",
            "SHJ-2026-602",
            3
        )

        def failing_store_file(staged_path, destination, content_hash):
            raise OSError("Simulated disk full")

        with patched(ingestion_manager, "store_file", failing_store_file):
            result = ingest(ws, [store_fail])

        outcome = outcome_for(result, store_fail.name)

        check(
            "Store failure",
            (outcome["status"], outcome["stage"]),
            (STATUS_FAILED, "store"),
            display=f"failed at store ({outcome['error']})"
        )

        check(
            "Vectors written before the store failure rolled back",
            read_vectors(ws),
            vectors_before,
            display=f"{len(vectors_before)} vectors, as before"
        )

        check(
            "Raw folder after store failure",
            raw_files(ws),
            files_before,
            display="unchanged"
        )

        check(
            "Journal after store failure",
            events_for(ws, store_fail.name),
            ["begin", "rolled_back"]
        )


def test_counts_match():

    section("8. Result counts match the per-file outcomes")

    with workspace() as ws:

        existing = make_bol(ws.inputs / "BOL_SHJ-2026-701.pdf", "SHJ-2026-701", 7)

        ingest(ws, [existing])

        changed_dir = ws.inputs / "changed"
        changed_dir.mkdir()

        duplicate = ws.inputs / "copy_of_bol.pdf"
        shutil.copy2(existing, duplicate)

        changed = make_bol(changed_dir / existing.name, "SHJ-2026-701", 70)
        new_file = make_pod(ws.inputs / "POD_SHJ-2026-701.pdf", "SHJ-2026-701", 7)
        failed = ws.inputs / "broken.pdf"
        failed.write_bytes(b"%PDF-1.4 truncated")

        result = ingest(ws, [new_file, duplicate, changed, failed])

        check("Files discovered", result["files_discovered"], 4)
        check("Files added", result["files_added"], 1)
        check("Duplicates skipped", result["duplicates_skipped"], 1)
        check("Changed detected", result["changed_detected"], 1)
        check("Inconsistent detected", result["inconsistent_detected"], 0)
        check("Files processed", result["files_processed"], 1)
        check("Files failed", result["files_failed"], 1)

        check(
            "Error messages",
            [error.split(":")[0] for error in result["errors"]],
            [failed.name]
        )

        print("PASS: Counts agree with per-file outcomes in every test")


def test_raw_pdf_without_vectors():

    section("9. A PDF in the raw folder without its vectors is inconsistent")

    with workspace() as ws:

        # A PDF left in the raw folder without being indexed,
        # e.g. after a full rebuild failed part-way
        orphan_pdf = make_bol(
            ws.inputs / "BOL_SHJ-2026-801.pdf",
            "SHJ-2026-801",
            11
        )

        ws.raw.mkdir()
        shutil.copy2(orphan_pdf, ws.raw / orphan_pdf.name)

        # An unrelated, properly ingested file
        indexed = make_pod(
            ws.inputs / "POD_SHJ-2026-802.pdf",
            "SHJ-2026-802",
            6
        )

        ingest(ws, [indexed])

        upload_dir = ws.inputs / "upload"
        upload_dir.mkdir()

        upload = upload_dir / "bol_rescan.pdf"
        shutil.copy2(orphan_pdf, upload)

        files_before = raw_files(ws)
        vectors_before = read_vectors(ws)

        result = ingest(ws, [upload])
        outcome = outcome_for(result, upload.name)

        check("Status", outcome["status"], STATUS_INCONSISTENT)

        check(
            "Reason",
            "has no vectors in the index" in outcome["message"],
            True,
            display=outcome["message"]
        )

        check("Inconsistent detected", result["inconsistent_detected"], 1)
        check("Not counted as duplicate", result["duplicates_skipped"], 0)

        check(
            "Raw folder not repaired",
            raw_files(ws),
            files_before,
            display="unchanged"
        )

        check(
            "Index not repaired",
            read_vectors(ws),
            vectors_before,
            display=f"{len(vectors_before)} vectors, unchanged"
        )

        # The raw PDF was replaced by hand after indexing, so
        # its vectors come from different content
        hand_edited = make_pod(
            upload_dir / "pod_edited.pdf",
            "SHJ-2026-802",
            7
        )

        shutil.copy2(hand_edited, ws.raw / indexed.name)

        files_before = raw_files(ws)
        vectors_before = read_vectors(ws)

        result = ingest(ws, [hand_edited])
        outcome = outcome_for(result, hand_edited.name)

        check(
            "Raw PDF replaced by hand",
            outcome["status"],
            STATUS_INCONSISTENT,
            display=f"{outcome['status']} ({outcome['message']})"
        )

        check(
            "Raw folder and index unchanged",
            (raw_files(ws), read_vectors(ws)),
            (files_before, vectors_before),
            display="yes"
        )

        # Control: vectors from a full rebuild record no hash, so
        # a raw PDF with vectors under its name stays a duplicate
        legacy_pdf = make_invoice(
            ws.inputs / "INVOICE_SHJ-2026-803.pdf",
            "SHJ-2026-803",
            "$300.00"
        )

        shutil.copy2(legacy_pdf, ws.raw / legacy_pdf.name)

        add_legacy_vectors(
            ws,
            legacy_pdf.name,
            "SHJ-2026-803",
            "FREIGHT INVOICE legacy text"
        )

        result = ingest(ws, [legacy_pdf])
        outcome = outcome_for(result, legacy_pdf.name)

        check(
            "Raw PDF with full-rebuild vectors",
            (outcome["status"], outcome["matched_files"][0]["index_status"]),
            (STATUS_DUPLICATE, INDEX_UNVERIFIED),
            display="duplicate (indexed by a full rebuild, unverified)"
        )


def test_vectors_without_pdf():

    section("10. Vectors without their PDF are inconsistent")

    with workspace() as ws:

        pdf = make_invoice(
            ws.inputs / "INVOICE_SHJ-2026-901.pdf",
            "SHJ-2026-901",
            "$450.00"
        )

        # A hard crash after the vectors were written but before the
        # PDF was stored. (Ctrl+C is now rolled back, so a real
        # process termination is used to leave the orphan vectors.)
        crash_during_ingestion(ws, pdf, "after_upsert")

        check(
            "Setup: vectors left without the PDF",
            (sources_in(read_vectors(ws)), raw_files(ws)),
            ({pdf.name}, {}),
            display="1 source indexed, raw folder empty"
        )

        check(
            "Setup: journal shows the unfinished operation",
            events_for(ws, pdf.name),
            ["begin"]
        )

        files_before = raw_files(ws)
        vectors_before = read_vectors(ws)

        renamed = ws.inputs / "invoice_copy.pdf"
        shutil.copy2(pdf, renamed)

        result = ingest(ws, [pdf, renamed])

        for filename in (pdf.name, renamed.name):

            outcome = outcome_for(result, filename)

            check(
                f"{filename} status",
                outcome["status"],
                STATUS_INCONSISTENT
            )

            check(
                f"{filename} reason",
                "missing from the raw folder" in outcome["message"],
                True,
                display=outcome["message"]
            )

        check("Inconsistent detected", result["inconsistent_detected"], 2)
        check("Not counted as duplicate", result["duplicates_skipped"], 0)
        check("Not processed", result["files_processed"], 0)

        check(
            "Unfinished operation reported as a warning",
            any("unfinished operation" in w for w in result["warnings"]),
            True
        )

        check(
            "Raw folder not repaired",
            raw_files(ws),
            files_before,
            display="still empty"
        )

        check(
            "Index not repaired",
            read_vectors(ws),
            vectors_before,
            display=f"{len(vectors_before)} vectors, unchanged"
        )


def test_interruptions_roll_back():

    section("11. Interruptions are rolled back and re-raised")

    interruptions = [
        ("KeyboardInterrupt", KeyboardInterrupt()),
        ("SystemExit", SystemExit(1)),
        ("Streamlit StopException", StopException()),
        ("Streamlit RerunException", RerunException(RerunData())),
    ]

    with workspace() as ws:

        # A committed file first, which must survive everything
        kept = make_bol(ws.inputs / "BOL_SHJ-2026-1101.pdf", "SHJ-2026-1101", 4)

        ingest(ws, [kept])

        for index, (label, interruption) in enumerate(interruptions):

            files_before = raw_files(ws)
            vectors_before = read_vectors(ws)

            pdf = make_pod(
                ws.inputs / f"POD_SHJ-2026-11{index + 10}.pdf",
                f"SHJ-2026-11{index + 10}",
                4
            )

            def interrupted_store_file(*args, error=interruption):
                raise error

            raised = None

            with patched(
                ingestion_manager,
                "store_file",
                interrupted_store_file
            ):

                try:
                    ingest_files([pdf], ws.raw, ws.chroma, embed_text=fake_embedding)
                except BaseException as error:
                    raised = error

            check(
                f"{label}: re-raised",
                type(raised),
                type(interruption)
            )

            check(
                f"{label}: vectors rolled back",
                read_vectors(ws),
                vectors_before,
                display=f"{len(vectors_before)} vectors, as before"
            )

            check(
                f"{label}: raw folder unchanged",
                raw_files(ws),
                files_before,
                display="yes"
            )

            records = [
                record
                for record in journal_records(ws)
                if record["filename"] == pdf.name
            ]

            check(
                f"{label}: journal",
                (
                    [record["event"] for record in records],
                    records[-1]["reason"],
                ),
                (
                    ["begin", "rolled_back"],
                    f"Interrupted ({type(interruption).__name__})",
                ),
                display=f"begin, rolled_back ({records[-1]['reason']})"
            )

        # Interrupted before any write: nothing to roll back
        early = make_invoice(ws.inputs / "INVOICE_SHJ-2026-1120.pdf", "SHJ-2026-1120", "$1")

        def interrupted_embedding(text):
            raise KeyboardInterrupt

        files_before = raw_files(ws)
        vectors_before = read_vectors(ws)

        try:
            ingest_files([early], ws.raw, ws.chroma, embed_text=interrupted_embedding)
            raised = None
        except KeyboardInterrupt as error:
            raised = error

        check(
            "Interrupted while embedding: re-raised, nothing written",
            (
                type(raised),
                read_vectors(ws) == vectors_before,
                raw_files(ws) == files_before,
                events_for(ws, early.name),
            ),
            (KeyboardInterrupt, True, True, []),
            display="yes, no journal records"
        )

        check(
            "Committed file survived every interruption",
            (
                kept.name in raw_files(ws),
                kept.name in sources_in(read_vectors(ws)),
                events_for(ws, kept.name),
            ),
            (True, True, ["begin", "commit"]),
            display="stored, indexed, committed"
        )


def test_rollback_failure_reported():

    section("12. A failed rollback is reported separately")

    with workspace() as ws:

        pdf = make_pod(ws.inputs / "POD_SHJ-2026-1201.pdf", "SHJ-2026-1201", 2)

        def failing_store_file(*args):
            raise OSError("Simulated disk full")

        def failing_delete(self, *args, **kwargs):
            raise RuntimeError("Simulated index outage")

        with patched(ingestion_manager, "store_file", failing_store_file):
            with patched(Collection, "delete", failing_delete):
                result = ingest(ws, [pdf])

        outcome = outcome_for(result, pdf.name)

        check(
            "Original error",
            outcome["error"],
            "OSError: Simulated disk full"
        )

        check(
            "Rollback error reported separately",
            "Simulated index outage" in (outcome["rollback_error"] or ""),
            True,
            display=outcome["rollback_error"]
        )

        check(
            "Result lists them separately",
            (result["errors"], len(result["rollback_errors"])),
            ([f"{pdf.name}: OSError: Simulated disk full"], 1),
            display="1 error, 1 rollback error"
        )

        records = [
            record
            for record in journal_records(ws)
            if record["filename"] == pdf.name
        ]

        check(
            "Journal records the failed rollback",
            [record["event"] for record in records],
            ["begin", "rollback_failed"]
        )

        remaining = sorted(read_vectors(ws))

        check(
            "Remaining vectors listed in the journal",
            sorted(records[-1]["vector_ids"]),
            remaining,
            display=f"{len(remaining)} vector(s) left, listed"
        )

        check("PDF not stored", raw_files(ws), {}, display="yes")


def test_verification_failures():

    section("13. Failed verification is never reported as success")

    with workspace() as ws:

        # The index does not return the written IDs
        pdf = make_bol(ws.inputs / "BOL_SHJ-2026-1301.pdf", "SHJ-2026-1301", 6)

        real_get = Collection.get

        def get_missing_ids(self, *args, **kwargs):

            if kwargs.get("include") == []:
                return {"ids": []}

            return real_get(self, *args, **kwargs)

        with patched(Collection, "get", get_missing_ids):
            result = ingest(ws, [pdf])

        outcome = outcome_for(result, pdf.name)

        check(
            "Index verification failure",
            (outcome["status"], outcome["stage"]),
            (STATUS_FAILED, "index"),
            display=f"failed at index ({outcome['error']})"
        )

        check(
            "Nothing left behind",
            (read_vectors(ws), raw_files(ws), events_for(ws, pdf.name)),
            ({}, {}, ["begin", "rolled_back"]),
            display="no vectors, no PDF, rolled back"
        )

        # The stored PDF does not match what was indexed (e.g. a
        # concurrent writer replaced it)
        pod = make_pod(ws.inputs / "POD_SHJ-2026-1302.pdf", "SHJ-2026-1302", 6)

        def corrupting_store_file(staged_path, destination, content_hash):
            destination.write_bytes(b"%PDF-1.4 replaced by someone else")

        with patched(ingestion_manager, "store_file", corrupting_store_file):
            result = ingest(ws, [pod])

        outcome = outcome_for(result, pod.name)

        check(
            "Stored PDF verification failure",
            (outcome["status"], outcome["stage"]),
            (STATUS_FAILED, "store"),
            display=f"failed at store ({outcome['error']})"
        )

        check(
            "Vectors rolled back, file left for inspection",
            (
                sources_in(read_vectors(ws)),
                pod.name in raw_files(ws),
                events_for(ws, pod.name),
            ),
            (set(), True, ["begin", "rolled_back"]),
            display="no vectors, PDF left in place, rolled back"
        )


def test_multiple_filenames_same_content():

    section("14. The same content under several filenames")

    def scenario(indexed_name, dropped_name):

        with workspace() as ws:

            pdf = make_bol(ws.inputs / indexed_name, "SHJ-2026-1401", 14)

            ingest(ws, [pdf])

            shutil.copy2(pdf, ws.raw / dropped_name)

            files_before = raw_files(ws)
            vectors_before = read_vectors(ws)

            upload = ws.inputs / "upload.pdf"
            shutil.copy2(pdf, upload)

            outcome = outcome_for(ingest(ws, [upload]), upload.name)

            unchanged = (
                raw_files(ws) == files_before
                and read_vectors(ws) == vectors_before
            )

            return outcome, unchanged

    first, unchanged_1 = scenario("A.pdf", "B.pdf")
    second, unchanged_2 = scenario("B.pdf", "A.pdf")

    check(
        "A indexed, identical B without vectors",
        (first["status"], first["matched_files"]),
        (
            STATUS_INCONSISTENT,
            [
                {"filename": "A.pdf", "index_status": INDEX_VERIFIED},
                {"filename": "B.pdf", "index_status": INDEX_MISSING},
            ],
        ),
        display=first["message"]
    )

    check(
        "B indexed, identical A without vectors",
        (second["status"], [item["filename"] for item in second["matched_files"]]),
        (STATUS_INCONSISTENT, ["A.pdf", "B.pdf"]),
        display=second["message"]
    )

    check(
        "Deterministic order, independent of which file is indexed",
        [item["filename"] for item in first["matched_files"]],
        [item["filename"] for item in second["matched_files"]],
        display="A.pdf, B.pdf"
    )

    check("Nothing written in either case", (unchanged_1, unchanged_2), (True, True))

    # Both copies indexed by a full rebuild (two vector sets)
    with workspace() as ws:

        ws.raw.mkdir()

        pdf = make_invoice(ws.inputs / "INVOICE_SHJ-2026-1402.pdf", "SHJ-2026-1402", "$14")

        for index, name in enumerate(("copy_1.pdf", "copy_2.pdf")):

            shutil.copy2(pdf, ws.raw / name)

            add_legacy_vectors(
                ws,
                name,
                "SHJ-2026-1402",
                "FREIGHT INVOICE identical text",
                index=index
            )

        vectors_before = read_vectors(ws)

        outcome = outcome_for(ingest(ws, [pdf]), pdf.name)

        check(
            "Two full-rebuild copies",
            (
                outcome["status"],
                [item["index_status"] for item in outcome["matched_files"]],
            ),
            (STATUS_INCONSISTENT, [INDEX_UNVERIFIED, INDEX_UNVERIFIED]),
            display=outcome["message"]
        )

        check(
            "No third vector set",
            read_vectors(ws),
            vectors_before,
            display=f"{len(vectors_before)} vectors"
        )


def test_uploads():

    section("15. Uploads through ingest_uploads")

    uploads_before = temp_folders(UPLOAD_PREFIX)

    with workspace() as ws:

        first = make_bol(ws.inputs / "a.pdf", "SHJ-2026-1501", 1).read_bytes()
        second = make_pod(ws.inputs / "b.pdf", "SHJ-2026-1501", 1).read_bytes()

        result = ingest_uploads(
            [
                FakeUpload("scan.pdf", first),
                FakeUpload("scan.pdf", second),
                FakeUpload("sub/dir/BOL_SHJ-2026-1502.pdf", first),
            ],
            ws.raw,
            ws.chroma,
            embed_text=fake_embedding
        )

        assert_counts_match(result, 3)

        check(
            "Statuses",
            [outcome["status"] for outcome in result["files"]],
            [STATUS_ADDED, STATUS_CHANGED, STATUS_DUPLICATE],
            display=(
                "scan.pdf added, second scan.pdf changed, "
                "renamed copy duplicate"
            )
        )

        check(
            "Directory parts of upload names dropped",
            result["files"][2]["filename"],
            "BOL_SHJ-2026-1502.pdf"
        )

        check(
            "Raw folder",
            sorted(raw_files(ws)),
            ["scan.pdf"]
        )

        check(
            "Stored content is the first upload",
            raw_files(ws)["scan.pdf"][0],
            hashlib.sha256(first).hexdigest(),
            display="yes"
        )

    check(
        "Temporary upload folders removed",
        temp_folders(UPLOAD_PREFIX) - uploads_before,
        set(),
        display="yes"
    )


def test_concurrent_ingestion():

    section("16. Concurrent uploads in one process are serialized")

    # Like two Streamlit sessions uploading the same file at once.
    # A slow embedder keeps both runs in flight together.
    with workspace() as ws:

        pdf = make_bol(ws.inputs / "BOL_SHJ-2026-1601.pdf", "SHJ-2026-1601", 16)

        def slow_embedding(text):
            time.sleep(0.5)
            return fake_embedding(text)

        results = []
        errors = []

        def upload():

            try:
                results.append(
                    ingest_files([pdf], ws.raw, ws.chroma, embed_text=slow_embedding)
                )
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=upload) for _ in range(2)]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join(timeout=120)

        check("No errors", errors, [], display="none")

        check(
            "One added, one duplicate",
            sorted(result["files"][0]["status"] for result in results),
            [STATUS_ADDED, STATUS_DUPLICATE]
        )

        added = [
            result["files"][0]
            for result in results
            if result["files"][0]["status"] == STATUS_ADDED
        ][0]

        check(
            "Exactly one vector set, still in the index",
            sorted(read_vectors(ws)),
            sorted(added["vector_ids"]),
            display=f"{len(added['vector_ids'])} vector(s)"
        )

        check("PDF stored once", sorted(raw_files(ws)), [pdf.name])

        check(
            "Journal",
            events_for(ws, pdf.name),
            ["begin", "commit", "duplicate"]
        )


@pytest.mark.ollama
@requires_embeddings
def test_real_embedding():

    section("Real pipeline with Ollama embeddings")

    fixtures = sorted(FIXTURE_DIR.glob("*.pdf"))

    fixtures_before = {
        path.name: calculate_sha256(path)
        for path in fixtures
    }

    with workspace() as ws:

        result = ingest(ws, fixtures, embed_text=create_embedding)

        check("Fixture files added", result["files_added"], 3)

        vectors = read_vectors(ws)

        check(
            "Shipment IDs",
            {metadata["shipment_id"] for _, metadata in vectors.values()},
            {"SHJ-2026-002"}
        )

        check(
            "Document types",
            sorted({metadata["document_type"] for _, metadata in vectors.values()}),
            ["BOL", "INVOICE", "POD"],
            display="BOL, INVOICE, POD"
        )

    check(
        "Fixture PDFs unchanged",
        {path.name: calculate_sha256(path) for path in fixtures},
        fixtures_before,
        display="yes"
    )


# Staging-folder cleanup and the production-data check run for
# every session in tests/conftest.py.

if __name__ == "__main__":

    sys.exit(pytest.main([__file__, "-q"]))
