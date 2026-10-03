"""
LogiDoc-RAG Shipment Intelligence Service

Provides a clean business-level intelligence summary
for the Streamlit application.

This module builds intelligence from the existing
reconciliation and indexed document data.
"""

from datetime import datetime
from pathlib import Path
import re

from reconciliation import reconcile_shipment
from rag import get_all_documents


# ============================================================
# HELPERS
# ============================================================

def parse_date(value):
    """
    Convert a logistics date string into a datetime object.
    """

    if not value:
        return None

    value = str(value).strip()

    formats = [
        "%B %d, %Y",
        "%b %d, %Y",
        "%Y-%m-%d",
        "%m/%d/%Y",
    ]

    for date_format in formats:

        try:
            return datetime.strptime(
                value,
                date_format
            )

        except ValueError:
            continue

    return None


def safe_number(value):
    """
    Safely convert a value to a number.
    """

    if value is None:
        return None

    try:
        return float(value)

    except (ValueError, TypeError):
        return None


# ============================================================
# DOCUMENT COMPLETENESS
# ============================================================

def calculate_document_completeness():
    """
    Calculate how many core logistics documents are available.

    Expected document types:
        BOL
        POD
        INVOICE
    """

    try:

        documents, metadatas = get_all_documents()

    except Exception:

        return {
            "documents_found": 0,
            "documents_expected": 3,
            "completeness": 0,
            "document_types": [],
        }

    document_types = set()

    for metadata in metadatas or []:

        if not isinstance(metadata, dict):
            continue

        document_type = metadata.get(
            "document_type"
        )

        if document_type:

            document_types.add(
                str(document_type).upper()
            )

    expected_types = {
        "BOL",
        "POD",
        "INVOICE",
    }

    found_types = (
        document_types
        & expected_types
    )

    completeness = round(
        (
            len(found_types)
            / len(expected_types)
        )
        * 100
    )

    return {
        "documents_found": len(found_types),
        "documents_expected": 3,
        "completeness": completeness,
        "document_types": sorted(found_types),
    }


# ============================================================
# DELIVERY INTELLIGENCE
# ============================================================

def calculate_delivery_intelligence(
    reconciliation
):
    """
    Determine delivery status and delay days.
    """

    delivery = reconciliation.get(
        "delivery",
        {}
    )

    expected = delivery.get(
        "expected"
    )

    actual = delivery.get(
        "actual"
    )

    expected_date = parse_date(
        expected
    )

    actual_date = parse_date(
        actual
    )

    if not expected_date or not actual_date:

        return {
            "status": "NOT AVAILABLE",
            "delay_days": 0,
            "expected": expected,
            "actual": actual,
        }

    delay_days = (
        actual_date - expected_date
    ).days

    if delay_days > 0:

        status = "DELAYED"

    elif delay_days < 0:

        status = "EARLY"

    else:

        status = "ON TIME"

    return {
        "status": status,
        "delay_days": max(
            delay_days,
            0
        ),
        "expected": expected,
        "actual": actual,
    }


# ============================================================
# QUANTITY INTELLIGENCE
# ============================================================

def calculate_quantity_intelligence(
    reconciliation
):
    """
    Determine shipment quantity status.
    """

    quantity = reconciliation.get(
        "quantity",
        {}
    )

    bol = safe_number(
        quantity.get("bol")
    )

    pod = safe_number(
        quantity.get("pod")
    )

    difference = safe_number(
        quantity.get("difference")
    )

    if difference is None:

        if (
            bol is not None
            and pod is not None
        ):

            difference = bol - pod

        else:

            difference = 0

    difference = int(
        abs(difference)
    )

    if (
        bol is not None
        and pod is not None
        and pod < bol
    ):

        status = "SHORTAGE"

    elif (
        bol is not None
        and pod is not None
        and pod > bol
    ):

        status = "OVERAGE"

    elif (
        bol is not None
        and pod is not None
        and pod == bol
    ):

        status = "MATCH"

    else:

        status = "NOT AVAILABLE"

    return {
        "bol_pieces": (
            int(bol)
            if bol is not None
            else None
        ),
        "delivered_pieces": (
            int(pod)
            if pod is not None
            else None
        ),
        "difference": difference,
        "status": status,
    }


# ============================================================
# EXCEPTION INTELLIGENCE
# ============================================================

