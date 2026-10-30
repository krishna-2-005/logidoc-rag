"""
Freight audit: three-way matching of the carrier invoice against the rate
confirmation (contract) and the BOL/POD (execution), charge by charge.

The extractions are built directly (the extractor itself is tested in
test_document_extraction.py); the last tests run the audit on extractions
of real generated PDFs through the audit service and the API.

Run from the project root:
    python -m pytest tests/test_freight_audit.py
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_extraction as de
import freight_audit as fa
from document_extraction import (
    BOL, CONFLICTING, EXTRACTED, INVOICE, MISSING, POD, RATE, DocumentExtraction, ExtractedField, LineItem,
)


# ============================================================
# DOCUMENTS
# ============================================================

def extraction(document_type, filename, items=(), sha=None, unclassified=(), **values):

    fields = {}

    for name, value in values.items():
        if value is MISSING or value is CONFLICTING:
            fields[name] = ExtractedField(name=name, state=value)
        else:
            fields[name] = ExtractedField(name=name, state=EXTRACTED, value=value, page=1, source_file=filename)

    return DocumentExtraction(
        filename=filename, sha256=sha or filename, document_type=document_type, status=de.S_EXTRACTED, fields=fields,
        line_items=[LineItem(description=label, amount=amount, page=1, source_file=filename) for label, amount in items],
        unclassified_amounts=list(unclassified),
    )


SHIPMENT = "SHJ-2026-101"


def rate(**values):

    defaults = dict(shipment_id=SHIPMENT, carrier="Swift Lines", origin="Chicago, Illinois", destination="Atlanta, Georgia",
                    pickup_date="2026-10-01", delivery_date="2026-10-03", agreed_rate=1400.0, fuel_surcharge=180.0,
                    currency="USD", payment_terms="Net 30", weight={"amount": 1000.0, "unit": "lb"})

    return extraction(RATE, "RATE_CONFIRMATION.pdf", **{k: v for k, v in {**defaults, **values}.items() if v is not None})


def invoice(items=(("Base Freight", 1400.0), ("Fuel Surcharge", 180.0)), total=1580.0, filename="INVOICE.pdf", sha=None,
            **values):

    defaults = dict(shipment_id=SHIPMENT, invoice_number="INV-101", carrier="Swift Lines", origin="Chicago, Illinois",
                    destination="Atlanta, Georgia", currency="USD", payment_terms="Net 30", invoice_total=total)

    return extraction(INVOICE, filename, items=items, sha=sha,
                      **{k: v for k, v in {**defaults, **values}.items() if v is not None})


def bol(**values):

    defaults = dict(shipment_id=SHIPMENT, carrier="Swift Lines", origin="Chicago, Illinois", destination="Atlanta, Georgia",
                    pickup_date="2026-10-01", expected_delivery_date="2026-10-03", weight={"amount": 1000.0, "unit": "lb"})

    return extraction(BOL, "BOL.pdf", **{k: v for k, v in {**defaults, **values}.items() if v is not None})


def pod(**values):

    defaults = dict(shipment_id=SHIPMENT, carrier="Swift Lines", delivery_date="2026-10-03")

    return extraction(POD, "POD.pdf", **{k: v for k, v in {**defaults, **values}.items() if v is not None})


def audit(*documents, texts=(), policy=None, others=()):

    documents = documents or (rate(), bol(), pod(), invoice())

    return fa.audit_freight(documents, texts=texts, policy=policy or fa.load_policy(""), other_invoices=others)


def charge(result, name):

    return next(item for item in result["charges"] if item["charge"] == name)


def checked(result, name):

    return next(item for item in result["checks"] if item["check"] == name)


# ============================================================
# TOLERANCE POLICY
# ============================================================

def test_the_default_policy_is_exact_matching():

    policy = fa.load_policy("")

    assert policy == {"default": fa.Tolerance()}
    assert fa.rule_of(policy, "fuel_surcharge").describe() == "exact match"


def test_a_configured_rule_overrides_only_itself():

    policy = fa.load_policy('{"fuel_surcharge": {"absolute": 5, "percent": 2}}')

    assert fa.rule_of(policy, "fuel_surcharge") == fa.Tolerance(Decimal("5"), Decimal("2"))
    assert fa.rule_of(policy, "base_freight") == fa.Tolerance()


@pytest.mark.parametrize("text, message", [
    ("{not json", "not valid JSON"),
    ("[1, 2]", "JSON object"),
    ('{"freight": {"absolute": 1}}', "Unknown tolerance rule"),
    ('{"default": {"amount": 1}}', "only 'absolute' and 'percent'"),
    ('{"default": {"absolute": -1}}', "must not be negative"),
    ('{"default": {"percent": "lots"}}', "not a number"),
])
def test_unusable_policies_are_refused(text, message):

    with pytest.raises(fa.PolicyError, match=message):
        fa.load_policy(text)


@pytest.mark.parametrize("expected, billed, tolerance, outcome", [
    ("100.00", "100.00", fa.Tolerance(), fa.MATCHED),
    ("100.00", "100.01", fa.Tolerance(), fa.DISCREPANCY),
    ("100.00", "104.00", fa.Tolerance(absolute=Decimal("5")), fa.MATCHED_WITH_TOLERANCE),
    ("100.00", "106.00", fa.Tolerance(absolute=Decimal("5")), fa.DISCREPANCY),
    ("1000.00", "1019.00", fa.Tolerance(percent=Decimal("2")), fa.MATCHED_WITH_TOLERANCE),
    ("1000.00", "979.00", fa.Tolerance(percent=Decimal("2")), fa.DISCREPANCY),
])
def test_compare_amounts(expected, billed, tolerance, outcome):

    assert fa.compare_amounts(Decimal(expected), Decimal(billed), tolerance)[0] == outcome


# ============================================================
# CHARGE-LEVEL THREE-WAY MATCH
# ============================================================

def test_everything_agrees():

    result = audit()

    assert result["outcome"] == fa.MATCHED
    assert {item["charge"]: item["outcome"] for item in result["charges"]} == {
        "Base freight": fa.MATCHED,
        "Fuel surcharge": fa.MATCHED,
        "Line items against the invoice total": fa.MATCHED,
        "Invoice total against the contract": fa.MATCHED,
    }
    assert result["documents"] == {"contract": ["RATE_CONFIRMATION.pdf"], "execution": ["BOL.pdf", "POD.pdf"],
                                   "invoice": ["INVOICE.pdf"]}
    assert result["explanation"] == ["Every charge and reference agrees with the contract and the execution documents."]


def test_each_comparison_carries_amounts_variance_currency_and_sources():

    line = charge(audit(), "Fuel surcharge")

    assert (line["expected"], line["billed"], line["variance"], line["currency"]) == (180.0, 180.0, 0.0, "USD")
    assert line["expected_source"] == {"file": "RATE_CONFIRMATION.pdf", "page": 1, "document_type": RATE}
    assert line["billed_source"]["file"] == "INVOICE.pdf" and line["billed_source"]["lines"] == ["Fuel Surcharge"]


def test_an_overbilled_fuel_surcharge_is_a_discrepancy():

    result = audit(rate(), bol(), pod(), invoice(items=(("Base Freight", 1400.0), ("Fuel Surcharge", 200.0)), total=1600.0))

    fuel = charge(result, "Fuel surcharge")

    assert fuel["outcome"] == fa.DISCREPANCY and fuel["variance"] == 20.0
    assert "USD 20.00 above the contracted USD 180.00, outside the tolerance (exact match)" in fuel["explanation"]
    assert result["outcome"] == fa.DISCREPANCY
    assert result["financial_impact"]["billed_above_contract"] == 20.0
    assert any(reason.startswith("Fuel surcharge:") for reason in result["explanation"])


def test_a_difference_inside_the_tolerance():

    policy = fa.load_policy('{"fuel_surcharge": {"absolute": 25}}')
    result = audit(rate(), bol(), pod(), invoice(items=(("Base Freight", 1400.0), ("Fuel Surcharge", 200.0)), total=1600.0),
                   policy=policy)

    assert charge(result, "Fuel surcharge")["outcome"] == fa.MATCHED_WITH_TOLERANCE
    # The total differs by exactly the tolerated amount
    assert charge(result, "Invoice total against the contract")["outcome"] == fa.MATCHED_WITH_TOLERANCE
    assert result["outcome"] == fa.MATCHED_WITH_TOLERANCE
    assert result["tolerances"]["fuel_surcharge"] == {"absolute": 25.0, "percent": 0.0}


def test_an_unbilled_contracted_fuel_surcharge_is_reported_not_invented():

    result = audit(rate(), bol(), pod(), invoice(items=(("Base Freight", 1400.0),), total=1400.0))

    fuel = charge(result, "Fuel surcharge")

    assert fuel["billed"] == 0.0 and fuel["variance"] == -180.0
    assert "The invoice bills no fuel surcharge." in fuel["explanation"]
    assert result["financial_impact"]["billed_above_contract"] == 0.0


def test_no_rate_confirmation_is_insufficient_evidence():

    result = audit(bol(), pod(), invoice())

    assert result["outcome"] == fa.INSUFFICIENT_EVIDENCE
    assert charge(result, "Base freight")["expected"] is None
    assert result["explanation"][0].startswith("No rate confirmation belongs to this shipment")


def test_no_invoice_has_nothing_to_audit():

    result = audit(rate(), bol(), pod())

    assert result["outcome"] == fa.INSUFFICIENT_EVIDENCE
    assert result["explanation"] == ["No carrier invoice belongs to this shipment, so there is nothing to audit."]


def test_a_missing_agreed_rate_is_never_treated_as_zero():

    result = audit(rate(agreed_rate=MISSING), bol(), pod(), invoice())

    base = charge(result, "Base freight")

    assert base["outcome"] == fa.INSUFFICIENT_EVIDENCE and base["expected"] is None
    assert "agreed rate is missing" in base["explanation"]


def test_conflicting_agreed_rates_need_review():

    assert charge(audit(rate(agreed_rate=CONFLICTING), bol(), pod(), invoice()), "Base freight")["outcome"] == fa.PENDING_REVIEW


def test_unreadable_invoice_charges_are_insufficient_evidence():

    unreadable = invoice(items=(("Base Freight", 1400.0),), total=1580.0)
    unreadable.unclassified_amounts = [{"label": "Fuel", "page": 1, "reason": "amount_not_understood"}]

    result = audit(rate(), bol(), pod(), unreadable)

    assert charge(result, "Fuel surcharge")["outcome"] == fa.INSUFFICIENT_EVIDENCE
    assert charge(result, "Line items against the invoice total")["outcome"] == fa.INSUFFICIENT_EVIDENCE


def test_different_currencies_are_not_compared():

    result = audit(rate(currency="CAD"), bol(), pod(), invoice())

    assert charge(result, "Base freight")["outcome"] == fa.PENDING_REVIEW
    assert checked(result, "Currency")["outcome"] == fa.DISCREPANCY
    assert result["currency"] is None


def test_several_invoices_need_a_person():

    result = audit(rate(), bol(), pod(), invoice(), invoice(filename="INVOICE_2.pdf", invoice_number="INV-102"))

    assert charge(result, "Invoice charges")["outcome"] == fa.PENDING_REVIEW
    assert "2 invoices" in charge(result, "Invoice charges")["explanation"]


# ============================================================
# ONE RATE WITHOUT A SEPARATE FUEL SURCHARGE
# ============================================================

def test_single_rate_read_as_all_in():

    result = audit(rate(agreed_rate=1580.0, fuel_surcharge=None), bol(), pod(), invoice())

    line = charge(result, "Base freight and fuel surcharge")

    assert line["outcome"] == fa.MATCHED
    assert "read as an all-in rate" in line["explanation"]
    assert result["outcome"] == fa.MATCHED


def test_single_rate_read_as_line_haul_leaves_fuel_for_review():

    result = audit(rate(fuel_surcharge=None), bol(), pod(), invoice())

    assert charge(result, "Base freight")["outcome"] == fa.MATCHED
    assert charge(result, "Fuel surcharge")["outcome"] == fa.PENDING_REVIEW
    assert charge(result, "Invoice total against the contract")["outcome"] == fa.PENDING_REVIEW
    assert result["outcome"] == fa.PENDING_REVIEW


def test_single_rate_matching_neither_reading_is_not_guessed():

    result = audit(rate(agreed_rate=1500.0, fuel_surcharge=None), bol(), pod(), invoice())

    line = charge(result, "Base freight and fuel surcharge")

    assert line["outcome"] == fa.PENDING_REVIEW
    assert "does not say whether the rate includes fuel" in line["explanation"]
    assert result["outcome"] == fa.PENDING_REVIEW


# ============================================================
# ACCESSORIALS: CONTRACT AND EXECUTION EVIDENCE
# ============================================================

def with_accessorial(label, amount):

    return invoice(items=(("Base Freight", 1400.0), ("Fuel Surcharge", 180.0), (label, amount)), total=1580.0 + amount)


def text(document_type, body, source="X.pdf", page=1):

    return {"source": source, "page": page, "document_type": document_type, "text": body}


def test_an_accessorial_with_execution_evidence_waits_for_approval():

    texts = [text(POD, "Delivery Status: Delivered. Driver waited; detention 2 hours at consignee.", "POD.pdf", 1)]

    result = audit(rate(), bol(), pod(), with_accessorial("Detention Charge", 75.0), texts=texts)

    line = charge(result, "Detention Charge")

    assert line["outcome"] == fa.PENDING_REVIEW
    assert "the POD mentions it (POD.pdf, page 1)" in line["explanation"]
    assert line["evidence"][0]["document_type"] == POD and "detention" in line["evidence"][0]["text"]
    # The total differs by exactly that charge, so it is not called a discrepancy
    assert charge(result, "Invoice total against the contract")["outcome"] == fa.PENDING_REVIEW
    assert result["outcome"] == fa.PENDING_REVIEW
    assert result["financial_impact"]["pending_review"] == 75.0


def test_an_accessorial_named_in_the_contract():

    texts = [text(RATE, "Accessorials: Detention $50.00 per hour after 2 free hours.", "RATE_CONFIRMATION.pdf", 2)]

    line = charge(audit(rate(), bol(), pod(), with_accessorial("Detention Charge", 100.0), texts=texts), "Detention Charge")

    assert line["outcome"] == fa.PENDING_REVIEW
    assert "The rate confirmation mentions it (RATE_CONFIRMATION.pdf, page 2)" in line["explanation"]


def test_an_accessorial_without_any_evidence_is_not_called_invalid():

    texts = [text(POD, "Delivery Status: Delivered.", "POD.pdf")]

    result = audit(rate(), bol(), pod(), with_accessorial("Lumper Fee", 50.0), texts=texts)

    line = charge(result, "Lumper Fee")

    assert line["outcome"] == fa.INSUFFICIENT_EVIDENCE
    assert "Ask the carrier for supporting documents" in line["explanation"]
    assert result["financial_impact"]["without_evidence"] == 50.0


def test_a_generic_accessorial_label_cannot_be_evidenced():

    texts = [text(POD, "Other remarks: none.", "POD.pdf")]

    line = charge(audit(rate(), bol(), pod(), with_accessorial("Other Accessorial Charges", 50.0), texts=texts),
                  "Other Accessorial Charges")

    assert line["outcome"] == fa.INSUFFICIENT_EVIDENCE
    assert "The label does not say what the charge is" in line["explanation"]


def test_an_accessorial_of_zero_is_nothing_billed():

    line = charge(audit(rate(), bol(), pod(), with_accessorial("Detention Charge", 0.0)), "Detention Charge")

    assert line["outcome"] == fa.MATCHED and line["explanation"] == "Nothing billed."


def test_evidence_from_another_documents_type_does_not_count():

    # The invoice itself naming the charge is not evidence for it
    texts = [text(INVOICE, "Detention Charge: $75.00", "INVOICE.pdf")]

    assert charge(audit(rate(), bol(), pod(), with_accessorial("Detention Charge", 75.0), texts=texts),
                  "Detention Charge")["outcome"] == fa.INSUFFICIENT_EVIDENCE


# ============================================================
# REFERENCES AND DUPLICATES
# ============================================================

def test_a_carrier_that_differs_from_the_contract():

    found = checked(audit(rate(carrier="Blue Ridge Haulers"), bol(), pod(), invoice()), "Carrier")

    assert found["outcome"] == fa.DISCREPANCY
    assert "Blue Ridge Haulers (RATE_CONFIRMATION.pdf)" in found["explanation"]


def test_a_pickup_one_day_after_the_contract():

    found = checked(audit(rate(), bol(pickup_date="2026-10-02"), pod(), invoice()), "Pickup date")

    assert found["outcome"] == fa.DISCREPANCY
    assert "2026-10-02 (BOL.pdf) is 1 day after the rate confirmation's 2026-10-01" in found["explanation"]


def test_weight_uses_its_own_tolerance():

    heavier = bol(weight={"amount": 1015.0, "unit": "lb"})

    assert checked(audit(rate(), heavier, pod(), invoice()), "Weight")["outcome"] == fa.DISCREPANCY
    assert checked(audit(rate(), heavier, pod(), invoice(), policy=fa.load_policy('{"weight": {"percent": 2}}')),
                   "Weight")["outcome"] == fa.MATCHED_WITH_TOLERANCE


def test_weights_in_different_units_are_not_converted():

    found = checked(audit(rate(), bol(weight={"amount": 453.6, "unit": "kg"}), pod(), invoice()), "Weight")

    assert found["outcome"] == fa.PENDING_REVIEW


def test_a_reference_only_one_document_states_does_not_decide_the_outcome():

    result = audit(rate(weight=None), bol(), pod(), invoice())

    assert checked(result, "Weight")["outcome"] == fa.INSUFFICIENT_EVIDENCE
    assert result["outcome"] == fa.MATCHED


def test_an_invoice_number_on_another_invoice_is_a_duplicate():

    other = invoice(filename="INVOICE_SHJ-2026-102.pdf", sha="other", shipment_id="SHJ-2026-102")

    result = audit(rate(), bol(), pod(), invoice(), others=[other])

    assert checked(result, "Invoice number")["outcome"] == fa.DUPLICATE
    assert "INVOICE_SHJ-2026-102.pdf" in checked(result, "Invoice number")["explanation"]
    assert result["outcome"] == fa.DUPLICATE


def test_the_same_file_is_not_its_own_duplicate():

    assert checked(audit(rate(), bol(), pod(), invoice(), others=[invoice()]), "Invoice number")["outcome"] == fa.MATCHED


def test_colliding_shipment_references():

    result = audit(rate(shipment_id="AB-12-3"), bol(shipment_id="AB-1-23"), pod(shipment_id="AB-12-3"),
                   invoice(shipment_id="AB-12-3"))

    assert checked(result, "Shipment reference")["outcome"] == fa.DISCREPANCY
    assert result["outcome"] == fa.DISCREPANCY


# ============================================================
# THE APPLICATION: real PDFs, the audit service, exceptions, the API
# ============================================================

def pairs(**values):

    lines = []

    for label, value in values.items():
        lines += [f"{label.replace('_', ' ')}:", str(value), ""]

    return lines


def write_contracted_shipment(folder, fuel_billed="$200.00"):
    """SHJ-2026-101: fuel billed above the rate confirmation, detention billed and mentioned on the POD."""

    from test_ingestion_manager import make_pdf

    common = dict(Carrier="Swift Lines", Origin="Chicago, Illinois", Destination="Atlanta, Georgia")

    make_pdf(folder / "BOL_SHJ-2026-101.pdf", "BILL OF LADING", [f"Shipment Number: {SHIPMENT}", ""] + pairs(
        **common, Pickup_Date="October 1, 2026", Expected_Delivery_Date="October 3, 2026", Number_of_Pieces=48))
    make_pdf(folder / "POD_SHJ-2026-101.pdf", "PROOF OF DELIVERY", [f"Shipment Number: {SHIPMENT}", ""] + pairs(
        Carrier="Swift Lines", Delivery_Date="October 3, 2026", Number_of_Pieces_Delivered=48,
        Delivery_Status="Delivered", Remarks="Driver held at consignee, detention 2 hours"))
    make_pdf(folder / "INVOICE_SHJ-2026-101.pdf", "FREIGHT INVOICE", ["Invoice Number: INV-101", f"Shipment Number: {SHIPMENT}", ""] + pairs(
        **common, Base_Freight="$1,400.00", Fuel_Surcharge=fuel_billed, Detention_Charge="$75.00",
        Total_Invoice_Amount="$1,675.00", Payment_Terms="Net 30"))
    make_pdf(folder / "RATE_CONFIRMATION_SHJ-2026-101.pdf", "RATE CONFIRMATION", [f"Shipment Number: {SHIPMENT}", ""] + pairs(
        **common, Pickup_Date="October 1, 2026", Delivery_Date="October 3, 2026", Line_Haul="$1,400.00",
        Fuel_Surcharge="$180.00", Payment_Terms="Net 30"))


@pytest.fixture
def rig(tmp_path):

    from test_api import Rig

    result = Rig(tmp_path, "required")
    write_contracted_shipment(result.raw)
    result.state.service.invalidate()

    return result


def test_the_audit_service_runs_the_freight_audit_on_real_pdfs(rig):

    shipment = rig.state.service.shipment(SHIPMENT)
    result = shipment["freight_audit"]

    assert result["documents"]["contract"] == ["RATE_CONFIRMATION_SHJ-2026-101.pdf"]
    assert result["outcome"] == fa.DISCREPANCY

    fuel = charge(result, "Fuel surcharge")
    assert (fuel["expected"], fuel["billed"], fuel["variance"], fuel["outcome"]) == (180.0, 200.0, 20.0, fa.DISCREPANCY)
    assert fuel["expected_source"]["file"] == "RATE_CONFIRMATION_SHJ-2026-101.pdf"
    assert charge(result, "Base freight")["outcome"] == fa.MATCHED

    detention = charge(result, "Detention Charge")
    assert detention["outcome"] == fa.PENDING_REVIEW
    assert detention["evidence"][0]["source"] == "POD_SHJ-2026-101.pdf"


def test_the_sample_shipments_without_a_contract_are_unchanged(rig):

    service = rig.state.service
    result = service.shipment("SHJ-2026-001")["freight_audit"]

    # No contract: nothing is compared with one
    assert result["documents"]["contract"] == []
    assert charge(result, "Base freight")["outcome"] == fa.INSUFFICIENT_EVIDENCE
    assert result["explanation"][0].startswith("No rate confirmation belongs to this shipment")
    # The sample invoice's line items do not add up to its total (its known invoice discrepancy)
    assert charge(result, "Line items against the invoice total")["outcome"] == fa.DISCREPANCY
    assert result["outcome"] == fa.DISCREPANCY

    # And no freight exception is raised without a contract: the existing exceptions are unchanged
    assert not [item for item in service.exceptions() if item["shipment_key"] == "SHJ2026001"
                and item["category"].startswith("Freight audit")]


def test_audit_results_become_exceptions_once(rig):

    service = rig.state.service

    first = {item["exception_key"]: item for item in service.exceptions() if item["shipment_key"] == "SHJ2026101"}
    service.invalidate()
    again = [item for item in service.exceptions() if item["shipment_key"] == "SHJ2026101"]

    fuel = first["SHJ2026101|freight-fuel-surcharge"]
    assert fuel["category"] == "Freight audit: Fuel surcharge" and fuel["severity"] == "HIGH"
    assert fuel["audit_outcome"] == fa.DISCREPANCY
    assert fuel["financial_impact"]["amount"] == 20.0
    assert "Dispute or short-pay" in fuel["recommended_action"]
    assert any("RATE_CONFIRMATION_SHJ-2026-101.pdf" in reference for reference in fuel["evidence"])

    detention = first["SHJ2026101|freight-detention-charge"]
    assert detention["kind"] == "Needs review" and detention["audit_outcome"] == fa.PENDING_REVIEW

    # No duplicate for the same unresolved issue on a second audit
    assert sorted(item["exception_key"] for item in again) == sorted(first)


def test_the_api_returns_the_audit_and_explains_its_exceptions(rig):

    body = rig.client("alice").get(f"/api/shipments/{SHIPMENT}").json()

    assert body["freight_audit"]["outcome"] == fa.DISCREPANCY
    assert any(reason.startswith("Fuel surcharge:") for reason in body["freight_audit"]["explanation"])

    explanation = next(item for item in body["explanations"] if item["category"] == "Freight audit: Fuel surcharge")
    assert "180.00" in explanation["expected"] and "200.00" in explanation["observed"]
    assert explanation["next_step"].startswith("Dispute or short-pay")
    assert {row["source"] for row in explanation["sources"]} >= {"RATE_CONFIRMATION_SHJ-2026-101.pdf", "INVOICE_SHJ-2026-101.pdf"}


def test_a_broken_tolerance_setting_is_reported_not_crashed(rig, monkeypatch):

    import config

    monkeypatch.setattr(config, "FREIGHT_TOLERANCES", "{not json")
    rig.state.service.invalidate()

    shipment = rig.state.service.shipment(SHIPMENT)

    assert shipment["result"]["success"] is True
    assert shipment["freight_audit"]["outcome"] is None
    assert "tolerances cannot be used" in shipment["freight_audit"]["explanation"][0]
