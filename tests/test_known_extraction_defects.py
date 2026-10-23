"""
Regression tests for the six extraction defects D1 to D6
(docs/real_world_evaluation/KNOWN_GAPS.md).

The defects were found with SYNTHETIC layout probes: hand-built variants a freight
document could plausibly have. None was observed on a real carrier document, and
these tests prove only that the extractor now handles these synthetic variants.
They say nothing about real-world accuracy; that is what the real-document
evaluation measures (docs/real_world_evaluation/PROTOCOL.md).

Each defect has the original case, nearby cases that must still work (so the fix
does not match too much) and the cases that must go to review rather than be
guessed.

Run: python -m pytest tests/test_known_extraction_defects.py
"""

import sys
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_extraction as de  # noqa: E402

HEADER = ["FREIGHT INVOICE", "Invoice Number: INV-1", "Shipment Number: SHJ-2026-123"]
POD = ["PROOF OF DELIVERY", "Shipment Number: SHJ-2026-123", "Delivery Date:", "October 5, 2026", "Number of Pieces Delivered:", "45"]
BOL = ["BILL OF LADING", "Shipment Number: SHJ-2026-123", "Number of Pieces:", "10", "Expected Delivery Date:", "October 3, 2026"]


def extract(tmp_path, lines):
    document = pymupdf.open()
    page = document.new_page()

    for index, line in enumerate(lines):
        page.insert_text((30, 50 + index * 13), line, fontsize=9)

    path = Path(tmp_path) / "probe.pdf"
    document.save(str(path))
    document.close()

    return de.extract_document(path)


def invoice(tmp_path, *lines):
    return extract(tmp_path, HEADER + list(lines))


def field_from_text(doc_type, name, text):
    """A field read from page text, without a PDF: for characters the PDF font cannot draw."""

    return de.read_spec_field(name, de.SCHEMAS[doc_type][name], de.parse_pairs(text, 1), "f.pdf", False)


def arithmetic(e):
    return {issue["code"] for issue in de.validate_shipment([e])}


# =========================================================================
# D1: a placeholder is not a printed name
# =========================================================================

PLACEHOLDERS = ["______________", "_ _ _ _ _", "- - - - - - -", "--------", "–––––––", "————", "........", "…………", "[            ]", "[ ]", "( )",
                "☐", "□", "▢", "X ______", "x__________", "X", "(signature)", "[sign here]", "Signature", "Print Name", "N/A", "|          |", "***", "====="]


@pytest.mark.parametrize("text", PLACEHOLDERS)
def test_d1_placeholder_content_is_not_a_name(text):
    assert de.is_placeholder(text)

    item = field_from_text(de.POD, "signature", f"Signature:\n{text}")

    assert item.state == de.MISSING and item.value is None
    assert item.needs_review and any(w.startswith("placeholder_only") for w in item.warnings)


@pytest.mark.parametrize("text", PLACEHOLDERS[:6])
def test_d1_a_blank_received_by_is_missing_and_needs_review_too(text):
    item = field_from_text(de.POD, "received_by", f"Received By:\n{text}")

    assert item.state == de.MISSING and item.needs_review


def test_d1_the_original_case_a_blank_signature_line_needs_review(tmp_path):
    e = extract(tmp_path, POD + ["Signature:", "______________"])

    assert e.fields["signature"].state == de.MISSING and e.fields["signature"].value is None
    assert not e.complete and "signature: needs review (see its warnings)" in e.review_reasons


@pytest.mark.parametrize("name", ["Michael Anderson", "J. O'Neil", "Xavier Cruz", "Mr. X", "Name Tag Express", "Signed Sealed Logistics", "A", "R. Singh-Brown", "M. Anderson (receiving)", "Dock 4"])
def test_d1_legitimate_printed_names_are_preserved(tmp_path, name):
    assert not de.is_placeholder(name)

    e = extract(tmp_path, POD + ["Signature:", name])

    assert e.fields["signature"].state == de.EXTRACTED and e.value("signature") == name and not e.fields["signature"].needs_review
    assert e.complete


