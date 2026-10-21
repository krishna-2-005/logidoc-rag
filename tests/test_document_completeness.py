"""
Tests for src/document_completeness.py: the document completeness and
ingestion quality register.

The PDFs are real files in temporary folders: the project's sample
fixtures, synthetic BOL, invoice and POD PDFs written with the
ingestion tests' make_pdf, and deliberately broken files (not a PDF, no
extractable text, half-blank pages). No index, no Ollama, and the real
data/raw folder is never read.

Run from the project root:
    python -m pytest tests/test_document_completeness.py
"""

import csv
import io
import itertools
import sys
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_completeness as dc  # noqa: E402
from isolation import SAMPLE_SHIPMENT_DIR  # noqa: E402
from test_ingestion_manager import make_pdf  # noqa: E402


X = "SHJ-2026-701"
Y = "SHJ-2026-702"

TITLES = {"BOL": "BILL OF LADING", "INVOICE": "FREIGHT INVOICE", "POD": "PROOF OF DELIVERY"}


def typed_pdf(directory, doc_type, shipment=X, name=None, number=None):
    """
    A synthetic PDF of the given type for a shipment (no shipment
    number line when shipment is None).
    """

    lines = ([f"Shipment Number: {shipment}"] if shipment else []) + [
        "", "Carrier:", "Test Freight Lines", "",
        {"BOL": "Number of Pieces:", "INVOICE": "Total Invoice Amount:", "POD": "Delivery Date:"}[doc_type],
        {"BOL": "48", "INVOICE": "$1,550.00", "POD": "October 5, 2026"}[doc_type],
    ]

    return make_pdf(
        Path(directory) / (name or f"{doc_type}_{shipment or 'NOID'}.pdf"),
        TITLES[doc_type], lines,
    )


def folder_with(tmp_path, *doc_types, shipment=X):

    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)

    for doc_type in doc_types:
        typed_pdf(raw, doc_type, shipment)

    return raw


def blank_pdf(path, pages=1):

    document = pymupdf.open()

    for _ in range(pages):
        document.new_page()

    document.save(str(path))
    document.close()

    return path


def rows(register):

    return {row["shipment_id"]: row for row in register["shipments"]}


# ============================================================
# EVERY COMBINATION OF DOCUMENT TYPES
# ============================================================

COMBINATIONS = [
    combination
    for size in range(0, 4)
    for combination in itertools.combinations(("BOL", "INVOICE", "POD"), size)
]


@pytest.mark.parametrize("present", COMBINATIONS, ids=lambda combination: "+".join(combination) or "none")
def test_every_document_combination(tmp_path, present):

    raw = folder_with(tmp_path, *present)

    result = dc.register(dc.scan_folder(raw))

    if not present:
        assert result["shipments"] == []
        return

    row = rows(result)[X]

    expected_missing = [doc_type for doc_type in ("BOL", "INVOICE", "POD") if doc_type not in present]

    assert row["missing"] == expected_missing
    assert row["readiness"] == (dc.INCOMPLETE if expected_missing else dc.READY)
    assert row["documents"] == len(present)
    assert {doc_type for doc_type, names in row["by_type"].items() if names} == set(present)

    if expected_missing:
        needed = {"BOL": "BOL", "INVOICE": "invoice", "POD": "POD"}
        assert row["next_step"].startswith("Upload the ")
        assert all(needed[doc_type] in row["next_step"].split(" for ")[0] for doc_type in expected_missing)
        assert f"Shipment Number {X}" in row["next_step"]
    else:
        assert row["next_step"] == "All required documents are present."


# ============================================================
# FILES: WHAT EXTRACTION DID
# ============================================================

def test_sample_fixtures_are_parsed():

    entries = {entry["filename"]: entry for entry in dc.scan_folder(SAMPLE_SHIPMENT_DIR)}

    assert set(entries) == {"BOL_SHJ-2026-001.pdf", "INVOICE_SHJ-2026-001.pdf", "POD_SHJ-2026-001.pdf"}

    bol = entries["BOL_SHJ-2026-001.pdf"]

    assert (bol["status"], bol["document_type"], bol["shipment_id"], bol["shipment_key"]) == (
        dc.PARSED, "BOL", "SHJ-2026-001", "SHJ2026001",
    )
    assert (bol["total_pages"], bol["text_pages"]) == (1, 1)
    assert len(bol["sha256"]) == 64 and bol["size"] > 0

    result = dc.register(entries.values())

    assert rows(result)["SHJ-2026-001"]["readiness"] == dc.READY
    assert rows(result)["SHJ-2026-001"]["warnings"] == []


