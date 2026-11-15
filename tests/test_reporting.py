"""
Freight audit and carrier reporting: totals, currency separation, filters and
their descriptions, provenance, CSV formula injection, PDF rendering, export
authorization and empty reports (docs/REPORTING.md).

Synthetic PDFs and synthetic report inputs in temporary folders; no network.

Run from the project root:
    python -m pytest tests/test_reporting.py
"""

import csv
import io
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pymupdf
import pytest
from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import reporting  # noqa: E402
import risk_analytics as risk  # noqa: E402
from api import routes_reporting  # noqa: E402
from auth import UserStore  # noqa: E402
from authz import ADMIN, Principal  # noqa: E402
from test_api import PASSWORD, Rig  # noqa: E402
from test_freight_audit import write_contracted_shipment  # noqa: E402

NOW = datetime(2026, 11, 15, 9, 30, tzinfo=timezone.utc)


# ============================================================
# SYNTHETIC INPUTS
# ============================================================

def fact(number, currency, total, carrier="Alpha Freight", day=date(2026, 10, 5), outcome=None, variance=None):

    key = f"SYN{number:03d}"

    return {
        "shipment_id": f"SYN-{number:03d}", "shipment_key": key, "carrier_key": carrier.casefold(), "carrier": carrier,
        "carrier_names": [carrier], "date": day, "date_basis": "pickup" if day else None, "final_status": "NO DISCREPANCIES",
        "completed": True, "delivery_status": "ON TIME", "measured": True, "not_measured_reason": None, "days_late": 0,
        "invoices": 1, "invoice_total": total, "currency": currency, "arithmetic_status": "MATCH", "arithmetic_difference": 0,
        "has_contract": variance is not None, "freight_outcome": outcome, "contracted": None if variance is None else total - variance,
        "contract_variance": variance, "complete": True, "missing": [], "exceptions": [],
    }


def inputs(facts, exceptions=(), outcomes=None):

    shipments = [{"shipment_id": row["shipment_id"], "shipment_key": row["shipment_key"], "result": {"success": True},
                  "freight_audit": {"outcome": (outcomes or {}).get(row["shipment_key"])}} for row in facts]
    exceptions = list(exceptions)
    rows = risk.assess(shipments, exceptions, [{"shipment_key": row["shipment_key"], "missing": []} for row in facts], facts)

    return reporting.Inputs(shipments, exceptions, [], facts, rows, risk.DEFAULT_MODEL, NOW)


def exception(number, code, status="open", amount=None, currency="USD", description="Billed above the contract."):

    key = f"SYN{number:03d}"

    return {"exception_key": f"{key}|{code}", "shipment_id": f"SYN-{number:03d}", "shipment_key": key, "kind": risk.DISCREPANCY,
            "category": "Freight audit: fuel", "severity": "HIGH", "description": description, "review_status": status,
            "financial_impact": {"amount": amount, "currency": currency, "basis": "billed above the contract"} if amount else None,
            "audit_outcome": "DISCREPANCY", "evidence": ["INVOICE_SYN.pdf p.1 (INVOICE): Fuel Surcharge: $200.00"]}


def section(report, title):

    return {item["label"]: item for item in next(part for part in report["totals"] if part["title"] == title)["rows"]}


# ============================================================
# TOTALS AND CURRENCIES
# ============================================================

def test_amounts_are_totalled_per_currency_and_never_added_across():

    report = reporting.build("freight-audit-summary", inputs([
        fact(1, "USD", 1000.0, variance=20.0), fact(2, "USD", 500.5), fact(3, "EUR", 300.0, variance=-5.0), fact(4, "Not stated", None),
    ]), reporting.Filters())

    invoiced = section(report, "Invoiced, by currency")
    assert invoiced["USD"]["value"] == 1500.5 and invoiced["USD"]["note"] == "2 invoice(s) with a readable total"
    assert invoiced["EUR"]["value"] == 300.0
    assert invoiced["Without a readable total"]["value"] == 1                      # counted apart, never as 0
    assert section(report, "Contract variance (billed - contracted), by currency") == {
        "EUR": {"label": "EUR", "value": -5.0, "display": "EUR -5.00", "note": "1 shipment(s) compared"},
        "USD": {"label": "USD", "value": 20.0, "display": "USD 20.00", "note": "1 shipment(s) compared"},
    }
    assert section(report, "Shipments")["Shipments"]["value"] == len(report["rows"]) == 4
    assert report["rows"][3]["invoice_total"] is None


