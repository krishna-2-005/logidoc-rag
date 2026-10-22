"""
Tests for src/document_extraction.py: typed fields, provenance, extraction
states, cross-document validation, and agreement with the legacy readers.

Documents are synthetic PDFs written with pymupdf or the two fixture shipments;
no model, no network, no production data.

Run: python -m pytest tests/test_document_extraction.py
"""

import json
import sys
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_extraction as de  # noqa: E402
from document_extraction import (  # noqa: E402
    CONFLICTING, EXTRACTED, MISSING, UNPARSEABLE, UNREADABLE, UNSUPPORTED, ExtractedField, extract_document,
)

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def make_pdf(tmp_path, name, pages):
    """pages: one text per page (None = a page with no text at all)."""

    document = pymupdf.open()

    for text in pages:
        page = document.new_page()

        if text:
            for index, line in enumerate(text.split("\n")):
                page.insert_text((40, 50 + index * 14), line, fontsize=9)

    path = Path(tmp_path) / name
    document.save(str(path))
    document.close()

    return path


def doc(title, shipment, pairs):
    lines = [title, f"Shipment Number: {shipment}", ""]

    for label, value in pairs:
        lines += [f"{label}:", value, ""]

    return "\n".join(lines)


def bol(shipment="SHJ-2026-901", **changes):
    pairs = {"Shipper": "Acme Parts\n12 Dock Rd, Dallas", "Consignee": "Beta Works\nHouston", "Carrier": "Test Lines",
             "Origin": "Dallas, Texas", "Destination": "Houston, Texas", "Pickup Date": "October 2, 2026",
             "Expected Delivery Date": "October 3, 2026", "Number of Pieces": "10", "Total Weight": "1,200 lbs"}
    pairs.update(changes)

    return doc("BILL OF LADING", shipment, [(k, v) for k, v in pairs.items() if v is not None])


def invoice(shipment="SHJ-2026-901", extra=(), **changes):
    pairs = {"Carrier": "Test Lines", "Billing Date": "October 4, 2026", "Base Freight": "$500.00", "Fuel Surcharge": "$50.00",
             "Total Invoice Amount": "$550.00"}
    pairs.update(changes)

    lines = ["FREIGHT INVOICE", "Invoice Number: INV-901", f"Shipment Number: {shipment}", ""]

    for label, value in [*pairs.items(), *extra]:
        if value is not None:
            lines += [f"{label}:", value, ""]

    return "\n".join(lines)


def ex(tmp_path, text, name="d.pdf"):
    return extract_document(make_pdf(tmp_path, name, text if isinstance(text, list) else [text]))


# ---- the two fixture shipments ------------------------------------------------------

def fixture(shipment, name):
    return extract_document(FIXTURES / f"shipment_{shipment}" / f"{name}_{shipment}.pdf")


def test_a_fixture_bol_is_fully_typed_with_provenance():
    e = fixture("SHJ-2026-001", "BOL")

    assert e.document_type == "BOL" and e.status == "extracted" and e.complete and de.validate_extraction(e) == []
    assert e.value("piece_count") == 48 and e.value("expected_delivery_date") == "2026-10-03"
    assert e.value("weight") == {"amount": 12500.0, "unit": "lb"}
    assert e.value("shipper") == {"name": "Texas Industrial Components", "address_lines": ["Dallas, Texas"]}
    assert e.value("consignee")["name"] == "Gulf Manufacturing Solutions"

    for item in e.fields.values():
        assert item.source_file == "BOL_SHJ-2026-001.pdf"
        assert item.state != EXTRACTED or (item.page == 1 and item.method and item.basis == "exact_label" and item.raw_text)


def test_a_fixture_invoice_has_number_date_charges_and_no_invented_currency_or_tax():
    e = fixture("SHJ-2026-001", "INVOICE")

    assert e.value("invoice_number") == "INV-SHJ-2026-001" and e.value("invoice_date") == "2026-10-04"
    assert e.value("due_date") == "2026-11-03" and e.value("invoice_total") == 1550.0
    assert [(i.description, i.amount) for i in e.line_items] == [
        ("Base Freight", 1200.0), ("Fuel Surcharge", 180.0), ("Detention Charge", 0.0), ("Other Accessorial Charges", 50.0)]

    for name in ("currency", "tax", "subtotal"):
        assert e.fields[name].state == MISSING and e.fields[name].value is None
    assert "no currency is assumed" in e.fields["currency"].warnings[0]


