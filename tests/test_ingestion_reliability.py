"""
Tests for the reliable document ingestion layer: src/document_validation.py,
src/ingestion_register.py, src/ingestion_service.py and
src/document_lifecycle.py.

Real PDFs (synthetic, written with pymupdf), a real persistent ChromaDB
in a temporary folder and the real document loader and chunker are used.
The embedding service is a deterministic stand-in, so the tests run
without Ollama and can simulate transient failures. data/raw and
chroma_db are never used; tests/conftest.py checks they are unchanged.

Run from the project root:
    python -m pytest tests/test_ingestion_reliability.py
"""

import hashlib
import sqlite3
import sys
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_analytics as analytics  # noqa: E402
import document_lifecycle as lifecycle  # noqa: E402
import document_validation as validation  # noqa: E402
import ingestion_manager  # noqa: E402
import ingestion_register as register_module  # noqa: E402
import ingestion_service as service  # noqa: E402
import reconciliation  # noqa: E402
from authz import ADMIN, REVIEWER, PermissionDenied, Principal  # noqa: E402
from consistency_report import check_destination  # noqa: E402
from ingestion_journal import read_journal  # noqa: E402
from ingestion_register import IngestionRegister  # noqa: E402
from ingestion_service import Upload  # noqa: E402
from test_audit_analytics import clean  # noqa: E402
from test_ingestion_manager import fake_embedding, make_bol, make_invoice, make_pdf, make_pod  # noqa: E402


ADMINISTRATOR = Principal("alice", ADMIN, True, 1)
REVIEWER_USER = Principal("bob", REVIEWER, True, 2)

A = "SHJ-2026-001"
B = "SHJ-2026-002"


# ============================================================
# FIXTURES AND HELPERS
# ============================================================

class Workspace:

    def __init__(self, root):

        self.root = Path(root)
        self.raw = self.root / "raw"
        self.chroma = self.root / "chroma"
        self.quarantine = self.root / "quarantine"
        self.archive = self.root / "archive"
        self.raw.mkdir()
        self.register = IngestionRegister(self.root / "ingestion.sqlite3")

    def collection(self):

        return ingestion_manager.get_destination_collection(self.chroma)

    def vector_count(self):

        return self.collection().count()

    def ids_of(self, source):

        data = self.collection().get(include=["metadatas"])

        return sorted(
            vector_id for vector_id, metadata in zip(data["ids"], data["metadatas"])
            if metadata.get("source") == source
        )

    def stored(self):

        return sorted(path.name for path in self.raw.glob("*.pdf"))

    def process(self, uploads, principal=ADMINISTRATOR, **kwargs):

        kwargs.setdefault("embed_text", fake_embedding)
        kwargs.setdefault("sleep", lambda seconds: None)

        return service.process_files(
            principal, uploads, self.raw, self.chroma, self.register, self.quarantine, **kwargs
        )


@pytest.fixture
def ws(tmp_path):

    return Workspace(tmp_path)


def upload_of(tmp_path, name, builder, *arguments, **keywords):
    """
    An Upload of a synthetic PDF.
    """

    source = Path(tmp_path) / "sources"
    source.mkdir(exist_ok=True)
    path = builder(source / name, *arguments, **keywords)

    return Upload(name, Path(path).read_bytes())


def bol(tmp_path, name="BOL_A.pdf", shipment=A, pieces=48, note=""):

    return upload_of(tmp_path, name, make_bol, shipment, pieces, note)


def pdf_bytes(pages):
    """
    A PDF with one page per entry: text, or None for a page with no
    text at all.
    """

    document = pymupdf.open()

    for text in pages:

        page = document.new_page()

        if text:
            page.insert_text((50, 80), text, fontsize=12)

    data = document.tobytes()
    document.close()

    return data


def image_only_pdf():

    document = pymupdf.open()
    page = document.new_page()
    picture = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60))
    picture.clear_with(180)
    page.insert_image(pymupdf.Rect(50, 50, 200, 200), pixmap=picture)
    data = document.tobytes()
    document.close()

    return data


def encrypted_pdf():

    document = pymupdf.open()
    document.new_page().insert_text((50, 80), "Shipment Number: SHJ-2026-001")
    data = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    document.close()

    return data


def flaky(failures, error=None):
    """
    An embedding function that fails `failures` times, then works.
    """

    state = {"calls": 0}

    def embed(text):

        if state["calls"] < failures:
            state["calls"] += 1
            raise error or ConnectionError("Connection refused")

        return fake_embedding(text)

    embed.state = state

    return embed


def sha(data):

    return hashlib.sha256(data).hexdigest()


# ============================================================
# VALIDATION
# ============================================================

def test_a_text_pdf_is_accepted():

    result = validation.validate_pdf("BOL.pdf", pdf_bytes(["Shipment Number: SHJ-2026-001"]))

    assert result["ok"] and result["status"] is None
    assert (result["pages"], result["text_pages"], result["warnings"]) == (1, 1, [])


