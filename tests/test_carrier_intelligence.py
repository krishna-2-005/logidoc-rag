"""
Carrier intelligence: carrier identity, delivery, money, documentation, audit
and exception figures per carrier, and that every aggregate reconciles with
the shipments it was made from.

The unit tests build the audit's own structures by hand (a snapshot item with
its reconciliation result and freight audit inputs, the exceptions with their
review state, the completeness rows); the last tests run real generated PDFs
through the API.

Run from the project root:
    python -m pytest tests/test_carrier_intelligence.py
"""

import csv
import io
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import carrier_intelligence as ci
from trend_analytics import NOT_STATED


# ============================================================
# BUILDING THE AUDIT'S STRUCTURES
# ============================================================

def field(value):

    return {"state": "EXTRACTED", "value": value, "page": 1, "corrected": False}


def shipment(sid, carrier="Swift Lines", pickup="2026-10-01", delivered="2026-10-03", status="ON TIME", days_late=0,
             total=1580.0, currency=None, arithmetic="MATCH", difference=0.0, contract=None, outcome=None,
             documents=("BOL", "POD", "INVOICE"), carriers=None, success=True, invoice_date=None):
    """One snapshot item. carriers: {document type: carrier} to make documents disagree."""

    carriers = carriers or {}
    inputs = []

    if "BOL" in documents:
        fields = {"carrier": field(carriers.get("BOL", carrier))} if carriers.get("BOL", carrier) else {}
        if pickup:
            fields["pickup_date"] = field(pickup)
        inputs.append({"file": f"BOL_{sid}.pdf", "document_type": "BOL", "fields": fields})

    if "POD" in documents:
        fields = {"carrier": field(carriers.get("POD", carrier))} if carriers.get("POD", carrier) else {}
        if delivered:
            fields["delivery_date"] = field(delivered)
        inputs.append({"file": f"POD_{sid}.pdf", "document_type": "POD", "fields": fields})

    if "INVOICE" in documents:
        fields = {"carrier": field(carriers.get("INVOICE", carrier))} if carriers.get("INVOICE", carrier) else {}
        if total is not None:
            fields["invoice_total"] = field(total)
        if currency:
            fields["currency"] = field(currency)
        if invoice_date:
            fields["invoice_date"] = field(invoice_date)
        inputs.append({"file": f"INVOICE_{sid}.pdf", "document_type": "INVOICE", "fields": fields})

    charges = []

    if contract is not None:
        inputs.append({"file": f"RATE_{sid}.pdf", "document_type": "RATE_CONFIRMATION", "fields": {"carrier": field(carrier)}})
        charges.append({"charge": "Invoice total against the contract", "expected": contract, "billed": total,
                        "variance": None if total is None else round(total - contract, 2)})

    key = sid.replace("-", "")

    return {
        "shipment_id": sid,
        "shipment_key": key,
        "result": {
            "success": success,
            "final_status": "NO DISCREPANCIES" if success else None,
            "delivery": {"status": status, "days_late": days_late if status in ("ON TIME", "EARLY", "DELAYED") else None},
            "invoice": {"status": arithmetic, "difference": difference},
            "carrier": {"bol": carrier, "invoice": carrier, "pod": carrier},
        },
        "freight_audit": {
            "inputs": inputs,
            "documents": {"contract": [f"RATE_{sid}.pdf"] if contract is not None else [], "execution": [], "invoice": []},
            "charges": charges,
            "outcome": outcome,
        },
    }


def complete(item, missing=()):

    return {"shipment_key": item["shipment_key"], "missing": list(missing)}


def exception(item, category, status="open", amount=None, currency=None, severity="HIGH"):

    return {
        "exception_key": f"{item['shipment_key']}|{category.lower().replace(' ', '-')}",
        "shipment_key": item["shipment_key"],
        "category": category,
        "severity": severity,
        "review_status": status,
        "financial_impact": {"amount": amount, "currency": currency, "basis": "test"} if amount is not None else None,
    }


def run(items, exceptions=(), missing=None, **kwargs):

    missing = missing or {}
    rows = [complete(item, missing.get(item["shipment_id"], ())) for item in items]

    return ci.analyze(items, list(exceptions), rows, **kwargs)


def card(result, name):

    return next(c for c in result["carriers"] if c["carrier"] == name)


# ============================================================
# CARRIER IDENTITY
# ============================================================