def test_a_fixture_pod_has_signature_text_and_an_unsupported_signature_image():
    e = fixture("SHJ-2026-001", "POD")

    assert e.value("delivered_pieces") == 45 and e.value("delivery_date") == "2026-10-05" and e.value("signature") == "Michael Anderson"
    assert e.fields["signature_image"].state == UNSUPPORTED and e.fields["signature_image"].value is None


@pytest.mark.parametrize("shipment", ["SHJ-2026-001", "SHJ-2026-002"])
def test_the_new_extraction_agrees_with_both_legacy_readers_on_the_fixtures(shipment):
    """The audit's readers are not replaced; they must say the same thing."""

    import reconciliation
    from shipment_calculations import read_field

    documents = {name: fixture(shipment, name) for name in ("BOL", "POD", "INVOICE")}
    records = []

    for name, e in documents.items():
        pdf = FIXTURES / f"shipment_{shipment}" / f"{name}_{shipment}.pdf"
        text = "\n".join(page.get_text("text", sort=True) for page in pymupdf.open(pdf))
        records.append({"document": text, "normalized": " ".join(text.split()), "raw": text, "type": name.lower(),
                        "metadata": {"source": e.filename, "page": 1, "document_type": name, "shipment_id": shipment}})

    legacy = {name: read_field(name, records)[0].value for name in
              ("expected_pieces", "delivered_pieces", "base_freight", "fuel_surcharge", "detention_charge", "other_charges", "invoice_total")}

    assert legacy["expected_pieces"] == documents["BOL"].value("piece_count")
    assert legacy["delivered_pieces"] == documents["POD"].value("delivered_pieces")
    assert legacy["invoice_total"] == documents["INVOICE"].value("invoice_total")

    amounts = {i.description: i.amount for i in documents["INVOICE"].line_items}
    assert (amounts["Base Freight"], amounts["Fuel Surcharge"], amounts["Detention Charge"], amounts["Other Accessorial Charges"]) == (
        legacy["base_freight"], legacy["fuel_surcharge"], legacy["detention_charge"], legacy["other_charges"])

    bol_text = records[0]["document"]

    for name in ("carrier", "origin", "destination"):
        assert documents["BOL"].value(name) == reconciliation.extract_field(bol_text, name.capitalize())

    assert str(read_field("expected_date", records)[0].value) == documents["BOL"].value("expected_delivery_date")
    assert str(read_field("actual_date", records)[0].value) == documents["POD"].value("delivery_date")


# ---- states: missing, unparseable, unreadable, unsupported, conflicting ------------------

def test_a_missing_field_is_missing_and_never_zero_or_empty(tmp_path):
    e = ex(tmp_path, bol(**{"Total Weight": None, "Number of Pieces": None}))

    assert e.fields["weight"].state == MISSING and e.fields["weight"].value is None
    assert e.fields["piece_count"].state == MISSING and e.fields["piece_count"].value is None
    assert not e.complete and "required field piece_count is missing" in e.review_reasons


def test_a_label_without_a_value_is_missing_with_a_warning(tmp_path):
    e = ex(tmp_path, "BILL OF LADING\nShipment Number: SHJ-2026-901\n\nCarrier:\n\nNumber of Pieces:\n\nOrigin:\nDallas")

    assert e.fields["piece_count"].state == MISSING and "no value" in e.fields["piece_count"].warnings[0]


@pytest.mark.parametrize("name, value, kind", [
    ("Pickup Date", "10/05/2026", "ambiguous slash date"), ("Pickup Date", "February 30, 2026", "impossible date"),
    ("Pickup Date", "soon", "not a date"), ("Number of Pieces", "forty", "words"), ("Number of Pieces", "12.5", "fraction"),
    ("Total Weight", "heavy", "not a weight"),
])
def test_text_that_is_not_a_valid_value_is_unparseable_and_keeps_the_raw_text(tmp_path, name, value, kind):
    e = ex(tmp_path, bol(**{name: value}))
    item = next(f for f in e.fields.values() if f.raw_text and f.raw_text.startswith(value) and f.state == UNPARSEABLE)

    assert item.value is None and item.validation == "invalid" and item.warnings
    assert any("unparseable" in reason for reason in e.review_reasons)


