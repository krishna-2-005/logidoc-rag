from pathlib import Path
import sys
import re

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from chunker import create_all_chunks


def extract_field(text, field_name):
    """
    Extract a value appearing after a field label.

    Example:
    Carrier:
    SwiftLine Logistics

    Returns:
    SwiftLine Logistics
    """

    pattern = rf"{re.escape(field_name)}:\s*\n([^\n]+)"

    match = re.search(
        pattern,
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(1).strip()

    return None


def extract_amount(text, field_name):
    """
    Extract a monetary value.

    Example:
    Total Invoice Amount:
    $1,430.00
    """

    value = extract_field(text, field_name)

    if value:
        value = value.replace("$", "").replace(",", "").strip()

        try:
            return float(value)
        except ValueError:
            return None

    return None


def extract_document_data(chunks):
    """
    Organize chunks by document type.
    """

    documents = {}

    for chunk in chunks:

        metadata = chunk["metadata"]

        document_type = metadata.get("document_type")

        if document_type not in documents:
            documents[document_type] = {
                "text": "",
                "metadata": metadata
            }

        documents[document_type]["text"] += (
            "\n" + chunk["text"]
        )

    return documents


def compare_values(label, value1, value2):
    """
    Compare two values and return MATCH / MISMATCH / NOT AVAILABLE.
    """

    if value1 is None or value2 is None:
        return "NOT AVAILABLE"

    if value1.strip().lower() == value2.strip().lower():
        return "MATCH"

    return "MISMATCH"


def compare_numbers(label, value1, value2):
    """
    Compare two numeric values.
    """

    if value1 is None or value2 is None:
        return "NOT AVAILABLE"

    if value1 == value2:
        return "MATCH"

    return "MISMATCH"


def reconcile_shipment():

    print("=" * 70)
    print("LogiDoc-RAG → Shipment Reconciliation")
    print("=" * 70)

    print("\nLoading logistics documents...")

    chunks = create_all_chunks()

    if not chunks:
        print("\nNo documents found.")
        return

    print(f"Total chunks loaded: {len(chunks)}")

    documents = extract_document_data(chunks)

    print("\nDocuments detected:")

    for document_type in documents:
        print(f"- {document_type}")

    # ---------------------------------------------------------
    # Get documents
    # ---------------------------------------------------------

    bol = documents.get("BOL")
    invoice = documents.get("INVOICE")
    pod = documents.get("POD")

    print("\n" + "-" * 70)
    print("DOCUMENT AVAILABILITY")
    print("-" * 70)

    print(
        f"BOL     : {'FOUND' if bol else 'NOT FOUND'}"
    )

    print(
        f"Invoice : {'FOUND' if invoice else 'NOT FOUND'}"
    )

    print(
        f"POD     : {'FOUND' if pod else 'NOT FOUND'}"
    )

    if not bol or not invoice or not pod:

        print(
            "\nReconciliation requires BOL, Invoice and POD."
        )

        return

    # ---------------------------------------------------------
    # Extract text
    # ---------------------------------------------------------

    bol_text = bol["text"]
    invoice_text = invoice["text"]
    pod_text = pod["text"]

    # ---------------------------------------------------------
    # Shipment ID
    # ---------------------------------------------------------

    shipment_id = (
        bol["metadata"].get("shipment_id")
        or invoice["metadata"].get("shipment_id")
        or pod["metadata"].get("shipment_id")
    )

    # ---------------------------------------------------------
    # Carrier
    # ---------------------------------------------------------

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

    # ---------------------------------------------------------
    # Origin
    # ---------------------------------------------------------

    bol_origin = extract_field(
        bol_text,
        "Origin"
    )

    invoice_origin = extract_field(
        invoice_text,
        "Origin"
    )

    # ---------------------------------------------------------
    # Destination
    # ---------------------------------------------------------

    bol_destination = extract_field(
        bol_text,
        "Destination"
    )

    invoice_destination = extract_field(
        invoice_text,
        "Destination"
    )

    # ---------------------------------------------------------
    # Number of pieces
    # ---------------------------------------------------------

    bol_pieces = extract_field(
        bol_text,
        "Number of Pieces"
    )

    pod_pieces = extract_field(
        pod_text,
        "Number of Pieces Delivered"
    )

    # ---------------------------------------------------------
    # Dates
    # ---------------------------------------------------------

    expected_delivery = extract_field(
        bol_text,
        "Expected Delivery Date"
    )

    actual_delivery = extract_field(
        pod_text,
        "Delivery Date"
    )

    # ---------------------------------------------------------
    # Invoice amounts
    # ---------------------------------------------------------

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

    # ---------------------------------------------------------
    # Perform comparisons
    # ---------------------------------------------------------

    carrier_status_1 = compare_values(
        "Carrier",
        bol_carrier,
        invoice_carrier
    )

    carrier_status_2 = compare_values(
        "Carrier",
        bol_carrier,
        pod_carrier
    )

    origin_status = compare_values(
        "Origin",
        bol_origin,
        invoice_origin
    )

    destination_status = compare_values(
        "Destination",
        bol_destination,
        invoice_destination
    )

    pieces_status = compare_numbers(
        "Pieces",
        (
            int(bol_pieces)
            if bol_pieces and bol_pieces.isdigit()
            else None
        ),
        (
            int(pod_pieces)
            if pod_pieces and pod_pieces.isdigit()
            else None
        )
    )

    # ---------------------------------------------------------
    # Delivery status
    # ---------------------------------------------------------

    if expected_delivery and actual_delivery:

        if (
            expected_delivery.strip().lower()
            == actual_delivery.strip().lower()
        ):
            delivery_status = "ON TIME"
        else:
            delivery_status = "DATE DIFFERENCE"

    else:
        delivery_status = "NOT AVAILABLE"

    # ---------------------------------------------------------
    # Print report
    # ---------------------------------------------------------

    print("\n")
    print("=" * 70)
    print("SHIPMENT RECONCILIATION REPORT")
    print("=" * 70)

    print(f"\nShipment ID: {shipment_id}")

    # ---------------------------------------------------------
    # Carrier
    # ---------------------------------------------------------

    print("\n" + "-" * 70)
    print("CARRIER")
    print("-" * 70)

    print(f"BOL     : {bol_carrier}")
    print(f"Invoice : {invoice_carrier}")
    print(f"POD     : {pod_carrier}")

    print(
        f"Status  : "
        f"{carrier_status_1}"
        if carrier_status_1 == "MISMATCH"
        else f"Status  : {carrier_status_1}"
    )

    # ---------------------------------------------------------
    # Route
    # ---------------------------------------------------------

    print("\n" + "-" * 70)
    print("ROUTE")
    print("-" * 70)

    print("\nOrigin")
    print(f"BOL     : {bol_origin}")
    print(f"Invoice : {invoice_origin}")
    print(f"Status  : {origin_status}")

    print("\nDestination")
    print(f"BOL     : {bol_destination}")
    print(f"Invoice : {invoice_destination}")
    print(f"Status  : {destination_status}")

    # ---------------------------------------------------------
    # Quantity
    # ---------------------------------------------------------

    print("\n" + "-" * 70)
    print("QUANTITY")
    print("-" * 70)

    print(f"BOL : {bol_pieces} pieces")
    print(f"POD : {pod_pieces} pieces")
    print(f"Status : {pieces_status}")

    # ---------------------------------------------------------
    # Delivery
    # ---------------------------------------------------------

    print("\n" + "-" * 70)
    print("DELIVERY")
    print("-" * 70)

    print(f"Expected : {expected_delivery}")
    print(f"Actual   : {actual_delivery}")
    print(f"Status   : {delivery_status}")

    # ---------------------------------------------------------
    # Invoice
    # ---------------------------------------------------------

    print("\n" + "-" * 70)
    print("INVOICE")
    print("-" * 70)

    print(
        f"Base Freight           : "
        f"${base_freight:,.2f}"
        if base_freight is not None
        else "Base Freight           : N/A"
    )

    print(
        f"Fuel Surcharge         : "
        f"${fuel_surcharge:,.2f}"
        if fuel_surcharge is not None
        else "Fuel Surcharge         : N/A"
    )

    print(
        f"Detention Charge       : "
        f"${detention_charge:,.2f}"
        if detention_charge is not None
        else "Detention Charge       : N/A"
    )

    print(
        f"Other Accessorial      : "
        f"${other_charges:,.2f}"
        if other_charges is not None
        else "Other Accessorial      : N/A"
    )

    print(
        f"Total Invoice Amount   : "
        f"${total_invoice:,.2f}"
        if total_invoice is not None
        else "Total Invoice Amount   : N/A"
    )

    # ---------------------------------------------------------
    # Final status
    # ---------------------------------------------------------

    statuses = [
        carrier_status_1,
        carrier_status_2,
        origin_status,
        destination_status,
        pieces_status,
    ]

    mismatches = [
        status
        for status in statuses
        if status == "MISMATCH"
    ]

    print("\n" + "=" * 70)
    print("FINAL STATUS")
    print("=" * 70)

    if mismatches:
        print("\n⚠ DISCREPANCIES DETECTED")
        print(f"Total discrepancies: {len(mismatches)}")
    else:
        print("\n✓ NO MAJOR DISCREPANCIES FOUND")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    reconcile_shipment()