def test_shipments_of_one_carrier_are_one_carrier():

    result = run([shipment("S-1"), shipment("S-2"), shipment("S-3", carrier="Blue Ridge Haulers")])

    assert [c["carrier"] for c in result["carriers"]] == ["Blue Ridge Haulers", "Swift Lines"]
    assert card(result, "Swift Lines")["shipments"] == 2


def test_case_and_spacing_do_not_make_a_new_carrier():

    result = run([shipment("S-1", carrier="Swift Lines"), shipment("S-2", carrier="swift  lines"),
                  shipment("S-3", carrier=" SWIFT LINES "), shipment("S-4", carrier="Swift Lines")])

    assert len(result["carriers"]) == 1
    only = result["carriers"][0]
    assert only["shipments"] == 4
    # The form written most often is its name; every form seen is kept
    assert only["carrier"] == "Swift Lines"
    assert only["names"] == ["SWIFT LINES", "Swift Lines", "swift lines"]


def test_similar_names_stay_different_carriers_and_are_listed_for_review():

    result = run([shipment("S-1", carrier="Swift Lines, Inc."), shipment("S-2", carrier="Swift Lines Inc"),
                  shipment("S-3", carrier="Swift Logistics")])

    assert sorted(c["carrier"] for c in result["carriers"]) == ["Swift Lines Inc", "Swift Lines, Inc.", "Swift Logistics"]
    assert result["possible_duplicates"] == [["Swift Lines Inc", "Swift Lines, Inc."]]


def test_a_shipment_whose_documents_name_different_carriers_is_not_attributed():

    result = run([shipment("S-1"), shipment("S-2", carriers={"INVOICE": "Blue Ridge Haulers"})])

    assert card(result, "Swift Lines")["shipments"] == 1
    assert result["unattributed"]["conflicting"] == [{"shipment_id": "S-2", "names": ["Blue Ridge Haulers", "Swift Lines"]}]
    assert "Blue Ridge Haulers" not in [c["carrier"] for c in result["carriers"]]


def test_a_shipment_without_a_carrier_is_not_stated():

    result = run([shipment("S-1", carrier=None)])

    assert result["carriers"] == []
    assert result["unattributed"]["not_stated"] == ["S-1"]
    assert result["totals"]["shipments"] == 1


# ============================================================
# DELIVERY
# ============================================================

def test_on_time_rate_counts_only_measured_deliveries():

    result = run([
        shipment("S-1", status="ON TIME", days_late=0),
        shipment("S-2", status="EARLY", days_late=-1),
        shipment("S-3", status="DELAYED", days_late=2),
        shipment("S-4", status="DELAYED", days_late=4),
        shipment("S-5", status="NEEDS REVIEW"),                    # dates missing or conflicting
        shipment("S-6", pickup=None, status="ON TIME"),            # no BOL pickup date
        shipment("S-7", success=False, delivered=None),            # not reconciled
    ])

    d = card(result, "Swift Lines")["delivery"]

    assert (d["measured"], d["on_time"], d["late"]) == (4, 2, 2)
    assert d["on_time_rate"]["count"] == 2 and d["on_time_rate"]["n"] == 4 and d["on_time_rate"]["rate"] == 0.5
    assert d["average_delay_days"] == 3.0                         # (2 + 4) / 2, late deliveries only
    assert d["completed"] == 6                                    # every POD with a delivery date
    assert d["not_measured"] == {"delivery dates missing, unreadable or conflicting": 1, "no BOL pickup date": 1,
                                 "not reconciled": 1}


def test_no_measured_delivery_is_no_rate_not_zero():

    d = card(run([shipment("S-1", status="NEEDS REVIEW")]), "Swift Lines")["delivery"]

    assert d["on_time_rate"]["rate"] is None and d["on_time_rate"]["n"] == 0
    assert d["average_delay_days"] is None


# ============================================================
# MONEY
# ============================================================

