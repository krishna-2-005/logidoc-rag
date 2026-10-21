"""
Tests for src/audit_analytics.py: KPIs, the shipment audit register,
the exception register and the analytics CSV exports.

Shipments are reconciled with the real reconcile_shipment() from
synthetic document chunks (the helpers of
test_reconciliation_consistency: no PDFs, no index, no Ollama).

Run from the project root:
    python -m pytest tests/test_audit_analytics.py
"""

import csv
import io
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_reconciliation_consistency import A, B, documents, drop, reconciliation  # noqa: E402

import audit_analytics as analytics  # noqa: E402
from audit_export import NOT_AVAILABLE  # noqa: E402
from review_store import OPEN, RESOLVED, ReviewStore  # noqa: E402


C = "SHJ-2026-303"
D = "SHJ-2026-404"


def clean(shipment):

    return documents(shipment=shipment, delivered=48, actual="October 3, 2026", total="$1,430.00")


def edit(chunks, doc_type, old, new):
    """
    The chunks with text replaced in one document type.
    """

    return [
        dict(item, text=item["text"].replace(old, new))
        if item["metadata"]["document_type"] == doc_type else item
        for item in chunks
    ]


def snapshot(chunks):

    return analytics.build_snapshot(chunks, reconciliation.reconcile_shipment)


def rows_by_id(chunks):

    return {row["shipment_id"]: row for row in analytics.register_rows(snapshot(chunks))}


def mixed_fleet():
    """
    A: discrepancies (shortage, delay, invoice $120); B: clean;
    C: no POD (cannot be reconciled); D: delivered count missing
    (needs review).
    """

    return (
        documents(shipment=A)
        + clean(B)
        + [item for item in clean(C) if item["metadata"]["document_type"] != "POD"]
        + drop(clean(D), "POD", "Number of Pieces Delivered")
    )


# ============================================================
# KPIs AND NO FALSE CLEAN
# ============================================================

def test_kpis_from_known_results():

    rows = analytics.register_rows(snapshot(mixed_fleet()))

    metrics = analytics.kpis(rows)

    assert metrics == {
        "identified": 4,
        "reconciled": 3,
        "with_discrepancies": 1,
        "needs_review": 2,              # C (not reconciled) and D
        "no_discrepancies": 1,          # B only
        "invoice_total": 1550.0 + 1430.0 + 1430.0,
        "invoice_total_covered": 3,
        "invoice_variance": 120.0,
        "invoice_variance_net": 120.0,
        "invoice_variance_covered": 3,
        "discrepancy_rate": round(100 / 3, 1),
    }


def test_statuses_in_the_register():

    rows = rows_by_id(mixed_fleet())

    assert rows[A]["final_status"] == "DISCREPANCIES DETECTED"
    assert rows[A]["discrepancy_categories"] == ["Delivery Date", "Invoice", "Quantity"]
    assert (rows[A]["invoice_total"], rows[A]["invoice_difference"]) == (1550.0, 120.0)

    assert rows[B]["final_status"] == "NO DISCREPANCIES"
    assert not rows[B]["needs_review"]

    assert rows[C]["final_status"] == "NOT RECONCILED"
    assert rows[C]["error"] == "Reconciliation requires BOL, Invoice and POD."
    assert rows[C]["needs_review"] and not rows[C]["reconciled"]
    assert (rows[C]["invoice_total"], rows[C]["quantity_status"]) == (None, None)

    assert rows[D]["final_status"] == "NEEDS REVIEW"
    assert rows[D]["quantity_status"] == "NEEDS REVIEW"
    assert rows[D]["review_count"] == 1


def test_conflicting_values_are_never_clean():

    # Two BOLs of B with different piece counts
    conflicting = clean(B) + [
        dict(item, text=item["text"].replace("Number of Pieces:\n48", "Number of Pieces:\n50"),
             metadata=dict(item["metadata"], source=f"BOL_{B}_revised.pdf"))
        for item in clean(B) if item["metadata"]["document_type"] == "BOL"
    ]

    row = rows_by_id(conflicting)[B]
    metrics = analytics.kpis([row])

    assert row["final_status"] == "NEEDS REVIEW"
    assert row["needs_review"]
    assert metrics["no_discrepancies"] == 0
    assert metrics["needs_review"] == 1


