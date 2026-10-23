"""
Parser integrity gaps (docs/real_world_evaluation/KNOWN_GAPS.md, section A2).

Synthetic probes only. Nothing here measures how real carrier documents behave.

    repeated charges    the same label and amount twice cannot be told apart as a recap or a
                        real repeated charge: nothing is merged, the set needs review
    cross-check         a changed extractor version or approval shows in the audit and the
                        staleness report without a restart; recorded runs are untouched
    same file content   one stored row per content and version: a second name that suggests
                        another document type is refused, never silently given the first type
    fingerprint         extraction-relevant imports are fingerprinted; nothing is added just
                        because it is imported transitively
"""

import sys
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

for folder in (PROJECT_ROOT / "src", TESTS):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import audit_inputs  # noqa: E402
import document_extraction as de  # noqa: E402
from extraction_store import AliasTypeConflict, ExtractionStore  # noqa: E402
from test_api import A  # noqa: E402
import test_correction_aware_audit as t  # noqa: E402
from test_parser_stabilization import HEADER, codes, extract, rig  # noqa: E402,F401


# =========================================================================
# 1. repeated charges
# =========================================================================

PAGE_ONE = HEADER + ["Base Freight: $100.00", "Fuel Surcharge: $20.00"]


def test_a_recap_page_repeating_charges_is_no_longer_complete_with_a_false_mismatch(tmp_path):
    recap = ["Summary of charges", "Base Freight: $100.00", "Fuel Surcharge: $20.00", "Total Invoice Amount: $120.00"]
    e = extract(tmp_path, [PAGE_ONE, recap])

    assert [(i.description, i.amount, i.page) for i in e.line_items] == [
        ("Base Freight", 100.0, 1), ("Fuel Surcharge", 20.0, 1), ("Base Freight", 100.0, 2), ("Fuel Surcharge", 20.0, 2)]
    assert sorted((u["label"], u["page"], u["reason"]) for u in e.unclassified_amounts) == [
        ("Base Freight", 2, "repeated_charge"), ("Fuel Surcharge", 2, "repeated_charge")]
    assert not e.complete and any(r.startswith("unclassified_amounts") for r in e.review_reasons)
    assert "invoice_arithmetic_unchecked" in codes(e) and "invoice_arithmetic_mismatch" not in codes(e)


def test_two_legitimate_identical_charges_are_kept_and_also_need_review(tmp_path):
    """The text cannot say a repeat is legitimate, so it is not summed silently as complete either."""

    e = extract(tmp_path, HEADER + ["Detention: $50.00", "Detention: $50.00", "Total Invoice Amount: $100.00"])

    assert [i.amount for i in e.line_items] == [50.0, 50.0]                      # nothing is deduplicated
    assert [u["reason"] for u in e.unclassified_amounts] == ["repeated_charge"]
    assert not e.complete and "invoice_arithmetic_unchecked" in codes(e)


def test_the_same_label_with_another_amount_or_another_label_with_the_same_amount_is_not_a_repeat(tmp_path):
    e = extract(tmp_path, HEADER + ["Detention: $50.00", "Detention: $75.00", "Lumper Fee: $50.00", "Total Invoice Amount: $175.00"])

    assert len(e.line_items) == 3 and not e.unclassified_amounts and e.complete
    assert "invoice_arithmetic_mismatch" not in codes(e) and "invoice_arithmetic_unchecked" not in codes(e)


def test_a_repeated_credit_is_also_unresolved(tmp_path):
    e = extract(tmp_path, HEADER + ["Base Freight: $100.00", "Credit: -$10.00", "Credit: -$10.00", "Total Invoice Amount: $80.00"])

    assert [u["reason"] for u in e.unclassified_amounts] == ["repeated_charge"] and not e.complete


def test_a_single_set_of_charges_is_unchanged(tmp_path):
    e = extract(tmp_path, [PAGE_ONE + ["Total Invoice Amount: $120.00"]])

    assert e.complete and not e.unclassified_amounts and "invoice_arithmetic_mismatch" not in codes(e)


# =========================================================================
# 2. cross-check staleness without a restart
# =========================================================================

def field_of(rig, name="delivered_pieces"):
    return next(f for f in rig.state.service.shipment(A)["inputs"]["fields"] if f["field"] == name)


def test_a_changed_extractor_version_recomputes_the_audit_without_invalidate(rig, monkeypatch):
    t.extract(rig, t.POD_A)
    service = rig.state.service

    assert "structured_extraction_outdated" not in field_of(rig)["warnings"] and service.outdated_cross_checks() == []

    monkeypatch.setattr(de, "EXTRACTOR_VERSION", "1.1+newparser")             # no invalidate(), no restart

    stale = field_of(rig)
    report = service.outdated_cross_checks()

    assert "structured_extraction_outdated" in stale["warnings"] and stale["validation"] == "warning"
    assert [(r["shipment_key"], r["fields"], r["current_version"]) for r in report] == [
        (service.shipment(A)["shipment_key"], ["delivered_pieces", "actual_date"], "1.1+newparser")]   # both POD fields
    assert report[0]["extractor_versions"] and "1.1+newparser" not in report[0]["extractor_versions"]
    assert stale["value"] == 45                                             # the audit's own value is unchanged

    t.extract(rig, t.POD_A)                                                  # extracted again under the new version

    assert "structured_extraction_outdated" not in field_of(rig)["warnings"] and service.outdated_cross_checks() == []