def test_a_file_that_is_not_a_pdf_is_unreadable_not_missing(tmp_path):

    raw = folder_with(tmp_path, "BOL", "INVOICE")
    (raw / "POD_SHJ-2026-701.pdf").write_bytes(b"this is not a pdf")

    result = dc.register(dc.scan_folder(raw))

    [unusable] = result["unusable"]

    assert (unusable["filename"], unusable["status"]) == ("POD_SHJ-2026-701.pdf", dc.UNREADABLE)
    assert unusable["error"]
    assert unusable["message"] == dc.STATUS_TEXT[dc.UNREADABLE]

    # The POD still counts as missing: an unreadable file is never
    # attributed to the shipment from its name
    row = rows(result)[X]
    assert row["missing"] == ["POD"] and row["documents"] == 2

    # ... and the next step says an unusable file exists
    assert "1 file(s) could not be used" in row["next_step"]
    assert "POD_SHJ-2026-701.pdf" in row["next_step"]


def test_a_scan_without_text_is_no_text(tmp_path):

    raw = folder_with(tmp_path, "BOL", "INVOICE")
    blank_pdf(raw / "POD_SHJ-2026-701.pdf")

    result = dc.register(dc.scan_folder(raw))

    [unusable] = result["unusable"]

    assert (unusable["status"], unusable["pages"], unusable["pages_with_text"]) == (dc.NO_TEXT, 1, 0)
    assert "OCR is not supported" in unusable["message"]
    assert rows(result)[X]["missing"] == ["POD"]


def test_an_empty_pdf(tmp_path, monkeypatch):

    class NoPages:
        def __len__(self):
            return 0
        def __iter__(self):
            return iter(())
        def close(self):
            pass

    raw = folder_with(tmp_path, "BOL")

    monkeypatch.setattr(dc.pymupdf, "open", lambda *args, **kwargs: NoPages())

    [entry] = dc.scan_folder(raw)

    assert (entry["status"], entry["total_pages"]) == (dc.EMPTY, 0)
    assert dc.register([entry])["unusable"][0]["message"] == dc.STATUS_TEXT[dc.EMPTY]


def test_partial_extraction_is_a_warning_not_a_failure(tmp_path):

    raw = folder_with(tmp_path, "BOL", "INVOICE")

    # A two-page POD whose second page has no text
    document = pymupdf.open()
    document.new_page().insert_text((50, 50), "PROOF OF DELIVERY", fontsize=20)
    document[0].insert_text((50, 90), f"Shipment Number: {X}", fontsize=11)
    document[0].insert_text((50, 110), "Delivery Date: October 5, 2026", fontsize=11)
    document.new_page()
    document.save(str(raw / "POD_SHJ-2026-701.pdf"))
    document.close()

    entries = dc.scan_folder(raw)
    pod = next(entry for entry in entries if entry["filename"].startswith("POD"))

    assert (pod["status"], pod["total_pages"], pod["text_pages"], pod["document_type"]) == (dc.PARTIAL, 2, 1, "POD")

    row = rows(dc.register(entries))[X]

    assert row["readiness"] == dc.READY and row["missing"] == []
    assert row["partial_extraction"] and row["attention"]
    assert row["warnings"] == ["Partial extraction (pages without text): POD_SHJ-2026-701.pdf"]


def test_a_loader_failure_is_unreadable_with_its_error(tmp_path):

    raw = folder_with(tmp_path, "BOL")

    def broken(path):
        raise RuntimeError("classifier exploded")

    [entry] = dc.scan_folder(raw, loader=broken)

    assert entry["status"] == dc.UNREADABLE
    assert "classifier exploded" in entry["error"]
    assert dc.register([entry])["shipments"] == []


# ============================================================
# SHIPMENT IDENTITY
# ============================================================

def test_documents_without_a_shipment_id_are_listed_not_attributed(tmp_path):

    raw = folder_with(tmp_path, "BOL", "INVOICE")
    typed_pdf(raw, "POD", shipment=None, name="POD_loose.pdf")

    result = dc.register(dc.scan_folder(raw))

    assert [item["filename"] for item in result["unidentified"]] == ["POD_loose.pdf"]
    assert result["unidentified"][0]["document_type"] == "POD"
    assert "not part of any shipment" in result["unidentified"][0]["message"]

    # Never counted as the shipment's POD
    row = rows(result)[X]
    assert row["missing"] == ["POD"]
    assert "POD_loose.pdf" in row["next_step"]


