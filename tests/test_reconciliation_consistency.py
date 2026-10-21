"""
Reconciliation, Shipment Intelligence and chat must agree.

reconcile_shipment() and analyze_shipment() are run on synthetic
document chunks (reconciliation.load_document_chunks and the index read
in intelligence_service are replaced), and the chat calculation
(shipment_calculations.calculate) on the same text. No PDFs, no index,
no Ollama and no files are involved.

Covers the approved audit rules: early delivery is not a discrepancy,
an invoice whose line items do not add up is an Invoice discrepancy and
exception, and missing, unreadable or conflicting essential data is
NEEDS REVIEW, never MATCH, ON TIME or CLEAR. Also the shipment
isolation checks on identifiers.

Run from the project root:
    python -m pytest tests/test_reconciliation_consistency.py
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from offline_imports import stubbed_services

with stubbed_services():

    import intelligence_service
    import reconciliation
    from shipment_identity import belongs_to_shipment

from shipment_calculations import ABSTAINED, calculate


A = "SHJ-2026-001"
B = "SHJ-2026-002"

ITEMS = {
    "Base Freight": "$1,200.00",
    "Fuel Surcharge": "$180.00",
    "Detention Charge": "$0.00",
    "Other Accessorial Charges": "$50.00",
}


# ============================================================
# SYNTHETIC DOCUMENTS (same layout as the sample PDFs)
# ============================================================

def chunk(doc_type, text, source, shipment=A, page=1):

    return {
        "text": text,
        "metadata": {
            "source": source,
            "document_type": doc_type,
            "shipment_id": shipment,
            "page": page,
            "chunk": 0,
        },
    }


def field(label, value):

    return f"{label}:\n{value}\n\n" if value is not None else ""


def documents(
    shipment=A,
    pieces=48,
    delivered=45,
    expected="October 3, 2026",
    actual="October 5, 2026",
    items=ITEMS,
    total="$1,550.00",
    label=None,
):
    """
    BOL, POD and invoice chunks of one shipment. label: the shipment
    ID written in the metadata (default: shipment).
    """

    label = shipment if label is None else label

    bol = (
        f"Shipment Number: {shipment}\n\n"
        + field("Carrier", "Swift Lines")
        + field("Origin", "Chicago, Illinois")
        + field("Destination", "Atlanta, Georgia")
        + field("Pickup Date", "October 1, 2026")
        + field("Expected Delivery Date", expected)
        + field("Number of Pieces", pieces)
    )

    pod = (
        f"Shipment Number: {shipment}\n\n"
        + field("Carrier", "Swift Lines")
        + field("Delivery Date", actual)
        + field("Number of Pieces Delivered", delivered)
        + field("Delivery Status", "Delivered")
    )

    invoice = (
        f"Invoice Number: INV-{shipment}\n\n"
        f"Shipment Number: {shipment}\n\n"
        + field("Carrier", "Swift Lines")
        + field("Origin", "Chicago, Illinois")
        + field("Destination", "Atlanta, Georgia")
        + "".join(
            field(name, amount)
            for name, amount in items.items()
        )
        + field("Total Invoice Amount", total)
    )

    return [
        chunk("BOL", bol, f"BOL_{shipment}.pdf", label),
        chunk("POD", pod, f"POD_{shipment}.pdf", label),
        chunk("INVOICE", invoice, f"INVOICE_{shipment}.pdf", label),
    ]


def as_records(chunks):
    """
    The chunks as rag.get_document_records() builds them for chat.
    """

    return [
        {
            "document": item["text"],
            "metadata": item["metadata"],
            "type": item["metadata"]["document_type"].lower(),
            "normalized": " ".join(item["text"].split()),
            "raw": item["text"],
        }
        for item in chunks
    ]


@pytest.fixture
def index(monkeypatch):
    """
    index(chunks) makes reconciliation and intelligence read these
    chunks instead of the PDFs and the vector index.
    """

    def install(chunks):

        monkeypatch.setattr(
            reconciliation, "load_document_chunks", lambda: (list(chunks), [])
        )

        monkeypatch.setattr(
            intelligence_service,
            "get_all_documents",
            lambda: (
                [item["text"] for item in chunks],
                [item["metadata"] for item in chunks],
            ),
        )

        return chunks

    return install


def reconcile(index, chunks, shipment=None):

    index(chunks)

    result = reconciliation.reconcile_shipment(shipment)

    assert result["success"], result

    return result


def analyze(index, chunks, shipment=None):

    index(chunks)

    result = intelligence_service.analyze_shipment(shipment)

    assert result["success"], result

    return result


def categories(items):

    return sorted(item["category"] for item in items)


# ============================================================
# EARLY AND ON-TIME DELIVERY
# ============================================================

def test_early_delivery_is_not_a_discrepancy(index):

    chunks = documents(
        B, pieces=60, delivered=60,
        expected="October 9, 2026", actual="October 8, 2026",
        total="$1,430.00",
    )

    result = reconcile(index, chunks)

    assert result["delivery"]["status"] == "EARLY"
    assert result["delivery"]["days_late"] == -1
    assert result["delivery"]["expected"] == "October 9, 2026"
    assert result["delivery"]["actual"] == "October 8, 2026"
    assert result["discrepancies"] == []
    assert result["review_items"] == []
    assert result["final_status"] == "NO DISCREPANCIES"

    intelligence = analyze(index, chunks)

    assert intelligence["delivery"]["status"] == "EARLY"
    assert intelligence["delivery"]["delay_days"] == 0
    assert intelligence["exceptions"] == []
    assert intelligence["shipment_status"] == "CLEAR"


def test_delivery_on_the_expected_date_is_on_time(index):

    chunks = documents(
        pieces=48, delivered=48, total="$1,430.00",
        expected="October 3, 2026", actual="October 3, 2026",
    )

    result = reconcile(index, chunks)

    assert result["delivery"]["status"] == "ON TIME"
    assert result["delivery"]["days_late"] == 0
    assert result["final_status"] == "NO DISCREPANCIES"
    assert analyze(index, chunks)["shipment_status"] == "CLEAR"


def test_late_delivery_is_still_a_discrepancy(index):

    chunks = documents(pieces=48, delivered=48, total="$1,430.00")

    result = reconcile(index, chunks)

    assert result["delivery"]["status"] == "DELAYED"
    assert result["delivery"]["days_late"] == 2
    assert categories(result["discrepancies"]) == ["Delivery Date"]
    assert result["final_status"] == "DISCREPANCIES DETECTED"

    intelligence = analyze(index, chunks)

    assert intelligence["delivery"]["status"] == "DELAYED"
    assert intelligence["delivery"]["delay_days"] == 2
    assert categories(intelligence["exceptions"]) == ["Delivery"]


# ============================================================
# QUANTITY
# ============================================================

def test_shortage_keeps_the_existing_reconciliation_keys(index):

    chunks = documents(expected="October 3, 2026", actual="October 3, 2026",
                       total="$1,430.00")

    result = reconcile(index, chunks)

    assert result["quantity"]["bol"] == 48
    assert result["quantity"]["pod"] == 45
    assert result["quantity"]["difference"] == 3
    assert result["quantity"]["status"] == "MISMATCH"
    assert categories(result["discrepancies"]) == ["Quantity"]

    quantity = analyze(index, chunks)["quantity"]

    assert (quantity["bol_pieces"], quantity["delivered_pieces"]) == (48, 45)
    assert (quantity["difference"], quantity["status"]) == (3, "SHORTAGE")


def test_over_delivery_is_an_overage(index):

    chunks = documents(pieces=45, delivered=48, total="$1,430.00",
                       expected="October 3, 2026", actual="October 3, 2026")

    result = reconcile(index, chunks)

    assert result["quantity"]["status"] == "MISMATCH"
    assert result["quantity"]["difference"] == -3

    intelligence = analyze(index, chunks)

    assert intelligence["quantity"]["status"] == "OVERAGE"
    assert intelligence["quantity"]["difference"] == 3


def test_quantities_with_thousands_separators_are_read(index):

    chunks = documents(pieces="1,200", delivered="1,150",
                       expected="October 3, 2026", actual="October 3, 2026",
                       total="$1,430.00")

    result = reconcile(index, chunks)

    assert (result["quantity"]["bol"], result["quantity"]["pod"]) == (1200, 1150)
    assert result["quantity"]["difference"] == 50


# ============================================================
# INVOICE
# ============================================================

def test_invoice_mismatch_in_reconciliation_and_intelligence(index):

    chunks = documents()

    result = reconcile(index, chunks)

    invoice = result["invoice"]

    assert invoice["status"] == "MISMATCH"
    assert invoice["line_item_sum"] == 1430.00
    assert invoice["total"] == 1550.00
    assert invoice["difference"] == 120.00
    assert invoice["base_freight"] == 1200.00
    assert invoice["fuel_surcharge"] == 180.00
    assert invoice["detention_charge"] == 0.00
    assert invoice["other_charges"] == 50.00
    assert [name for name, _ in invoice["line_items"]] == [
        "Base Freight", "Fuel Surcharge", "Detention Charge",
        "Other Accessorial Charges",
    ]
    assert "Detention Charge $0.00" in invoice["breakdown"]

    assert categories(result["discrepancies"]) == [
        "Delivery Date", "Invoice", "Quantity"
    ]

    intelligence = analyze(index, chunks)

    invoice_exceptions = [
        item for item in intelligence["exceptions"]
        if item["category"] == "Invoice"
    ]

    assert len(invoice_exceptions) == 1
    assert invoice_exceptions[0]["severity"] == "HIGH"
    assert "$1,430.00" in invoice_exceptions[0]["message"]
    assert "$1,550.00" in invoice_exceptions[0]["message"]
    assert "$120.00" in invoice_exceptions[0]["message"]

    assert intelligence["invoice_total"] == 1550.00
    assert intelligence["invoice"]["status"] == "MISMATCH"
    assert categories(intelligence["exceptions"]) == [
        "Delivery", "Invoice", "Quantity"
    ]
    assert intelligence["exception_count"] == 3
    assert intelligence["shipment_status"] == "EXCEPTION"


def test_invoice_total_below_the_sum_reports_the_absolute_difference(index):

    chunks = documents(total="$1,400.00")

    result = reconcile(index, chunks)

    assert result["invoice"]["difference"] == -30.00

    invoice = [
        item for item in result["discrepancies"]
        if item["category"] == "Invoice"
    ]

    assert len(invoice) == 1
    assert "a difference of $30.00." in invoice[0]["description"]

    message = [
        item["message"]
        for item in analyze(index, chunks)["exceptions"]
        if item["category"] == "Invoice"
    ]

    assert len(message) == 1
    assert "a difference of $30.00." in message[0]


def test_money_text_formats_negative_amounts():

    from display_text import money_text

    assert money_text(1430) == "$1,430.00"
    assert money_text(-30.0) == "-$30.00"
    assert money_text(0) == "$0.00"
    assert money_text(None) == "N/A"
    assert money_text("1,430.00") == "N/A"
    assert money_text(True) == "N/A"


def test_invoice_that_adds_up_raises_nothing(index):

    chunks = documents(
        pieces=48, delivered=48, total="$1,430.00",
        expected="October 3, 2026", actual="October 3, 2026",
    )

    result = reconcile(index, chunks)

    assert result["invoice"]["status"] == "MATCH"
    assert result["invoice"]["difference"] == 0
    assert result["discrepancies"] == []

    intelligence = analyze(index, chunks)

    assert intelligence["exceptions"] == []
    assert intelligence["shipment_status"] == "CLEAR"


def test_invoice_evidence_cites_source_page_and_shipment(index):

    result = reconcile(index, documents())

    assert {
        item["document_type"] for item in result["invoice"]["evidence"]
    } == {"INVOICE"}

    assert [
        (item["source"], item["page"], item["shipment_id"], item["text"])
        for item in result["invoice"]["evidence"]
    ] == [
        ("INVOICE_SHJ-2026-001.pdf", 1, A, "Base Freight: $1,200.00"),
        ("INVOICE_SHJ-2026-001.pdf", 1, A, "Fuel Surcharge: $180.00"),
        ("INVOICE_SHJ-2026-001.pdf", 1, A, "Detention Charge: $0.00"),
        ("INVOICE_SHJ-2026-001.pdf", 1, A, "Other Accessorial Charges: $50.00"),
        ("INVOICE_SHJ-2026-001.pdf", 1, A, "Total Invoice Amount: $1,550.00"),
    ]


# ============================================================
# NEEDS REVIEW
# ============================================================

def drop(chunks, doc_type, label):
    """
    The chunks without the line of one field of one document type.
    """

    changed = []

    for item in chunks:

        if item["metadata"]["document_type"] == doc_type:

            lines = item["text"].split("\n\n")

            item = dict(
                item,
                text="\n\n".join(
                    line for line in lines if not line.startswith(f"{label}:")
                ),
            )

        changed.append(item)

    return changed


MISSING_CASES = {
    "delivered pieces": (
        lambda: drop(documents(), "POD", "Number of Pieces Delivered"),
        "quantity", "delivered piece count",
    ),
    "expected pieces": (
        lambda: drop(documents(), "BOL", "Number of Pieces"),
        "quantity", "expected piece count",
    ),
    "actual delivery date": (
        lambda: drop(documents(), "POD", "Delivery Date"),
        "delivery", "actual delivery date",
    ),
    "expected delivery date": (
        lambda: drop(documents(), "BOL", "Expected Delivery Date"),
        "delivery", "expected delivery date",
    ),
    "invoice total": (
        lambda: drop(documents(), "INVOICE", "Total Invoice Amount"),
        "invoice", "Total Invoice Amount",
    ),
    "detention charge": (
        lambda: drop(documents(), "INVOICE", "Detention Charge"),
        "invoice", "Detention Charge",
    ),
    "ambiguous delivery date": (
        lambda: documents(actual="10/05/2026"),
        "delivery", "'10/05/2026'",
    ),
    "conflicting BOL pieces": (
        lambda: documents()
        + [chunk("BOL", "Number of Pieces:\n50\n", "BOL_COPY.pdf")],
        "quantity", "different values",
    ),
    "conflicting invoices": (
        lambda: documents()
        + [chunk(
            "INVOICE",
            "Base Freight:\n$1,200.00\n\nFuel Surcharge:\n$180.00\n\n"
            "Detention Charge:\n$0.00\n\nOther Accessorial Charges:\n$50.00\n\n"
            "Total Invoice Amount:\n$1,430.00\n",
            "INVOICE_COPY.pdf",
        )],
        "invoice", "different values",
    ),
}


@pytest.mark.parametrize("name", sorted(MISSING_CASES))
def test_unusable_essential_data_needs_review(index, name):

    build, section, fragment = MISSING_CASES[name]

    chunks = build()

    result = reconcile(index, chunks)

    assert result[section]["status"] == "NEEDS REVIEW", name
    assert fragment in result[section]["reason"], result[section]["reason"]

    # never counted as a match, and listed for review
    assert section in categories(
        [{"category": item["category"].lower()} for item in result["review_items"]]
    )
    assert result["final_status"] in ("NEEDS REVIEW", "DISCREPANCIES DETECTED")

    intelligence = analyze(index, chunks)

    assert intelligence["shipment_status"] in ("NEEDS REVIEW", "EXCEPTION")
    assert intelligence["shipment_status"] != "CLEAR"
    assert any(
        item["category"] == "Data Review"
        and fragment in item["message"]
        for item in intelligence["exceptions"]
    )

    key = {"quantity": "quantity", "delivery": "delivery"}.get(section)

    if key:
        assert intelligence[key]["status"] == "NEEDS REVIEW"
        assert intelligence[key]["status"] not in ("MATCH", "ON TIME")

    if section == "invoice":
        assert intelligence["invoice"]["status"] == "NEEDS REVIEW"


def test_needs_review_alone_is_not_clear_and_not_an_exception_status(index):

    chunks = drop(
        documents(
            pieces=48, delivered=48, total="$1,430.00",
            expected="October 3, 2026", actual="October 3, 2026",
        ),
        "POD", "Number of Pieces Delivered",
    )

    result = reconcile(index, chunks)

    assert result["discrepancies"] == []
    assert result["final_status"] == "NEEDS REVIEW"
    assert result["quantity"]["pod"] is None
    assert result["quantity"]["difference"] is None
    assert result["quantity"]["bol"] == 48

    intelligence = analyze(index, chunks)

    assert intelligence["shipment_status"] == "NEEDS REVIEW"
    assert intelligence["quantity"]["status"] == "NEEDS REVIEW"
    assert intelligence["quantity"]["difference"] is None
    assert categories(intelligence["exceptions"]) == ["Data Review"]


def test_a_real_discrepancy_outranks_needs_review(index):

    chunks = drop(documents(), "POD", "Number of Pieces Delivered")

    intelligence = analyze(index, chunks)

    assert intelligence["shipment_status"] == "EXCEPTION"
    assert categories(intelligence["exceptions"]) == [
        "Data Review", "Delivery", "Invoice"
    ]


# ============================================================
# RECONCILIATION, INTELLIGENCE AND CHAT AGREE
# ============================================================

QUESTIONS = {
    "quantity": "How many pieces were missing between the BOL and the POD?",
    "delivery": "Was the shipment delivered on time?",
    "invoice": "Do the invoice line items add up to the total invoice amount?",
}

AGREEMENT_CASES = {
    "shortage_delay_invoice": documents,
    "early_clean": lambda: documents(
        B, pieces=60, delivered=60,
        expected="October 9, 2026", actual="October 8, 2026",
        total="$1,430.00",
    ),
    "overage": lambda: documents(pieces=45, delivered=48),
    "missing_total": lambda: drop(documents(), "INVOICE", "Total Invoice Amount"),
    "missing_date": lambda: drop(documents(), "POD", "Delivery Date"),
    "ambiguous_date": lambda: documents(actual="10/05/2026"),
    "unknown_charge": lambda: documents(
        items={**ITEMS, "Lumper Fee": "$75.00"}
    ),
}


@pytest.mark.parametrize("name", sorted(AGREEMENT_CASES))
def test_reconciliation_intelligence_and_chat_agree(index, name):

    chunks = AGREEMENT_CASES[name]()

    result = reconcile(index, chunks)
    intelligence = analyze(index, chunks)
    records = as_records(chunks)

    # --- quantity -------------------------------------------------
    chat = calculate(QUESTIONS["quantity"], records)
    reconciled = result["quantity"]
    analysed = intelligence["quantity"]

    if chat.status == ABSTAINED:
        assert reconciled["status"] == analysed["status"] == "NEEDS REVIEW"
    else:
        assert reconciled["difference"] == chat.value
        assert analysed["difference"] == abs(chat.value)
        assert analysed["status"] == (
            "SHORTAGE" if chat.value > 0
            else "OVERAGE" if chat.value < 0
            else "MATCH"
        )

    # --- delivery -------------------------------------------------
    chat = calculate(QUESTIONS["delivery"], records)
    reconciled = result["delivery"]
    analysed = intelligence["delivery"]

    if chat.status == ABSTAINED:
        assert reconciled["status"] == analysed["status"] == "NEEDS REVIEW"
    else:
        assert reconciled["days_late"] == chat.value
        assert analysed["delay_days"] == max(chat.value, 0)
        assert analysed["status"] == reconciled["status"] == (
            "DELAYED" if chat.value > 0
            else "EARLY" if chat.value < 0
            else "ON TIME"
        )

    # --- invoice --------------------------------------------------
    chat = calculate(QUESTIONS["invoice"], records)
    reconciled = result["invoice"]
    analysed = intelligence["invoice"]

    if chat.status == ABSTAINED:
        assert reconciled["status"] == analysed["status"] == "NEEDS REVIEW"
        assert reconciled["reason"] == chat.reason
    else:
        assert reconciled["line_item_sum"] == chat.value
        assert analysed["status"] == reconciled["status"]
        assert chat.answer.startswith(
            "Yes." if reconciled["status"] == "MATCH" else "No."
        )
        assert reconciled["breakdown"] in chat.answer

    # --- the shipment is CLEAR only when nothing is wrong or unverified
    statuses = {
        intelligence["quantity"]["status"],
        intelligence["delivery"]["status"],
        intelligence["invoice"]["status"],
    }

    if intelligence["shipment_status"] == "CLEAR":
        assert statuses <= {"MATCH", "ON TIME", "EARLY"}


# ============================================================
# SHIPMENT ISOLATION
# ============================================================

def interleave(first, second):

    return [
        item
        for pair in zip(first, second)
        for item in pair
    ]


def test_each_shipment_reconciles_only_its_own_documents(index):

    a = documents(A)
    b = documents(
        B, pieces=60, delivered=60,
        expected="October 9, 2026", actual="October 8, 2026",
        total="$1,430.00",
    )

    chunks = interleave(b, a)

    for shipment, pieces, total in ((A, 48, 1550.0), (B, 60, 1430.0)):

        result = reconcile(index, chunks, shipment)

        assert result["shipment_id"] == shipment
        assert result["quantity"]["bol"] == pieces
        assert result["invoice"]["total"] == total

        sources = {
            item["source"]
            for section in ("quantity", "delivery", "invoice")
            for item in result[section]["evidence"]
        }

        assert sources == {
            f"BOL_{shipment}.pdf",
            f"POD_{shipment}.pdf",
            f"INVOICE_{shipment}.pdf",
        }

        assert {
            item["shipment_id"]
            for section in ("quantity", "delivery", "invoice")
            for item in result[section]["evidence"]
        } == {shipment}


def test_unscoped_reconciliation_refuses_to_mix_two_shipments(index):

    index(documents(A) + documents(B))

    result = reconciliation.reconcile_shipment()

    assert result["success"] is False
    assert "multiple shipments" in result["error"]


def test_documents_without_a_shipment_id_are_never_merged(index):

    stray_pod = chunk(
        "POD",
        "Delivery Date:\nOctober 20, 2026\n\nNumber of Pieces Delivered:\n60\n",
        "POD_UNLABELED.pdf",
        shipment=None,
    )

    # first in the list: the first value found used to win
    chunks = [stray_pod] + documents(
        pieces=48, delivered=45, total="$1,430.00",
        expected="October 3, 2026", actual="October 3, 2026",
    )

    result = reconcile(index, chunks)

    assert result["quantity"]["pod"] == 45
    assert result["delivery"]["actual"] == "October 3, 2026"
    assert result["excluded_unidentified"] == 1

    intelligence = analyze(index, chunks)

    assert intelligence["quantity"]["delivered_pieces"] == 45


def test_documents_without_a_shipment_id_are_not_used_for_a_selected_shipment(index):

    stray_pod = chunk(
        "POD",
        "Number of Pieces Delivered:\n60\n",
        "POD_UNLABELED.pdf",
        shipment=None,
    )

    result = reconcile(index, [stray_pod] + documents(), A)

    assert result["quantity"]["pod"] == 45
    assert result["excluded_unidentified"] == 1


@pytest.mark.parametrize(
    "labels",
    [
        (A, A, A),
        (A, "shj-2026-001", "SHJ 2026 001"),
        (A, "SHJ2026001", A),
    ],
)
def test_spellings_of_one_shipment_id_are_one_shipment(index, labels):
    """
    rag_scope compares IDs with identifier_key (case, spaces and
    hyphens ignored); reconciliation and intelligence must too.
    """

    chunks = [
        chunk(item["metadata"]["document_type"], item["text"],
              item["metadata"]["source"], label)
        for item, label in zip(documents(), labels)
    ]

    for shipment in (None, A, "shj2026001"):

        result = reconcile(index, chunks, shipment)

        assert result["quantity"]["bol"] == 48
        assert result["quantity"]["pod"] == 45

    intelligence = analyze(index, chunks, A)

    assert intelligence["documents"]["found"] == 3


def test_different_shipments_stay_different_with_similar_ids(index):

    chunks = documents("SHJ-2026-001") + documents("SHJ-2026-0010")

    result = reconcile(index, chunks, "SHJ-2026-001")

    assert {
        item["shipment_id"] for item in result["quantity"]["evidence"]
    } == {"SHJ-2026-001"}


# ============================================================
# EVIDENCE FILTER (used by the dashboard's evidence cards)
# ============================================================

def test_evidence_filter_follows_the_metadata_not_a_mention_in_the_text():

    other_shipment = {"shipment_id": B}

    # a chunk of another shipment that mentions this shipment's ID
    assert not belongs_to_shipment(
        other_shipment, f"Related shipment: {A}", A
    )

    assert belongs_to_shipment({"shipment_id": A}, "no ID in the text", A)
    assert belongs_to_shipment({"shipment_id": "shj 2026 001"}, "", A)


def test_evidence_filter_for_chunks_without_metadata():

    # no shipment_id in the metadata: the text decides, as before
    assert belongs_to_shipment({}, f"Shipment Number: {A}", A)
    assert belongs_to_shipment({"shipment_id": None}, f"Shipment: {A}", A)
    assert not belongs_to_shipment({}, f"Shipment Number: {B}", A)


def test_evidence_filter_without_a_selected_shipment_keeps_everything():

    assert belongs_to_shipment({"shipment_id": B}, "", None)
    assert belongs_to_shipment({"shipment_id": B}, "", "")