def test_d1_a_name_under_a_blank_line_is_read_but_flagged(tmp_path):
    e = extract(tmp_path, POD + ["Signature:", "______________", "Michael Anderson"])
    item = e.fields["signature"]

    assert item.state == de.EXTRACTED and item.value == "Michael Anderson" and item.needs_review
    assert any(w.startswith("placeholder_line_skipped") for w in item.warnings) and not e.complete


def test_d1_a_name_and_a_blank_occurrence_is_extracted_but_needs_review(tmp_path):
    e = extract(tmp_path, POD + ["Signature:", "Michael Anderson", "Signature:", "______________"])

    assert e.value("signature") == "Michael Anderson" and e.fields["signature"].needs_review


def test_d1_a_blank_date_is_missing_not_unparseable_and_a_blank_commodity_is_missing_without_review(tmp_path):
    e = extract(tmp_path, ["BILL OF LADING", "Shipment Number: SHJ-2026-123", "Number of Pieces:", "10", "Expected Delivery Date:", "__________", "Commodity:", "-"])

    assert e.fields["expected_delivery_date"].state == de.MISSING and e.fields["commodity"].state == de.MISSING
    assert not e.fields["commodity"].needs_review
    assert "required field expected_delivery_date is missing" in e.review_reasons


@pytest.mark.parametrize("value", ["TBD", "pending", "illegible", "see attached", "unsigned"])
def test_d1_words_that_say_something_are_not_placeholders(value):
    assert not de.is_placeholder(value)
    assert field_from_text(de.POD, "signature", f"Signature:\n{value}").state == de.EXTRACTED


def test_d1_a_signature_the_fixtures_already_have_is_unchanged():
    e = de.extract_document(PROJECT_ROOT / "tests" / "fixtures" / "shipment_SHJ-2026-001" / "POD_SHJ-2026-001.pdf")

    assert e.value("signature") == "Michael Anderson" and e.complete


# =========================================================================
# D2: several labels on one line
# =========================================================================

def test_d2_the_original_case_shipper_and_consignee_on_one_line(tmp_path):
    e = extract(tmp_path, BOL + ["Shipper: Acme Parts        Consignee: Beta Works"])

    assert e.value("shipper") == {"name": "Acme Parts", "address_lines": []}
    assert e.value("consignee") == {"name": "Beta Works", "address_lines": []}


@pytest.mark.parametrize("line, shipper, consignee", [
    ("Shipper: Acme Parts        Consignee: Beta Works", "Acme Parts", "Beta Works"),
    ("Shipper: Acme Parts Consignee: Beta Works", "Acme Parts", "Beta Works"),                                   # single space
    ("Consignee: Beta Works        Shipper: Acme Parts", "Acme Parts", "Beta Works"),                            # the other order
    ("SHIPPER: ACME PARTS   CONSIGNEE: BETA WORKS", "ACME PARTS", "BETA WORKS"),                                 # capitals
    ("shipper: Acme Parts   consignee: Beta Works", "Acme Parts", "Beta Works"),                                 # lower case
    ("Shipper: Acme, Inc.   Consignee: Beta & Sons (Texas)", "Acme, Inc.", "Beta & Sons (Texas)"),            # punctuation
    ("Ship From: Acme Parts   Ship To: Beta Works", "Acme Parts", "Beta Works"),                                 # the alternative labels
    ("Shipper : Acme Parts   Consignee : Beta Works", None, None),                                                # a space before the colon is not a label of this kind
])
def test_d2_orderings_spacing_and_punctuation(line, shipper, consignee):
    pairs = de.parse_pairs(line, 1)

    if shipper is None:
        assert len(pairs) <= 2
        return

    by_key = {pair.key: pair for pair in pairs}
    first = field_from_text(de.BOL, "shipper", line)
    second = field_from_text(de.BOL, "consignee", line)

    assert first.value == {"name": shipper, "address_lines": []} and second.value == {"name": consignee, "address_lines": []}
    assert len(by_key) == 2


def test_d2_three_labels_on_one_line_each_stop_at_the_next():
    line = "Shipper: Acme Parts   Consignee: Beta Works   Carrier: Test Lines"

    assert field_from_text(de.BOL, "shipper", line).value["name"] == "Acme Parts"
    assert field_from_text(de.BOL, "consignee", line).value["name"] == "Beta Works"
    assert field_from_text(de.BOL, "carrier", line).value == "Test Lines"