def test_missing_invoice_figures_are_not_counted_as_zero():

    # B's invoice total cannot be read
    rows = analytics.register_rows(snapshot(documents(shipment=A) + drop(clean(B), "INVOICE", "Total Invoice Amount")))

    metrics = analytics.kpis(rows)

    assert metrics["invoice_total"] == 1550.0
    assert metrics["invoice_total_covered"] == 1
    assert metrics["invoice_variance"] == 120.0
    assert metrics["invoice_variance_covered"] == 1
    assert metrics["no_discrepancies"] == 0


def test_a_discrepancy_with_a_review_item_counts_in_both():

    # Quantity shortage and an unreadable delivery date
    row = rows_by_id(drop(documents(shipment=A), "POD", "Delivery Date"))[A]

    metrics = analytics.kpis([row])

    assert row["final_status"] == "DISCREPANCIES DETECTED"
    assert (metrics["with_discrepancies"], metrics["needs_review"], metrics["no_discrepancies"]) == (1, 1, 0)


def test_empty_dataset():

    empty = snapshot([])

    assert empty == {"shipments": [], "unidentified_sources": []}
    assert analytics.register_rows(empty) == []
    assert analytics.build_exceptions(empty) == []

    metrics = analytics.kpis([])

    assert metrics["identified"] == metrics["reconciled"] == 0
    assert metrics["invoice_total"] is None
    assert metrics["invoice_variance"] is None
    assert metrics["discrepancy_rate"] is None


# ============================================================
# SHIPMENT ISOLATION
# ============================================================

def test_each_shipment_uses_only_its_own_documents():

    shots = snapshot(documents(shipment=A) + clean(B))

    for shipment in shots["shipments"]:

        result = shipment["result"]

        for category in ("quantity", "delivery", "invoice"):
            assert {item["shipment_id"] for item in result[category]["evidence"]} == {shipment["shipment_id"]}

    rows = {row["shipment_id"]: row for row in analytics.register_rows(shots)}

    assert rows[A]["final_status"] == "DISCREPANCIES DETECTED"
    assert rows[B]["final_status"] == "NO DISCREPANCIES"


def test_documents_without_a_shipment_id_are_listed_not_attributed():

    stray = [
        dict(item, metadata=dict(item["metadata"], shipment_id="", source="unlabelled_pod.pdf"))
        for item in documents(shipment=A, delivered=10) if item["metadata"]["document_type"] == "POD"
    ]

    shots = snapshot(clean(B) + stray)

    assert [item["shipment_id"] for item in shots["shipments"]] == [B]
    assert shots["unidentified_sources"] == ["unlabelled_pod.pdf"]
    assert analytics.register_rows(shots)[0]["final_status"] == "NO DISCREPANCIES"


def test_separator_variants_are_one_shipment():

    variant = documents(shipment=A, label="shj 2026 001")

    shots = snapshot(documents(shipment=A)[:1] + variant[1:])

    assert len(shots["shipments"]) == 1
    assert shots["shipments"][0]["shipment_key"] == "SHJ2026001"
    assert shots["shipments"][0]["result"]["success"]


def test_a_result_with_another_shipments_evidence_is_rejected():

    def leaky(shipment_id, chunks=None):

        result = reconciliation.reconcile_shipment(shipment_id, chunks=chunks)
        result["invoice"]["evidence"].append({"source": f"INVOICE_{B}.pdf", "page": 1,
                                             "document_type": "INVOICE", "shipment_id": B, "text": "x"})
        return result

    shots = analytics.build_snapshot(documents(shipment=A), leaky)

    row = analytics.register_rows(shots)[0]

    assert row["final_status"] == "NOT RECONCILED"
    assert "another shipment" in row["error"]


