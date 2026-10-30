from pathlib import Path
import sys
import re

# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from chunker import describe_problems, read_chunks
from shipment_identity import collision_message, identifier_collisions, identifier_key
from shipment_calculations import (
    DELIVERY_DELAYED,
    NEEDS_REVIEW,
    QUANTITY_MATCH,
    INVOICE_MISMATCH,
    Records,
    audit_delivery,
    audit_invoice,
    audit_quantity,
)


# ============================================================
# FIELD EXTRACTION
# ============================================================

def extract_field(text, field_name):
    """
    Extract value appearing after a field label.
    Handles blank lines between label and value.
    """

    pattern = rf"{re.escape(field_name)}:\s*\n\s*([^\n]+)"

    match = re.search(
        pattern,
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(1).strip()

    return None


def extract_amount(text, field_name):
    value = extract_field(text, field_name)

    if value:
        value = (
            value
            .replace("$", "")
            .replace(",", "")
            .strip()
        )

        try:
            return float(value)
        except ValueError:
            return None

    return None


# ============================================================
# DOCUMENT ORGANIZATION
# ============================================================

def extract_document_data(chunks):

    documents = {}

    for chunk in chunks:

        metadata = chunk["metadata"]

        document_type = metadata.get(
            "document_type"
        )

        if document_type not in documents:

            documents[document_type] = {
                "text": "",
                "metadata": metadata
            }

        documents[document_type]["text"] += (
            "\n" + chunk["text"]
        )

    return documents


# ============================================================
# COMPARISON
# ============================================================

def compare_values(value1, value2):

    if value1 is None or value2 is None:
        return "NOT AVAILABLE"

    if (
        value1.strip().lower()
        == value2.strip().lower()
    ):
        return "MATCH"

    return "MISMATCH"


def compare_numbers(value1, value2):

    if value1 is None or value2 is None:
        return "NOT AVAILABLE"

    if value1 == value2:
        return "MATCH"

    return "MISMATCH"


# ============================================================
# AUDITS (shared with the chat calculations)
# ============================================================

def audit_records(chunks, overrides=None):
    """
    The chunks in the record format of shipment_calculations
    (rag.get_document_records): quantities, delivery dates and the
    invoice are read and calculated by the same code as the chat
    answers.

    overrides: {field: FieldValue} of approved reviewer corrections
    (audit_inputs.build_overrides); the readers then use them in place of
    the document's own value for those fields. None reads the documents
    only, exactly as before.
    """

    records = Records([
        {
            "document": chunk["text"],
            "metadata": chunk["metadata"],
            "type": str(chunk["metadata"].get("document_type") or "").lower(),
            "normalized": " ".join(str(chunk["text"]).split()),
            "raw": chunk["text"],
        }
        for chunk in chunks
    ])

    if overrides:
        records.overrides = dict(overrides)

    return records


def evidence_entries(audit):
    """
    The fields an audit read, with source file, page and shipment.
    """

    entries = []

    for item in audit.evidence:

        metadata = item.record["metadata"]

        entries.append({
            "source": metadata.get("source"),
            "page": metadata.get("page"),
            "document_type": metadata.get("document_type"),
            "shipment_id": metadata.get("shipment_id"),
            "text": item.text,
            # Where the value came from: the document ("legacy_reader") or a
            # reviewer-approved correction (page unknown, never invented)
            "value_origin": metadata.get("value_origin") or "legacy_reader",
            "correction_id": metadata.get("correction_id"),
        })

    return entries


# ============================================================
# RECONCILIATION
# ============================================================

def load_document_chunks():
    """
    The chunks of the raw document folder and the files that gave
    none (chunker.read_chunks). reconcile_shipment reads the PDFs
    through this function when it is not given chunks.
    """

    return read_chunks()


def unreadable_message(problems):
    """
    Why the audit cannot rule out that unreadable files matter.
    """

    return (
        f"{len(problems)} file(s) in the document folder could not be used "
        f"({describe_problems(problems)}). Their shipment cannot be "
        "established, so the audit cannot rule out that they hold a BOL, "
        "invoice or POD of this shipment."
    )


def reconcile_shipment(shipment_id=None, chunks=None, unreadable_files=None, overrides=None):
    """
    Reconcile the BOL, Invoice and POD of one shipment.

    shipment_id selects that shipment's documents. Without it,
    the documents must belong to a single shipment: documents
    from different shipments are never mixed.

    chunks: document chunks already loaded (chunker.read_chunks), so
    that several shipments can be reconciled from one reading of the
    PDFs; they are read from the raw folder when omitted.

    unreadable_files: the files that gave no chunks, as
    chunker.read_chunks reports them ({"filename", "status",
    "error"}); read together with the chunks when those are omitted.
    Such a file has no shipment ID we can trust, so it is never
    attributed to a shipment, and never treated as an empty or verified
    document either: a result is never "NO DISCREPANCIES" while a file
    that might belong to the shipment could not be read (it becomes
    NEEDS REVIEW with a "Documents" review item), and when a required
    document is missing the error names the unreadable files.
    """

    if chunks is None:
        chunks, unreadable_files = load_document_chunks()
    else:
        chunks = list(chunks)

    unreadable_files = list(unreadable_files or [])

    unreadable_note = (
        " These files could not be used and cannot be assigned to any "
        f"shipment: {describe_problems(unreadable_files)}."
        if unreadable_files else ""
    )

    if not chunks:

        if unreadable_files:
            return {
                "success": False,
                "error": (
                    "No document could be read."
                    + unreadable_note
                ),
                "unreadable_files": [item["filename"] for item in unreadable_files],
            }

        return None

    # --------------------------------------------------------
    # Keep only one shipment's documents
    # --------------------------------------------------------

    # IDs are compared like the chat scope does (identifier_key: case,
    # spaces and hyphens do not matter). A document without a shipment
    # ID cannot be attributed to a shipment and is never used.
    excluded_unidentified = 0

    if shipment_id:

        requested_shipment = identifier_key(
            shipment_id
        )

        excluded_unidentified = sum(
            1
            for chunk in chunks
            if not identifier_key(chunk["metadata"].get("shipment_id"))
        )

        chunks = [
            chunk
            for chunk in chunks
            if requested_shipment
            and identifier_key(
                chunk["metadata"].get("shipment_id")
            )
            == requested_shipment
        ]

        if not chunks:
            return {
                "success": False,
                "error": (
                    f"No documents found for shipment "
                    f"{shipment_id}."
                    + unreadable_note
                )
            }

    else:

        shipment_ids = {
            identifier_key(
                chunk["metadata"].get("shipment_id")
            )
            for chunk in chunks
        } - {None}

        if len(shipment_ids) > 1:
            return {
                "success": False,
                "error": (
                    "Documents from multiple shipments are "
                    "indexed. Select a shipment to reconcile."
                )
            }

        # One identified shipment: documents without an ID are left
        # out instead of being merged into it
        if shipment_ids:

            identified = [
                chunk
                for chunk in chunks
                if identifier_key(chunk["metadata"].get("shipment_id"))
            ]

            excluded_unidentified = len(chunks) - len(identified)

            chunks = identified

    # IDs that share a key but are split differently ("AB-12-3" and
    # "AB-1-23") may be two shipments: never reconciled as one
    collisions = identifier_collisions(
        chunk["metadata"].get("shipment_id")
        for chunk in chunks
    )

    if collisions:
        forms = sorted({form for pair in collisions for form in pair})
        return {
            "success": False,
            "error": collision_message(collisions) + unreadable_note,
            "identity_collision": {"key": identifier_key(forms[0]), "forms": forms},
            "unreadable_files": [item["filename"] for item in unreadable_files],
        }

    documents = extract_document_data(chunks)

    bol = documents.get("BOL")
    invoice = documents.get("INVOICE")
    pod = documents.get("POD")

    # --------------------------------------------------------
    # Required documents
    # --------------------------------------------------------

    if not bol or not invoice or not pod:
        return {
            "success": False,
            "error": "Reconciliation requires BOL, Invoice and POD." + unreadable_note
        }

    bol_text = bol["text"]
    invoice_text = invoice["text"]
    pod_text = pod["text"]

    # --------------------------------------------------------
    # Shipment ID
    # --------------------------------------------------------

    shipment_id = (
        bol["metadata"].get("shipment_id")
        or invoice["metadata"].get("shipment_id")
        or pod["metadata"].get("shipment_id")
    )

    # --------------------------------------------------------
    # Carrier
    # --------------------------------------------------------

    bol_carrier = extract_field(
        bol_text,
        "Carrier"
    )

    invoice_carrier = extract_field(
        invoice_text,
        "Carrier"
    )

    pod_carrier = extract_field(
        pod_text,
        "Carrier"
    )

    carrier_bol_invoice = compare_values(
        bol_carrier,
        invoice_carrier
    )

    carrier_bol_pod = compare_values(
        bol_carrier,
        pod_carrier
    )

    # --------------------------------------------------------
    # Route
    # --------------------------------------------------------

    bol_origin = extract_field(
        bol_text,
        "Origin"
    )

    invoice_origin = extract_field(
        invoice_text,
        "Origin"
    )

    bol_destination = extract_field(
        bol_text,
        "Destination"
    )

    invoice_destination = extract_field(
        invoice_text,
        "Destination"
    )

    origin_status = compare_values(
        bol_origin,
        invoice_origin
    )

    destination_status = compare_values(
        bol_destination,
        invoice_destination
    )

    # --------------------------------------------------------
    # Quantity
    # --------------------------------------------------------

    # Quantities, delivery dates and the invoice are read and
    # calculated by shipment_calculations, like the chat answers
    records = audit_records(chunks, overrides)

    quantity_audit = audit_quantity(records)

    bol_pieces = quantity_audit.values["expected"]
    pod_pieces = quantity_audit.values["delivered"]

    if quantity_audit.status == NEEDS_REVIEW:
        pieces_status = NEEDS_REVIEW
        pieces_difference = None
    else:
        pieces_status = (
            "MATCH"
            if quantity_audit.status == QUANTITY_MATCH
            else "MISMATCH"
        )
        pieces_difference = quantity_audit.values["shortage"]

    # --------------------------------------------------------
    # Delivery dates
    # --------------------------------------------------------

    # ON TIME, EARLY or DELAYED; an early delivery is not a discrepancy
    delivery_audit = audit_delivery(records)

    expected_delivery = delivery_audit.values["expected"]
    actual_delivery = delivery_audit.values["actual"]

    delivery_status = delivery_audit.status

    # --------------------------------------------------------
    # Invoice
    # --------------------------------------------------------

    invoice_audit = audit_invoice(records)

    line_items = dict(invoice_audit.values["line_items"])

    base_freight = line_items["Base Freight"]
    fuel_surcharge = line_items["Fuel Surcharge"]
    detention_charge = line_items["Detention Charge"]
    other_charges = line_items["Other Accessorial Charges"]

    total_invoice = invoice_audit.values["total"]

    # --------------------------------------------------------
    # Discrepancies
    # --------------------------------------------------------

    discrepancies = []

    if pieces_status == "MISMATCH":

        discrepancies.append({
            "category": "Quantity",
            "description": (
                f"BOL shows {bol_pieces} pieces, "
                f"but POD shows {pod_pieces} pieces delivered."
            ),
            "difference": pieces_difference
        })

    if delivery_status == DELIVERY_DELAYED:

        late = delivery_audit.values["days_late"]

        discrepancies.append({
            "category": "Delivery Date",
            "description": (
                f"Expected delivery was {expected_delivery}, "
                f"but actual delivery was {actual_delivery} "
                f"({late} day{'' if late == 1 else 's'} late)."
            ),
            "days_late": late
        })

    if invoice_audit.status == INVOICE_MISMATCH:

        values = invoice_audit.values

        discrepancies.append({
            "category": "Invoice",
            "description": (
                f"Invoice line items ({values['breakdown']}) add up to "
                f"${values['line_sum']:,.2f}, but the Total Invoice "
                f"Amount is ${values['total']:,.2f}, a difference of "
                f"${abs(values['difference']):,.2f}."
            ),
            "difference": values["difference"]
        })

    if carrier_bol_invoice == "MISMATCH":

        discrepancies.append({
            "category": "Carrier",
            "description": (
                "Carrier differs between BOL and Invoice."
            )
        })

    if carrier_bol_pod == "MISMATCH":

        discrepancies.append({
            "category": "Carrier",
            "description": (
                "Carrier differs between BOL and POD."
            )
        })

    if origin_status == "MISMATCH":

        discrepancies.append({
            "category": "Origin",
            "description": (
                "Origin differs between BOL and Invoice."
            )
        })

    if destination_status == "MISMATCH":

        discrepancies.append({
            "category": "Destination",
            "description": (
                "Destination differs between BOL and Invoice."
            )
        })

    # --------------------------------------------------------
    # Final status
    # --------------------------------------------------------

    # Essential data that is missing, unreadable or conflicting is
    # never reported as a match
    review_items = [
        {"category": category, "reason": audit.reason}
        for category, audit in (
            ("Quantity", quantity_audit),
            ("Delivery", delivery_audit),
            ("Invoice", invoice_audit),
        )
        if audit.status == NEEDS_REVIEW
    ]

    # A file that could not be read might hold a document of this
    # shipment: the result cannot be called clean (and the files stay
    # visible), whatever else was found
    if unreadable_files:
        review_items.append({
            "category": "Documents",
            "reason": unreadable_message(unreadable_files),
        })

    if discrepancies:
        final_status = "DISCREPANCIES DETECTED"
    elif review_items:
        final_status = "NEEDS REVIEW"
    else:
        final_status = "NO DISCREPANCIES"

    # --------------------------------------------------------
    # Structured result
    # --------------------------------------------------------

    return {

        "success": True,

        "shipment_id": shipment_id,

        "carrier": {
            "bol": bol_carrier,
            "invoice": invoice_carrier,
            "pod": pod_carrier,
            "bol_invoice_status": carrier_bol_invoice,
            "bol_pod_status": carrier_bol_pod
        },

        "route": {
            "origin": bol_origin,
            "destination": bol_destination,
            "invoice_origin": invoice_origin,
            "invoice_destination": invoice_destination,
            "origin_status": origin_status,
            "destination_status": destination_status
        },

        # status: MATCH, MISMATCH or NEEDS REVIEW; difference is
        # BOL - POD (negative for an over-delivery)
        "quantity": {
            "bol": bol_pieces,
            "pod": pod_pieces,
            "difference": pieces_difference,
            "status": pieces_status,
            "reason": quantity_audit.reason,
            "evidence": evidence_entries(quantity_audit)
        },

        # status: ON TIME, EARLY, DELAYED or NEEDS REVIEW; days_late
        # is negative for an early delivery
        "delivery": {
            "expected": expected_delivery,
            "actual": actual_delivery,
            "status": delivery_status,
            "days_late": delivery_audit.values["days_late"],
            "reason": delivery_audit.reason,
            "evidence": evidence_entries(delivery_audit)
        },

        # status: MATCH, MISMATCH or NEEDS REVIEW; line_items lists
        # every line item (amount None when unknown), breakdown is
        # the sum as text, difference is total - line_item_sum
        "invoice": {
            "base_freight": base_freight,
            "fuel_surcharge": fuel_surcharge,
            "detention_charge": detention_charge,
            "other_charges": other_charges,
            "total": total_invoice,
            "line_items": invoice_audit.values["line_items"],
            "line_item_sum": invoice_audit.values["line_sum"],
            "difference": invoice_audit.values["difference"],
            "breakdown": invoice_audit.values["breakdown"],
            "status": invoice_audit.status,
            "reason": invoice_audit.reason,
            "evidence": evidence_entries(invoice_audit)
        },

        "discrepancies": discrepancies,

        "review_items": review_items,

        "excluded_unidentified": excluded_unidentified,

        # Files that could not be used: never part of a shipment
        "unreadable_files": [item["filename"] for item in unreadable_files],

        "final_status": final_status
    }


# ============================================================
# CLI TEST
# ============================================================

if __name__ == "__main__":

    result = reconcile_shipment()

    if not result:
        print("No documents found.")
        sys.exit()

    if not result.get("success"):
        print(result["error"])
        sys.exit()

    print("=" * 70)
    print("LOGIDOC-RAG SHIPMENT RECONCILIATION")
    print("=" * 70)

    print(
        f"\nShipment ID: "
        f"{result['shipment_id']}"
    )

    print("\nQUANTITY")
    print("-" * 70)

    print(
        f"BOL: "
        f"{result['quantity']['bol']} pieces"
    )

    print(
        f"POD: "
        f"{result['quantity']['pod']} pieces"
    )

    print(
        f"Difference: "
        f"{result['quantity']['difference']} pieces"
    )

    print(
        f"Status: "
        f"{result['quantity']['status']}"
    )

    print("\nDELIVERY")
    print("-" * 70)

    print(
        f"Expected: "
        f"{result['delivery']['expected']}"
    )

    print(
        f"Actual: "
        f"{result['delivery']['actual']}"
    )

    print(
        f"Status: "
        f"{result['delivery']['status']}"
    )

    print("\nINVOICE")
    print("-" * 70)

    invoice_total = result["invoice"]["total"]

    print(
        "Total: "
        + (
            f"${invoice_total:,.2f}"
            if invoice_total is not None
            else "N/A"
        )
    )

    print(
        f"Status: "
        f"{result['invoice']['status']}"
    )

    print("\nFINAL STATUS")
    print("-" * 70)

    print(result["final_status"])

    if result["discrepancies"]:

        print("\nDISCREPANCIES:")

        for item in result["discrepancies"]:
            print(
                f"- {item['category']}: "
                f"{item['description']}"
            )

    if result["review_items"]:

        print("\nNEEDS REVIEW:")

        for item in result["review_items"]:
            print(
                f"- {item['category']}: "
                f"{item['reason']}"
            )