def test_financial_figures_per_currency():

    result = run([
        shipment("S-1", total=1600.0, currency="USD", contract=1580.0, outcome="DISCREPANCY"),
        shipment("S-2", total=1500.0, currency="USD", contract=1500.0, outcome="MATCHED"),
        shipment("S-3", total=900.0, currency="USD"),                       # no contract
        shipment("S-4", total=2000.0, currency="CAD"),
        shipment("S-5", total=700.0),                                      # currency not stated
        shipment("S-6", total=None, currency="USD"),                       # total not read
    ])

    money = {row["currency"]: row for row in card(result, "Swift Lines")["financial"]["by_currency"]}

    assert list(money) == ["CAD", "USD", NOT_STATED]
    usd = money["USD"]
    assert (usd["invoices_with_total"], usd["invoiced_total"]) == (3, 4000.0)
    assert (usd["invoices_compared_with_contract"], usd["contracted_total"], usd["audited_billed_total"]) == (2, 3080.0, 3100.0)
    assert usd["verified_variance"] == 20.0
    assert usd["average_invoice_variance"] == 10.0                 # (|20| + |0|) / 2
    assert usd["cost_per_shipment"] == round(4000 / 3, 2)
    assert money["CAD"]["invoiced_total"] == 2000.0 and money["CAD"]["contracted_total"] is None
    assert money[NOT_STATED]["invoiced_total"] == 700.0
    assert card(result, "Swift Lines")["financial"]["invoices_total_not_read"] == 1


def test_currencies_are_never_added_together():

    result = run([shipment("S-1", total=100.0, currency="USD"), shipment("S-2", total=100.0, currency="EUR")])

    assert {row["currency"]: row["invoiced_total"] for row in result["totals"]["financial"]["by_currency"]} == {
        "EUR": 100.0, "USD": 100.0}


def test_disputed_and_waived_amounts_are_kept_apart():

    a, b, c = shipment("S-1"), shipment("S-2"), shipment("S-3")
    exceptions = [
        exception(a, "Freight audit: Fuel surcharge", "disputed", 20.0, "USD"),
        exception(b, "Freight audit: Detention Charge", "waived", 75.0, "USD"),
        exception(b, "Freight audit: Lumper Fee", "waived", 50.0, "USD"),
        exception(c, "Invoice mismatch", "open", 120.0, None),
        exception(c, "Quantity shortage", "open"),                          # no amount: not counted as 0
        exception(a, "Delivery delay", "resolved", None),
    ]

    amounts = card(run([a, b, c], exceptions), "Swift Lines")["exceptions"]["amounts"]

    assert amounts["disputed"] == [{"currency": "USD", "amount": 20.0, "exceptions": 1}]
    assert amounts["waived"] == [{"currency": "USD", "amount": 125.0, "exceptions": 2}]
    assert amounts["open"] == [{"currency": NOT_STATED, "amount": 120.0, "exceptions": 1}]
    assert amounts["resolved"] == []


# ============================================================
# DOCUMENTS, AUDIT, EXCEPTIONS
# ============================================================

def test_document_compliance():

    items = [shipment("S-1"), shipment("S-2", documents=("BOL", "INVOICE")), shipment("S-3", documents=("POD",)),
             shipment("S-4")]
    missing = {"S-2": ["POD"], "S-3": ["BOL", "INVOICE"]}

    docs = card(run(items, missing=missing), "Swift Lines")["documents"]

    assert (docs["shipments"], docs["complete"], docs["missing_pod"], docs["missing_bol"], docs["missing_invoice"]) == (4, 2, 1, 1, 1)
    assert docs["completeness_rate"]["rate"] == 0.5 and docs["completeness_rate"]["n"] == 4


def test_matched_and_discrepant_invoices_and_the_pass_rate_denominator():

    items = [
        shipment("S-1", contract=1580.0, outcome="MATCHED"),
        shipment("S-2", contract=1580.0, outcome="MATCHED_WITH_TOLERANCE", total=1585.0),
        shipment("S-3", contract=1500.0, outcome="DISCREPANCY"),
        shipment("S-4", contract=1580.0, outcome="PENDING_REVIEW"),
        shipment("S-5", contract=1580.0, outcome="DUPLICATE"),
        shipment("S-6", arithmetic="MISMATCH", difference=120.0),          # no contract
        shipment("S-7", arithmetic="NEEDS REVIEW"),
    ]

    audit = card(run(items), "Swift Lines")["audit"]

    assert (audit["invoices"], audit["audited_against_contract"], audit["without_contract"]) == (7, 5, 2)
    assert (audit["matched"], audit["discrepant"], audit["pending_review"], audit["duplicate"]) == (2, 1, 1, 1)
    assert audit["audit_pass_rate"]["count"] == 2 and audit["audit_pass_rate"]["n"] == 5
    assert (audit["arithmetic_checked"], audit["arithmetic_match"], audit["arithmetic_mismatch"], audit["arithmetic_needs_review"]) == (6, 5, 1, 1)
    assert audit["invoice_accuracy_rate"]["rate"] == 5 / 6


