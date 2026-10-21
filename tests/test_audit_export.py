"""
Tests for src/audit_export.py: the freight audit CSV of one shipment.

Exports are made from real reconcile_shipment() results on synthetic
documents (the helpers of test_reconciliation_consistency: no PDFs,
no index, no Ollama), and from hand-built results for CSV escaping and
the refusal cases. The dashboard test drives app.py with AppTest on
the temporary two-shipment index of test_multi_shipment.

Run from the project root:
    python -m pytest tests/test_audit_export.py
"""

import csv
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from audit_export import (
    COLUMNS,
    NOT_AVAILABLE,
    ExportError,
    audit_csv,
    audit_rows,
    export_filename,
)

# Synthetic documents and the reconciliation fixture
from test_reconciliation_consistency import (  # noqa: F401
    A,
    B,
    documents,
    drop,
    index,
    reconcile,
)


def parse(data):
    """
    The CSV bytes as (header, rows); the file must be UTF-8.
    """

    assert data.startswith(b"\xef\xbb\xbf")

    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))

    return reader.fieldnames, list(reader)


def only(rows, record_type, category=None, field=None):

    return [
        row for row in rows
        if row["record_type"] == record_type
        and (category is None or row["category"] == category)
        and (field is None or row["field"] == field)
    ]


def one(rows, record_type, category=None, field=None):

    found = only(rows, record_type, category, field)

    assert len(found) == 1, (record_type, category, field, found)

    return found[0]


# ============================================================
# SUCCESSFUL EXPORTS
# ============================================================

def test_export_of_a_shipment_with_discrepancies(index):

    # SHJ-2026-001 layout: 48 vs 45 pieces, 2 days late, invoice line
    # items $1,430.00 but Total Invoice Amount $1,550.00
    result = reconcile(index, documents(), A)

    header, rows = parse(audit_csv(result, A))

    assert header == list(COLUMNS)
    assert {row["shipment_id"] for row in rows} == {A}

    summary = one(rows, "AUDIT SUMMARY")
    assert summary["final_status"] == "DISCREPANCIES DETECTED"

    quantity = one(rows, "AUDIT", "Quantity")
    assert (quantity["expected"], quantity["actual"], quantity["difference"]) == ("48", "45", "3")
    assert quantity["status"] == "MISMATCH"
    assert quantity["difference_basis"] == "BOL - POD"

    delivery = one(rows, "AUDIT", "Delivery")
    assert (delivery["expected"], delivery["actual"]) == ("October 3, 2026", "October 5, 2026")
    assert (delivery["status"], delivery["difference"]) == ("DELAYED", "2")

    invoice = one(rows, "AUDIT", "Invoice")
    assert invoice["status"] == "MISMATCH"
    assert (invoice["expected"], invoice["actual"], invoice["difference"]) == ("1430.00", "1550.00", "120.00")
    assert (invoice["invoice_total"], invoice["line_item_sum"]) == ("1550.00", "1430.00")
    assert invoice["description"] == result["invoice"]["breakdown"]

    assert {
        row["field"]: (row["actual"], row["status"])
        for row in only(rows, "LINE ITEM")
    } == {
        "Base Freight": ("1200.00", "MISMATCH"),
        "Fuel Surcharge": ("180.00", "MISMATCH"),
        "Detention Charge": ("0.00", "MISMATCH"),
        "Other Accessorial Charges": ("50.00", "MISMATCH"),
    }

    discrepancies = {row["category"]: row for row in only(rows, "DISCREPANCY")}
    assert set(discrepancies) == {"Quantity", "Delivery Date", "Invoice"}
    assert discrepancies["Invoice"]["difference"] == "120.00"
    assert discrepancies["Delivery Date"]["difference"] == "2"
    assert discrepancies["Invoice"]["description"] == next(
        item["description"] for item in result["discrepancies"]
        if item["category"] == "Invoice"
    )

    assert only(rows, "REVIEW") == []


def test_export_of_a_clear_shipment(index):

    result = reconcile(
        index,
        documents(delivered=48, actual="October 3, 2026", total="$1,430.00"),
        A,
    )

    _, rows = parse(audit_csv(result, A))

    assert one(rows, "AUDIT SUMMARY")["final_status"] == "NO DISCREPANCIES"
    assert one(rows, "AUDIT", "Quantity")["status"] == "MATCH"
    assert one(rows, "AUDIT", "Delivery")["status"] == "ON TIME"
    assert one(rows, "AUDIT", "Invoice")["difference"] == "0.00"
    assert only(rows, "DISCREPANCY") == [] and only(rows, "REVIEW") == []