def build_exceptions(
    reconciliation,
    quantity,
    delivery
):
    """
    Build structured business exceptions.
    """

    exceptions = []

    if quantity["status"] == "SHORTAGE":

        exceptions.append(
            {
                "category": "Quantity",
                "severity": "HIGH",
                "message": (
                    f"{quantity['difference']} "
                    "pieces are missing between "
                    "the BOL and POD."
                ),
            }
        )

    elif quantity["status"] == "OVERAGE":

        exceptions.append(
            {
                "category": "Quantity",
                "severity": "MEDIUM",
                "message": (
                    f"{quantity['difference']} "
                    "additional pieces were "
                    "recorded on the POD."
                ),
            }
        )

    if delivery["status"] == "DELAYED":

        exceptions.append(
            {
                "category": "Delivery",
                "severity": "HIGH",
                "message": (
                    f"Shipment was delivered "
                    f"{delivery['delay_days']} day(s) "
                    "after the expected delivery date."
                ),
            }
        )

    discrepancies = reconciliation.get(
        "discrepancies",
        []
    )

    for discrepancy in discrepancies:

        if not isinstance(
            discrepancy,
            dict
        ):
            continue

        category = str(
            discrepancy.get(
                "category",
                ""
            )
        )

        # Avoid duplicating quantity/delivery
        # exceptions already created above.
        if category in {
            "Quantity",
            "Delivery Date",
        }:
            continue

        exceptions.append(
            {
                "category": category
                or "Other",
                "severity": "MEDIUM",
                "message": str(
                    discrepancy.get(
                        "description",
                        "Discrepancy detected."
                    )
                ),
            }
        )

    return exceptions


# ============================================================
# MAIN INTELLIGENCE FUNCTION
# ============================================================

def analyze_shipment():
    """
    Generate the complete shipment intelligence summary.
    """

    try:

        reconciliation = (
            reconcile_shipment()
        )

    except Exception as error:

        return {
            "success": False,
            "error": str(error),
        }

    if not isinstance(
        reconciliation,
        dict
    ):

        return {
            "success": False,
            "error": (
                "Reconciliation returned "
                "an invalid result."
            ),
        }

    if not reconciliation.get(
        "success",
        False
    ):

        return {
            "success": False,
            "error": reconciliation.get(
                "error",
                "Shipment reconciliation failed."
            ),
        }

    completeness = (
        calculate_document_completeness()
    )

    quantity = (
        calculate_quantity_intelligence(
            reconciliation
        )
    )

    delivery = (
        calculate_delivery_intelligence(
            reconciliation
        )
    )

    exceptions = (
        build_exceptions(
            reconciliation,
            quantity,
            delivery
        )
    )

    invoice = reconciliation.get(
        "invoice",
        {}
    )

    invoice_total = safe_number(
        invoice.get("total")
    )

    if exceptions:

        shipment_status = "EXCEPTION"

    else:

        shipment_status = "CLEAR"

    return {
        "success": True,

        "shipment_id": reconciliation.get(
            "shipment_id",
            "Unknown"
        ),

        "shipment_status": shipment_status,

        "exception_count": len(
            exceptions
        ),

        "exceptions": exceptions,

        "documents": {
            "found": completeness[
                "documents_found"
            ],
            "expected": completeness[
                "documents_expected"
            ],
            "completeness": completeness[
                "completeness"
            ],
            "types": completeness[
                "document_types"
            ],
        },

        "quantity": quantity,

        "delivery": delivery,

        "invoice_total": invoice_total,

        "carrier": reconciliation.get(
            "carrier",
            {}
        ),

        "route": reconciliation.get(
            "route",
            {}
        ),
    }


# ============================================================
# CONSOLE REPORT
# ============================================================

def print_intelligence_report(
    intelligence
):
    """
    Print a clean intelligence report.
    """

    if not intelligence.get(
        "success",
        False
    ):

        print(
            "Shipment Intelligence failed:"
        )

        print(
            intelligence.get(
                "error",
                "Unknown error"
            )
        )

        return

    print("=" * 70)
    print("SHIPMENT INTELLIGENCE")
    print("=" * 70)

    print(
        f"Shipment Status : "
        f"{intelligence['shipment_status']}"
    )

    print(
        f"Exceptions      : "
        f"{intelligence['exception_count']}"
    )

    documents = intelligence[
        "documents"
    ]

    print(
        f"Documents       : "
        f"{documents['found']}/"
        f"{documents['expected']}"
    )

    print(
        f"Completeness    : "
        f"{documents['completeness']}%"
    )

    quantity = intelligence[
        "quantity"
    ]

    print(
        f"Quantity Status : "
        f"{quantity['status']}"
    )

    print(
        f"Quantity Diff   : "
        f"{quantity['difference']}"
    )

    delivery = intelligence[
        "delivery"
    ]

    print(
        f"Delivery Status : "
        f"{delivery['status']}"
    )

    print(
        f"Delay Days      : "
        f"{delivery['delay_days']}"
    )

    invoice_total = (
        intelligence["invoice_total"]
    )

    if invoice_total is not None:

        print(
            f"Invoice Total   : "
            f"${invoice_total:,.2f}"
        )

    else:

        print(
            "Invoice Total   : N/A"
        )

    print("=" * 70)


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    intelligence = (
        analyze_shipment()
    )

    print_intelligence_report(
        intelligence
    )