def test_shipments_are_not_merged(tmp_path):

    raw = tmp_path / "raw"
    raw.mkdir()

    for doc_type in ("BOL", "INVOICE"):
        typed_pdf(raw, doc_type, X)

    for doc_type in ("POD", "BOL"):
        typed_pdf(raw, doc_type, Y)

    result = rows(dc.register(dc.scan_folder(raw)))

    # X has no POD even though Y does; Y has no invoice
    assert result[X]["missing"] == ["POD"] and result[Y]["missing"] == ["INVOICE"]
    assert all(Y not in name for name in result[X]["files"])
    assert all(X not in name for name in result[Y]["files"])


def test_separator_variants_of_an_id_are_one_shipment(tmp_path):

    raw = tmp_path / "raw"
    raw.mkdir()

    typed_pdf(raw, "BOL", "SHJ-2026-701", name="BOL_a.pdf")
    typed_pdf(raw, "INVOICE", "shj-2026-701", name="INVOICE_a.pdf")
    typed_pdf(raw, "POD", "SHJ-2026-701", name="POD_a.pdf")

    result = dc.register(dc.scan_folder(raw))

    assert len(result["shipments"]) == 1
    assert result["shipments"][0]["readiness"] == dc.READY
    assert result["shipments"][0]["shipment_key"] == "SHJ2026701"


def test_other_document_types_do_not_complete_a_shipment(tmp_path):

    raw = folder_with(tmp_path, "BOL", "INVOICE")

    make_pdf(raw / "notes.pdf", "MEETING NOTES", [f"Shipment Number: {X}", "", "Remarks: discussed delivery"])

    row = rows(dc.register(dc.scan_folder(raw)))[X]

    assert row["missing"] == ["POD"]
    assert row["documents"] == 3
    assert any("not a BOL, invoice or POD" in warning and "notes.pdf" in warning for warning in row["warnings"])


# ============================================================
# DUPLICATES
# ============================================================

def test_identical_files_are_flagged(tmp_path):

    raw = folder_with(tmp_path, "BOL", "INVOICE", "POD")
    (raw / "BOL_copy.pdf").write_bytes((raw / "BOL_SHJ-2026-701.pdf").read_bytes())

    result = dc.register(dc.scan_folder(raw))

    assert result["duplicates"] == [["BOL_SHJ-2026-701.pdf", "BOL_copy.pdf"]]

    row = rows(result)[X]

    assert row["readiness"] == dc.READY and row["duplicate_files"] and row["multiple_of_a_type"]
    assert any(warning.startswith("Identical file content: BOL_SHJ-2026-701.pdf, BOL_copy.pdf") for warning in row["warnings"])
    assert any(warning.startswith("2 BOL documents") for warning in row["warnings"])


def test_two_documents_of_a_type_are_a_warning_not_a_duplicate(tmp_path):

    raw = folder_with(tmp_path, "BOL", "INVOICE", "POD")
    make_pdf(raw / "BOL_revised.pdf", "BILL OF LADING", [f"Shipment Number: {X}", "", "Number of Pieces:", "50"])

    result = dc.register(dc.scan_folder(raw))

    assert result["duplicates"] == []

    row = rows(result)[X]

    assert row["multiple_of_a_type"] and not row["duplicate_files"]
    assert row["readiness"] == dc.READY


# ============================================================
# INDEX, EMPTY COLLECTIONS, FILTERS, CSV
# ============================================================

def test_indexed_documents(tmp_path):

    raw = folder_with(tmp_path, "BOL", "INVOICE", "POD")

    files = dc.scan_folder(raw)

    row = rows(dc.register(files))[X]
    assert row["indexed_documents"] is None       # index not known

    row = rows(dc.register(files, indexed_sources={"BOL_SHJ-2026-701.pdf", "INVOICE_SHJ-2026-701.pdf"}))[X]

    assert row["indexed_documents"] == 2 and row["documents"] == 3
    assert row["warnings"] == ["Not in the search index (chat cannot see them): POD_SHJ-2026-701.pdf"]
    assert row["readiness"] == dc.READY     # the audit reads the PDFs, not the index


def test_empty_and_missing_folders(tmp_path):

    assert dc.scan_folder(tmp_path / "missing") == []
    assert dc.scan_folder(tmp_path) == []

    assert dc.register([]) == {"shipments": [], "unidentified": [], "unusable": [], "duplicates": []}
    assert dc.filter_shipments([], search="x", incomplete_only=True) == []

    rows_csv = list(csv.reader(io.StringIO(dc.register_csv([]).decode("utf-8-sig"))))
    assert rows_csv == [list(dc.COLUMNS)]