def test_an_unparseable_money_amount_is_not_a_total(tmp_path):
    e = ex(tmp_path, invoice(**{"Total Invoice Amount": "TBD"}))

    assert e.fields["invoice_total"].state == UNPARSEABLE and e.fields["invoice_total"].raw_text == "TBD"
    assert not e.complete


def test_a_weight_without_a_unit_is_read_with_a_warning(tmp_path):
    e = ex(tmp_path, bol(**{"Total Weight": "2,000"}))

    assert e.value("weight") == {"amount": 2000.0, "unit": None} and e.fields["weight"].validation == "warning"
    assert ex(tmp_path, bol(**{"Total Weight": "850 kg"}), "k.pdf").value("weight") == {"amount": 850.0, "unit": "kg"}


def test_two_different_values_are_conflicting_and_none_is_chosen(tmp_path):
    text = bol() + "\n\nNumber of Pieces:\n12\n"
    e = ex(tmp_path, text)

    item = e.fields["piece_count"]

    assert item.state == CONFLICTING and item.value is None
    assert sorted(a["value"] for a in item.alternatives) == [10, 12]
    assert not e.complete and "piece_count: conflicting values" in e.review_reasons


def test_the_same_value_twice_is_not_a_conflict(tmp_path):
    e = ex(tmp_path, [bol(), bol()])

    assert e.fields["piece_count"].state == EXTRACTED and e.value("piece_count") == 10 and e.pages == 2


def test_conflicting_pages_report_each_page(tmp_path):
    e = ex(tmp_path, [bol(), bol(**{"Number of Pieces": "11"})])

    assert {(a["value"], a["page"]) for a in e.fields["piece_count"].alternatives} == {(10, 1), (11, 2)}


def test_a_document_naming_two_shipments_is_attributed_to_neither(tmp_path):
    e = ex(tmp_path, [bol("SHJ-2026-901"), bol("SHJ-2026-902")])

    assert e.fields["shipment_id"].state == CONFLICTING and e.shipment_id is None
    assert {a["value"] for a in e.fields["shipment_id"].alternatives} == {"SHJ-2026-901", "SHJ-2026-902"}
    assert de.group_by_shipment([e]) == {None: [e]}


def test_unsupported_fields_and_document_types_are_explicit(tmp_path):
    e = ex(tmp_path, "SOME MEMO\nShipment Number: SHJ-2026-901\n\nNotes:\nhello")

    assert e.document_type not in de.DOCUMENT_TYPES
    assert set(e.fields) == {"shipment_id"} and any("not supported" in r for r in e.review_reasons)


# ---- unreadable sources ---------------------------------------------------------------------

def test_an_image_only_pdf_needs_ocr_and_every_field_is_unreadable_not_missing(tmp_path):
    document = pymupdf.open()
    picture = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60))
    picture.clear_with(180)
    document.new_page().insert_image(pymupdf.Rect(50, 50, 200, 200), pixmap=picture)
    path = Path(tmp_path) / "scan.pdf"
    document.save(str(path))

    e = extract_document(path)

    assert e.status == "needs_ocr" and not e.complete and "no OCR" in e.review_reasons[0]
    assert all(f.state == UNREADABLE and f.value is None for f in e.fields.values())


def test_a_partial_pdf_cannot_claim_a_missing_field_is_absent(tmp_path):
    e = ex(tmp_path, [bol(**{"Total Weight": None}), None])

    assert e.status == "partial" and e.pages == 2 and e.pages_without_text == [2] and not e.complete
    assert e.fields["weight"].state == UNREADABLE and "may be on one of them" in e.fields["weight"].warnings[0]
    assert e.fields["piece_count"].state == EXTRACTED and e.fields["piece_count"].page == 1


def test_a_damaged_file_is_reported_not_raised(tmp_path):
    path = Path(tmp_path) / "broken.pdf"
    path.write_bytes(b"this is not a pdf")

    e = extract_document(path)

    assert e.status == "unreadable" and e.error and not e.complete and all(f.state in (UNREADABLE, UNSUPPORTED) for f in e.fields.values())


def test_a_missing_file_is_reported_not_raised(tmp_path):
    e = extract_document(Path(tmp_path) / "nothing.pdf")

    assert e.status == "unreadable" and e.sha256 == ""


# ---- pages are never invented --------------------------------------------------------------------

def test_the_page_is_the_page_the_value_was_read_on(tmp_path):
    e = ex(tmp_path, ["BILL OF LADING\nShipment Number: SHJ-2026-901\n\nCarrier:\nTest Lines", "Number of Pieces:\n10\n\nPickup Date:\nOctober 2, 2026"])

    assert e.fields["carrier"].page == 1 and e.fields["piece_count"].page == 2 and e.fields["pickup_date"].page == 2


