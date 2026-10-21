"""
Tests for src/audit_pdf_export.py: the freight audit PDF report of one
shipment.

Reports are made from real reconcile_shipment() results on synthetic
documents (the helpers of test_reconciliation_consistency: no PDFs,
no index, no Ollama) and from hand-built results for long text,
Unicode and the refusal cases. Assertions read the text of the
generated PDF with PyMuPDF, as a reader would see it.

Run from the project root:
    python -m pytest tests/test_audit_pdf_export.py
"""

import re
import sys
from datetime import datetime
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from audit_export import ExportError
from audit_pdf_export import TITLE, audit_pdf, pdf_filename

# Synthetic documents and the reconciliation fixture
from test_reconciliation_consistency import (  # noqa: F401
    A,
    B,
    documents,
    drop,
    index,
    reconcile,
)


GENERATED = datetime(2026, 10, 21, 9, 30)


def pdf_text(data):
    """
    (page count, the text of every page with whitespace collapsed).
    """

    document = pymupdf.open(stream=data, filetype="pdf")

    text = " ".join(" ".join(page.get_text().split()) for page in document)

    return document.page_count, text


def section(text, start, end=None):
    """
    The text between two section headings.
    """

    begin = text.index(start)
    stop = text.index(end, begin) if end else len(text)

    return text[begin:stop]


# ============================================================
# SUCCESSFUL REPORTS
# ============================================================

def test_report_of_a_shipment_with_discrepancies(index):

    # SHJ-2026-001 layout: 48 vs 45 pieces, 2 days late, invoice line
    # items $1,430.00 but Total Invoice Amount $1,550.00
    result = reconcile(index, documents(), A)

    data = audit_pdf(result, A, generated_at=GENERATED)

    assert data.startswith(b"%PDF-")
    assert len(data) > 1000

    pages, text = pdf_text(data)

    assert TITLE in text
    assert f"Page 1 of {pages}" in text

    overview = section(text, "A. Shipment Overview", "B. Quantity Reconciliation")
    assert f"Shipment ID {A}" in overview
    assert "Carrier Swift Lines" in overview
    assert "Origin Chicago, Illinois" in overview
    assert "Destination Atlanta, Georgia" in overview
    assert "Final audit status DISCREPANCIES DETECTED" in overview
    assert "Report generated 2026-10-21 09:30" in overview

    quantity = section(text, "B. Quantity Reconciliation", "C. Delivery Reconciliation")
    assert "BOL expected quantity 48" in quantity
    assert "POD delivered quantity 45" in quantity
    assert "+3 (shortage of 3)" in quantity
    assert "Audit status MISMATCH" in quantity

    delivery = section(text, "C. Delivery Reconciliation", "D. Invoice Reconciliation")
    assert "Expected delivery date October 3, 2026" in delivery
    assert "Actual delivery date October 5, 2026" in delivery
    assert "+2 (2 days late)" in delivery
    assert "Audit status DELAYED" in delivery

    invoice = section(text, "D. Invoice Reconciliation", "E. Discrepancies Detected")
    for line in (
        "Base Freight $1,200.00",
        "Fuel Surcharge $180.00",
        "Detention Charge $0.00",
        "Other Accessorial Charges $50.00",
        "Invoice line-item sum $1,430.00",
        "Stated invoice total $1,550.00",
        "Difference (total - line items) +$120.00",
        "Invoice audit status MISMATCH",
    ):
        assert line in invoice, line

    discrepancies = section(text, "E. Discrepancies Detected", "F. Items Requiring Review")
    for item in result["discrepancies"]:
        assert item["category"] in discrepancies
    assert "BOL shows 48 pieces, but POD shows 45 pieces delivered." in discrepancies
    assert "a difference of $120.00." in discrepancies

    assert "No items require review." in section(
        text, "F. Items Requiring Review", "G. Evidence and Source References"
    )


def test_evidence_and_source_references(index):

    result = reconcile(index, documents(), A)

    _, text = pdf_text(audit_pdf(result, A))

    evidence = section(text, "G. Evidence and Source References")

    assert "Audit Source file Page Type Shipment Field text" in evidence
    assert f"Quantity BOL_{A}.pdf 1 BOL {A} Number of Pieces: 48" in evidence
    assert f"Delivery POD_{A}.pdf 1 POD {A} Delivery Date: October 5, 2026" in evidence
    assert "Total Invoice Amount: $1,550.00" in evidence

    # Every cited field is listed
    for category in ("quantity", "delivery", "invoice"):
        for item in result[category]["evidence"]:
            assert item["text"] in evidence, item