def test_early_delivery_and_negative_invoice_difference_keep_their_sign(index):

    # Line items $1,430.00, Total Invoice Amount $1,400.00
    result = reconcile(
        index,
        documents(actual="October 2, 2026", total="$1,400.00"),
        A,
    )

    _, rows = parse(audit_csv(result, A))

    delivery = one(rows, "AUDIT", "Delivery")
    assert (delivery["status"], delivery["difference"]) == ("EARLY", "-1")

    invoice = one(rows, "AUDIT", "Invoice")
    assert invoice["difference"] == "-30.00"
    assert one(rows, "DISCREPANCY", "Invoice")["difference"] == "-30.00"

    # An early delivery is not a discrepancy
    assert "Delivery Date" not in {row["category"] for row in only(rows, "DISCREPANCY")}


# ============================================================
# NEEDS REVIEW AND MISSING VALUES
# ============================================================

def test_missing_quantity_needs_review_and_is_never_zero(index):

    result = reconcile(index, drop(documents(delivered=48, actual="October 3, 2026", total="$1,430.00"),
                                   "POD", "Number of Pieces Delivered"), A)

    _, rows = parse(audit_csv(result, A))

    assert one(rows, "AUDIT SUMMARY")["final_status"] == "NEEDS REVIEW"

    quantity = one(rows, "AUDIT", "Quantity")
    assert quantity["status"] == "NEEDS REVIEW"
    assert quantity["actual"] == NOT_AVAILABLE
    assert quantity["difference"] == NOT_AVAILABLE
    assert quantity["reason"] == result["quantity"]["reason"] != ""

    review = one(rows, "REVIEW", "Quantity")
    assert review["status"] == "NEEDS REVIEW"
    assert review["reason"] == result["review_items"][0]["reason"]


def test_missing_invoice_line_item_is_not_presented_as_verified(index):

    result = reconcile(index, drop(documents(delivered=48, actual="October 3, 2026", total="$1,430.00"),
                                   "INVOICE", "Detention Charge"), A)

    _, rows = parse(audit_csv(result, A))

    invoice = one(rows, "AUDIT", "Invoice")
    assert invoice["status"] == "NEEDS REVIEW"
    assert invoice["expected"] == NOT_AVAILABLE   # no line-item sum
    assert invoice["difference"] == NOT_AVAILABLE
    assert invoice["actual"] == "1430.00"         # the stated total was read

    line_items = {row["field"]: (row["actual"], row["status"]) for row in only(rows, "LINE ITEM")}
    assert line_items["Detention Charge"] == (NOT_AVAILABLE, NOT_AVAILABLE)
    # Amounts that were read keep the invoice audit's NEEDS REVIEW
    assert line_items["Base Freight"] == ("1200.00", "NEEDS REVIEW")

    assert one(rows, "REVIEW", "Invoice")["reason"]


# ============================================================
# EVIDENCE
# ============================================================

def test_evidence_rows_carry_source_page_type_and_shipment(index):

    result = reconcile(index, documents(), A)

    _, rows = parse(audit_csv(result, A))

    evidence = only(rows, "EVIDENCE")

    assert {row["category"] for row in evidence} == {"Quantity", "Delivery", "Invoice"}
    assert {row["evidence_shipment_id"] for row in evidence} == {A}

    pieces = [row for row in evidence if row["evidence_text"] == "Number of Pieces: 48"]
    assert len(pieces) == 1
    assert (pieces[0]["source"], pieces[0]["page"], pieces[0]["document_type"], pieces[0]["category"]) == (
        f"BOL_{A}.pdf", "1", "BOL", "Quantity",
    )

    total = [row for row in evidence if row["evidence_text"] == "Total Invoice Amount: $1,550.00"]
    assert [(row["source"], row["document_type"]) for row in total] == [(f"INVOICE_{A}.pdf", "INVOICE")]

    # One evidence row per field the reconciliation cited
    assert len(evidence) == sum(
        len(result[category]["evidence"]) for category in ("quantity", "delivery", "invoice")
    )


# ============================================================
# CSV ESCAPING AND UNICODE
# ============================================================

