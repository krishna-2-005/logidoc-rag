"""
LogiDoc-RAG Exception Intelligence Engine

Phase 3:
Converts reconciliation discrepancies into structured operational
exceptions with severity, category, impact, and recommended action.
"""


def create_exception(
    category,
    severity,
    title,
    description,
    impact,
    recommended_action,
):
    return {
        "category": category,
        "severity": severity,
        "title": title,
        "description": description,
        "impact": impact,
        "recommended_action": recommended_action,
    }


def analyze_reconciliation(result):
    exceptions = []

    bol_pieces = result.get("bol_pieces")
    delivered_pieces = result.get("delivered_pieces")

    expected_delivery = result.get("expected_delivery")
    actual_delivery = result.get("actual_delivery")

    invoice_total = result.get("invoice_total")

    # ---------------------------------------------------------
    # QUANTITY EXCEPTION
    # ---------------------------------------------------------
    if (
        bol_pieces is not None
        and delivered_pieces is not None
        and bol_pieces != delivered_pieces
    ):
        difference = abs(bol_pieces - delivered_pieces)

        severity = "HIGH"

        exceptions.append(
            create_exception(
                category="Quantity",
                severity=severity,
                title="Shipment quantity mismatch",
                description=(
                    f"BOL shows {bol_pieces} pieces, "
                    f"but POD shows {delivered_pieces} pieces delivered. "
                    f"Difference: {difference} pieces."
                ),
                impact=(
                    f"{difference} piece(s) are unaccounted for "
                    "between the shipment document and delivery confirmation."
                ),
                recommended_action=(
                    "Review the POD and delivery records, confirm the missing "
                    "pieces with the carrier, and investigate whether a "
                    "partial delivery or documentation error occurred."
                ),
            )
        )

    # ---------------------------------------------------------
    # DELIVERY DATE EXCEPTION
    # ---------------------------------------------------------
    if (
        expected_delivery
        and actual_delivery
        and str(expected_delivery).strip() != str(actual_delivery).strip()
    ):
        exceptions.append(
            create_exception(
                category="Delivery",
                severity="MEDIUM",
                title="Delivery date discrepancy",
                description=(
                    f"Expected delivery was {expected_delivery}, "
                    f"but actual delivery was {actual_delivery}."
                ),
                impact=(
                    "The shipment was delivered later than the expected "
                    "delivery date."
                ),
                recommended_action=(
                    "Review carrier tracking and delivery records to determine "
                    "the reason for the delay and update the shipment timeline."
                ),
            )
        )

    # ---------------------------------------------------------
    # INVOICE EXCEPTION
    # ---------------------------------------------------------
    if invoice_total is not None:
        try:
            invoice_value = float(invoice_total)

            if invoice_value < 0:
                exceptions.append(
                    create_exception(
                        category="Invoice",
                        severity="HIGH",
                        title="Invalid invoice amount",
                        description=(
                            f"The invoice contains an invalid total "
                            f"amount of ${invoice_value:,.2f}."
                        ),
                        impact="Invoice validation requires manual review.",
                        recommended_action=(
                            "Review the invoice calculation and billing details "
                            "before processing payment."
                        ),
                    )
                )

        except (ValueError, TypeError):
            exceptions.append(
                create_exception(
                    category="Invoice",
                    severity="MEDIUM",
                    title="Invoice amount validation required",
                    description="The invoice total could not be validated as a numeric value.",
                    impact="Automated invoice validation is incomplete.",
                    recommended_action=(
                        "Review the invoice total and confirm the amount "
                        "before payment processing."
                    ),
                )
            )

    return exceptions


def get_exception_summary(exceptions):
    summary = {
        "total": len(exceptions),
        "high": 0,
        "medium": 0,
        "low": 0,
    }

    for exception in exceptions:
        severity = exception.get("severity", "").lower()

        if severity == "high":
            summary["high"] += 1
        elif severity == "medium":
            summary["medium"] += 1
        elif severity == "low":
            summary["low"] += 1

    return summary


def print_exceptions(exceptions):
    print("=" * 70)
    print("EXCEPTION INTELLIGENCE")
    print("=" * 70)

    if not exceptions:
        print("No exceptions detected.")
        print("=" * 70)
        return

    for index, exception in enumerate(exceptions, start=1):
        print()
        print(f"EXCEPTION {index}")
        print("-" * 70)
        print(f"Category: {exception['category']}")
        print(f"Severity: {exception['severity']}")
        print(f"Title: {exception['title']}")
        print(f"Description: {exception['description']}")
        print(f"Impact: {exception['impact']}")
        print(f"Recommended Action: {exception['recommended_action']}")

    summary = get_exception_summary(exceptions)

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total Exceptions: {summary['total']}")
    print(f"High: {summary['high']}")
    print(f"Medium: {summary['medium']}")
    print(f"Low: {summary['low']}")
    print("=" * 70)


if __name__ == "__main__":
    test_result = {
        "bol_pieces": 48,
        "delivered_pieces": 45,
        "expected_delivery": "October 3, 2026",
        "actual_delivery": "October 5, 2026",
        "invoice_total": 1550.00,
    }

    exceptions = analyze_reconciliation(test_result)

    print_exceptions(exceptions)