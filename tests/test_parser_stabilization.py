"""
Regression tests from the parser stabilization audit (after the D1 to D6 fixes).

Every case here was reproduced with SYNTHETIC documents before it was fixed; none was observed on a
real carrier document, and passing them says nothing about real-world accuracy.

    split     a known label inside the text of another value is not a field (a regression D2 introduced)
    charges   a charge that contradicts itself, and an amount outside any label, are never silently accepted
    version   the PDF library is part of the extractor version, and what is fingerprinted is what extraction imports
    reuse     an extraction made later (a new extractor version) shows the approval the audit uses, and the
              audit says when its cross-check comes from an older extractor

Run: python -m pytest tests/test_parser_stabilization.py
"""

import ast
import sys
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_extraction as de  # noqa: E402
from auth import UserStore  # noqa: E402
from authz import ADMIN  # noqa: E402
from extraction_store import ExtractionStore, StoreError  # noqa: E402
from test_api import A, PASSWORD, Rig  # noqa: E402
import test_correction_aware_audit as t  # noqa: E402

HEADER = ["FREIGHT INVOICE", "Invoice Number: INV-1", "Shipment Number: SHJ-2026-123"]
BOL = ["BILL OF LADING", "Shipment Number: SHJ-2026-123", "Number of Pieces:", "10", "Expected Delivery Date:", "October 3, 2026"]


def extract(tmp_path, pages):
    """pages: a list of lines, or a list of such lists (one per page)."""

    pages = pages if pages and isinstance(pages[0], list) else [pages]
    document = pymupdf.open()

    for lines in pages:
        page = document.new_page()

        for index, line in enumerate(lines):
            page.insert_text((30, 50 + index * 13), line, fontsize=9)

    path = Path(tmp_path) / "probe.pdf"
    document.save(str(path))
    document.close()

    return de.extract_document(path)


def codes(e):
    return {issue["code"] for issue in de.validate_shipment([e])}


# =========================================================================
# split: labels inside another value
# =========================================================================

def test_a_note_that_contains_a_label_does_not_become_that_field(tmp_path):
    """The reproduced regression: before this a note produced an EXTRACTED weight the document never stated."""

    e = extract(tmp_path, BOL + ["Special Instructions: Max Weight: 40,000 lbs"])

    assert e.fields["weight"].state == de.MISSING and e.fields["weight"].value is None


@pytest.mark.parametrize("line", [
    "Special Instructions: Do not exceed Weight: 40,000 lbs",
    "Notes: Consignee: call before delivery",
    "Remarks: Origin: see attached map",
    "Comment: Carrier: confirm pickup window",
])
def test_labels_inside_the_text_of_an_unknown_label_are_not_split(line):
    pairs = de.parse_pairs(line, 1)

    assert len(pairs) == 1 and pairs[0].lines == [line.split(": ", 1)[1]]


@pytest.mark.parametrize("line", [
    "Shipment Number: SHJ-2026-123    Carrier: Test Lines",       # an unknown label first, the next one stands apart
    "Shipment Number: SHJ-2026-123\tCarrier: Test Lines",
    "PO Number: 4471   Origin: Dallas, Texas   Destination: Austin, Texas",
])
def test_a_label_that_stands_apart_is_split_even_after_an_unknown_label(line):
    assert len(de.parse_pairs(line, 1)) >= 2


def test_a_single_space_after_an_unknown_label_is_not_enough_to_split():
    """The trade-off: a single-spaced second label after an unknown one stays in the first value and is MISSING."""

    pairs = de.parse_pairs("Shipment Number: SHJ-2026-123 Carrier: Test Lines", 1)

    assert [p.key for p in pairs] == ["shipment number"]


@pytest.mark.parametrize("line", ["Shipper: Acme Parts Consignee: Beta Works", "Consignee: Beta Works Shipper: Acme Parts"])
def test_a_line_that_starts_with_a_known_label_still_splits_with_single_spaces(line):
    assert sorted(p.key for p in de.parse_pairs(line, 1)) == ["consignee", "shipper"]


# =========================================================================
# charges: contradictions and amounts outside any label
# =========================================================================

def test_a_charge_whose_quantity_times_rate_contradicts_its_amount_is_not_complete(tmp_path):
    e = extract(tmp_path, HEADER + ["Storage: 2 x $15.00 = $35.00", "Total Invoice Amount: $35.00"])

    assert [(item.description, item.amount) for item in e.line_items] == [("Storage", 35.0)]
    assert any("does not equal" in w for item in e.line_items for w in item.warnings)
    assert any(u["reason"] == "quantity_rate_mismatch" for u in e.unclassified_amounts)
    assert not e.complete and "invoice_arithmetic_unchecked" in codes(e) and "invoice_arithmetic_mismatch" not in codes(e)
    assert "unclassified_amounts: 1 amount(s) could not be classified as charges" in e.review_reasons