@pytest.mark.parametrize(
    "name, data, status, code",
    [
        ("report.docx", b"PK\x03\x04", "unsupported", "unsupported_type"),
        ("a.pdf", b"", "failed", "empty_file"),
        ("a.pdf", b"This is plain text, not a PDF at all.", "unsupported", "not_a_pdf"),
        ("../evil.pdf", b"%PDF-1.4", "failed", "bad_filename"),
        ("sub/evil.pdf", b"%PDF-1.4", "failed", "bad_filename"),
        ("NUL.pdf", b"%PDF-1.4", "failed", "bad_filename"),
        ("x" * 200 + ".pdf", b"%PDF-1.4", "failed", "bad_filename"),
        ("tab\tname.pdf", b"%PDF-1.4", "failed", "bad_filename"),
    ],
)
def test_rejected_files_get_a_status_a_code_a_reason_and_an_action(name, data, status, code):

    result = validation.validate_pdf(name, data)

    assert not result["ok"]
    assert (result["status"], result["code"]) == (status, code)
    assert result["message"] and result["action"]


def test_an_oversized_file_is_rejected_with_the_limit_in_the_reason():

    result = validation.validate_pdf("big.pdf", pdf_bytes(["x"]), max_bytes=100)

    assert result["code"] == "too_large" and "limit" in result["message"]


def test_a_damaged_pdf_is_rejected_not_crashed_on():

    result = validation.validate_pdf("broken.pdf", b"%PDF-1.4\n" + bytes(range(256)) * 20)

    assert not result["ok"] and result["status"] == "failed"
    assert result["code"] in ("corrupted", "no_pages")


def test_a_password_protected_pdf_is_rejected():

    result = validation.validate_pdf("locked.pdf", encrypted_pdf())

    assert (result["status"], result["code"]) == ("failed", "password_protected")
    assert "password" in result["action"].lower()


def test_an_image_only_pdf_is_reported_as_needing_ocr_not_indexed():

    result = validation.validate_pdf("scan.pdf", image_only_pdf())

    assert (result["status"], result["code"]) == ("unsupported", "needs_ocr")
    assert "OCR" in result["message"] and "OCR" in result["action"]
    assert "no OCR" in result["action"].replace("has no OCR", "no OCR")


def test_a_pdf_with_some_blank_pages_is_accepted_with_a_warning():

    result = validation.validate_pdf("mixed.pdf", pdf_bytes(["Shipment Number: SHJ-2026-001", None, None]))

    assert result["ok"] and result["warnings"] == ["partial_text"]
    assert (result["pages"], result["text_pages"], result["empty_pages"]) == (3, 1, [2, 3])


def test_too_many_pages_are_rejected():

    result = validation.validate_pdf("long.pdf", pdf_bytes(["a", "b", "c"]), max_pages=2)

    assert result["code"] == "too_many_pages"


# ============================================================
# REGISTER
# ============================================================

def test_the_register_enforces_the_status_lifecycle(ws):

    register = ws.register
    file_id = register.queue("a.pdf", "0" * 64, 10, actor="alice", authenticated=True)

    assert register.get(file_id)["status"] == "queued"

    with pytest.raises(register_module.RegisterError, match="cannot become"):
        register.update(file_id, "partial")

    register.update(file_id, "processing")
    register.update(file_id, "indexed", chunks_indexed=3)

    # A finished file does not move on its own
    with pytest.raises(register_module.RegisterError, match="cannot become"):
        register.update(file_id, "failed")

    assert [event["status"] for event in register.events(file_id)] == ["queued", "processing", "indexed"]
    assert register.get(file_id)["attempts"] == 1


def test_the_register_never_stores_file_contents(ws):

    columns = {
        row[1]
        for table in ("ingestion_files", "ingestion_events", "ingestion_actions")
        for row in sqlite3.connect(ws.register.path).execute(f"PRAGMA table_info({table})")
    }

    assert not columns & {"text", "content", "document", "data", "pages_text"}


def test_register_events_are_append_only(ws):

    file_id = ws.register.queue("a.pdf")

    with sqlite3.connect(ws.register.path) as connection:

        for statement in ("UPDATE ingestion_events SET message = 'x'", "DELETE FROM ingestion_events"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)

    assert ws.register.events(file_id)


def test_an_unknown_status_or_field_is_refused(ws):

    file_id = ws.register.queue("a.pdf")

    with pytest.raises(register_module.RegisterError):
        ws.register.update(file_id, "teleported")

    with pytest.raises(register_module.RegisterError, match="Unknown fields"):
        ws.register.update(file_id, "processing", sha256="x", quarantine_path="/etc/passwd")


def test_register_search_never_runs_input_as_sql(ws):

    ws.register.queue("a.pdf")

    assert ws.register.files(search="'; DROP TABLE ingestion_files; --") == []
    assert ws.register.files(search="%") == []
    assert len(ws.register.files()) == 1


def test_a_crash_leaves_no_file_looking_busy(ws):

    stuck = ws.register.queue("a.pdf")
    other = ws.register.queue("b.pdf")
    ws.register.update(other, "processing")

    assert ws.register.recover_interrupted() == 2

    for file_id in (stuck, other):
        record = ws.register.get(file_id)
        assert (record["status"], record["code"]) == ("failed", "interrupted")
        assert record["action"]