def test_filters(tmp_path):

    raw = tmp_path / "raw"
    raw.mkdir()

    for doc_type in ("BOL", "INVOICE", "POD"):
        typed_pdf(raw, doc_type, X)

    typed_pdf(raw, "BOL", Y)
    typed_pdf(raw, "INVOICE", Y)

    typed_pdf(raw, "BOL", "SHJ-2026-703")

    result = dc.register(dc.scan_folder(raw))["shipments"]

    def ids(**filters):
        return sorted(row["shipment_id"] for row in dc.filter_shipments(result, **filters))

    assert ids() == [X, Y, "SHJ-2026-703"]
    assert ids(incomplete_only=True) == [Y, "SHJ-2026-703"]
    assert ids(missing_types=["POD"]) == [Y, "SHJ-2026-703"]
    assert ids(missing_types=["INVOICE"]) == ["SHJ-2026-703"]
    assert ids(missing_types=["BOL"]) == []
    assert ids(search="shj 2026 702") == [Y]
    assert ids(search="703", incomplete_only=True) == ["SHJ-2026-703"]
    assert ids(extraction_issues=True) == []
    assert ids(attention_only=True) == []


def test_csv(tmp_path):

    raw = folder_with(tmp_path, "BOL", "INVOICE")

    exported = list(csv.DictReader(io.StringIO(dc.register_csv(dc.register(dc.scan_folder(raw))["shipments"]).decode("utf-8-sig"))))

    assert dc.register_csv([]).startswith(b"\xef\xbb\xbf")
    assert len(exported) == 1
    assert (exported[0]["readiness"], exported[0]["bol"], exported[0]["pod"], exported[0]["missing"]) == (
        "incomplete", "present", "missing", "POD",
    )
    assert exported[0]["next_step"].startswith(f"Upload the POD for {X}")


# ============================================================
# A FILE THAT CANNOT BE READ DOES NOT STOP THE OTHERS
# ============================================================

def test_readable_chunks_skip_an_unreadable_file(tmp_path):

    raw = folder_with(tmp_path, "BOL", "INVOICE", "POD")
    (raw / "broken_scan.pdf").write_bytes(b"this is not a pdf")

    chunks, skipped = dc.readable_chunks(raw)

    assert skipped == ["broken_scan.pdf"]
    assert {chunk["metadata"]["source"] for chunk in chunks} == {
        "BOL_SHJ-2026-701.pdf", "INVOICE_SHJ-2026-701.pdf", "POD_SHJ-2026-701.pdf",
    }
    assert {chunk["metadata"]["shipment_id"] for chunk in chunks} == {X}

    # The same file reported by the completeness scan, with the reason
    assert [item["filename"] for item in dc.register(dc.scan_folder(raw))["unusable"]] == ["broken_scan.pdf"]


def test_readable_chunks_equal_the_projects_chunks_when_every_file_reads(tmp_path):

    from chunker import create_all_chunks
    from isolation import redirect_project_paths

    raw = folder_with(tmp_path, "BOL", "INVOICE", "POD")

    with redirect_project_paths(raw, tmp_path / "chroma_db"):
        expected = create_all_chunks()

    chunks, skipped = dc.readable_chunks(raw)

    assert skipped == [] and chunks == expected and len(chunks) == 3


def test_readable_chunks_of_a_missing_or_empty_folder(tmp_path):

    assert dc.readable_chunks(tmp_path / "missing") == ([], [])
    assert dc.readable_chunks(tmp_path) == ([], [])


# ============================================================
# UNUSABLE FILES STAY VISIBLE ON EVERY SHIPMENT
# ============================================================

def test_unusable_files_are_a_warning_on_every_shipment(tmp_path):

    raw = tmp_path / "raw"
    raw.mkdir()

    for doc_type in ("BOL", "INVOICE", "POD"):
        typed_pdf(raw, doc_type, X)
        typed_pdf(raw, doc_type, Y)

    blank_pdf(raw / "blank_scan.pdf")
    (raw / "broken_scan.pdf").write_bytes(b"this is not a pdf")

    result = dc.register(dc.scan_folder(raw))

    for row in result["shipments"]:

        # Both shipments have every document, and are ready ...
        assert row["readiness"] == dc.READY

        # ... but neither is shown as free of doubt
        assert row["attention"]
        assert row["warnings"] == [
            "2 file(s) could not be used and cannot be ruled out as belonging to this "
            "shipment (the audit result is NEEDS REVIEW at best): "
            "blank_scan.pdf (no_text), broken_scan.pdf (unreadable)"
        ]

    assert [item["filename"] for item in result["unusable"]] == ["blank_scan.pdf", "broken_scan.pdf"]

    # Without them, no warning
    (raw / "blank_scan.pdf").unlink()
    (raw / "broken_scan.pdf").unlink()

    assert all(row["warnings"] == [] and not row["attention"] for row in dc.register(dc.scan_folder(raw))["shipments"])