def test_d2_dates_and_references_on_one_line_are_separate_fields():
    line = "Invoice Number: INV-77   Invoice Date: October 4, 2026   Due Date: November 3, 2026"

    assert field_from_text(de.INVOICE, "invoice_number", line).value == "INV-77"
    assert field_from_text(de.INVOICE, "invoice_date", line).value == "2026-10-04"
    assert field_from_text(de.INVOICE, "due_date", line).value == "2026-11-03"


@pytest.mark.parametrize("line, shipper, consignee", [
    ("Shipper: Ship To Go Logistics   Consignee: Weight Watchers Ltd", "Ship To Go Logistics", "Weight Watchers Ltd"),   # label words, no colon
    ("Shipper: Carrier Services   Consignee: Origin Foods", "Carrier Services", "Origin Foods"),
    ("Shipper: Total Freight Rate Inc   Consignee: Equipment Rental Co", "Total Freight Rate Inc", "Equipment Rental Co"),
])
def test_d2_values_made_of_ordinary_words_that_resemble_labels_are_not_cut(line, shipper, consignee):
    assert field_from_text(de.BOL, "shipper", line).value["name"] == shipper
    assert field_from_text(de.BOL, "consignee", line).value["name"] == consignee


def test_d2_a_label_made_of_words_is_one_label_not_a_split():
    assert [pair.label for pair in de.parse_pairs("Base Rate: $100.00", 1)] == ["Base Rate"]
    assert [pair.label for pair in de.parse_pairs("Fuel Rate: $18.00", 1)] == ["Fuel Rate"]
    assert [pair.label for pair in de.parse_pairs("Total Weight: 1,200 lbs", 1)] == ["Total Weight"]


def test_d2_a_label_in_the_middle_of_a_word_is_not_a_label():
    pairs = de.parse_pairs("Shipper: Acme Subconsignee: x", 1)

    assert [pair.key for pair in pairs] == ["shipper"] and pairs[0].lines == ["Acme Subconsignee: x"]


def test_d2_a_single_label_line_and_a_single_colon_value_are_unchanged():
    pairs = de.parse_pairs("Shipper: Acme Parts\nDallas, Texas\nConsignee:\nBeta Works", 1)

    assert [(pair.key, pair.lines, pair.ambiguous) for pair in pairs] == [("shipper", ["Acme Parts", "Dallas, Texas"], False), ("consignee", ["Beta Works"], False)]


def test_d2_lines_under_a_line_of_several_labels_cannot_be_assigned_and_need_review(tmp_path):
    e = extract(tmp_path, BOL + ["Shipper: Acme Parts        Consignee: Beta Works", "12 Dock Rd                  45 Port Ave"])

    for name in ("shipper", "consignee"):
        assert e.fields[name].needs_review and any(w.startswith("columns_ambiguous") for w in e.fields[name].warnings)

    assert not e.complete


def test_d2_addresses_on_their_own_lines_are_unambiguous(tmp_path):
    e = extract(tmp_path, BOL + ["Shipper:", "Acme Parts", "12 Dock Rd", "Consignee:", "Beta Works", "45 Port Ave"])

    assert e.value("shipper") == {"name": "Acme Parts", "address_lines": ["12 Dock Rd"]} and not e.fields["shipper"].needs_review
    assert e.value("consignee") == {"name": "Beta Works", "address_lines": ["45 Port Ave"]}


def test_d2_label_only_labels_side_by_side_are_both_missing_not_merged():
    pairs = de.parse_pairs("Shipper:   Consignee:", 1)

    assert [(pair.key, pair.lines) for pair in pairs] == [("shipper", []), ("consignee", [])]


# =========================================================================
# D3: a total is never a charge
# =========================================================================