def test_an_error_needs_a_real_note_to_be_resolved(ws):

    file_id = ws.register.queue("a.pdf")
    ws.register.update(file_id, "failed", code="corrupted", message="damaged", action="re-export")

    with pytest.raises(register_module.RegisterError, match="note of at least"):
        ws.register.resolve(file_id, "alice", "ok")

    ws.register.resolve(file_id, "alice", "Corrected copy uploaded under another name.")

    assert ws.register.errors() == []

    with pytest.raises(register_module.RegisterError, match="already resolved"):
        ws.register.resolve(file_id, "alice", "Corrected copy uploaded under another name.")


# ============================================================
# INGESTION
# ============================================================

def test_a_good_pdf_is_indexed_stored_and_recorded(ws, tmp_path):

    result = ws.process([bol(tmp_path)])

    record = result["files"][0]

    assert (result["indexed"], result["failed"], result["corpus_changed"]) == (1, 0, True)
    assert record["status"] == "indexed"
    assert (record["document_type"], record["shipment_id"], record["shipment_key"]) == ("BOL", A, "SHJ2026001")
    assert record["chunks_indexed"] >= 1 and record["sha256"]
    assert ws.stored() == ["BOL_A.pdf"]
    assert ws.vector_count() == record["chunks_indexed"]
    assert [item["event"] for item in read_journal(ws.raw)["records"]] == ["begin", "commit"]


def test_duplicate_content_under_another_name_is_not_indexed_twice(ws, tmp_path):

    first = bol(tmp_path, "BOL_A.pdf")
    ws.process([first])
    before = ws.vector_count()

    result = ws.process([Upload("BOL_A_copy.pdf", first.data)])

    record = result["files"][0]

    assert (record["status"], record["code"], record["duplicate_of"]) == ("indexed", "duplicate", "BOL_A.pdf")
    assert (result["indexed"], result["duplicates"], result["corpus_changed"]) == (0, 1, False)
    assert ws.stored() == ["BOL_A.pdf"] and ws.vector_count() == before


def test_the_same_name_with_other_content_is_refused_not_overwritten(ws, tmp_path):

    ws.process([bol(tmp_path, pieces=48)])
    original = (ws.raw / "BOL_A.pdf").read_bytes()
    vectors = ws.vector_count()

    record = ws.process([bol(tmp_path, pieces=50)])["files"][0]

    assert (record["status"], record["code"]) == ("failed", "name_conflict")
    assert "Replace" in record["action"]
    assert (ws.raw / "BOL_A.pdf").read_bytes() == original and ws.vector_count() == vectors


@pytest.mark.parametrize(
    "name, make, status, code",
    [
        ("scan.pdf", image_only_pdf, "unsupported", "needs_ocr"),
        ("locked.pdf", encrypted_pdf, "failed", "password_protected"),
        ("empty.pdf", lambda: b"", "failed", "empty_file"),
        ("notes.txt", lambda: b"hello", "unsupported", "unsupported_type"),
    ],
    ids=["image-only", "encrypted", "empty", "wrong-type"],
)
def test_unusable_files_are_recorded_with_a_reason_and_never_indexed(ws, name, make, status, code):

    result = ws.process([Upload(name, make())])

    record = result["files"][0]

    assert (record["status"], record["code"]) == (status, code)
    assert record["message"] and record["action"]
    assert ws.stored() == [] and ws.vector_count() == 0 and not result["corpus_changed"]
    assert [error["code"] for error in ws.register.errors()] == [code]


def test_a_bad_file_does_not_stop_the_good_ones(ws, tmp_path):

    result = ws.process([Upload("scan.pdf", image_only_pdf()), bol(tmp_path), Upload("a.pdf", b"")])

    assert [item["status"] for item in result["files"]] == ["unsupported", "indexed", "failed"]
    assert ws.stored() == ["BOL_A.pdf"]


def test_a_document_without_a_shipment_id_is_flagged_not_silently_accepted(ws):

    data = pdf_bytes(["BILL OF LADING\nCarrier: Test Freight Lines\nNumber of Pieces: 4"])

    record = ws.process([Upload("no_id.pdf", data)])["files"][0]

    assert record["status"] == "partial" and record["code"] == "no_shipment_id"
    assert record["shipment_id"] is None and "belongs to no shipment" in record["message"]
    assert ws.stored() == ["no_id.pdf"] and ws.vector_count() >= 1


def test_pages_without_text_make_the_file_partial_with_the_pages_counted(ws):

    data = pdf_bytes(["BILL OF LADING\nShipment Number: SHJ-2026-001\nNumber of Pieces: 4", None])

    record = ws.process([Upload("two_pages.pdf", data)])["files"][0]

    assert record["status"] == "partial" and record["code"] == "partial_text"
    assert (record["pages"], record["text_pages"]) == (2, 1)
    assert record["shipment_id"] == A