def test_documents_are_read_once(monkeypatch):

    calls = []

    def reconcile(shipment_id, chunks=None):
        calls.append(chunks)
        return reconciliation.reconcile_shipment(shipment_id, chunks=chunks)

    # The snapshot must never go back to the PDFs
    monkeypatch.setattr(reconciliation, "create_all_chunks", lambda: pytest.fail("read again"))

    chunks = documents(shipment=A) + clean(B)

    analytics.build_snapshot(chunks, reconcile)

    assert len(calls) == 2
    assert all(len(item) == len(chunks) for item in calls)


def test_source_fingerprint_changes_with_the_documents(tmp_path):

    assert analytics.source_fingerprint(tmp_path / "missing") == ()

    first = tmp_path / "BOL_X.pdf"
    first.write_bytes(b"%PDF-1")

    before = analytics.source_fingerprint(tmp_path)

    (tmp_path / "notes.txt").write_text("ignored")
    assert analytics.source_fingerprint(tmp_path) == before

    (tmp_path / "POD_X.pdf").write_bytes(b"%PDF-2")
    added = analytics.source_fingerprint(tmp_path)
    assert added != before

    first.write_bytes(b"%PDF-1 changed")
    assert analytics.source_fingerprint(tmp_path) != added


# ============================================================
# FILTERS
# ============================================================

def test_register_filters():

    rows = analytics.register_rows(snapshot(mixed_fleet()))

    def ids(**filters):
        return [row["shipment_id"] for row in analytics.filter_rows(rows, **filters)]

    assert ids() == [A, B, C, D]
    assert ids(search="shj 2026 002") == [B]
    assert ids(search="2026-30") == [C]
    assert ids(search="nothing") == []
    assert ids(carriers=["Swift Lines"]) == [A, B, D]
    assert ids(carriers=[NOT_AVAILABLE]) == [C]
    assert ids(final_statuses=["NEEDS REVIEW", "NOT RECONCILED"]) == [C, D]
    assert ids(categories=["Invoice"]) == [A]
    assert ids(invoice_mismatch=True) == [A]
    assert ids(delivery_delayed=True) == [A]
    assert ids(needs_review=True) == [C, D]
    assert ids(carriers=["Swift Lines"], needs_review=True) == [D]


# ============================================================
# EXCEPTIONS
# ============================================================

def exceptions_of(chunks):

    return {item["exception_key"]: item for item in analytics.build_exceptions(snapshot(chunks))}


def test_exceptions_for_every_category():

    chunks = (
        documents(shipment=A)                                    # shortage, delay, invoice
        + clean(B)
        + documents(shipment=D, delivered=50, actual="October 3, 2026", total="$1,430.00")
    )

    chunks = edit(chunks, "INVOICE", "Swift Lines", "Other Carrier")
    chunks = edit(chunks, "INVOICE", "Chicago, Illinois", "Dallas, Texas")

    found = exceptions_of(chunks)

    expected = {
        "SHJ2026001|quantity": ("Quantity shortage", "HIGH", 3),
        "SHJ2026001|delivery": ("Delivery delay", "HIGH", 2),
        "SHJ2026001|invoice": ("Invoice mismatch", "HIGH", 120.0),
        "SHJ2026404|quantity": ("Quantity overage", "MEDIUM", -2),
        "SHJ2026002|carrier-invoice": ("Carrier inconsistency", "MEDIUM", None),
        "SHJ2026002|origin": ("Route inconsistency", "MEDIUM", None),
    }

    for key, (category, severity, difference) in expected.items():
        item = found[key]
        assert (item["category"], item["severity"], item["difference"], item["kind"]) == (
            category, severity, difference, analytics.DISCREPANCY,
        ), key

    # Evidence references name the source file and page
    assert any(f"INVOICE_{A}.pdf p.1 (INVOICE)" in reference for reference in found["SHJ2026001|invoice"]["evidence"])