def test_d3_the_original_case_total_is_the_invoice_total_and_not_a_charge(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Total:", "$100.00")

    assert e.value("invoice_total") == 100.0
    assert [item.description for item in e.line_items] == ["Base Freight"]
    assert e.complete and "invoice_arithmetic_mismatch" not in arithmetic(e)


@pytest.mark.parametrize("label", list(de.SCHEMAS[de.INVOICE]["invoice_total"].labels) + list(de.SCHEMAS[de.INVOICE]["tax"].labels) + list(de.SCHEMAS[de.INVOICE]["subtotal"].labels))
def test_d3_no_label_the_schema_uses_for_a_total_tax_or_subtotal_can_be_a_charge(tmp_path, label):
    e = invoice(tmp_path, "Base Freight:", "$100.00", f"{label.title()}:", "$7.00")

    assert [item.description for item in e.line_items] == ["Base Freight"] and label in de.NON_ITEM_LABELS


def test_d3_the_same_total_written_twice_is_one_total(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Total Invoice Amount:", "$100.00", "Total:", "$100.00")

    assert e.fields["invoice_total"].state == de.EXTRACTED and e.value("invoice_total") == 100.0 and e.complete


def test_d3_two_different_totals_conflict_and_the_invoice_needs_review(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Total Invoice Amount:", "$100.00", "Total:", "$120.00")

    assert e.fields["invoice_total"].state == de.CONFLICTING and not e.complete
    assert "invoice_arithmetic_mismatch" not in arithmetic(e)


@pytest.mark.parametrize("label", ["Net Total", "Total Charges", "Invoice Grand Total", "Total (USD)", "Subtotal Before Tax"])
def test_d3_any_other_label_with_the_word_total_is_excluded_not_a_charge(tmp_path, label):
    e = invoice(tmp_path, "Base Freight:", "$100.00", f"{label}:", "$100.00")

    assert [item.description for item in e.line_items] == ["Base Freight"]
    assert e.excluded_amounts and {item["reason"] for item in e.excluded_amounts} <= {"total_like_label", "not_a_charge"}


def test_d3_a_total_tax_amount_is_a_tax_not_a_total_and_is_not_dropped(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Total Tax Amount:", "$8.00", "Total Invoice Amount:", "$108.00")

    assert [item.description for item in e.line_items] == ["Base Freight"] and not e.complete
    assert any(item["reason"] == "tax_without_field" for item in e.unclassified_amounts)


def test_d3_with_only_a_total_like_label_the_total_is_still_missing(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Net Total:", "$100.00")

    assert e.fields["invoice_total"].state == de.MISSING and not e.complete


def test_d3_words_that_only_contain_total_are_still_charges(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Subtotally Fee:", "$5.00", "Totally Fast Delivery:", "$10.00", "Total:", "$115.00")

    assert [item.description for item in e.line_items] == ["Base Freight", "Subtotally Fee", "Totally Fast Delivery"]
    assert "invoice_arithmetic_mismatch" not in arithmetic(e)


def test_d3_arithmetic_is_unchecked_when_the_charges_are_incomplete_or_ambiguous(tmp_path):
    ambiguous = invoice(tmp_path, "Base Freight:", "$100.00", "Handling:", "25", "Total:", "$125.00")
    missing = invoice(tmp_path, "Total:", "$125.00")

    assert "invoice_arithmetic_unchecked" in arithmetic(ambiguous) and "invoice_arithmetic_mismatch" not in arithmetic(ambiguous)
    assert "invoice_arithmetic_unchecked" in arithmetic(missing) and not missing.line_items


def test_d3_arithmetic_still_finds_a_real_mismatch(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Fuel Surcharge:", "$10.00", "Total:", "$150.00")

    assert "invoice_arithmetic_mismatch" in arithmetic(e)


# =========================================================================
# D4: VAT and other taxes
# =========================================================================

def test_d4_the_original_case_vat_with_a_rate_is_the_tax_field(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "VAT 20%:", "$20.00", "Total Invoice Amount:", "$120.00")

    assert e.value("tax") == 20.0 and e.value("invoice_total") == 120.0
    assert [item.description for item in e.line_items] == ["Base Freight"] and not e.unclassified_amounts
    assert e.complete and not (arithmetic(e) & {"invoice_arithmetic_mismatch", "invoice_arithmetic_unchecked"})


@pytest.mark.parametrize("label", ["VAT 20%", "VAT (20%)", "VAT (20 %)", "VAT 20 %", "GST 5%", "HST 13%", "Sales Tax 8.25%", "Tax (8%)", "Total Tax 20%"])
def test_d4_tax_labels_with_a_rate_are_the_tax_field(tmp_path, label):
    e = invoice(tmp_path, "Base Freight:", "$100.00", f"{label}:", "$20.00", "Total Invoice Amount:", "$120.00")

    assert e.value("tax") == 20.0 and len(e.line_items) == 1 and e.complete


def test_d4_the_pair_keeps_the_rate_out_of_its_key_and_records_it():
    pair = de.parse_pairs("VAT (20%): $20.00", 1)[0]

    assert pair.key == "vat" and pair.percent == 20.0 and pair.lines == ["$20.00"]


def test_d4_vat_is_not_counted_twice_when_it_is_written_as_tax_and_as_vat(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Tax:", "$20.00", "VAT 20%:", "$20.00", "Total Invoice Amount:", "$120.00")

    assert e.fields["tax"].state == de.EXTRACTED and e.value("tax") == 20.0 and not e.unclassified_amounts
    assert "invoice_arithmetic_mismatch" not in arithmetic(e) and e.complete


def test_d4_two_different_tax_lines_conflict_instead_of_being_summed_or_dropped(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "GST 5%:", "$5.00", "HST 13%:", "$13.00", "Total Invoice Amount:", "$118.00")

    assert e.fields["tax"].state == de.CONFLICTING and not e.complete
    assert not (arithmetic(e) & {"invoice_arithmetic_mismatch"}) and "invoice_arithmetic_unchecked" in arithmetic(e)


@pytest.mark.parametrize("label", ["Subtotal Before Tax", "Total Incl. Tax", "Total Excluding VAT", "Total With GST"])
def test_d4_a_total_that_mentions_tax_is_a_total_not_a_tax_amount(tmp_path, label):
    e = invoice(tmp_path, "Base Freight:", "$100.00", f"{label}:", "$100.00")

    assert [item.description for item in e.line_items] == ["Base Freight"] and not e.unclassified_amounts


@pytest.mark.parametrize("label", ["Provincial Sales Tax", "Import VAT", "PST", "QST 9.975%", "Excise Taxes"])
def test_d4_a_tax_this_extractor_has_no_field_for_needs_review_and_is_not_a_charge(tmp_path, label):
    e = invoice(tmp_path, "Base Freight:", "$100.00", f"{label}:", "$7.00", "Total Invoice Amount:", "$107.00")

    assert [item.description for item in e.line_items] == ["Base Freight"] and not e.complete
    assert any(item["reason"] == "tax_without_field" for item in e.unclassified_amounts)
    assert "invoice_arithmetic_unchecked" in arithmetic(e)


def test_d4_a_tax_line_without_an_amount_does_not_pass_silently(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "VAT 20%:", "included", "Total Invoice Amount:", "$100.00")

    assert e.fields["tax"].state == de.UNPARSEABLE and not e.complete


def test_d4_a_vat_registration_number_is_not_a_tax_amount(tmp_path):
    e = invoice(tmp_path, "VAT Number:", "GB123456789", "VAT Reg No:", "GB123456789", "Base Freight:", "$100.00", "Total Invoice Amount:", "$100.00")

    assert e.fields["tax"].state == de.MISSING and not e.unclassified_amounts and e.complete


def test_d4_a_charge_whose_label_carries_a_rate_is_still_a_charge(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Fuel Surcharge 18%:", "$18.00", "Total Invoice Amount:", "$118.00")

    assert [(item.description, item.amount) for item in e.line_items] == [("Base Freight", 100.0), ("Fuel Surcharge 18%", 18.0)] and e.complete


def test_d4_an_amount_after_a_label_with_a_percent_sign_is_never_attached_to_the_line_above(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Late Fee 2%:", "$2.00", "Total Invoice Amount:", "$102.00")

    assert [(item.description, item.amount) for item in e.line_items] == [("Base Freight", 100.0), ("Late Fee 2%", 2.0)]


# =========================================================================
# D5: several amounts on one line
# =========================================================================

def test_d5_the_original_case_two_labelled_charges_are_separated_and_marked(tmp_path):
    e = invoice(tmp_path, "Base Freight: $100.00   Fuel: $18.00", "Total Invoice Amount: $118.00")

    assert [(item.description, item.amount) for item in e.line_items] == [("Base Freight", 100.0), ("Fuel", 18.0)]
    assert all("split_from_one_line" in item.warnings for item in e.line_items)
    assert e.complete and "invoice_arithmetic_mismatch" not in arithmetic(e)


def test_d5_three_labelled_charges_on_one_line(tmp_path):
    e = invoice(tmp_path, "Base Freight: $100.00   Fuel: $18.00   Detention: $25.00", "Total Invoice Amount: $143.00")

    assert [item.amount for item in e.line_items] == [100.0, 18.0, 25.0] and e.complete


@pytest.mark.parametrize("line", [
    "Base Freight: $100.00 and Fuel $18.00",             # a second amount with no label and a colon
    "Base Freight: $100.00 (incl. $5.00 fee)",           # an amount inside a remark
    "Base Freight: $100.00 / $18.00",
    "Base Freight: $100.00 USD $18.00 EUR",
])
def test_d5_amounts_that_cannot_be_separated_are_unresolved_and_the_invoice_needs_review(tmp_path, line):
    e = invoice(tmp_path, line, "Total Invoice Amount: $118.00")

    assert not e.line_items and not e.complete
    assert any(item["reason"] == "multiple_amounts_on_one_line" for item in e.unclassified_amounts)
    assert "invoice_arithmetic_unchecked" in arithmetic(e) and "invoice_arithmetic_mismatch" not in arithmetic(e)


def test_d5_a_charge_on_the_line_below_the_value_is_not_dropped_silently(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Fuel Surcharge $18.00", "Total Invoice Amount:", "$118.00")

    assert [item.amount for item in e.line_items] == [100.0] and not e.complete
    assert any(item["reason"] == "amount_on_following_line" for item in e.unclassified_amounts)


def test_d5_a_single_charge_with_text_the_parser_cannot_read_is_unresolved(tmp_path):
    e = invoice(tmp_path, "Handling: $12.50 per pallet", "Total Invoice Amount: $12.50")

    assert not e.line_items and not e.complete and any(item["reason"] == "amount_not_understood" for item in e.unclassified_amounts)


@pytest.mark.parametrize("line, expected", [
    ("Base Freight: $1,200.00", [("Base Freight", 1200.0)]),
    ("Storage: 2 x $15.00 = $30.00", [("Storage", 30.0)]),
    ("Credit: ($20.00)", [("Credit", -20.0)]),
    ("Lift Gate Service: CAD 40.00", [("Lift Gate Service", 40.0)]),
    ("Lumper Fee: 35.00 USD", [("Lumper Fee", 35.0)]),
])
def test_d5_ordinary_single_charges_are_unchanged_and_not_marked_split(tmp_path, line, expected):
    e = invoice(tmp_path, line, "Total Invoice Amount: $1.00")

    assert [(item.description, item.amount) for item in e.line_items] == expected and not any("split_from_one_line" in i.warnings for i in e.line_items)
    assert not e.unclassified_amounts


@pytest.mark.parametrize("line", ["Notes: Thank you for your business", "PO Number: 12345", "Remit To: PO Box 12, Dallas", "Contact: 555-0100", "Terms: Net 30, 2% 10 days"])
def test_d5_text_without_a_marked_amount_is_not_mistaken_for_a_charge_or_flagged(tmp_path, line):
    e = invoice(tmp_path, line, "Base Freight: $100.00", "Total Invoice Amount: $100.00")

    # "2% 10 days" is a payment term, and no amount follows its label: nothing to account for
    assert [item.description for item in e.line_items] == ["Base Freight"] and not e.unclassified_amounts and e.complete


# =========================================================================
# D6: a percentage is not an amount
# =========================================================================

def test_d6_the_original_case_a_percentage_fuel_surcharge_is_recorded_as_a_percentage(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Fuel Surcharge:", "18%", "Total Invoice Amount:", "$118.00")

    assert e.percentage_charges == [{"label": "Fuel Surcharge", "percent": 18.0, "page": 1}]
    assert [item.description for item in e.line_items] == ["Base Freight"] and not any(item.description == "Fuel Surcharge" for item in e.line_items)
    assert not e.complete and any(r.startswith("percentage_charges") for r in e.review_reasons)
    assert "invoice_arithmetic_unchecked" in arithmetic(e) and "invoice_arithmetic_mismatch" not in arithmetic(e)


@pytest.mark.parametrize("value, percent", [("18%", 18.0), ("18.5%", 18.5), ("18 %", 18.0), ("18% of linehaul", 18.0), ("(5%)", 5.0), ("18% ($18.00)", 18.0)])
def test_d6_percentage_forms_are_recognised_and_no_amount_is_invented(tmp_path, value, percent):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Fuel Surcharge:", value, "Total Invoice Amount:", "$118.00")

    assert [c["percent"] for c in e.percentage_charges] == [percent] and len(e.line_items) == 1 and not e.complete


def test_d6_a_percentage_tax_is_unparseable_with_the_reason_and_never_an_amount(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Sales Tax:", "8%", "Total Invoice Amount:", "$108.00")

    assert e.fields["tax"].state == de.UNPARSEABLE and e.fields["tax"].value is None and not e.complete
    assert any(w.startswith("percentage_without_basis") for w in e.fields["tax"].warnings)


def test_d6_a_percentage_fuel_surcharge_on_a_rate_confirmation_is_not_an_amount(tmp_path):
    e = extract(tmp_path, ["RATE CONFIRMATION", "Shipment Number: SHJ-2026-123", "Agreed Rate:", "$1,850.00", "Fuel Surcharge:", "18%"])

    assert e.value("agreed_rate") == 1850.0 and e.fields["fuel_surcharge"].state == de.UNPARSEABLE and e.fields["fuel_surcharge"].value is None
    assert not e.complete


def test_d6_amounts_that_merely_contain_a_percent_sign_after_an_amount_are_unresolved_not_dropped(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00 (18%)", "Total Invoice Amount:", "$100.00")

    assert not e.line_items and not e.complete and any(item["reason"] == "amount_not_understood" for item in e.unclassified_amounts)


@pytest.mark.parametrize("line", ["Fuel Surcharge: $18.00", "Fuel Surcharge: 18.00 USD", "Tax ID: 12-3456789", "Contract No: 18-2026"])
def test_d6_money_and_identifiers_are_not_percentages(tmp_path, line):
    e = invoice(tmp_path, line, "Base Freight: $100.00", "Total Invoice Amount: $118.00")

    assert not e.percentage_charges


def test_d6_a_percentage_in_a_label_with_an_amount_is_a_charge_not_a_percentage_charge(tmp_path):
    e = invoice(tmp_path, "Base Freight:", "$100.00", "Fuel Surcharge (18%):", "$18.00", "Total Invoice Amount:", "$118.00")

    assert not e.percentage_charges and [item.amount for item in e.line_items] == [100.0, 18.0] and e.complete


# =========================================================================
# Across the defects
# =========================================================================

def test_the_fixtures_and_a_clean_invoice_are_still_complete_and_unflagged(tmp_path):
    e = de.extract_document(PROJECT_ROOT / "tests" / "fixtures" / "shipment_SHJ-2026-001" / "INVOICE_SHJ-2026-001.pdf")

    assert e.complete and not e.unclassified_amounts and not e.percentage_charges and len(e.line_items) == 4

    clean = invoice(tmp_path, "Base Freight:", "$100.00", "Fuel Surcharge:", "$18.00", "Tax:", "$11.80", "Total Invoice Amount:", "$129.80")

    assert clean.complete and not (arithmetic(clean) & {"invoice_arithmetic_mismatch", "invoice_arithmetic_unchecked"})


def test_nothing_the_fixes_record_quotes_the_document(tmp_path):
    secret = "ZQX-SECRET-3141"
    e = invoice(tmp_path, f"{secret}: 18%", f"Handling {secret}: $12.50 per pallet", "Base Freight:", "$100.00", "Total Invoice Amount:", "$100.00")
    text = repr([e.review_reasons, e.warnings, [f.warnings for f in e.fields.values()]])

    assert secret not in text