@pytest.mark.parametrize("line, amount", [("Storage: 2 x $15.00 = $30.00", 30.0), ("Storage: 1.5 x $10.00 = $15.00", 15.0), ("Handling: 3 x $12.34 = $37.02", 37.02)])
def test_a_charge_that_multiplies_correctly_is_unchanged(tmp_path, line, amount):
    e = extract(tmp_path, HEADER + [line, f"Total Invoice Amount: ${amount:.2f}"])

    assert [item.amount for item in e.line_items] == [amount] and not e.unclassified_amounts and e.complete


def test_an_amount_before_the_first_label_is_not_dropped_silently(tmp_path):
    """The reproduced defect: the only charge was free text above the labels and the invoice was complete."""

    e = extract(tmp_path, ["FREIGHT INVOICE", "Fuel surcharge $18.00 applies", "Invoice Number: INV-1", "Shipment Number: SHJ-2026-123", "Total Invoice Amount: $18.00"])

    assert not e.line_items and not e.complete
    assert e.unclassified_amounts == [{"label": "(a line before the first label)", "page": 1, "reason": "amount_outside_a_label"}]
    assert "invoice_arithmetic_unchecked" in codes(e)


def test_such_an_amount_on_a_later_page_names_that_page(tmp_path):
    e = extract(tmp_path, [HEADER + ["Base Freight: $100.00", "Total Invoice Amount: $100.00"], ["Carried forward CAD 5.00", "Notes: none"]])

    assert [u["page"] for u in e.unclassified_amounts] == [2] and not e.complete


@pytest.mark.parametrize("line", ["Remit to our Dallas office", "Page 2 of 2", "Invoice for freight services", "Questions? Call 555-0100", "Terms apply, 30 days"])
def test_text_before_the_first_label_without_money_is_not_flagged(tmp_path, line):
    e = extract(tmp_path, ["FREIGHT INVOICE", line, "Invoice Number: INV-1", "Shipment Number: SHJ-2026-123", "Base Freight: $100.00", "Total Invoice Amount: $100.00"])

    assert not e.unclassified_amounts and e.complete


def test_an_amount_before_the_first_label_of_another_document_type_is_not_an_invoice_finding(tmp_path):
    e = extract(tmp_path, ["BILL OF LADING", "Declared value $5,000.00", "Shipment Number: SHJ-2026-123", "Number of Pieces:", "10", "Expected Delivery Date:", "October 3, 2026"])

    assert not e.unclassified_amounts


# =========================================================================
# version: what the extractor version covers
# =========================================================================

SOURCES = [PROJECT_ROOT / "src" / name for name in de.FINGERPRINT_SOURCES]


def test_the_pdf_library_is_part_of_the_extractor_version():
    assert de.FINGERPRINT_EXTRA == (f"pymupdf={pymupdf.__version__}",)
    assert de.EXTRACTOR_VERSION == f"{de.EXTRACTOR_SCHEMA}+{de.source_fingerprint(SOURCES, extra=de.FINGERPRINT_EXTRA)}"
    assert de.source_fingerprint(SOURCES, extra=("pymupdf=0.0.1",)) != de.source_fingerprint(SOURCES, extra=de.FINGERPRINT_EXTRA)
    assert de.source_fingerprint(SOURCES) != de.source_fingerprint(SOURCES, extra=de.FINGERPRINT_EXTRA)


def local_imports(name):
    tree = ast.parse((PROJECT_ROOT / "src" / f"{name}.py").read_text(encoding="utf-8"))
    local = {p.stem for p in (PROJECT_ROOT / "src").glob("*.py")}
    found = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])

    return found & local


def test_every_module_extraction_imports_is_fingerprinted_except_one_reviewed_exception():
    """
    chunker is imported by shipment_identity for create_all_chunks, which extraction never calls, so it
    cannot change an extraction. A new import that is not on this list fails here, forcing a decision.
    """

    seen, queue = set(), ["document_extraction"]

    while queue:
        name = queue.pop()

        if name not in seen:
            seen.add(name)
            queue.extend(local_imports(name))

    assert seen - {Path(n).stem for n in de.FINGERPRINT_SOURCES} <= {"chunker"}


# =========================================================================
# reuse: after the extractor version changes
# =========================================================================

@pytest.fixture
def rig(tmp_path):
    result = Rig(tmp_path, "required")
    UserStore(tmp_path / "auth.sqlite3").create_user("dave", PASSWORD, ADMIN)

    return result