def test_shipment_evidence_stays_separate(ws, tmp_path):

    ws.process([bol(tmp_path, "BOL_A.pdf", A), bol(tmp_path, "BOL_B.pdf", B)])

    data = ws.collection().get(include=["metadatas"])
    shipments = {(m["source"], m["shipment_id"]) for m in data["metadatas"]}

    assert shipments == {("BOL_A.pdf", A), ("BOL_B.pdf", B)}


def test_paths_in_upload_names_cannot_escape_the_raw_folder(ws, tmp_path):

    result = ws.process([Upload("../escape.pdf", bol(tmp_path).data), Upload("..\\escape2.pdf", bol(tmp_path).data)])

    assert [item["code"] for item in result["files"]] == ["bad_filename", "bad_filename"]
    assert not (ws.root / "escape.pdf").exists() and not (ws.root / "escape2.pdf").exists()
    assert ws.stored() == []


# ============================================================
# RETRIES AND INTERRUPTIONS
# ============================================================

def test_a_transient_failure_is_retried_and_indexes_the_file_exactly_once(ws, tmp_path):

    embed = flaky(2)
    pauses = []

    result = ws.process([bol(tmp_path)], embed_text=embed, sleep=pauses.append)
    record = result["files"][0]

    assert record["status"] == "indexed" and record["attempts"] == 3
    assert pauses == [0.5, 1.0]
    assert [event["code"] for event in ws.register.events(record["id"]) if event["code"]] == ["retrying", "retrying"]

    # One set of vectors, and a journal that shows two attempts rolled back or never begun
    assert ws.vector_count() == record["chunks_indexed"]
    assert ws.stored() == ["BOL_A.pdf"]
    events = [item["event"] for item in read_journal(ws.raw)["records"]]
    assert events.count("commit") == 1 and events.count("begin") == events.count("commit") + events.count("rolled_back")


def test_a_failure_that_is_not_transient_is_not_retried(ws, tmp_path):

    embed = flaky(5, RuntimeError("Simulated embedding service failure"))

    record = ws.process([bol(tmp_path)], embed_text=embed)["files"][0]

    assert (record["status"], record["code"]) == ("failed", "processing_failed")
    assert embed.state["calls"] == 1 and record["attempts"] == 1
    assert ws.stored() == [] and ws.vector_count() == 0 and record["quarantine_name"] is None


def test_retries_stop_after_the_limit_and_keep_a_copy_for_a_manual_retry(ws, tmp_path):

    record = ws.process([bol(tmp_path)], embed_text=flaky(99), max_attempts=3)["files"][0]

    assert (record["status"], record["code"]) == ("failed", "retries_exhausted")
    assert record["attempts"] == 3 and "tried 3 times" in record["message"]
    assert ws.stored() == [] and ws.vector_count() == 0

    kept = ws.quarantine / record["quarantine_name"]
    assert sha(kept.read_bytes()) == record["sha256"]
    assert record["quarantine_name"] == f"{record['sha256']}.pdf"


def test_a_kept_copy_is_retried_once_the_service_is_back_and_then_removed(ws, tmp_path):

    failed = ws.process([bol(tmp_path)], embed_text=flaky(99), max_attempts=2)["files"][0]

    result = service.retry(
        ADMINISTRATOR, ws.register, failed["id"], ws.raw, ws.chroma, ws.quarantine,
        embed_text=fake_embedding, sleep=lambda seconds: None,
    )

    record = result["files"][0]

    assert record["status"] == "indexed" and record["id"] == failed["id"]
    assert ws.stored() == ["BOL_A.pdf"] and ws.vector_count() == record["chunks_indexed"]
    assert not list(ws.quarantine.glob("*.pdf"))
    assert ws.register.errors() == []


def test_a_file_without_a_kept_copy_cannot_be_retried(ws):

    failed = ws.process([Upload("a.pdf", b"")])["files"][0]

    with pytest.raises(register_module.RegisterError, match="upload the corrected file"):
        service.retry(ADMINISTRATOR, ws.register, failed["id"], ws.raw, ws.chroma, ws.quarantine)


def test_a_tampered_kept_copy_is_not_retried(ws, tmp_path):

    failed = ws.process([bol(tmp_path)], embed_text=flaky(99), max_attempts=1)["files"][0]
    (ws.quarantine / failed["quarantine_name"]).write_bytes(b"changed")

    with pytest.raises(register_module.RegisterError, match="no longer matches"):
        service.retry(ADMINISTRATOR, ws.register, failed["id"], ws.raw, ws.chroma, ws.quarantine,
                      embed_text=fake_embedding)


def test_an_interruption_is_rolled_back_recorded_and_raised(ws, tmp_path):

    def interrupted(text):
        raise KeyboardInterrupt()

    uploads = [bol(tmp_path, "BOL_A.pdf"), bol(tmp_path, "BOL_B.pdf", B)]

    with pytest.raises(KeyboardInterrupt):
        ws.process(uploads, embed_text=interrupted)

    assert ws.stored() == [] and ws.vector_count() == 0

    # Both files are in the register, neither looks busy, and both are open errors
    records = ws.register.files()
    assert sorted((r["filename"], r["status"], r["code"]) for r in records) == [
        ("BOL_A.pdf", "failed", "interrupted"), ("BOL_B.pdf", "failed", "interrupted"),
    ]


