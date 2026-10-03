from pathlib import Path
import sys
import re

# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from chunker import create_all_chunks


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
# RECONCILIATION
# ============================================================

def reconcile_shipment():

    chunks = create_all_chunks()

    if not chunks:
        return None

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
            "error": "Reconciliation requires BOL, Invoice and POD."
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

    bol_pieces_raw = extract_field(
        bol_text,
        "Number of Pieces"
    )

    pod_pieces_raw = extract_field(
        pod_text,
        "Number of Pieces Delivered"
    )

    try:
        bol_pieces = int(bol_pieces_raw)
    except (TypeError, ValueError):
        bol_pieces = None

    try:
        pod_pieces = int(pod_pieces_raw)
    except (TypeError, ValueError):
        pod_pieces = None

    pieces_status = compare_numbers(
        bol_pieces,
        pod_pieces
    )

    if (
        bol_pieces is not None
        and pod_pieces is not None
    ):
        pieces_difference = (
            bol_pieces - pod_pieces
        )
    else:
        pieces_difference = None

    # --------------------------------------------------------
    # Delivery dates
    # --------------------------------------------------------

    expected_delivery = extract_field(
        bol_text,
        "Expected Delivery Date"
    )

    actual_delivery = extract_field(
        pod_text,
        "Delivery Date"
    )

    delivery_status = compare_values(
        expected_delivery,
        actual_delivery
    )

    # --------------------------------------------------------
    # Invoice
    # --------------------------------------------------------

    base_freight = extract_amount(
        invoice_text,
        "Base Freight"
    )

    fuel_surcharge = extract_amount(
        invoice_text,
        "Fuel Surcharge"
    )

    detention_charge = extract_amount(
        invoice_text,
        "Detention Charge"
    )

    other_charges = extract_amount(
        invoice_text,
        "Other Accessorial Charges"
    )

    total_invoice = extract_amount(
        invoice_text,
        "Total Invoice Amount"
    )

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

    if delivery_status == "MISMATCH":

        discrepancies.append({
            "category": "Delivery Date",
            "description": (
                f"Expected delivery was {expected_delivery}, "
                f"but actual delivery was {actual_delivery}."
            )
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

    final_status = (
        "DISCREPANCIES DETECTED"
        if discrepancies
        else "NO DISCREPANCIES"
    )

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

        "quantity": {
            "bol": bol_pieces,
            "pod": pod_pieces,
            "difference": pieces_difference,
            "status": pieces_status
        },

        "delivery": {
            "expected": expected_delivery,
            "actual": actual_delivery,
            "status": delivery_status
        },

        "invoice": {
            "base_freight": base_freight,
            "fuel_surcharge": fuel_surcharge,
            "detention_charge": detention_charge,
            "other_charges": other_charges,
            "total": total_invoice
        },

        "discrepancies": discrepancies,

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

    print(
        f"Total: "
        f"${result['invoice']['total']:,.2f}"
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