def test_repeated_exceptions_are_found():

    a, b, c = shipment("S-1"), shipment("S-2"), shipment("S-3")
    exceptions = [exception(a, "Delivery delay"), exception(b, "Delivery delay"), exception(c, "Quantity shortage")]

    e = card(run([a, b, c], exceptions), "Swift Lines")["exceptions"]

    assert e["repeated"] == [{"category": "Delivery delay", "shipments": ["S-1", "S-2"]}]
    assert e["total"] == 3 and e["exceptions_per_shipment"] == 1.0
    assert e["by_category"] == {"Delivery delay": 2, "Quantity shortage": 1}


# ============================================================
# EMPTY DATA AND DATE RANGES
# ============================================================

def test_no_data():

    result = ci.analyze([], [], [])

    assert result["carriers"] == [] and result["possible_duplicates"] == []
    assert result["totals"]["shipments"] == 0
    assert result["totals"]["delivery"]["on_time_rate"]["rate"] is None
    assert result["totals"]["documents"]["completeness_rate"]["rate"] is None
    assert result["totals"]["exceptions"]["exceptions_per_shipment"] is None
    assert ci.carrier_detail([], [], [], "swift lines") is None


def test_date_range():

    items = [
        shipment("S-1", pickup="2026-09-15"),
        shipment("S-2", pickup="2026-10-01"),
        shipment("S-3", pickup=None, delivered="2026-10-20"),             # dated by its delivery
        shipment("S-4", pickup=None, delivered=None, invoice_date=None),  # undated
    ]

    october = run(items, start=date(2026, 10, 1), end=date(2026, 10, 31))

    assert card(october, "Swift Lines")["shipments"] == 2
    assert october["unattributed"]["undated_excluded"] == ["S-4"]
    assert october["period"] == {"start": "2026-10-01", "end": "2026-10-31"}

    assert card(run(items), "Swift Lines")["shipments"] == 4               # no range: everything
    assert card(run(items, start=date(2026, 10, 2)), "Swift Lines")["shipments"] == 1


def test_monthly_trend():

    items = [shipment("S-1", pickup="2026-09-15", status="DELAYED", days_late=1, total=100.0, currency="USD"),
             shipment("S-2", pickup="2026-10-01", total=200.0, currency="USD"),
             shipment("S-3", pickup="2026-10-02", total=300.0, currency="USD")]

    detail = ci.carrier_detail(items, [], [complete(i) for i in items], "swift lines")

    assert [(m["month"], m["shipments"], m["on_time"], m["late"]) for m in detail["monthly"]] == [
        ("2026-09", 1, 0, 1), ("2026-10", 2, 2, 0)]
    assert detail["monthly"][1]["invoiced"] == [{"currency": "USD", "total": 500.0, "invoices": 2}]

    # The overview has the same trend over every carrier
    overall = ci.analyze(items, [], [complete(i) for i in items])["monthly"]
    assert [(m["month"], m["shipments"]) for m in overall] == [("2026-09", 1), ("2026-10", 2)]


# ============================================================
# AGGREGATES RECONCILE WITH THE RECORDS
# ============================================================

def test_carrier_figures_add_up_to_the_totals():

    items = [shipment(f"S-{n}", carrier=name, total=100.0 * n, currency="USD",
                      status="DELAYED" if n % 3 == 0 else "ON TIME", days_late=2 if n % 3 == 0 else 0)
             for n, name in enumerate(["Swift Lines", "Blue Ridge Haulers", "Swift Lines", "Pacific Crest", "Blue Ridge Haulers", "Swift Lines"], 1)]
    items.append(shipment("S-7", carriers={"INVOICE": "Other Carrier"}))
    items.append(shipment("S-8", carrier=None))

    result = run(items)
    cards = result["carriers"]

    attributed = sum(c["shipments"] for c in cards)
    assert attributed + len(result["unattributed"]["not_stated"]) + len(result["unattributed"]["conflicting"]) \
        == result["totals"]["shipments"] == len(items)

    for name in ("measured", "on_time", "late"):
        assert sum(c["delivery"][name] for c in cards) <= result["totals"]["delivery"][name]

    usd = lambda figures: next(r for r in figures["financial"]["by_currency"] if r["currency"] == "USD")["invoiced_total"]  # noqa: E731
    assert sum(usd(c) for c in cards) == sum(100.0 * n for n in range(1, 7))

    swift = ci.carrier_detail(items, [], [complete(i) for i in items], "swift lines")
    assert [row["shipment_id"] for row in swift["shipment_rows"]] == ["S-1", "S-3", "S-6"]
    assert swift["shipments"] == swift["shipment_count"] == 3