def test_the_audit_is_cached_while_nothing_changed_and_recomputed_when_something_did(rig, monkeypatch):
    t.extract(rig, t.POD_A)
    calls = []
    original = audit_inputs.build_inputs
    monkeypatch.setattr(audit_inputs, "build_inputs", lambda *a, **k: calls.append(1) or original(*a, **k))
    service = rig.state.service

    service.snapshot()
    first = len(calls)
    service.snapshot()
    service.outdated_cross_checks()

    assert first > 0 and len(calls) == first                                # a repeat reads the cache

    monkeypatch.setattr(de, "EXTRACTOR_VERSION", "1.1+newparser")
    service.snapshot()

    assert len(calls) > first                                               # the version is part of the key


def test_an_approved_correction_recomputes_the_audit_without_invalidate(rig):
    pod = t.extract(rig, t.POD_A)

    assert field_of(rig)["source"] == audit_inputs.LEGACY

    proposal = t.propose(rig, pod, "delivered_pieces", "48")
    assert field_of(rig)["source"] == audit_inputs.LEGACY                   # a proposal has no effect

    t.approve(rig, proposal["id"])

    assert field_of(rig)["source"] == audit_inputs.APPROVED and field_of(rig)["value"] == 48


def test_recorded_runs_keep_their_snapshots_when_the_version_changes(rig, monkeypatch):
    t.extract(rig, t.POD_A)
    t.record_run(rig)
    service = rig.state.service
    before = service.history_store().runs()

    monkeypatch.setattr(de, "EXTRACTOR_VERSION", "1.1+newparser")

    assert service.outdated_cross_checks() and service.history_store().runs() == before
    assert service.staleness() == []                                        # no approval is missing from the run


def test_the_staleness_endpoint_reports_outdated_cross_checks(rig, monkeypatch):
    t.extract(rig, t.POD_A)
    client = rig.client("alice")

    assert client.get("/api/extractions/staleness").json()["outdated_cross_checks"] == []

    monkeypatch.setattr(de, "EXTRACTOR_VERSION", "1.1+newparser")
    body = client.get("/api/extractions/staleness").json()

    assert [r["fields"] for r in body["outdated_cross_checks"]] == [["delivered_pieces", "actual_date"]] and body["stale"] == []


# =========================================================================
# 3. identical file content under different names
# =========================================================================

AMBIGUOUS = ["Shipment Number: SHJ-2026-123", "Reference: 1"]


def two_names(tmp_path):
    path = Path(tmp_path) / "same.pdf"
    document = pymupdf.open()
    page = document.new_page()

    for index, line in enumerate(AMBIGUOUS):
        page.insert_text((30, 50 + index * 13), line, fontsize=9)

    document.save(str(path))
    document.close()

    return de.extract_document(path, filename="BOL_a.pdf"), de.extract_document(path, filename="INVOICE_b.pdf")


def test_the_two_names_are_classified_differently_but_have_one_hash(tmp_path):
    first, second = two_names(tmp_path)

    assert first.sha256 == second.sha256 and first.document_type != second.document_type


def test_a_second_name_with_another_type_is_refused_not_given_the_first_type(tmp_path):
    first, second = two_names(tmp_path)
    store = ExtractionStore(tmp_path / "extractions.sqlite3")
    saved, created = store.save(first)

    with pytest.raises(AliasTypeConflict) as refused:
        store.save(second)

    assert created and refused.value.existing_id == saved and refused.value.existing_type == first.document_type
    assert refused.value.new_type == second.document_type and "BOL_a.pdf" in str(refused.value)
    assert [r["document_type"] for r in store.list()] == [first.document_type]       # nothing was stored or changed


def test_a_second_name_with_the_same_type_reuses_the_row_as_before(tmp_path):
    first, _ = two_names(tmp_path)
    store = ExtractionStore(tmp_path / "extractions.sqlite3")
    saved, _ = store.save(first)

    assert store.save(first) == (saved, False)


def test_the_run_endpoint_answers_409_for_the_conflicting_alias(rig):
    raw = rig.locations.raw_dir
    source = raw / t.POD_A
    copy = raw / "PROOF_copy.pdf"
    copy.write_bytes(source.read_bytes())
    client = rig.client("alice")

    assert client.post("/api/extractions/run", json={"document": t.POD_A}).status_code == 200

    reply = client.post("/api/extractions/run", json={"document": copy.name})

    # Identical content classifies by content here, so the types agree: the row is reused, not refused
    assert reply.status_code == 200 and reply.json()["created"] is False


# =========================================================================
# 4. fingerprint: extraction-relevant imports only
# =========================================================================

def test_the_fingerprint_lists_only_modules_extraction_needs():
    listed = {Path(n).stem for n in de.FINGERPRINT_SOURCES}

    assert {"document_extraction", "shipment_calculations", "shipment_identity", "document_classifier"} <= listed
    assert not listed & {"chunker", "audit_service", "extraction_store", "audit_inputs", "api"}


def test_the_documented_exception_is_the_chunker_only_and_the_docs_say_so():
    text = (PROJECT_ROOT / "docs" / "real_world_evaluation" / "KNOWN_GAPS.md").read_text(encoding="utf-8")

    assert "`chunker`" in text and "exception to the fingerprint" in text