def test_a_batch_recovers_the_records_an_earlier_crash_left_busy(ws, tmp_path):

    stale = ws.register.queue("old.pdf")
    ws.register.update(stale, "processing")

    ws.process([bol(tmp_path)])

    assert ws.register.get(stale)["code"] == "interrupted"


def test_a_failure_after_the_vectors_were_written_rolls_them_back(ws, tmp_path, monkeypatch):

    def no_store(*arguments, **keywords):
        raise OSError("disk full")

    monkeypatch.setattr(ingestion_manager, "store_file", no_store)

    record = ws.process([bol(tmp_path)])["files"][0]

    assert record["status"] == "failed" and "disk full" in record["message"]
    assert ws.vector_count() == 0 and ws.stored() == []
    assert "rolled_back" in [item["event"] for item in read_journal(ws.raw)["records"]]


# ============================================================
# NO FALSE CLEAN AUDIT
# ============================================================

def test_an_unresolved_failed_file_keeps_every_shipment_from_being_clean(ws):

    ws.process([Upload("scan.pdf", image_only_pdf())])

    problems = service.audit_problems(ws.register)

    assert [(item["filename"], item["status"]) for item in problems] == [("scan.pdf", "needs_ocr")]

    snapshot = analytics.build_snapshot(clean(A), reconciliation.reconcile_shipment, unreadable_files=problems)
    result = snapshot["shipments"][0]["result"]

    assert result["final_status"] == "NEEDS REVIEW"
    assert any(item["category"] == "Documents" for item in result["review_items"])

    # The same shipment without the open error is clean
    clear = analytics.build_snapshot(clean(A), reconciliation.reconcile_shipment)

    assert clear["shipments"][0]["result"]["final_status"] == "NO DISCREPANCIES"


def test_resolving_the_error_lets_the_audit_through_again(ws):

    failed = ws.process([Upload("scan.pdf", image_only_pdf())])["files"][0]

    service.resolve_error(ADMINISTRATOR, ws.register, failed["id"], "Checked: the scan is not part of any shipment.")

    assert service.audit_problems(ws.register) == []


def test_a_later_good_upload_of_the_same_name_supersedes_the_error(ws, tmp_path):

    ws.process([Upload("BOL_A.pdf", b"")])
    assert len(service.audit_problems(ws.register)) == 1

    ws.process([bol(tmp_path, "BOL_A.pdf")])

    assert service.audit_problems(ws.register) == [] and ws.register.errors() == []


def test_unreadable_files_of_the_raw_folder_and_register_are_merged_once_per_name(ws):

    ws.process([Upload("scan.pdf", image_only_pdf())])
    raw_problem = {"filename": "scan.pdf", "status": "unreadable", "error": "x"}
    other = {"filename": "zzz.pdf", "status": "unreadable", "error": "y"}

    merged = service.audit_problems(ws.register, [raw_problem, other])

    assert [(item["filename"], item["status"]) for item in merged] == [("scan.pdf", "unreadable"), ("zzz.pdf", "unreadable")]
    assert service.problems_token(merged) == (("scan.pdf", "unreadable"), ("zzz.pdf", "unreadable"))


# ============================================================
# PERMISSIONS
# ============================================================

def test_only_an_administrator_can_change_the_document_store(ws, tmp_path):

    ws.process([bol(tmp_path)])
    new = pdf_bytes(["Shipment Number: SHJ-2026-001\nNumber of Pieces: 5"])

    for action in (
        lambda: ws.process([bol(tmp_path, "BOL_B.pdf", B)], principal=REVIEWER_USER),
        lambda: ws.process([bol(tmp_path, "BOL_B.pdf", B)], principal=None),
        lambda: lifecycle.replace(REVIEWER_USER, ws.register, "BOL_A.pdf", new, ws.raw, ws.chroma, ws.archive),
        lambda: lifecycle.remove(REVIEWER_USER, ws.register, "BOL_A.pdf", ws.raw, ws.chroma, ws.archive),
        lambda: lifecycle.reprocess(REVIEWER_USER, ws.register, "BOL_A.pdf", ws.raw, ws.chroma),
        lambda: service.resolve_error(REVIEWER_USER, ws.register, 1, "A note that is long enough."),
    ):
        with pytest.raises(PermissionDenied):
            action()

    assert ws.stored() == ["BOL_A.pdf"] and not ws.archive.exists()


# ============================================================
# REPLACE
# ============================================================

def stored_bytes(ws, name="BOL_A.pdf"):

    return (ws.raw / name).read_bytes()


def corrected(tmp_path, pieces=50):

    return bol(tmp_path, "BOL_A.pdf", pieces=pieces).data


def do_replace(ws, data, **kwargs):

    kwargs.setdefault("embed_text", fake_embedding)

    return lifecycle.replace(ADMINISTRATOR, ws.register, "BOL_A.pdf", data, ws.raw, ws.chroma, ws.archive, **kwargs)