def hand_built_result(**changes):

    result = {
        "success": True,
        "shipment_id": A,
        "quantity": {"bol": 48, "pod": 45, "difference": 3, "status": "MISMATCH",
                     "reason": "", "evidence": []},
        "delivery": {"expected": None, "actual": None, "status": "NEEDS REVIEW",
                     "days_late": None, "reason": "dates missing", "evidence": []},
        "invoice": {"total": None, "line_items": [], "line_item_sum": None,
                    "difference": None, "breakdown": None, "status": "NEEDS REVIEW",
                    "reason": "", "evidence": []},
        "discrepancies": [],
        "review_items": [],
        "final_status": "DISCREPANCIES DETECTED",
    }

    result.update(changes)

    return result


def test_csv_escaping_and_unicode():

    description = 'Pieces short, "3" missing\nsecond line — €12'
    formula = '=HYPERLINK("http://example.com","x")'

    result = hand_built_result(
        discrepancies=[{"category": "Quantity", "description": description, "difference": 3}],
        quantity={
            "bol": 48, "pod": 45, "difference": 3, "status": "MISMATCH", "reason": "",
            "evidence": [{"source": "BOL, copy \"2\".pdf", "page": 1, "document_type": "BOL",
                          "shipment_id": A, "text": formula}],
        },
    )

    header, rows = parse(audit_csv(result, A))

    assert header == list(COLUMNS)

    # Commas, quotes, a newline and Unicode survive the round trip
    assert one(rows, "DISCREPANCY")["description"] == description

    evidence = one(rows, "EVIDENCE")
    assert evidence["source"] == 'BOL, copy "2".pdf'

    # A cell a spreadsheet would run as a formula is neutralised
    assert evidence["evidence_text"] == "'" + formula

    # Missing delivery values are NOT AVAILABLE, not 0 or empty
    delivery = one(rows, "AUDIT", "Delivery")
    assert (delivery["expected"], delivery["actual"], delivery["difference"]) == (NOT_AVAILABLE,) * 3

    # The rows themselves are unchanged (escaping happens on writing)
    assert one(audit_rows(result, A), "EVIDENCE")["evidence_text"] == formula


def evidence_item(**changes):

    item = {"source": f"POD_{A}.pdf", "page": 1, "document_type": "POD",
            "shipment_id": A, "text": "Number of Pieces Delivered: 45"}

    item.update(changes)

    return item


def test_formula_injection_in_metadata_and_non_text_columns():

    # Only text columns were protected; page, status and difference
    # values reached the file as written
    malicious = {
        "page": "=cmd|' /C calc'!A0",
        "document_type": "@SUM(1+1)",
        "source": " =HYPERLINK(\"http://example.com\")",
    }

    result = hand_built_result(
        quantity={
            "bol": 48, "pod": 45, "difference": "+2+3", "status": "=1+1",
            "reason": "", "evidence": [evidence_item(**malicious)],
        },
    )

    _, rows = parse(audit_csv(result, A))

    evidence = one(rows, "EVIDENCE")

    for column, value in malicious.items():
        assert evidence[column] == "'" + value, column

    quantity = one(rows, "AUDIT", "Quantity")
    assert quantity["status"] == "'=1+1"
    assert quantity["difference"] == "'+2+3"

    # The rows themselves are unchanged (protection happens on writing)
    raw = one(audit_rows(result, A), "EVIDENCE")
    assert raw["page"] == malicious["page"]


@pytest.mark.parametrize(
    "value",
    [
        "=1+1",
        "+1+1",
        "-1+1",
        "@SUM(A1)",
        "\t=1+1",
        "\r=1+1",
        "  =1+1",
        "\n-2+3",
        "\x01=1+1",
        " @SUM(A1)",
        "＝1+1",          # full-width "="
        "-5 pieces",          # starts like a number, but is text
    ],
)
def test_formula_like_evidence_text_is_neutralised(value):

    result = hand_built_result(
        quantity={"bol": 48, "pod": 45, "difference": 3, "status": "MISMATCH",
                  "reason": "", "evidence": [evidence_item(text=value)]},
    )

    _, rows = parse(audit_csv(result, A))

    # Written with a leading "'" and otherwise exactly as it was
    assert one(rows, "EVIDENCE")["evidence_text"] == "'" + value