def test_clean_audit(index):

    result = reconcile(
        index,
        documents(delivered=48, actual="October 3, 2026", total="$1,430.00"),
        A,
    )

    _, text = pdf_text(audit_pdf(result, A))

    assert "Final audit status NO DISCREPANCIES" in text
    assert "No discrepancies were detected." in text
    assert "No items require review." in text
    assert "Difference (total - line items) $0.00" in text
    assert "0 (on the expected date)" in text


def test_early_delivery_and_negative_invoice_difference_keep_their_sign(index):

    result = reconcile(
        index,
        documents(actual="October 2, 2026", total="$1,400.00"),
        A,
    )

    _, text = pdf_text(audit_pdf(result, A))

    delivery = section(text, "C. Delivery Reconciliation", "D. Invoice Reconciliation")
    assert "-1 (1 day early)" in delivery
    assert "Audit status EARLY" in delivery

    assert "Difference (total - line items) -$30.00" in text


# ============================================================
# NEEDS REVIEW AND MISSING VALUES
# ============================================================

def test_needs_review_reasons_and_missing_values(index):

    result = reconcile(
        index,
        drop(documents(delivered=48, actual="October 3, 2026", total="$1,430.00"),
             "POD", "Number of Pieces Delivered"),
        A,
    )

    _, text = pdf_text(audit_pdf(result, A))

    assert "Final audit status NEEDS REVIEW" in text

    quantity = section(text, "B. Quantity Reconciliation", "C. Delivery Reconciliation")
    assert "POD delivered quantity NOT AVAILABLE" in quantity
    assert "Difference (BOL - POD) NOT AVAILABLE" in quantity
    assert "Audit status NEEDS REVIEW" in quantity
    assert "Review reason" in quantity

    review = section(text, "F. Items Requiring Review", "G. Evidence and Source References")
    assert "Quantity NEEDS REVIEW" in review
    # The reason, wrapped over lines in the PDF, is there word for word
    assert " ".join(result["review_items"][0]["reason"].split()) in review


def test_missing_invoice_line_item_is_not_available_not_zero(index):

    result = reconcile(
        index,
        drop(documents(delivered=48, actual="October 3, 2026", total="$1,430.00"),
             "INVOICE", "Detention Charge"),
        A,
    )

    _, text = pdf_text(audit_pdf(result, A))

    invoice = section(text, "D. Invoice Reconciliation", "E. Discrepancies Detected")
    assert "Detention Charge NOT AVAILABLE" in invoice
    assert "Detention Charge $0.00" not in invoice
    assert "Invoice line-item sum NOT AVAILABLE" in invoice
    assert "Difference (total - line items) NOT AVAILABLE" in invoice
    assert "Invoice audit status NEEDS REVIEW" in invoice


# ============================================================
# LONG TEXT, MANY PAGES, UNICODE
# ============================================================

def hand_built_result(**changes):

    result = {
        "success": True,
        "shipment_id": A,
        "carrier": {"bol": "Swift Lines", "invoice": "Swift Lines", "pod": "Swift Lines",
                    "bol_invoice_status": "MATCH", "bol_pod_status": "MATCH"},
        "route": {"origin": "Chicago, Illinois", "destination": "Atlanta, Georgia",
                  "invoice_origin": "Chicago, Illinois", "invoice_destination": "Atlanta, Georgia",
                  "origin_status": "MATCH", "destination_status": "MATCH"},
        "quantity": {"bol": 48, "pod": 45, "difference": 3, "status": "MISMATCH",
                     "reason": "", "evidence": []},
        "delivery": {"expected": None, "actual": None, "status": "NEEDS REVIEW",
                     "days_late": None, "reason": "dates missing", "evidence": []},
        "invoice": {"total": None, "line_items": [], "line_item_sum": None,
                    "difference": None, "breakdown": None, "status": "NEEDS REVIEW",
                    "reason": "", "evidence": []},
        "discrepancies": [],
        "review_items": [{"category": "Delivery", "reason": "dates missing"}],
        "final_status": "DISCREPANCIES DETECTED",
    }

    result.update(changes)

    return result