def test_replacing_a_document_archives_the_old_one_and_swaps_the_vectors(ws, tmp_path):

    ws.process([bol(tmp_path, pieces=48)])
    old = stored_bytes(ws)
    old_ids = ws.ids_of("BOL_A.pdf")
    new = corrected(tmp_path, 50)

    result = do_replace(ws, new)

    assert result["action"] == "replaced" and result["old_sha256"] == sha(old) and result["new_sha256"] == sha(new)
    assert stored_bytes(ws) == new
    assert Path(result["archived"]).read_bytes() == old
    assert not set(old_ids) & set(ws.ids_of("BOL_A.pdf")) and ws.ids_of("BOL_A.pdf")
    assert all(vector_id.startswith(sha(new)) for vector_id in ws.ids_of("BOL_A.pdf"))
    assert lifecycle.verify_document(ws.collection(), ws.raw, "BOL_A.pdf")["consistent"]
    assert [a["action"] for a in ws.register.actions()] == ["replaced"]
    assert not list(ws.raw.glob(".*.new"))


def test_a_replacement_leaves_the_destination_consistent_for_the_checker(ws, tmp_path):

    ws.process([bol(tmp_path, pieces=48), bol(tmp_path, "BOL_B.pdf", B)])
    do_replace(ws, corrected(tmp_path))

    report = check_destination(ws.raw, ws.chroma)

    assert [issue for issue in report["issues"] if issue["severity"] in ("error", "warning")] == []
    assert [item["event"] for item in read_journal(ws.raw)["records"]][-2:] == ["begin", "commit"]


def test_replacing_with_identical_content_changes_nothing(ws, tmp_path):

    ws.process([bol(tmp_path)])
    before = ws.ids_of("BOL_A.pdf")

    result = do_replace(ws, stored_bytes(ws))

    assert result["action"] == "unchanged" and ws.ids_of("BOL_A.pdf") == before and not ws.archive.exists()


def test_an_unusable_replacement_is_refused_before_anything_changes(ws, tmp_path):

    ws.process([bol(tmp_path)])
    original, ids = stored_bytes(ws), ws.ids_of("BOL_A.pdf")

    for data in (image_only_pdf(), b"", encrypted_pdf(), b"not a pdf"):
        with pytest.raises(lifecycle.LifecycleError, match="cannot be used"):
            do_replace(ws, data)

    assert stored_bytes(ws) == original and ws.ids_of("BOL_A.pdf") == ids and not ws.archive.exists()


def test_a_replacement_that_copies_another_stored_document_is_refused(ws, tmp_path):

    ws.process([bol(tmp_path, "BOL_A.pdf"), bol(tmp_path, "BOL_B.pdf", B)])

    with pytest.raises(lifecycle.LifecycleError, match="same content as BOL_B.pdf"):
        do_replace(ws, stored_bytes(ws, "BOL_B.pdf"))


def test_a_replacement_cannot_target_a_missing_file_or_a_path(ws, tmp_path):

    with pytest.raises(lifecycle.LifecycleError, match="nothing to replace"):
        do_replace(ws, corrected(tmp_path))

    for name in ("../BOL_A.pdf", "sub/BOL_A.pdf", "..\\x.pdf"):
        with pytest.raises(lifecycle.LifecycleError, match="not a valid file name"):
            lifecycle.replace(ADMINISTRATOR, ws.register, name, corrected(tmp_path), ws.raw, ws.chroma, ws.archive)


def test_a_failed_embedding_changes_nothing(ws, tmp_path):

    ws.process([bol(tmp_path)])
    original, ids = stored_bytes(ws), ws.ids_of("BOL_A.pdf")

    with pytest.raises(ConnectionError):
        do_replace(ws, corrected(tmp_path), embed_text=flaky(99))

    assert stored_bytes(ws) == original and ws.ids_of("BOL_A.pdf") == ids
    assert not list(ws.raw.glob(".*.new")) and not ws.archive.exists()


def test_a_failure_during_the_swap_restores_the_vectors_and_the_pdf(ws, tmp_path, monkeypatch):

    ws.process([bol(tmp_path)])
    original, ids = stored_bytes(ws), ws.ids_of("BOL_A.pdf")
    before = lifecycle.corpus_signature(ws.raw, ws.chroma)

    def broken(collection, wanted):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(lifecycle, "verify_vectors_present", broken)

    with pytest.raises(RuntimeError, match="index unavailable"):
        do_replace(ws, corrected(tmp_path))

    monkeypatch.undo()

    assert stored_bytes(ws) == original and ws.ids_of("BOL_A.pdf") == ids
    assert lifecycle.verify_document(ws.collection(), ws.raw, "BOL_A.pdf")["consistent"]
    assert not list(ws.raw.glob(".*.new"))
    assert [item["event"] for item in read_journal(ws.raw)["records"]][-2:] == ["begin", "rolled_back"]
    assert lifecycle.corpus_signature(ws.raw, ws.chroma) == before
    assert ws.register.actions() == []


