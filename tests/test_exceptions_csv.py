"""
The legacy exceptions CSV (/api/reports/exceptions.csv and the Streamlit
export): evidence is referenced (file, page, document type), never quoted, and
every cell is protected against formula injection, while the structured
discrepancy fields stay as they were.
"""

import csv
import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_analytics as analytics  # noqa: E402
from test_api import Rig  # noqa: E402

PASSAGE = "Consignee account 4111 1111 1111 1111, contact jane.doe@example.com, total due 9,999.00"


def parse(data):

    text = data.decode("utf-8-sig")

    return list(csv.DictReader(io.StringIO(text))), text


def exception(**changes):

    item = {
        "exception_key": "SHJ-1|invoice", "shipment_key": "SHJ-1", "shipment_id": "SHJ-1", "kind": "discrepancy",
        "category": "Invoice mismatch", "severity": "high", "description": "Invoice total differs from the contract.",
        "difference": -30.0, "unit": "USD", "review_status": "open", "needs_recheck": False, "reviewer": None,
        "review_note": None, "review_updated": None,
        "evidence": analytics.evidence_references([
            {"source": "invoice.pdf", "page": 2, "document_type": "INVOICE", "text": PASSAGE},
            {"source": "rate confirmation.pdf", "page": 1, "document_type": "RATE_CONFIRMATION", "text": "=1+2 rate 1,370.00"},
            {"source": "invoice.pdf", "page": 2, "document_type": "INVOICE", "text": "another line of the same page"},
        ]),
    }
    item.update(changes)

    return item


def test_evidence_is_referenced_never_quoted():

    rows, text = parse(analytics.exceptions_csv([exception()]))

    assert rows[0]["evidence"] == "invoice.pdf p.2 (INVOICE); rate confirmation.pdf p.1 (RATE_CONFIRMATION)"

    for fragment in ("4111", "jane.doe@example.com", "9,999.00", "1,370.00", "another line", "=1+2"):
        assert fragment not in text, fragment


def test_text_that_is_not_a_reference_is_left_out():
    """A bare string could be document text itself: it is never exported."""

    rows, text = parse(analytics.exceptions_csv([exception(evidence=[PASSAGE, "", "bol.pdf p.? (NOT AVAILABLE):"])]))

    assert rows[0]["evidence"] == "bol.pdf p.? (NOT AVAILABLE)"
    assert "4111" not in text


def test_the_structured_discrepancy_fields_are_kept():

    rows, _ = parse(analytics.exceptions_csv([exception()]))

    assert list(rows[0]) == list(analytics.EXCEPTION_COLUMNS)
    assert (rows[0]["shipment_id"], rows[0]["kind"], rows[0]["category"], rows[0]["severity"]) == (
        "SHJ-1", "discrepancy", "Invoice mismatch", "high")
    assert (rows[0]["difference"], rows[0]["difference_unit"], rows[0]["review_status"]) == ("-30.00", "USD", "open")
    assert rows[0]["description"] == "Invoice total differs from the contract."


def test_every_cell_is_protected_against_formula_injection():

    hostile = {
        "shipment_id": "=HYPERLINK(\"http://x\",\"open\")",
        "description": "+SUM(A1:A9)",
        "reviewer": "@evil",
        "review_note": "-2+3",
        "category": "＝cmd|' /C calc'!A0",
        "severity": "\tTAB",
        "review_updated": "\rCR",
        "evidence": analytics.evidence_references([{"source": "=evil.pdf", "page": 1, "document_type": "BOL", "text": "x"}]),
    }
    rows, _ = parse(analytics.exceptions_csv([exception(**hostile)]))
    row = rows[0]

    assert row["shipment_id"] == "'=HYPERLINK(\"http://x\",\"open\")"
    assert row["description"] == "'+SUM(A1:A9)"
    assert row["reviewer"] == "'@evil"
    assert row["review_note"] == "'-2+3"
    assert row["category"].startswith("'＝")
    assert row["severity"] == "'\tTAB"
    assert row["evidence"] == "'=evil.pdf p.1 (BOL)"
    # A plain negative number stays a number
    assert row["difference"] == "-30.00"


def test_the_api_export_quotes_no_document_text(tmp_path):

    rig = Rig(tmp_path, "required")
    reviewer = rig.client("bob")
    detected = reviewer.get("/api/exceptions").json()["exceptions"]

    reply = reviewer.get("/api/reports/exceptions.csv")
    assert reply.status_code == 200
    rows, text = parse(reply.content)

    passages = [item.split("): ", 1)[1] for row in detected for item in row["evidence"] if "): " in item]
    passages = [passage for passage in passages if len(passage.strip()) >= 12]

    assert passages, "the sample documents give exceptions with quoted evidence"
    assert not [passage for passage in passages if passage in text]
    assert any(row["evidence"] for row in rows)
    assert all(analytics.evidence_reference(part) == part
               for row in rows for part in row["evidence"].split("; ") if part)