def test_long_evidence_and_many_pages():

    long_text = "Remark: " + "pallet wrapped and sealed at dock seven " * 150 + "X" * 400

    evidence = [
        {"source": f"POD_{A}_{number}.pdf", "page": number, "document_type": "POD",
         "shipment_id": A, "text": long_text if number == 3 else f"Note {number}: received"}
        for number in range(60)
    ]

    result = hand_built_result(
        quantity={"bol": 48, "pod": 45, "difference": 3, "status": "MISMATCH",
                  "reason": "", "evidence": evidence},
    )

    data = audit_pdf(result, A)

    pages, text = pdf_text(data)

    assert pages >= 4
    assert f"Page {pages} of {pages}" in text

    # Nothing is clipped: the last row, the end of the long text and
    # every character of a 400-character word are there
    assert "Note 59: received" in text
    assert "pallet wrapped and sealed at dock seven" in text
    assert sum(len(run) for run in re.findall(r"X+", text)) == 400

    document = pymupdf.open(stream=data, filetype="pdf")
    evidence_pages = [page for page in document if "Note " in page.get_text()]

    # The evidence table's header is repeated on every page it covers
    assert len(evidence_pages) >= 2
    assert all("Field text" in page.get_text() for page in evidence_pages)

    # A row longer than a page says where it continues
    assert "(continued)" in text


HEADINGS = (
    "A. Shipment Overview", "B. Quantity Reconciliation",
    "C. Delivery Reconciliation", "D. Invoice Reconciliation",
    "E. Discrepancies Detected", "F. Items Requiring Review",
    "G. Evidence and Source References",
)


@pytest.mark.parametrize(
    "case",
    ["discrepancies", "clear", "needs review", "many evidence rows"],
)
def test_no_section_heading_ends_a_page(index, case):

    # A heading was left alone at the bottom of a page when the table
    # under it moved to the next page
    if case == "discrepancies":
        result = reconcile(index, documents(), A)
    elif case == "clear":
        result = reconcile(index, documents(delivered=48, actual="October 3, 2026", total="$1,430.00"), A)
    elif case == "needs review":
        result = reconcile(index, drop(documents(), "INVOICE", "Detention Charge"), A)
    else:
        result = reconcile(index, documents(), A)
        result["quantity"]["evidence"] = result["quantity"]["evidence"] + [
            {"source": f"POD_{A}_{number}.pdf", "page": number, "document_type": "POD",
             "shipment_id": A, "text": f"Note {number}: " + "received in good order " * number}
            for number in range(40)
        ]

    document = pymupdf.open(stream=audit_pdf(result, A), filetype="pdf")

    for page in document:

        blocks = [
            " ".join(block[4].split())
            for block in sorted(page.get_text("blocks"), key=lambda block: block[1])
            if not block[4].strip().startswith("Page ")
        ]

        assert blocks[-1] not in HEADINGS, (case, page.number + 1, blocks[-1])

    # Every heading is still there, once
    _, text = pdf_text(document.tobytes())

    for heading in HEADINGS:
        assert text.count(heading) == 1, heading


def test_unicode_within_the_report_fonts():

    result = hand_built_result(
        discrepancies=[{
            "category": "Invoice",
            "description": "Total in € — £12, carrier’s note; ID SHJ‑2026‑001 ✓",
            "difference": 12.5,
        }],
    )

    _, text = pdf_text(audit_pdf(result, A))

    # € — £ ’ are in the Helvetica fonts and appear as written
    assert "Total in € — £12, carrier’s note;" in text

    # U+2011 is not: it becomes "-"; an unsupported symbol becomes "?"
    assert "ID SHJ-2026-001 ?" in text
    assert "‑" not in text and "✓" not in text

    assert "+$12.50" in text


# ============================================================
# REFUSALS
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
def test_report_is_refused(result, selected, reason):

    with pytest.raises(ExportError, match=reason):
        audit_pdf(result, selected)


def test_report_filename():

    assert pdf_filename({"shipment_id": A}) == f"freight_audit_{A}.pdf"
    assert pdf_filename({"shipment_id": "SHJ/2026 001"}) == "freight_audit_SHJ_2026_001.pdf"