def test_a_failure_after_the_file_was_swapped_puts_the_old_pdf_back(ws, tmp_path, monkeypatch):

    ws.process([bol(tmp_path)])
    original, ids = stored_bytes(ws), ws.ids_of("BOL_A.pdf")

    real = lifecycle.calculate_sha256
    state = {"calls": 0}

    def fails_on_the_final_check(path):

        state["calls"] += 1

        # The last check of replace() verifies the swapped-in file
        if Path(path).name == "BOL_A.pdf" and state["calls"] >= 4:
            return "0" * 64

        return real(path)

    monkeypatch.setattr(lifecycle, "calculate_sha256", fails_on_the_final_check)

    with pytest.raises(IOError, match="does not match"):
        do_replace(ws, corrected(tmp_path))

    monkeypatch.undo()

    assert stored_bytes(ws) == original and ws.ids_of("BOL_A.pdf") == ids
    assert lifecycle.verify_document(ws.collection(), ws.raw, "BOL_A.pdf")["consistent"]


# ============================================================
# REMOVE
# ============================================================

def do_remove(ws, name="BOL_A.pdf"):

    return lifecycle.remove(ADMINISTRATOR, ws.register, name, ws.raw, ws.chroma, ws.archive)


def test_removing_a_document_archives_it_and_clears_its_vectors_only(ws, tmp_path):

    ws.process([bol(tmp_path, "BOL_A.pdf"), bol(tmp_path, "BOL_B.pdf", B)])
    original, other_ids = stored_bytes(ws), ws.ids_of("BOL_B.pdf")

    result = do_remove(ws)

    assert ws.stored() == ["BOL_B.pdf"] and ws.ids_of("BOL_A.pdf") == [] and ws.ids_of("BOL_B.pdf") == other_ids
    assert Path(result["archived"]).read_bytes() == original
    assert result["vectors_removed"] >= 1
    assert lifecycle.verify_document(ws.collection(), ws.raw, "BOL_A.pdf")["consistent"]
    assert [a["action"] for a in ws.register.actions()] == ["removed"]

    report = check_destination(ws.raw, ws.chroma)
    assert [i for i in report["issues"] if i["severity"] in ("error", "warning")] == []


def test_a_failed_removal_keeps_the_pdf_and_the_vectors(ws, tmp_path, monkeypatch):

    ws.process([bol(tmp_path)])
    original, ids = stored_bytes(ws), ws.ids_of("BOL_A.pdf")

    def broken_unlink(self, *arguments, **keywords):
        raise PermissionError("file is locked")

    monkeypatch.setattr(Path, "unlink", broken_unlink)

    with pytest.raises(PermissionError):
        do_remove(ws)

    monkeypatch.undo()

    assert stored_bytes(ws) == original and ws.ids_of("BOL_A.pdf") == ids
    assert lifecycle.verify_document(ws.collection(), ws.raw, "BOL_A.pdf")["consistent"]


def test_removing_a_missing_file_or_a_path_is_refused(ws):

    with pytest.raises(lifecycle.LifecycleError, match="not stored"):
        do_remove(ws, "nothing.pdf")

    with pytest.raises(lifecycle.LifecycleError, match="not a valid file name"):
        do_remove(ws, "../x.pdf")


# ============================================================
# REPROCESS
# ============================================================

def reprocess(ws, name="BOL_A.pdf"):

    return lifecycle.reprocess(ADMINISTRATOR, ws.register, name, ws.raw, ws.chroma, embed_text=fake_embedding)


def test_a_stored_pdf_without_vectors_is_reprocessed_once(ws, tmp_path):

    ws.process([bol(tmp_path)])
    ids = ws.ids_of("BOL_A.pdf")
    ws.collection().delete(ids=ids)

    assert not lifecycle.verify_document(ws.collection(), ws.raw, "BOL_A.pdf")["consistent"]

    first = reprocess(ws)
    second = reprocess(ws)

    assert (first["action"], first["chunks"]) == ("indexed", len(ids))
    assert (second["action"], second["chunks"]) == ("already_indexed", 0)
    assert ws.ids_of("BOL_A.pdf") == ids
    assert lifecycle.verify_document(ws.collection(), ws.raw, "BOL_A.pdf")["consistent"]
    assert [a["action"] for a in ws.register.actions()] == ["reprocessed"]


def test_reprocessing_refuses_to_duplicate_content_indexed_under_another_name(ws, tmp_path):

    first = bol(tmp_path, "BOL_A.pdf")
    ws.process([first])
    (ws.raw / "BOL_copy.pdf").write_bytes(first.data)

    with pytest.raises(lifecycle.LifecycleError, match="duplicate"):
        reprocess(ws, "BOL_copy.pdf")

    assert ws.ids_of("BOL_copy.pdf") == []


def test_reprocessing_replaces_vectors_that_belong_to_other_content(ws, tmp_path):

    ws.process([bol(tmp_path, pieces=48)])
    stale = ws.ids_of("BOL_A.pdf")

    # Someone put a corrected file in place by hand, outside the app
    new = corrected(tmp_path, 50)
    (ws.raw / "BOL_A.pdf").write_bytes(new)

    result = reprocess(ws)

    assert result["action"] == "indexed"
    assert not set(stale) & set(ws.ids_of("BOL_A.pdf"))
    assert lifecycle.verify_document(ws.collection(), ws.raw, "BOL_A.pdf")["consistent"]