def test_fields_that_were_not_read_have_no_page(tmp_path):
    e = ex(tmp_path, bol(**{"Total Weight": None}))

    assert e.fields["weight"].page is None and e.fields["weight"].raw_text is None


def test_a_field_cannot_carry_an_inconsistent_state():
    with pytest.raises(ValueError):
        ExtractedField("x", EXTRACTED)
    with pytest.raises(ValueError):
        ExtractedField("x", MISSING, value=0)
    with pytest.raises(ValueError):
        ExtractedField("x", "SOMETHING")
    with pytest.raises(ValueError):
        ExtractedField("x", EXTRACTED, value=1, page=0)
    assert len(ExtractedField("x", EXTRACTED, value=1, raw_text="y" * 500).raw_text) == 200


# ---- invoices: currency, tax, arbitrary line items --------------------------------------------------

def test_a_stated_currency_code_is_extracted(tmp_path):
    assert ex(tmp_path, invoice(Currency="CAD")).value("currency") == "CAD"
    assert ex(tmp_path, invoice(**{"Total Invoice Amount": "USD 550.00", "Base Freight": "USD 500.00"}), "b.pdf").value("currency") == "USD"


def test_symbols_that_name_one_currency_are_read_and_a_bare_dollar_is_not(tmp_path):
    assert ex(tmp_path, invoice(**{"Total Invoice Amount": "£550.00"}), "e.pdf").value("currency") == "GBP"
    bare = ex(tmp_path, invoice(), "d.pdf")
    assert bare.fields["currency"].state == MISSING and "no currency is assumed" in bare.fields["currency"].warnings[0]


def test_an_unknown_or_conflicting_currency_is_not_guessed(tmp_path):
    assert ex(tmp_path, invoice(Currency="ZZZ")).fields["currency"].state == UNPARSEABLE
    mixed = ex(tmp_path, invoice(**{"Base Freight": "USD 500.00", "Fuel Surcharge": "CAD 50.00"}), "m.pdf")
    assert mixed.fields["currency"].state == CONFLICTING and mixed.fields["currency"].value is None


def test_tax_and_subtotal_are_read_and_arbitrary_line_items_are_listed(tmp_path):
    e = ex(tmp_path, invoice(extra=[("Lift Gate Service", "$40.00"), ("Storage", "2 x $15.00 = $30.00"), ("Credit", "-$20.00"),
                                    ("Subtotal", "$600.00"), ("Sales Tax", "$48.00")], **{"Total Invoice Amount": "$648.00"}))

    assert e.value("tax") == 48.0 and e.value("subtotal") == 600.0 and e.value("invoice_total") == 648.0

    items = {i.description: i for i in e.line_items}

    assert set(items) == {"Base Freight", "Fuel Surcharge", "Lift Gate Service", "Storage", "Credit"}
    assert (items["Storage"].quantity, items["Storage"].rate, items["Storage"].amount) == (2.0, 15.0, 30.0)
    assert items["Credit"].amount == -20.0 and items["Lift Gate Service"].page == 1


def test_a_quantity_times_rate_that_does_not_add_up_is_warned(tmp_path):
    e = ex(tmp_path, invoice(extra=[("Storage", "2 x $15.00 = $35.00")]))

    assert any("does not equal" in w for i in e.line_items for w in i.warnings)


def test_a_line_that_is_not_an_amount_is_not_a_line_item(tmp_path):
    e = ex(tmp_path, invoice(extra=[("Notes", "Call before delivery"), ("Contact", "555-0100")]))

    assert {i.description for i in e.line_items} == {"Base Freight", "Fuel Surcharge"}


# ---- rate confirmations --------------------------------------------------------------------------------

def test_a_rate_confirmation_is_read_into_its_own_schema(tmp_path):
    text = doc("RATE CONFIRMATION", "SHJ-2026-903", [("Carrier", "Test Lines"), ("Origin", "Dallas, Texas"), ("Destination", "Atlanta, Georgia"),
                                                      ("Pickup Date", "October 6, 2026"), ("Equipment", "53-foot Dry Van"),
                                                      ("Agreed Rate", "$1,850.00"), ("Fuel Surcharge", "$120.00"), ("Payment Terms", "Net 30"),
                                                      ("Currency", "USD")])
    e = ex(tmp_path, text)

    assert e.document_type == "RATE_CONFIRMATION" and e.value("agreed_rate") == 1850.0 and e.value("fuel_surcharge") == 120.0
    assert e.value("pickup_date") == "2026-10-06" and e.value("payment_terms") == "Net 30" and e.value("currency") == "USD"
    assert e.complete and "invoice_total" not in e.fields


