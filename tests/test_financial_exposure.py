"""
Tests for src/financial_exposure.py: invoice money figures from audit
results.

Shipments are reconciled with the real reconcile_shipment() from
synthetic document chunks (no PDFs, no index, no Ollama).

Fleet used below:

    SHJ-2026-001  line items $1,430.00, total $1,550.00   mismatch, +$120.00
    SHJ-2026-002  line items $1,430.00, total $1,430.00   verified
    SHJ-2026-505  line items $1,430.00, total $1,400.00   mismatch, -$30.00
    SHJ-2026-404  Detention Charge missing, total $1,430  unverifiable, total read
    SHJ-2026-606  Total Invoice Amount missing            unverifiable, no total
    SHJ-2026-303  no POD                                  cannot be reconciled

Run from the project root:
    python -m pytest tests/test_financial_exposure.py
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

from test_audit_analytics import A, B, C, clean, edit, snapshot  # noqa: E402
from test_reconciliation_consistency import documents, drop  # noqa: E402

import financial_exposure as fx  # noqa: E402
from audit_export import NOT_AVAILABLE  # noqa: E402

G = "SHJ-2026-505"
D = "SHJ-2026-404"
E = "SHJ-2026-606"


def other_carrier(chunks, name="Other Lines"):
    """
    The shipment's carrier changed on every document (so the carriers
    still agree with each other).
    """

    for doc_type in ("BOL", "INVOICE", "POD"):
        chunks = edit(chunks, doc_type, "Swift Lines", name)

    return chunks


def fleet():

    return (
        documents(shipment=A)
        + clean(B)
        + other_carrier(documents(shipment=G, delivered=48, actual="October 3, 2026", total="$1,400.00"))
        + drop(other_carrier(clean(D)), "INVOICE", "Detention Charge")
        + drop(clean(E), "INVOICE", "Total Invoice Amount")
        + [item for item in clean(C) if item["metadata"]["document_type"] != "POD"]
    )


def rows_of(chunks):

    return fx.exposure_rows(snapshot(chunks))


def by_id(rows):

    return {row["shipment_id"]: row for row in rows}


# ============================================================
# PER SHIPMENT
# ============================================================

def test_classification_and_amounts():

    rows = by_id(rows_of(fleet()))

    assert {shipment: row["classification"] for shipment, row in rows.items()} == {
        A: fx.MISMATCH, B: fx.VERIFIED, G: fx.MISMATCH,
        D: fx.UNVERIFIABLE, E: fx.UNVERIFIABLE, C: fx.UNVERIFIABLE,
    }

    a = rows[A]
    assert (a["stated_total"], a["line_item_sum"], a["signed_difference"], a["absolute_difference"]) == (
        1550.0, 1430.0, 120.0, 120.0,
    )
    assert a["amount_under_review"] is None
    assert a["discrepancy_categories"] == ["Delivery Date", "Invoice", "Quantity"]

    # A total below its line items keeps its sign
    g = rows[G]
    assert (g["stated_total"], g["signed_difference"], g["absolute_difference"]) == (1400.0, -30.0, 30.0)

    # An invoice that adds up has a known difference of zero
    b = rows[B]
    assert (b["signed_difference"], b["absolute_difference"], b["amount_under_review"]) == (0.0, 0.0, None)


def test_unverifiable_invoices_keep_unknown_amounts_unknown():

    rows = by_id(rows_of(fleet()))

    # Detention Charge missing: the total was read, the sum cannot be formed
    d = rows[D]
    assert (d["stated_total"], d["line_item_sum"], d["signed_difference"], d["absolute_difference"]) == (
        1430.0, None, None, None,
    )
    assert d["amount_under_review"] == 1430.0
    assert d["invoice_status"] == "NEEDS REVIEW"

    # No total: nothing is under review in money terms, and nothing is 0
    e = rows[E]
    assert (e["stated_total"], e["signed_difference"], e["amount_under_review"]) == (None, None, None)

    # Could not be reconciled at all
    c = rows[C]
    assert c["invoice_status"] == "NOT RECONCILED"
    assert (c["stated_total"], c["line_item_sum"], c["signed_difference"], c["amount_under_review"]) == (
        None, None, None, None,
    )
    assert c["classification"] == fx.UNVERIFIABLE


# ============================================================
# SUMMARY
# ============================================================

def test_summary_of_known_amounts():

    figures = fx.summary(rows_of(fleet()))

    assert figures == {
        "invoices": 6,
        "verified": 1,
        "mismatches": 2,
        "unverifiable": 3,
        "totals_read": 4,
        "totals_missing": 2,
        "stated_total": 1550.0 + 1430.0 + 1400.0 + 1430.0,
        "net_difference": 90.0,                 # +120 and -30
        "total_above_line_items": 120.0,
        "total_below_line_items": 30.0,
        "absolute_variance": 150.0,
        "amount_under_review": 1430.0,
        "unverifiable_without_total": 2,
        "mixed_currency": False,
        "currencies": [],
    }


def test_missing_totals_are_not_counted_as_zero():

    rows = rows_of(drop(clean(E), "INVOICE", "Total Invoice Amount") + clean(B))

    figures = fx.summary(rows)

    assert figures["stated_total"] == 1430.0          # only B's
    assert (figures["totals_read"], figures["totals_missing"]) == (1, 1)

    # Only missing totals: no total at all, not $0.00
    only_missing = fx.summary(rows_of(drop(clean(E), "INVOICE", "Total Invoice Amount")))

    assert only_missing["stated_total"] is None
    assert only_missing["amount_under_review"] is None
    assert only_missing["unverifiable_without_total"] == 1


def test_summary_without_mismatches_has_no_variance_amounts():

    figures = fx.summary(rows_of(clean(B)))

    assert (figures["mismatches"], figures["verified"]) == (0, 1)
    assert figures["absolute_variance"] is None and figures["net_difference"] is None
    assert figures["stated_total"] == 1430.0


def test_empty_data():

    figures = fx.summary([])

    assert figures["invoices"] == 0
    assert all(figures[key] is None for key in (
        "stated_total", "net_difference", "absolute_variance", "amount_under_review",
    ))
    assert fx.by_carrier([]) == [] and fx.by_category([]) == []
    assert fx.filter_rows([], search="x") == []

    rows = list(csv.reader(io.StringIO(fx.rows_csv([]).decode("utf-8-sig"))))
    assert rows == [list(fx.COLUMNS)]

    assert fx.exposure_rows({"shipments": [], "unidentified_sources": []}) == []


def test_currencies_are_never_added_together():

    shots = snapshot(documents(shipment=A) + clean(B))

    # No currency in the documents: summed as stated
    assert fx.summary(fx.exposure_rows(shots))["stated_total"] == 2980.0

    # If invoices ever carry a currency, different ones are not mixed
    for shipment, currency in zip(shots["shipments"], ("USD", "EUR")):
        shipment["result"]["invoice"]["currency"] = currency

    figures = fx.summary(fx.exposure_rows(shots))

    assert figures["mixed_currency"] and figures["currencies"] == ["EUR", "USD"]
    assert figures["stated_total"] is None and figures["absolute_variance"] is None
    assert (figures["invoices"], figures["mismatches"]) == (2, 1)

    # The same currency is added
    for shipment in shots["shipments"]:
        shipment["result"]["invoice"]["currency"] = "USD"

    figures = fx.summary(fx.exposure_rows(shots))

    assert not figures["mixed_currency"] and figures["stated_total"] == 2980.0


# ============================================================
# GROUPS AND FILTERS
# ============================================================

def test_breakdown_by_carrier():

    groups = {group["carrier"]: group for group in fx.by_carrier(rows_of(fleet()))}

    assert set(groups) == {"Swift Lines", "Other Lines", NOT_AVAILABLE}

    swift = groups["Swift Lines"]
    assert (swift["invoices"], swift["mismatches"], swift["unverifiable"]) == (3, 1, 1)
    assert swift["stated_total"] == 2980.0           # A and B; E has no total
    assert (swift["net_difference"], swift["absolute_variance"]) == (120.0, 120.0)

    other = groups["Other Lines"]
    assert (other["invoices"], other["mismatches"], other["unverifiable"]) == (2, 1, 1)
    assert (other["stated_total"], other["net_difference"], other["absolute_variance"]) == (2830.0, -30.0, 30.0)
    assert other["amount_under_review"] == 1430.0

    # The shipment that could not be reconciled has no carrier
    unknown = groups[NOT_AVAILABLE]
    assert (unknown["invoices"], unknown["unverifiable"], unknown["stated_total"]) == (1, 1, None)


def test_breakdown_by_discrepancy_category():

    groups = {group["category"]: group for group in fx.by_category(rows_of(fleet()))}

    assert set(groups) == {"Delivery Date", "Invoice", "Quantity"}

    assert groups["Invoice"]["shipments"] == 2
    assert groups["Invoice"]["absolute_variance"] == 150.0
    assert groups["Invoice"]["amount_basis"] == "absolute invoice difference"

    # Quantity and delivery have no money in the documents
    for category in ("Quantity", "Delivery Date"):
        assert groups[category]["shipments"] == 1
        assert groups[category]["absolute_variance"] is None
        assert groups[category]["amount_basis"] == "no monetary value in the documents"


def test_filters():

    rows = rows_of(fleet())

    def ids(**filters):
        return sorted(row["shipment_id"] for row in fx.filter_rows(rows, **filters))

    assert ids() == sorted([A, B, G, D, E, C])
    assert ids(search="shj 2026 505") == [G]
    assert ids(search="2026-60") == [E]
    assert ids(carriers=["Other Lines"]) == sorted([G, D])
    assert ids(carriers=[NOT_AVAILABLE]) == [C]
    assert ids(classifications=[fx.MISMATCH]) == sorted([A, G])
    assert ids(classifications=[fx.UNVERIFIABLE, fx.VERIFIED]) == sorted([B, D, E, C])
    assert ids(categories=["Invoice"]) == sorted([A, G])
    assert ids(carriers=["Other Lines"], classifications=[fx.MISMATCH]) == [G]
    assert ids(search="nothing") == []

    # The summary follows the filter
    assert fx.summary(fx.filter_rows(rows, classifications=[fx.MISMATCH]))["absolute_variance"] == 150.0
    assert fx.summary(fx.filter_rows(rows, carriers=["Other Lines"]))["net_difference"] == -30.0


# ============================================================
# CSV
# ============================================================

def parse(data):

    assert data.startswith(b"\xef\xbb\xbf")

    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))


def test_csv_respects_the_filter_and_keeps_signs_and_gaps():

    rows = rows_of(fleet())

    exported = {row["shipment_id"]: row for row in parse(fx.rows_csv(fx.filter_rows(rows, carriers=["Other Lines"])))}

    assert set(exported) == {G, D}
    assert list(next(iter(exported.values()))) == list(fx.COLUMNS)

    # A negative difference is a number, not text to protect
    assert exported[G]["signed_difference"] == "-30.00"
    assert exported[G]["absolute_difference"] == "30.00"
    assert exported[G]["classification"] == fx.CLASS_LABELS[fx.MISMATCH]
    assert exported[G]["note"] == fx.NOTE

    # Unknown amounts are NOT AVAILABLE, never 0
    assert exported[D]["line_item_sum"] == NOT_AVAILABLE
    assert exported[D]["signed_difference"] == NOT_AVAILABLE
    assert exported[D]["amount_under_review"] == "1430.00"
    assert exported[D]["note"] == ""

    everything = {row["shipment_id"]: row for row in parse(fx.rows_csv(rows))}

    assert everything[B]["signed_difference"] == "0.00"
    assert everything[E]["stated_total"] == NOT_AVAILABLE
    assert everything[C]["invoice_status"] == "NOT RECONCILED"


def test_csv_formula_protection():

    rows = rows_of(other_carrier(documents(shipment=A), '=HYPERLINK("http://example.com","x")'))

    exported = parse(fx.rows_csv(rows))[0]

    assert exported["carrier"] == "'=HYPERLINK(\"http://example.com\",\"x\")"
    assert exported["signed_difference"] == "120.00"


def test_the_notes_do_not_claim_recoverable_amounts():

    for text in (fx.NOTE, *fx.METRICS.values()):
        assert "recover" not in text.lower() or "not" in text.lower()

    assert "does not prove overbilling" in fx.NOTE
    assert "not a loss" in fx.METRICS["amount_under_review"]


def test_above_and_below_are_known_zeros_once_a_mismatch_exists():

    # Only a total above its line items: nothing is below, which is $0.00, not unknown
    figures = fx.summary(rows_of(documents(shipment=A) + clean(B)))

    assert (figures["total_above_line_items"], figures["total_below_line_items"]) == (120.0, 0.0)

    # Only a total below its line items
    below = fx.summary(rows_of(documents(shipment=A, delivered=48, actual="October 3, 2026", total="$1,400.00")))

    assert (below["total_above_line_items"], below["total_below_line_items"]) == (0.0, 30.0)
    assert below["net_difference"] == -30.0

    # No mismatch at all: nothing to add up
    none = fx.summary(rows_of(clean(B)))

    assert (none["total_above_line_items"], none["total_below_line_items"]) == (None, None)