def test_exception_amounts_are_split_by_status_and_currency():

    report = reporting.build("discrepancies", inputs([fact(1, "USD", 100.0), fact(2, "EUR", 100.0)], [
        exception(1, "a", amount=20.0), exception(1, "b", status="resolved", amount=5.0), exception(2, "c", amount=7.0, currency="EUR"),
        exception(2, "d"),
    ]), reporting.Filters())

    assert section(report, "Amount at stake, every listed exception, by currency")["USD"]["value"] == 25.0
    assert section(report, "Amount at stake, unresolved only, by currency")["USD"]["value"] == 20.0
    assert section(report, "Amount at stake, unresolved only, by currency")["EUR"]["value"] == 7.0
    assert section(report, "Review status") == {
        "open": {"label": "open", "value": 3, "display": "3", "note": None},
        "resolved": {"label": "resolved", "value": 1, "display": "1", "note": None},
    }
    assert any("no determinable amount" in note for note in report["notes"])
    # The evidence names the file and page; the passage it quotes stays out of the report
    assert report["rows"][0]["evidence"] == "INVOICE_SYN.pdf p.1 (INVOICE)"


# ============================================================
# FILTERS
# ============================================================

def test_each_filter_selects_and_is_described():

    data = inputs([fact(1, "USD", 10.0, day=date(2026, 9, 30)), fact(2, "USD", 20.0, carrier="Bravo Lines"),
                   fact(3, "USD", 30.0, day=None), fact(4, "USD", 40.0)],
                  [exception(4, "x", status="disputed")], outcomes={"SYN004": "DISCREPANCY", "SYN002": "MATCHED"})

    dated = reporting.build("freight-audit-summary", data, reporting.Filters(start=date(2026, 10, 1), end=date(2026, 10, 31)))
    assert [row["shipment_id"] for row in dated["rows"]] == ["SYN-002", "SYN-004"]
    assert dated["filters"][0].startswith("The shipment date") and "2026-10-01 to 2026-10-31" in dated["filters"][0]
    assert "1 undated shipment(s) are left out because a date range is set." in dated["notes"]

    def ids(**given):
        return [row["shipment_id"] for row in reporting.build("freight-audit-summary", data, reporting.Filters(**given),
                                                              {"bravo lines": "Bravo Lines"})["rows"]]

    assert ids(carrier="bravo lines") == ["SYN-002"]
    assert ids(shipment="syn-00") == ["SYN-001", "SYN-002", "SYN-003", "SYN-004"] and ids(shipment="004") == ["SYN-004"]
    assert ids(outcomes=("DISCREPANCY",)) == ["SYN-004"] and ids(outcomes=("NOT_RUN",)) == ["SYN-001", "SYN-003"]
    assert ids(statuses=("disputed",)) == ["SYN-004"]
    category = next(row["category"] for row in data.risk_rows if row["shipment_key"] == "SYN004")
    assert "SYN-004" in ids(categories=(category,))

    described = reporting.build("freight-audit-summary", data, reporting.Filters(carrier="bravo lines", outcomes=("MATCHED",)),
                                {"bravo lines": "Bravo Lines"})["filters"]
    assert described == ["Carrier: Bravo Lines", "Freight audit outcome: matched"]
    assert reporting.build("freight-audit-summary", data, reporting.Filters())["filters"] == ["No filter: every shipment"]


def test_a_filter_a_report_cannot_take_is_refused_never_ignored():

    data = inputs([fact(1, "USD", 10.0)])

    with pytest.raises(reporting.ReportError, match="cannot be filtered by carrier"):
        reporting.build("corrections", data, reporting.Filters(carrier="alpha freight"))
    with pytest.raises(reporting.ReportError, match="start date is after"):
        reporting.build("discrepancies", data, reporting.Filters(start=date(2026, 10, 2), end=date(2026, 10, 1)))
    with pytest.raises(reporting.ReportError, match="Unknown report"):
        reporting.build("everything", data, reporting.Filters())