def test_a_rate_confirmation_without_a_rate_needs_review(tmp_path):
    e = ex(tmp_path, doc("RATE CONFIRMATION", "SHJ-2026-903", [("Carrier", "Test Lines")]))

    assert e.fields["agreed_rate"].state == MISSING and not e.complete


# ---- shipment-level validation ----------------------------------------------------------------------------

def test_matching_documents_have_no_issues(tmp_path):
    b = ex(tmp_path, bol(), "b.pdf")
    i = ex(tmp_path, invoice(extra=[("Tax", "$0.00")]), "i.pdf")

    assert [x for x in de.validate_shipment([b, i]) if x["severity"] != "info"] == []


def test_different_carriers_routes_and_currencies_are_reported_not_resolved(tmp_path):
    b = ex(tmp_path, bol(), "b.pdf")
    i = ex(tmp_path, invoice(Carrier="Other Freight", Origin="Austin, Texas", Currency="CAD"), "i.pdf")
    codes = {x["code"] for x in de.validate_shipment([b, i])}

    assert {"carrier_mismatch", "origin_mismatch"} <= codes
    r = ex(tmp_path, doc("RATE CONFIRMATION", "SHJ-2026-901", [("Carrier", "Test Lines"), ("Agreed Rate", "USD 500.00"), ("Currency", "USD")]), "r.pdf")
    assert "currency_mismatch" in {x["code"] for x in de.validate_shipment([i, r])}


def test_documents_of_different_shipments_are_flagged_and_grouping_keeps_them_apart(tmp_path):
    a = ex(tmp_path, bol("SHJ-2026-901"), "a.pdf")
    b = ex(tmp_path, invoice("SHJ-2026-902"), "b.pdf")

    assert "shipment_mismatch" in {x["code"] for x in de.validate_shipment([a, b])}

    groups = de.group_by_shipment([a, b])

    assert set(groups) == {"SHJ2026901", "SHJ2026902"} and all(len(v) == 1 for v in groups.values())


def test_invoice_arithmetic_uses_line_items_and_tax_and_never_zero_for_missing(tmp_path):
    off = ex(tmp_path, invoice(**{"Total Invoice Amount": "$600.00"}), "o.pdf")
    issue = [x for x in de.validate_shipment([off]) if x["code"] == "invoice_arithmetic_mismatch"][0]

    assert "difference 50.00" in issue["message"] and "not proof of overbilling" in issue["message"]

    taxed = ex(tmp_path, invoice(extra=[("Tax", "$50.00")], **{"Total Invoice Amount": "$600.00"}), "t.pdf")
    assert [x for x in de.validate_shipment([taxed]) if x["severity"] != "info"] == []

    no_items = ex(tmp_path, "FREIGHT INVOICE\nInvoice Number: I-1\nShipment Number: SHJ-2026-901\n\nTotal Invoice Amount:\n$100.00", "n.pdf")
    unchecked = de.validate_shipment([no_items])
    assert [x["code"] for x in unchecked] == ["invoice_arithmetic_unchecked"]


def test_duplicate_content_is_noted(tmp_path):
    import shutil

    a = ex(tmp_path, bol(), "a.pdf")
    shutil.copy(Path(tmp_path) / "a.pdf", Path(tmp_path) / "b.pdf")
    b = extract_document(Path(tmp_path) / "b.pdf")

    assert a.sha256 == b.sha256 and "duplicate_document" in {x["code"] for x in de.validate_shipment([a, b])}


def test_extraction_is_deterministic_and_json_serializable(tmp_path):
    path = make_pdf(tmp_path, "d.pdf", [invoice()])

    first, second = extract_document(path), extract_document(path)

    assert first.to_dict() == second.to_dict()
    json.dumps(first.to_dict())


def test_extraction_changes_no_pdf(tmp_path):
    path = make_pdf(tmp_path, "d.pdf", [bol()])
    before = path.read_bytes()

    extract_document(path)

    assert path.read_bytes() == before