def test_the_shipment_page_is_paginated():

    items = [shipment(f"S-{n:02d}") for n in range(1, 8)]

    page = ci.carrier_detail(items, [], [complete(i) for i in items], "swift lines", offset=5, limit=5)

    assert page["shipment_count"] == 7 and [row["shipment_id"] for row in page["shipment_rows"]] == ["S-06", "S-07"]


# ============================================================
# EXPORT
# ============================================================

def test_the_csv_has_one_row_per_carrier_and_currency_and_never_a_zero_for_missing():

    result = run([shipment("S-1", total=100.0, currency="USD"), shipment("S-2", total=50.0, currency="EUR"),
                  shipment("S-3", carrier="=HYPERLINK(\"x\")", total=None, status="NEEDS REVIEW")])

    rows = list(csv.DictReader(io.StringIO(ci.carriers_csv(result).decode("utf-8-sig"))))

    swift = [row for row in rows if row["carrier"] == "Swift Lines"]
    assert sorted(row["currency"] for row in swift) == ["EUR", "USD"]

    other = next(row for row in rows if "HYPERLINK" in row["carrier"])
    assert not other["carrier"].startswith("=")                          # no spreadsheet formula
    assert other["on_time_rate"] == "NOT AVAILABLE" and other["invoiced_total"] == "NOT AVAILABLE"


# ============================================================
# THROUGH THE API, ON REAL GENERATED PDFS
# ============================================================

@pytest.fixture
def rig(tmp_path):

    from test_api import Rig
    from test_freight_audit import write_contracted_shipment

    result = Rig(tmp_path, "required")
    write_contracted_shipment(result.raw)
    result.state.service.invalidate()

    return result


def test_the_api_reconciles_with_the_shipment_list(rig):

    client = rig.client("bob")
    body = client.get("/api/carriers").json()
    shipments = client.get("/api/shipments").json()

    listed = shipments["shipments"] if isinstance(shipments, dict) else shipments
    unattributed = body["unattributed"]

    assert sum(c["shipments"] for c in body["carriers"]) + len(unattributed["not_stated"]) \
        + len(unattributed["conflicting"]) == len(listed) == body["totals"]["shipments"]

    swift = next(c for c in body["carriers"] if c["carrier_key"] == "swift lines")
    assert swift["audit"]["audited_against_contract"] == 1 and swift["audit"]["discrepant"] == 1
    assert swift["financial"]["by_currency"][0]["contracted_total"] == 1580.0
    assert swift["financial"]["by_currency"][0]["verified_variance"] == 95.0     # 1,675 billed - 1,580 contracted

    detail = client.get("/api/carriers/swift lines").json()
    assert "SHJ-2026-101" in [row["shipment_id"] for row in detail["shipment_rows"]]
    assert detail["identifier"] is None and "not extracted" in detail["identity_note"]


def test_the_api_validates_its_period_and_permissions(rig):

    assert rig.client("bob").get("/api/carriers", params={"start": "2026-11-01", "end": "2026-10-01"}).status_code == 422
    assert rig.client("bob").get("/api/carriers", params={"start": "not-a-date"}).status_code == 422
    assert rig.client(None).get("/api/carriers").status_code == 401
    assert rig.client("bob").get("/api/carriers/no such carrier").status_code == 404

    report = rig.client("bob").get("/api/reports/carriers.csv")
    assert report.status_code == 200 and "attachment" in report.headers["content-disposition"]
    assert report.content.decode("utf-8-sig").splitlines()[0].startswith("carrier,names,shipments")


def test_a_period_without_shipments_is_empty_not_an_error(rig):

    body = rig.client("bob").get("/api/carriers", params={"start": "2001-01-01", "end": "2001-12-31"}).json()

    assert body["carriers"] == [] and body["totals"]["shipments"] == 0