def approved_field(rig):
    return next(f for f in rig.state.service.shipment(A)["inputs"]["fields"] if f["field"] == "delivered_pieces")


def test_a_later_extraction_shows_the_approval_the_audit_uses(rig, monkeypatch):
    old = t.extract(rig, t.POD_A)
    proposal = t.propose(rig, old, "delivered_pieces", "48")
    t.approve(rig, proposal["id"])

    monkeypatch.setattr(de, "EXTRACTOR_VERSION", "1.1+newparser")
    new = t.extract(rig, t.POD_A)
    store = ExtractionStore(rig.locations.extraction_path())
    view = store.effective(new)["delivered_pieces"]

    assert new != old
    assert view["approved"]["corrected"] == 48 and view["value"] == 48 and view["source"] == "approved_correction"
    assert view["extracted"]["value"] == 45 and view["correction_id"] == proposal["id"]
    assert approved_field(rig)["source"] == "approved_correction" and approved_field(rig)["value"] == view["value"]


def test_the_approval_is_not_shown_for_another_file_or_another_field(rig, monkeypatch):
    old = t.extract(rig, t.POD_A)
    t.approve(rig, t.propose(rig, old, "delivered_pieces", "48")["id"])
    other_shipment = t.extract(rig, "POD_SHJ-2026-002.pdf")
    store = ExtractionStore(rig.locations.extraction_path())

    assert store.effective(other_shipment)["delivered_pieces"]["approved"] is None
    assert store.effective(other_shipment)["delivered_pieces"]["value"] == 60
    assert store.effective(old)["delivery_date"]["approved"] is None


def test_proposing_the_value_already_approved_is_refused_on_a_later_extraction_too(rig, monkeypatch):
    old = t.extract(rig, t.POD_A)
    t.approve(rig, t.propose(rig, old, "delivered_pieces", "48")["id"])
    monkeypatch.setattr(de, "EXTRACTOR_VERSION", "1.1+newparser")
    new = t.extract(rig, t.POD_A)

    with pytest.raises(StoreError, match="same as the current value"):
        t.propose(rig, new, "delivered_pieces", "48")

    changed = t.propose(rig, new, "delivered_pieces", "47")

    assert changed["previous"] == {"value": 48, "state": "EXTRACTED", "source": "approved_correction"}

    t.approve(rig, changed["id"], t.DAVE)

    field = approved_field(rig)

    assert field["value"] == 47 and field["superseded_correction_ids"]


def test_an_extractor_change_does_not_stale_or_duplicate_recorded_history(rig, monkeypatch):
    old = t.extract(rig, t.POD_A)
    t.record_run(rig)
    t.approve(rig, t.propose(rig, old, "delivered_pieces", "48")["id"])
    service = rig.state.service
    runs_before = len(service.history_store().runs())

    monkeypatch.setattr(de, "EXTRACTOR_VERSION", "1.1+newparser")
    t.extract(rig, t.POD_A)
    service.invalidate()
    recorded = t.record_run(rig)

    assert recorded["snapshots_written"] == 0 and service.staleness() == []
    assert sum(len(r["supersedes"]) for r in service.history_store().runs()) == 1 and len(service.history_store().runs()) == runs_before + 1


def test_the_audit_says_when_its_cross_check_comes_from_an_older_extractor(rig, monkeypatch):
    t.extract(rig, t.POD_A)
    service = rig.state.service

    current = next(f for f in service.shipment(A)["inputs"]["fields"] if f["field"] == "delivered_pieces")

    assert current["structured"] is not None and "structured_extraction_outdated" not in current["warnings"]

    monkeypatch.setattr(de, "EXTRACTOR_VERSION", "1.1+newparser")
    service.invalidate()                                   # a new extractor version means a restarted process: a fresh audit
    stale = next(f for f in service.shipment(A)["inputs"]["fields"] if f["field"] == "delivered_pieces")

    assert "structured_extraction_outdated" in stale["warnings"] and stale["validation"] == "warning"
    assert stale["value"] == current["value"] == 45          # the audit's value is unchanged: it reads the document

    t.extract(rig, t.POD_A)
    service.invalidate()
    fresh = next(f for f in service.shipment(A)["inputs"]["fields"] if f["field"] == "delivered_pieces")

    assert "structured_extraction_outdated" not in fresh["warnings"]


def test_reprocessing_the_same_file_with_the_same_version_stores_nothing_new(rig):
    first = t.extract(rig, t.POD_A)
    second = t.extract(rig, t.POD_A)

    assert first == second and len(ExtractionStore(rig.locations.extraction_path()).list()) == 1