def test_a_failed_reprocess_leaves_the_old_vectors(ws, tmp_path):

    ws.process([bol(tmp_path, pieces=48)])
    stale = ws.ids_of("BOL_A.pdf")
    (ws.raw / "BOL_A.pdf").write_bytes(corrected(tmp_path, 50))

    with pytest.raises(ConnectionError):
        lifecycle.reprocess(ADMINISTRATOR, ws.register, "BOL_A.pdf", ws.raw, ws.chroma, embed_text=flaky(99))

    assert ws.ids_of("BOL_A.pdf") == stale


def test_reprocessing_a_damaged_stored_file_is_refused(ws):

    (ws.raw / "BOL_A.pdf").write_bytes(b"%PDF-1.4 garbage")

    with pytest.raises(lifecycle.LifecycleError, match="cannot be indexed"):
        reprocess(ws)


# ============================================================
# CACHE AND INDEX CONSISTENCY
# ============================================================

def test_the_corpus_signature_changes_with_every_add_replace_and_remove(ws, tmp_path):

    empty = lifecycle.corpus_signature(ws.raw, ws.chroma)

    ws.process([bol(tmp_path, "BOL_A.pdf")])
    added = lifecycle.corpus_signature(ws.raw, ws.chroma)

    do_replace(ws, corrected(tmp_path))
    replaced = lifecycle.corpus_signature(ws.raw, ws.chroma)

    assert len({empty, added, replaced}) == 3

    do_remove(ws)
    removed = lifecycle.corpus_signature(ws.raw, ws.chroma)

    # The same state has the same signature: nothing is recomputed for nothing
    assert removed == empty and removed not in (added, replaced)
    assert lifecycle.corpus_signature(ws.raw, ws.chroma) == removed


def test_the_signature_notices_an_index_change_with_the_files_unchanged(ws, tmp_path):

    ws.process([bol(tmp_path)])
    before = lifecycle.corpus_signature(ws.raw, ws.chroma)

    ws.collection().delete(ids=ws.ids_of("BOL_A.pdf"))

    assert lifecycle.corpus_signature(ws.raw, ws.chroma) != before


def test_a_cache_never_outlives_the_documents_it_came_from(ws, tmp_path):

    calls = []
    cache = lifecycle.SignatureCache(lambda: lifecycle.corpus_signature(ws.raw, ws.chroma))

    def compute():
        calls.append(1)
        return ws.stored()

    ws.process([bol(tmp_path, "BOL_A.pdf")])

    assert cache.get("files", compute) == ["BOL_A.pdf"] and cache.get("files", compute) == ["BOL_A.pdf"]
    assert len(calls) == 1

    # Added: recomputed
    ws.process([bol(tmp_path, "BOL_B.pdf", B)])
    assert cache.get("files", compute) == ["BOL_A.pdf", "BOL_B.pdf"] and len(calls) == 2

    # Replaced: recomputed
    do_replace(ws, corrected(tmp_path))
    assert cache.get("files", compute) == ["BOL_A.pdf", "BOL_B.pdf"] and len(calls) == 3

    # Removed: recomputed, and never the answer that still listed the file
    do_remove(ws, "BOL_B.pdf")
    assert cache.get("files", compute) == ["BOL_A.pdf"] and len(calls) == 4


def test_the_analytics_fingerprint_changes_when_a_document_is_replaced_or_removed(ws, tmp_path):

    ws.process([bol(tmp_path)])
    first = analytics.source_fingerprint(ws.raw)

    do_replace(ws, corrected(tmp_path))
    second = analytics.source_fingerprint(ws.raw)

    do_remove(ws)

    assert first != second != analytics.source_fingerprint(ws.raw)


def test_originals_are_never_touched_by_adding_other_documents(ws, tmp_path):

    ws.process([bol(tmp_path, "BOL_A.pdf")])
    original, mtime = stored_bytes(ws), (ws.raw / "BOL_A.pdf").stat().st_mtime_ns

    ws.process([make_other(tmp_path, "POD_A.pdf"), Upload("bad.pdf", b"")])

    assert stored_bytes(ws) == original and (ws.raw / "BOL_A.pdf").stat().st_mtime_ns == mtime


def make_other(tmp_path, name):

    return upload_of(tmp_path, name, make_pod, A, 45)


def test_a_full_set_of_documents_can_be_ingested_and_each_is_recorded(ws, tmp_path):

    result = ws.process([
        bol(tmp_path, "BOL_A.pdf"), make_other(tmp_path, "POD_A.pdf"),
        upload_of(tmp_path, "INV_A.pdf", make_invoice, A, "$1,550.00"),
    ])

    assert [item["status"] for item in result["files"]] == ["indexed"] * 3
    assert sorted(item["document_type"] for item in result["files"]) == ["BOL", "INVOICE", "POD"]
    assert ws.register.summary()["indexed"] == 3 and ws.register.summary()["open_errors"] == 0


def test_the_other_helpers_still_build_documents():

    # make_pdf is part of the shared helpers; keep the import honest
    assert callable(make_pdf)
