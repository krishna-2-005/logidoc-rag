"""
Shipment Intelligence Engine

Phase 3:
Provides structured operational intelligence for logistics shipments.

This module calculates:
- document completeness
- quantity discrepancies
- delivery delays
- invoice totals
- exception count
- shipment status
"""

from datetime import datetime


EXPECTED_DOCUMENT_TYPES = {
    "BOL",
    "POD",
    "INVOICE",
}


def normalize_document_type(value):
    if not value:
        return None

    value = str(value).strip().upper()

    aliases = {
        "BILL OF LADING": "BOL",
        "BILL_OF_LADING": "BOL",
        "PROOF OF DELIVERY": "POD",
        "PROOF_OF_DELIVERY": "POD",
        "BILL": "INVOICE",
    }

    return aliases.get(value, value)


def get_document_types(documents):
    """
    Extract unique document types from indexed document metadata.
    """

    document_types = set()

    for document in documents or []:

        if not isinstance(document, dict):
            continue

        metadata = document.get("metadata", {})

        if not isinstance(metadata, dict):
            continue

        document_type = metadata.get("document_type")

        if not document_type:
            document_type = metadata.get("type")

        document_type = normalize_document_type(document_type)

        if document_type:
            document_types.add(document_type)

    return document_types


def calculate_document_completeness(documents):
    """
    Calculate whether the standard logistics document set is complete.
    """

    found_types = get_document_types(documents)

    missing = sorted(EXPECTED_DOCUMENT_TYPES - found_types)

    completeness = len(EXPECTED_DOCUMENT_TYPES - set(missing)) / len(
        EXPECTED_DOCUMENT_TYPES
    ) * 100

    return {
        "expected_documents": sorted(EXPECTED_DOCUMENT_TYPES),
        "found_documents": sorted(found_types),
        "missing_documents": missing,
        "completeness_percent": round(completeness),
        "complete": len(missing) == 0,
    }


def parse_date(value):
    """
    Convert common logistics date formats into datetime.
    """

    if not value:
        return None

    value = str(value).strip()

    formats = [
        "%B %d, %Y",
        "%b %d, %Y",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
    ]

    for date_format in formats:

        try:
            return datetime.strptime(value, date_format)

        except ValueError:
            continue

    return None


def calculate_delivery_status(expected_date, actual_date):
    """
    Determine delivery performance.
    """

    expected = parse_date(expected_date)
    actual = parse_date(actual_date)

    if not expected or not actual:
        return {
            "status": "UNKNOWN",
            "delay_days": None,
        }

    delay_days = (actual - expected).days

    if delay_days > 0:
        return {
            "status": "DELAYED",
            "delay_days": delay_days,
        }

    if delay_days == 0:
        return {
            "status": "ON TIME",
            "delay_days": 0,
        }

    return {
        "status": "EARLY",
        "delay_days": abs(delay_days),
    }


def calculate_quantity_status(bol_pieces, delivered_pieces):
    """
    Compare BOL quantity against delivered quantity.
    """

    if bol_pieces is None or delivered_pieces is None:
        return {
            "status": "UNKNOWN",
            "difference": None,
        }

    try:
        bol = int(bol_pieces)
        delivered = int(delivered_pieces)

    except (ValueError, TypeError):
        return {
            "status": "UNKNOWN",
            "difference": None,
        }

    difference = bol - delivered

    if difference > 0:
        return {
            "status": "SHORTAGE",
            "difference": difference,
        }

    if difference < 0:
        return {
            "status": "OVERAGE",
            "difference": abs(difference),
        }

    return {
        "status": "MATCHED",
        "difference": 0,
    }


def calculate_invoice_status(invoice_total):
    """
    Normalize invoice amount.
    """

    if invoice_total is None:
        return {
            "amount": None,
            "available": False,
        }

    try:
        amount = float(
            str(invoice_total)
            .replace("$", "")
            .replace(",", "")
            .strip()
        )

        return {
            "amount": round(amount, 2),
            "available": True,
        }

    except (ValueError, TypeError):
        return {
            "amount": None,
            "available": False,
        }


def calculate_exception_count(
    quantity_status,
    delivery_status,
    document_completeness,
):
    """
    Count operational exceptions.
    """

    exceptions = 0

    if quantity_status.get("status") not in ("MATCHED", "UNKNOWN"):
        exceptions += 1

    if delivery_status.get("status") == "DELAYED":
        exceptions += 1

    if not document_completeness.get("complete", False):
        exceptions += 1

    return exceptions


def build_shipment_intelligence(
    documents,
    bol_pieces=None,
    delivered_pieces=None,
    expected_delivery=None,
    actual_delivery=None,
    invoice_total=None,
):
    """
    Build a complete operational intelligence object.
    """

    document_completeness = calculate_document_completeness(documents)

    quantity_status = calculate_quantity_status(
        bol_pieces,
        delivered_pieces,
    )

    delivery_status = calculate_delivery_status(
        expected_delivery,
        actual_delivery,
    )

    invoice_status = calculate_invoice_status(
        invoice_total,
    )

    exception_count = calculate_exception_count(
        quantity_status,
        delivery_status,
        document_completeness,
    )

    if exception_count > 0:
        shipment_status = "EXCEPTION"

    elif (
        document_completeness["complete"]
        and quantity_status["status"] == "MATCHED"
        and delivery_status["status"] in ("ON TIME", "EARLY")
    ):
        shipment_status = "CLEAR"

    else:
        shipment_status = "REVIEW"

    return {
        "shipment_status": shipment_status,
        "exception_count": exception_count,
        "document_completeness": document_completeness,
        "quantity": quantity_status,
        "delivery": delivery_status,
        "invoice": invoice_status,
    }


def print_intelligence(result):
    """
    Console display for testing.
    """

    print("=" * 70)
    print("SHIPMENT INTELLIGENCE")
    print("=" * 70)

    print(f"Shipment Status : {result['shipment_status']}")
    print(f"Exceptions      : {result['exception_count']}")

    completeness = result["document_completeness"]

    print(
        f"Documents       : "
        f"{len(completeness['found_documents'])}/"
        f"{len(completeness['expected_documents'])}"
    )

    print(
        f"Completeness    : "
        f"{completeness['completeness_percent']}%"
    )

    quantity = result["quantity"]

    print(
        f"Quantity Status : "
        f"{quantity['status']}"
    )

    if quantity["difference"] is not None:
        print(
            f"Quantity Diff   : "
            f"{quantity['difference']}"
        )

    delivery = result["delivery"]

    print(
        f"Delivery Status : "
        f"{delivery['status']}"
    )

    if delivery["delay_days"] is not None:
        print(
            f"Delay Days      : "
            f"{delivery['delay_days']}"
        )

    invoice = result["invoice"]

    if invoice["available"]:
        print(
            f"Invoice Total   : "
            f"${invoice['amount']:,.2f}"
        )

    print("=" * 70)


if __name__ == "__main__":

    test_documents = [
        {
            "metadata": {
                "document_type": "BOL"
            }
        },
        {
            "metadata": {
                "document_type": "POD"
            }
        },
        {
            "metadata": {
                "document_type": "INVOICE"
            }
        },
    ]

    result = build_shipment_intelligence(
        documents=test_documents,
        bol_pieces=48,
        delivered_pieces=45,
        expected_delivery="October 3, 2026",
        actual_delivery="October 5, 2026",
        invoice_total="$1,550.00",
    )

    print_intelligence(result)