# ============================================================
# FILES
# ============================================================

HOSTILE = '=HYPERLINK("http://attacker.example","click")'


def hostile_report(report="discrepancies"):

    data = inputs([fact(1, "USD", 10.0, carrier=HOSTILE)], [exception(1, "x", amount=5.0, description="+cmd|' /C calc'!A0")])

    return reporting.build(report, data, reporting.Filters())


def test_csv_cells_cannot_run_as_formulas():

    content = reporting.to_csv(hostile_report())
    assert content.startswith("﻿".encode("utf-8"))

    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    cells = [cell for row in rows for cell in row]

    assert f"'{HOSTILE}" in cells and HOSTILE not in cells
    assert "'+cmd|' /C calc'!A0" in cells
    assert ["Report", "Discrepancies and exceptions"] == rows[0] and rows[1][0] == "Generated (UTC)"
    assert any(row[:1] == ["Filter"] for row in rows) and any(row[:1] == ["Source"] for row in rows)
    assert "5.00" in cells and "-5.00" not in cells                              # a plain number stays a number


def test_the_pdf_is_plain_text_with_nothing_embedded_or_linked():

    # The summary prints the carrier column: the hostile name reaches the PDF
    data = reporting.to_pdf(hostile_report("freight-audit-summary"))
    assert data.startswith(b"%PDF")

    with pymupdf.open(stream=data, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
        assert "FREIGHT AUDIT SUMMARY" in text and "Generated 2026-11-15T09:30:00+00:00" in text
        assert "HYPERLINK" in text                                               # shown as text, never as a link
        assert all(not page.get_links() for page in document)
        assert document.embfile_count() == 0
        assert document.metadata["title"] == "Freight audit summary"

    assert b"/JavaScript" not in data and b"/JS" not in data and b"/Launch" not in data and b"/URI" not in data


def test_an_empty_report_says_so_in_every_format():

    report = reporting.build("discrepancies", inputs([]), reporting.Filters())

    assert report["rows"] == [] and section(report, "Exceptions")["Exceptions"]["value"] == 0
    with pymupdf.open(stream=reporting.to_pdf(report), filetype="pdf") as document:
        assert "No row matches the filters." in "".join(page.get_text() for page in document)
    assert reporting.to_csv(report).decode("utf-8-sig").count("Totals") == 1


# ============================================================
# THE API
# ============================================================

@pytest.fixture
def rig(tmp_path):

    result = Rig(tmp_path, "required")
    UserStore(tmp_path / "auth.sqlite3").create_user("dave", PASSWORD, ADMIN)
    write_contracted_shipment(result.raw)
    result.state.service.invalidate()

    return result


def test_the_catalog_and_the_previews(rig):

    bob = rig.client("bob")                                                    # reviewers can report
    catalog = bob.get("/api/reporting/catalog").json()
    assert [item["report"] for item in catalog["reports"]] == ["freight-audit-summary", "discrepancies", "carrier-performance", "corrections"]
    assert {"key": "swift lines", "name": "Swift Lines"} in catalog["options"]["carriers"]

    summary = bob.get("/api/reporting/freight-audit-summary", params={"limit": 2}).json()
    assert summary["total_rows"] == 4 and len(summary["rows"]) == 2 and summary["offset"] == 0
    assert section(summary, "Shipments")["Shipments"]["value"] == 4                # totals cover every row, not the page
    assert summary["generated_at"].endswith("+00:00") and summary["provenance"][0].startswith("The current audit of 4 shipment(s)")
    assert not any("\\" in line or "sqlite3" in line for line in summary["provenance"])

    swift = bob.get("/api/reporting/carrier-performance", params={"carrier": "swift lines"}).json()
    assert [row["carrier"] for row in swift["rows"]] == ["Swift Lines"] and swift["filters"] == ["Carrier: Swift Lines"]

    assert bob.get("/api/reporting/corrections", params={"carrier": "swift lines"}).status_code == 422
    assert bob.get("/api/reporting/everything").status_code == 422
    assert bob.get("/api/reporting/discrepancies", params={"exception_status": "lost"}).status_code == 422


def test_downloads_match_the_preview_and_need_a_signed_in_user(rig):

    alice = rig.client("alice")
    params = {"exception_status": ["open", "disputed"]}
    preview = alice.get("/api/reporting/discrepancies", params={**params, "limit": 500}).json()

    csv_reply = alice.get("/api/reporting/discrepancies.csv", params=params)
    assert csv_reply.status_code == 200 and csv_reply.headers["content-type"].startswith("text/csv")
    assert csv_reply.headers["content-disposition"].startswith('attachment; filename="discrepancies_')
    table = list(csv.reader(io.StringIO(csv_reply.content.decode("utf-8-sig"))))
    header = table.index([column["label"] for column in preview["columns"]])
    assert len(table[header + 1:table.index([], header)]) == preview["total_rows"]

    pdf_reply = alice.get("/api/reporting/discrepancies.pdf", params=params)
    assert pdf_reply.status_code == 200 and pdf_reply.content.startswith(b"%PDF")

    anonymous = rig.client()
    for url in ("/api/reporting/catalog", "/api/reporting/discrepancies", "/api/reporting/discrepancies.csv",
                "/api/reporting/discrepancies.pdf", "/api/v1/reporting/corrections.csv"):
        assert anonymous.get(url).status_code == 401


def test_the_corrections_report_needs_history_view():

    with pytest.raises(HTTPException) as refused:
        routes_reporting.allowed(Principal("guest", "guest", True, 9), "corrections")
    assert refused.value.status_code == 403

    routes_reporting.allowed(Principal("guest", "guest", True, 9), "discrepancies")      # other reports: reports.download only
    routes_reporting.allowed(Principal("bob", "reviewer", True, 2), "corrections")


def test_the_corrections_report_lists_proposals_decisions_and_re_audits(rig):

    alice, bob, dave = rig.client("alice"), rig.client("bob"), rig.client("dave")
    assert alice.post("/api/history/runs", json={"label": "Before"}).status_code == 201
    extraction_id = alice.post("/api/extractions/run", json={"document": "INVOICE_SHJ-2026-101.pdf"}).json()["id"]
    line = next(item for item in alice.get(f"/api/extractions/{extraction_id}").json()["line_items"]
                if item["extracted"]["description"] == "Fuel Surcharge")
    proposal = bob.post(f"/api/extractions/{extraction_id}/line-items/{line['index']}/corrections",
                        json={"amount": "180.00", "reason": "Credit note CN-17 applies.", "based_on": None}).json()
    assert dave.post(f"/api/extractions/corrections/{proposal['id']}/decision", json={"decision": "approve"}).status_code == 200
    token = alice.get("/api/shipments/SHJ-2026-101/re-audit").json()["token"]
    assert alice.post("/api/shipments/SHJ-2026-101/re-audit", json={"token": token, "reason": "Credit note applied."}).status_code == 201

    report = alice.get("/api/reporting/corrections", params={"shipment": "SHJ-2026-101"}).json()
    events = [(row["event"], row["actor"]) for row in report["rows"]]
    assert events == [("Correction proposed", "bob"), ("Correction approved", "dave"), ("Re-audit recorded", "alice")]
    assert report["rows"][0]["subject"] == f"Charge line {line['index'] + 1} of INVOICE_SHJ-2026-101.pdf"
    assert report["rows"][0]["change"] == "Fuel Surcharge: 200.00 -> Fuel Surcharge: 180.00"
    assert report["rows"][2]["note"] == "Credit note applied."
    assert "No TMS integration is configured: no TMS decision is listed." in report["notes"]

    later = alice.get("/api/reporting/corrections", params={"start": "2099-01-01"}).json()
    assert later["rows"] == [] and later["filters"][0].startswith("The date of each event")