def test_review_cases_are_separate_from_discrepancies():

    found = exceptions_of(mixed_fleet())

    incomplete = found["SHJ2026303|reconciliation"]
    assert (incomplete["kind"], incomplete["category"], incomplete["severity"]) == (
        analytics.REVIEW, "Reconciliation incomplete", "HIGH",
    )

    review = found["SHJ2026404|review-quantity"]
    assert (review["kind"], review["category"], review["severity"]) == (
        analytics.REVIEW, "Quantity needs review", "MEDIUM",
    )

    # B is clean: no exceptions
    assert not [key for key in found if key.startswith("SHJ2026002")]

    # Discrepancies only where the audit found one
    assert {item["kind"] for key, item in found.items() if key.startswith("SHJ2026001")} == {analytics.DISCREPANCY}


def test_signature_follows_the_audited_values():

    first = exceptions_of(documents(shipment=A))["SHJ2026001|invoice"]
    again = exceptions_of(documents(shipment=A))["SHJ2026001|invoice"]
    changed = exceptions_of(documents(shipment=A, total="$1,600.00"))["SHJ2026001|invoice"]

    assert first["signature"] == again["signature"]
    assert first["signature"] != changed["signature"]


def test_resolved_exception_reopens_when_the_documents_change(tmp_path):

    store = ReviewStore(tmp_path / "reviews.sqlite3")

    exceptions = analytics.build_exceptions(snapshot(documents(shipment=A)))
    store.sync(exceptions)

    invoice = next(item for item in exceptions if item["category"] == "Invoice mismatch")
    store.set_status(invoice, RESOLVED, note="Credit note pending")

    # Shown again unchanged: still resolved
    merged = analytics.with_reviews(exceptions, store.sync(exceptions))
    assert next(item for item in merged if item["category"] == "Invoice mismatch")["review_status"] == RESOLVED

    # The invoice changes: the difference is now $170
    changed = analytics.build_exceptions(snapshot(documents(shipment=A, total="$1,600.00")))
    merged = analytics.with_reviews(changed, store.sync(changed))

    item = next(item for item in merged if item["category"] == "Invoice mismatch")

    assert (item["review_status"], item["needs_recheck"], item["difference"]) == (OPEN, True, 170.0)


# ============================================================
# CSV EXPORTS
# ============================================================

def parse(data):

    assert data.startswith(b"\xef\xbb\xbf")

    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))


def test_register_csv_respects_the_rows_given():

    rows = analytics.register_rows(snapshot(mixed_fleet()))

    exported = parse(analytics.register_csv(analytics.filter_rows(rows, needs_review=True)))

    assert [row["shipment_id"] for row in exported] == [C, D]
    assert list(exported[0]) == list(analytics.REGISTER_COLUMNS)

    c = exported[0]
    assert (c["final_status"], c["invoice_total"], c["quantity_status"]) == ("NOT RECONCILED", NOT_AVAILABLE, NOT_AVAILABLE)
    assert c["error"] == "Reconciliation requires BOL, Invoice and POD."
    assert exported[1]["final_status"] == "NEEDS REVIEW"


def test_analytics_csv_formula_protection_and_numbers():

    rows = analytics.register_rows(snapshot(edit(
        documents(shipment=A, total="$1,400.00"), "BOL", "Swift Lines", "=HYPERLINK(\"http://x\")",
    )))

    exported = parse(analytics.register_csv(rows))[0]

    assert exported["carrier"] == "'=HYPERLINK(\"http://x\")"
    assert exported["invoice_difference"] == "-30.00"
    assert exported["invoice_total"] == "1400.00"

    exceptions = analytics.with_reviews(analytics.build_exceptions(snapshot(documents(shipment=A, total="$1,400.00"))), {})
    exceptions[0]["description"] = "@SUM(A1:A9)"

    exported = parse(analytics.exceptions_csv(exceptions))

    assert list(exported[0]) == list(analytics.EXCEPTION_COLUMNS)
    assert exported[0]["description"] == "'@SUM(A1:A9)"

    invoice = next(row for row in exported if row["category"] == "Invoice mismatch")
    assert (invoice["difference"], invoice["review_status"]) == ("-30.00", "open")
    assert invoice["kind"] == analytics.DISCREPANCY