def test_legitimate_numbers_are_never_prefixed():

    result = hand_built_result(
        quantity={"bol": 48, "pod": 50, "difference": -2, "status": "MISMATCH",
                  "reason": "", "evidence": [evidence_item(page="-1", text="-30.00")]},
        delivery={"expected": "October 9, 2026", "actual": "October 8, 2026",
                  "status": "EARLY", "days_late": -1, "reason": "", "evidence": []},
        invoice={"total": 1400.0, "line_items": [("Base Freight", -30.0)],
                 "line_item_sum": 1430.0, "difference": -30.0, "breakdown": None,
                 "status": "MISMATCH", "reason": "", "evidence": []},
        discrepancies=[{"category": "Invoice", "description": "Total below the sum.",
                        "difference": -30.0}],
    )

    _, rows = parse(audit_csv(result, A))

    assert one(rows, "AUDIT", "Quantity")["difference"] == "-2"
    assert one(rows, "AUDIT", "Delivery")["difference"] == "-1"
    assert one(rows, "AUDIT", "Invoice")["difference"] == "-30.00"
    assert one(rows, "LINE ITEM", "Invoice", "Base Freight")["actual"] == "-30.00"
    assert one(rows, "DISCREPANCY", "Invoice")["difference"] == "-30.00"

    evidence = one(rows, "EVIDENCE")
    assert (evidence["page"], evidence["evidence_text"]) == ("-1", "-30.00")

    assert not [
        (row["record_type"], column)
        for row in rows
        for column, value in row.items()
        if value.startswith("'")
    ]


# ============================================================
# ONLY ONE SHIPMENT, ONLY SUCCESSFUL RESULTS
# ============================================================

@pytest.mark.parametrize(
    "result, selected, reason",
    [
        ({"success": False, "error": "Reconciliation requires BOL, Invoice and POD."},
         A, "did not succeed"),
        (None, A, "did not succeed"),
        (hand_built_result(shipment_id=None), None, "no shipment ID"),
        (hand_built_result(), B, f"not the selected shipment {B}"),
        (hand_built_result(invoice={
            "total": 1550.0, "line_items": [], "line_item_sum": None, "difference": None,
            "breakdown": None, "status": "NEEDS REVIEW", "reason": "",
            "evidence": [{"source": f"INVOICE_{B}.pdf", "page": 1, "document_type": "INVOICE",
                          "shipment_id": B, "text": "Total Invoice Amount: $9.00"}],
        }), A, "another shipment"),
    ],
)
def test_export_is_refused(result, selected, reason):

    with pytest.raises(ExportError, match=reason):
        audit_csv(result, selected)


def test_separator_variants_of_the_selected_shipment_are_accepted():

    _, rows = parse(audit_csv(hand_built_result(), "shj 2026 001"))

    assert {row["shipment_id"] for row in rows} == {A}


def test_export_filename():

    assert export_filename({"shipment_id": A}) == f"freight_audit_{A}.csv"
    assert export_filename({"shipment_id": "SHJ/2026 001"}) == "freight_audit_SHJ_2026_001.csv"


# ============================================================
# DASHBOARD
# ============================================================

@pytest.fixture(scope="module")
def two_shipment_dashboard():
    """
    Both sample shipments in a temporary workspace and index, built
    like test_multi_shipment (stand-in embeddings, no Ollama).
    """

    import document_loader
    import rag
    import vector_store
    from test_ingestion_manager import fake_embedding, patched
    from test_multi_shipment import isolated_workspace

    with isolated_workspace() as (raw_dir, chroma_dir):

        assert vector_store.CHROMA_DIR == chroma_dir
        assert rag.CHROMA_DIR == chroma_dir
        assert document_loader.RAW_DATA_DIR == raw_dir

        with patched(vector_store, "create_embedding", fake_embedding):
            with redirect_stdout(io.StringIO()):
                vector_store.build_vector_database()

        yield


def test_dashboard_offers_the_export_only_for_a_selected_shipment(two_shipment_dashboard):

    from test_multi_shipment import run_app

    at = run_app(A, "Run Reconciliation")

    assert not at.exception, [str(item.value) for item in at.exception]
    # The CSV export, and next to it the PDF report of the same result
    assert [button.label for button in at.get("download_button")] == [
        "Download freight audit (CSV)",
        "Download freight audit (PDF)",
    ]

    # Downloading one file must not rerun the app, which would clear
    # the result and the other button
    assert all(button.proto.ignore_rerun for button in at.get("download_button"))

    at = run_app("All Shipments", "Run Reconciliation")

    assert not at.exception, [str(item.value) for item in at.exception]
    assert at.get("download_button") == []
