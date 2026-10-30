"""
Cross-document checks of one shipment's extractions (validate_shipment).

Report-only findings added beside the existing ones: shipment IDs that
collide, the invoice against the rate confirmation (amount and payment
terms), pickup and delivery dates in an impossible order or differing
between documents, and one invoice number on different invoices. Each
finding has a code, a severity, a message and the source files; no
value is chosen or changed.

No PDFs, no index and no Ollama are involved.

Run from the project root:
    python -m pytest tests/test_cross_document_checks.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_extraction as de
from document_extraction import BOL, EXTRACTED, INVOICE, POD, RATE, DocumentExtraction, ExtractedField


def document(document_type, filename, sha=None, **values):

    return DocumentExtraction(
        filename=filename,
        sha256=sha or filename,
        document_type=document_type,
        status=de.S_EXTRACTED,
        fields={name: ExtractedField(name=name, state=EXTRACTED, value=value) for name, value in values.items()},
    )


def shipment(**changes):
    """BOL, POD, invoice and rate confirmation that agree, with changes per document."""

    docs = {
        "bol": dict(shipment_id="SHJ-2026-001", pickup_date="2026-10-01", expected_delivery_date="2026-10-03"),
        "pod": dict(shipment_id="SHJ-2026-001", delivery_date="2026-10-03"),
        "invoice": dict(shipment_id="SHJ-2026-001", invoice_number="INV-1", invoice_total=1580.0, payment_terms="Net 30"),
        "rate": dict(shipment_id="SHJ-2026-001", pickup_date="2026-10-01", agreed_rate=1400.0, fuel_surcharge=180.0,
                     payment_terms="Net 30"),
    }

    for name, values in changes.items():
        docs[name].update(values)

    return [
        document(BOL, "BOL.pdf", **docs["bol"]),
        document(POD, "POD.pdf", **docs["pod"]),
        document(INVOICE, "INVOICE.pdf", **docs["invoice"]),
        document(RATE, "RATE.pdf", **docs["rate"]),
    ]


def codes(extractions):
    """The findings by code; the existing info finding for invoices without line items is left out."""

    return {
        issue["code"]: issue
        for issue in de.validate_shipment(extractions)
        if issue["code"] != "invoice_arithmetic_unchecked"
    }


def test_documents_that_agree_have_no_new_findings():

    assert codes(shipment()) == {}


def test_colliding_shipment_ids():

    found = codes(shipment(bol={"shipment_id": "AB-12-3"}, pod={"shipment_id": "AB-12-3"},
                           invoice={"shipment_id": "AB-1-23"}, rate={"shipment_id": "AB-12-3"}))

    assert found["shipment_id_collision"]["severity"] == "error"
    assert "AB-12-3" in found["shipment_id_collision"]["message"] and "AB-1-23" in found["shipment_id_collision"]["message"]
    assert found["shipment_id_collision"]["sources"] == ["BOL.pdf", "INVOICE.pdf", "POD.pdf", "RATE.pdf"]


def test_legitimate_id_variants_do_not_collide():

    found = codes(shipment(pod={"shipment_id": "shj 2026 001"}, invoice={"shipment_id": "SHJ2026001"}))

    assert "shipment_id_collision" not in found and "shipment_mismatch" not in found


def test_invoice_above_the_rate_confirmation():

    issue = codes(shipment(invoice={"invoice_total": 1655.0}))["invoice_above_rate_confirmation"]

    assert issue["severity"] == "warning"
    assert "1655.00" in issue["message"] and "75.00 above" in issue["message"] and "1580.00" in issue["message"]
    assert "agreed rate plus fuel surcharge" in issue["message"]
    assert issue["sources"] == ["INVOICE.pdf", "RATE.pdf"]


def test_invoice_below_the_rate_confirmation_is_information():

    issue = codes(shipment(invoice={"invoice_total": 1500.0}))["invoice_below_rate_confirmation"]

    assert issue["severity"] == "info"
    assert "80.00 below" in issue["message"]


def test_rate_without_a_separate_fuel_surcharge():

    rate = shipment()[3]
    del rate.fields["fuel_surcharge"]

    issue = codes(shipment()[:3] + [rate])["invoice_above_rate_confirmation"]

    assert "180.00 above" in issue["message"] and "agreed rate (1400.00" in issue["message"]


def test_no_amount_comparison_without_both_amounts():

    assert codes(shipment()[:3]) == {}  # no rate confirmation


def test_different_payment_terms():

    issue = codes(shipment(rate={"payment_terms": "Net 15"}))["payment_terms_mismatch"]

    assert "Net 30 (INVOICE.pdf)" in issue["message"] and "Net 15 (RATE.pdf)" in issue["message"]


def test_payment_terms_that_differ_only_in_case_agree():

    assert codes(shipment(rate={"payment_terms": "NET 30"})) == {}


def test_delivery_before_pickup():

    issue = codes(shipment(pod={"delivery_date": "2026-09-28"}))["delivery_before_pickup"]

    assert issue["severity"] == "error"
    assert "POD delivery date 2026-09-28 is before the pickup date 2026-10-01" in issue["message"]


def test_expected_delivery_before_pickup():

    issue = codes(shipment(bol={"expected_delivery_date": "2026-09-30"}))["delivery_before_pickup"]

    assert "BOL expected delivery date 2026-09-30" in issue["message"]


def test_pickup_dates_that_differ():

    found = codes(shipment(rate={"pickup_date": "2026-10-02"}))

    assert "2026-10-01 (BOL.pdf)" in found["pickup_date_mismatch"]["message"]
    assert "delivery_before_pickup" not in found


def test_one_invoice_number_on_different_invoices():

    docs = shipment() + [document(INVOICE, "INVOICE_copy2.pdf", invoice_number="inv-1", invoice_total=1580.0,
                                  shipment_id="SHJ-2026-001")]

    issue = codes(docs)["duplicate_invoice_number"]

    assert issue["sources"] == ["INVOICE.pdf", "INVOICE_copy2.pdf"]
    assert "may be billed twice" in issue["message"]


def test_identical_invoice_files_stay_a_duplicate_document_only():

    docs = shipment() + [document(INVOICE, "INVOICE_again.pdf", sha="INVOICE.pdf", invoice_number="INV-1",
                                  invoice_total=1580.0, shipment_id="SHJ-2026-001")]

    found = codes(docs)

    assert "duplicate_invoice_number" not in found
    assert found["duplicate_document"]["sources"] == ["INVOICE.pdf", "INVOICE_again.pdf"]


def test_stored_payload_round_trip():

    original = shipment()[2]

    rebuilt = DocumentExtraction.from_dict(original.to_dict())

    assert rebuilt == original
    assert rebuilt.value("invoice_total") == 1580.0


# ============================================================
# API
# ============================================================

def test_api_lists_the_shipments_cross_document_findings(tmp_path):

    from test_api import Rig

    rig = Rig(tmp_path, "required")

    for kind in ("BOL", "POD", "INVOICE"):
        reply = rig.client("alice").post("/api/extractions/run", json={"document": f"{kind}_SHJ-2026-001.pdf"})
        assert reply.status_code == 200, reply.text

    body = rig.client("alice").get("/api/extractions", params={"shipment_id": "shj 2026 001"}).json()

    issues = body["cross_document_issues"]

    # The sample invoice's line items do not add up to its total
    assert "invoice_arithmetic_mismatch" in {issue["code"] for issue in issues}
    assert all({"code", "severity", "message", "sources"} <= set(issue) for issue in issues)

    # Only for one shipment's documents
    assert "cross_document_issues" not in rig.client("alice").get("/api/extractions").json()
