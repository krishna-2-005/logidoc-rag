"""
Tests for src/shipment_calculations.py on synthetic document records
(no PDFs, no index, no Ollama).

Covers quantity differences (R04-type), delivery-date calculations
(R03/R06-type) and invoice line-item sums (R07/R08-type), including
missing, conflicting and unreadable values, date boundaries, and the
evidence each answer cites. Also the routing regressions found in the
final review: comparison wording, "on time or early" and other
combined timing wording, pickup timing, quantity and timing in one
question, damaged pieces, unrecognised date formats, qualified
delivery-date labels, and invoice amounts without a "$".

Run from the project root:
    python -m pytest tests/test_shipment_calculations.py
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from shipment_calculations import ABSTAINED, ANSWERED, calculate


# ============================================================
# SYNTHETIC RECORDS (rag.get_document_records format)
# ============================================================

def record(doc_type, text, source, page=1):

    return {
        "document": text,
        "metadata": {
            "source": source,
            "page": page,
            "shipment_id": "SHJ-2026-901",
            "document_type": doc_type.upper(),
        },
        "type": doc_type,
        "normalized": " ".join(text.split()),
        "raw": text,
    }


def bol(pieces=None, expected=None, pickup=None, extra="", source="BOL_TEST.pdf"):

    fields = [
        f"Pickup Date: {pickup}" if pickup else "",
        f"Expected Delivery Date: {expected}" if expected else "",
        f"Number of Pieces: {pieces}" if pieces is not None else "",
    ]

    return record(
        "bol",
        "BILL OF LADING Shipment Number: SHJ-2026-901 "
        + " ".join(fields) + " Total Weight: 9,000 lbs " + extra,
        source,
    )


def pod(delivered=None, date=None, extra="", source="POD_TEST.pdf"):

    fields = [
        f"Delivery Date: {date}" if date else "",
        f"Number of Pieces Delivered: {delivered}" if delivered is not None else "",
    ]

    return record(
        "pod",
        "PROOF OF DELIVERY Shipment Number: SHJ-2026-901 "
        + " ".join(fields) + " Delivery Status: Delivered " + extra,
        source,
    )


def invoice(items, total, extra="", source="INVOICE_TEST.pdf"):

    lines = " ".join(f"{label}: ${value}" for label, value in items.items())

    total_text = f"Total Invoice Amount: ${total}" if total is not None else ""

    return record(
        "invoice",
        "FREIGHT INVOICE Invoice Number: INV-SHJ-2026-901 Billing Date: "
        f"October 4, 2026 {lines} {extra} {total_text} Payment Terms: Net 30 "
        "Due Date: November 3, 2026",
        source,
    )


SAMPLE_ITEMS = {
    "Base Freight": "1,200.00",
    "Fuel Surcharge": "180.00",
    "Detention Charge": "0.00",
    "Other Accessorial Charges": "50.00",
}


def cited(calculation):

    return [
        (item.record["metadata"]["source"], item.text)
        for item in calculation.evidence
    ]


def abstains(calculation, *fragments):

    assert calculation.status == ABSTAINED, calculation.answer
    assert calculation.evidence == []

    for fragment in fragments:
        assert fragment in calculation.answer, calculation.answer


# ============================================================
# QUANTITIES
# ============================================================

@pytest.mark.parametrize(
    "question",
    [
        "How many pieces were missing?",
        "How many pieces were missing between the BOL and the POD?",
        "What was the shortage in pieces?",
        "How many pieces were short?",
    ],
)
def test_shortage(question):

    result = calculate(question, [bol(48), pod(45)])

    assert result.status == ANSWERED
    assert result.answer == (
        "3 pieces were missing: the BOL lists 48 pieces, but the POD "
        "records 45 pieces delivered (a shortage of 3)."
    )
    assert cited(result) == [
        ("BOL_TEST.pdf", "Number of Pieces: 48"),
        ("POD_TEST.pdf", "Number of Pieces Delivered: 45"),
    ]


def test_single_piece_shortage_wording():

    result = calculate("How many pieces were missing?", [bol(10), pod(9)])

    assert result.answer.startswith("1 piece was missing:")


def test_over_delivery():

    missing = calculate("How many pieces were missing?", [bol(60), pod(62)])

    assert missing.answer == (
        "No pieces were missing; 2 pieces more were delivered than the BOL "
        "lists: the BOL lists 60 pieces and the POD records 62 pieces "
        "delivered (an over-delivery of 2)."
    )

    extra = calculate("How many extra pieces were delivered?", [bol(60), pod(62)])

    assert extra.answer == (
        "2 pieces more were delivered than the BOL lists: the BOL lists 60 "
        "pieces and the POD records 62 pieces delivered (an over-delivery "
        "of 2)."
    )

    assert calculate(
        "Were more pieces delivered than the BOL listed?", [bol(60), pod(62)]
    ).answer.startswith("Yes. 2 pieces more")


def test_zero_difference():

    missing = calculate("How many pieces were missing?", [bol(60), pod(60)])

    assert missing.answer == (
        "No pieces were missing: the BOL lists 60 pieces and the POD "
        "records 60 pieces delivered (a difference of 0)."
    )

    difference = calculate(
        "What is the difference between the expected and delivered quantities?",
        [bol(60), pod(60)],
    )

    assert difference.answer == (
        "The BOL lists 60 pieces and the POD records 60 pieces delivered: "
        "a difference of 0; the counts match."
    )


@pytest.mark.parametrize(
    "delivered, expected_answer",
    [
        (45, "No. The POD records 45 pieces delivered, 3 fewer than the 48 "
             "pieces on the BOL (a shortage of 3)."),
        (48, "Yes. The POD records 48 pieces delivered, matching the 48 "
             "pieces on the BOL."),
        (50, "Yes. The POD records 50 pieces delivered, 2 more than the 48 "
             "pieces on the BOL (an over-delivery of 2)."),
    ],
)
def test_were_all_pieces_delivered(delivered, expected_answer):

    result = calculate(
        "Were all of the pieces listed on the BOL delivered?",
        [bol(48), pod(delivered)],
    )

    assert result.answer == expected_answer


def test_yes_no_wording_for_missing_and_difference():

    assert calculate(
        "Were any pieces missing?", [bol(48), pod(45)]
    ).answer.startswith("Yes. 3 pieces were missing")

    assert calculate(
        "Were any pieces missing?", [bol(48), pod(48)]
    ).answer.startswith("No. No pieces were missing")

    assert calculate(
        "Do the BOL and POD piece counts match?", [bol(48), pod(48)]
    ).answer.startswith("Yes. The BOL lists 48 pieces")


def test_counts_and_thousands_separators():

    records = [bol("1,200"), pod("1,195")]

    assert calculate("How many pieces were expected?", records).answer == (
        "The BOL lists 1,200 pieces."
    )

    assert calculate("What quantity was received?", records).answer == (
        "1,195 pieces were delivered."
    )

    assert calculate("How many pieces were missing?", records).answer.startswith(
        "5 pieces were missing: the BOL lists 1,200 pieces"
    )


@pytest.mark.parametrize(
    "records, field",
    [
        ([bol(48), pod(None)], "delivered piece count"),
        ([bol(None), pod(45)], "expected piece count"),
        ([pod(45)], "expected piece count"),
        ([], "expected piece count"),
    ],
)
def test_missing_quantity_is_never_zero(records, field):

    result = calculate("How many pieces were missing?", records)

    abstains(result, field, "was not found")
    assert "0" not in result.answer.replace("2026", "")


def test_conflicting_bols():

    result = calculate(
        "How many pieces were missing?",
        [bol(48, source="BOL_A.pdf"), bol(50, source="BOL_B.pdf"), pod(45)],
    )

    abstains(
        result,
        "different values for the expected piece count",
        "48 (BOL_A.pdf page 1), 50 (BOL_B.pdf page 1)",
    )

    # The single-field count is not guessed either
    abstains(
        calculate(
            "How many pieces were expected?",
            [bol(48, source="BOL_A.pdf"), bol(50, source="BOL_B.pdf")],
        ),
        "different values",
    )


def test_conflict_inside_one_document():

    result = calculate(
        "How many pieces were delivered?",
        [pod(45, extra="Number of Pieces Delivered: 47")],
    )

    abstains(result, "45 (POD_TEST.pdf page 1), 47 (POD_TEST.pdf page 1)")


def test_identical_copies_are_not_a_conflict():

    result = calculate(
        "How many pieces were missing?",
        [bol(48, source="BOL_A.pdf"), bol(48, source="BOL_COPY.pdf"), pod(45)],
    )

    assert result.status == ANSWERED
    assert [source for source, _ in cited(result)] == [
        "BOL_A.pdf", "BOL_COPY.pdf", "POD_TEST.pdf",
    ]


def test_quantities_come_only_from_their_document_types():

    # A piece count on a POD or an invoice is not the BOL's
    result = calculate(
        "How many pieces were missing?",
        [
            bol(48),
            pod(45, extra="Number of Pieces: 99"),
            invoice(SAMPLE_ITEMS, "1,430.00", extra="Pieces: 7"),
        ],
    )

    assert result.answer.startswith("3 pieces were missing")


@pytest.mark.parametrize(
    "question",
    [
        "Who was the carrier?",
        "What was the delivery date?",
        "Were more than 40 pieces delivered?",
        "Was the number of pieces delivered more than 40?",
        "Were more pieces shipped than ordered?",
        "How many pieces does the invoice list?",
        "What is the total weight?",
    ],
)
def test_questions_that_are_not_quantity_calculations(question):

    assert calculate(question, [bol(48), pod(45)]) is None


SHORTAGE_OF_3 = (
    "3 pieces were missing: the BOL lists 48 pieces, but the POD records "
    "45 pieces delivered (a shortage of 3)."
)

DIFFERENCE_OF_3 = (
    "The BOL lists 48 pieces and the POD records 45 pieces delivered: a "
    "difference of 3 (a shortage of 3)."
)


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Is the number of pieces delivered consistent with the BOL?",
         "No. " + DIFFERENCE_OF_3),
        ("Were the delivered pieces consistent with the BOL count?",
         "No. " + DIFFERENCE_OF_3),
        ("Is there a discrepancy between the BOL and POD piece counts?",
         "Yes. " + DIFFERENCE_OF_3),
        ("How many more pieces were expected than delivered?", SHORTAGE_OF_3),
        ("How many less pieces were delivered than expected?", SHORTAGE_OF_3),
        ("How many fewer pieces were delivered than shipped?", SHORTAGE_OF_3),
        ("Were there more pieces than expected?",
         "No. No pieces were over-delivered; 3 pieces were missing: the BOL "
         "lists 48 pieces and the POD records 45 pieces delivered (a "
         "shortage of 3)."),
    ],
)
def test_comparisons_are_never_single_field_answers(question, expected):

    # The first five were answered "The BOL lists 48 pieces."
    result = calculate(question, [bol(48), pod(45)])

    assert result.status == ANSWERED
    assert result.answer == expected
    assert cited(result) == [
        ("BOL_TEST.pdf", "Number of Pieces: 48"),
        ("POD_TEST.pdf", "Number of Pieces Delivered: 45"),
    ]


@pytest.mark.parametrize(
    "question",
    [
        "How many pieces weren't delivered?",
        "How many pieces didn't arrive?",
        "How many pieces never arrived?",
        "How many pieces were not received?",
    ],
)
def test_negated_delivery_wording_is_the_shortage(question):

    # Was answered "45 pieces were delivered."
    result = calculate(question, [bol(48), pod(45)])

    assert result.answer == SHORTAGE_OF_3


@pytest.mark.parametrize(
    "question",
    ["Did all pieces arrive?", "Were all the pieces received?"],
)
def test_all_pieces_arrived(question):

    assert calculate(question, [bol(48), pod(45)]).answer == (
        "No. The POD records 45 pieces delivered, 3 fewer than the 48 "
        "pieces on the BOL (a shortage of 3)."
    )


@pytest.mark.parametrize(
    "question",
    [
        "How many more pieces were delivered than expected?",
        "How many fewer pieces were expected than delivered?",
        "How many less pieces were on the BOL than on the POD?",
    ],
)
def test_comparison_direction_for_an_over_delivery(question):

    assert calculate(question, [bol(60), pod(62)]).answer == (
        "2 pieces more were delivered than the BOL lists: the BOL lists 60 "
        "pieces and the POD records 62 pieces delivered (an over-delivery "
        "of 2)."
    )


def test_yes_no_comparisons_answer_the_question_asked():

    equal, short = [bol(48), pod(48)], [bol(48), pod(45)]
    difference = "Is there a difference between the BOL and POD piece counts?"
    consistent = "Is the number of pieces delivered consistent with the BOL?"

    # "difference": Yes when the counts differ
    assert calculate(difference, short).answer == "Yes. " + DIFFERENCE_OF_3
    assert calculate(difference, equal).answer.startswith("No. The BOL lists 48")

    # "consistent": Yes when they are equal
    assert calculate(consistent, equal).answer.startswith("Yes. The BOL lists 48")

    # Negated: the result, without a Yes or No that could be misread
    assert calculate(
        "Do the BOL and POD piece counts not match?", short
    ).answer == DIFFERENCE_OF_3


def test_damaged_pieces_are_not_the_delivered_count():

    # Previously "45 pieces were delivered."
    question = "How many pieces were damaged at delivery?"

    with_report = calculate(
        question,
        [bol(48), pod(45, extra="Damage Report: No visible damage reported. "
                                "Receiver Comments: All received.")],
    )

    abstains(
        with_report,
        "I cannot answer this: this shipment's documents do not give a "
        "number of damaged pieces; the POD Damage Report reads \"No visible "
        "damage reported.\" (POD_TEST.pdf page 1).",
    )

    without_report = calculate(question, [bol(48), pod(45)])

    abstains(without_report, "do not give a number of damaged pieces")

    for result in (with_report, without_report):
        assert "45" not in result.answer


@pytest.mark.parametrize(
    "question, count, expected",
    [
        ("How many pieces were damaged?", 2, "The POD records 2 damaged pieces."),
        ("Were any pieces damaged?", 1, "Yes. The POD records 1 damaged piece."),
        ("Were any pieces damaged?", 0, "No. The POD records 0 damaged pieces."),
    ],
)
def test_damaged_piece_count_from_the_pod(question, count, expected):

    result = calculate(question, [bol(48), pod(45, extra=f"Damaged Pieces: {count}")])

    assert result.answer == expected
    assert cited(result) == [("POD_TEST.pdf", f"Damaged Pieces: {count}")]


def test_conflicting_damaged_piece_counts():

    abstains(
        calculate(
            "How many pieces were damaged?",
            [pod(45, extra="Damaged Pieces: 2"),
             pod(45, extra="Damaged Pieces: 3", source="POD_B.pdf")],
        ),
        "different values for the damaged piece count",
    )


# ============================================================
# DELIVERY DATES
# ============================================================

def dated(expected=None, actual=None, pickup=None):

    return [bol(48, expected=expected, pickup=pickup), pod(48, date=actual)]


def test_late_delivery():

    result = calculate(
        "How many days late was the delivery?",
        dated("October 3, 2026", "October 5, 2026"),
    )

    assert result.answer == (
        "The delivery was 2 days late: delivered on October 5, 2026, 2 days "
        "after the expected delivery date of October 3, 2026."
    )
    assert cited(result) == [
        ("BOL_TEST.pdf", "Expected Delivery Date: October 3, 2026"),
        ("POD_TEST.pdf", "Delivery Date: October 5, 2026"),
    ]

    assert calculate(
        "Was the shipment delivered on time?",
        dated("October 3, 2026", "October 5, 2026"),
    ).answer == (
        "No. The shipment was delivered 2 days late: on October 5, 2026, 2 "
        "days after the expected delivery date of October 3, 2026."
    )


def test_early_delivery():

    records = dated("October 9, 2026", "October 8, 2026")

    assert calculate("How many days early was the delivery?", records).answer == (
        "The delivery was 1 day early: delivered on October 8, 2026, 1 day "
        "before the expected delivery date of October 9, 2026."
    )

    assert calculate("How many days late was the delivery?", records).answer == (
        "The delivery was not late; it was 1 day early: delivered on October "
        "8, 2026, 1 day before the expected delivery date of October 9, 2026."
    )

    assert calculate("Was the shipment delivered on time?", records).answer == (
        "Yes. The shipment was delivered 1 day early: on October 8, 2026, 1 "
        "day before the expected delivery date of October 9, 2026."
    )


def test_on_time_same_day():

    records = dated("October 9, 2026", "October 9, 2026")

    assert calculate("Was the shipment delivered on time?", records).answer == (
        "Yes. The shipment was delivered on time: on October 9, 2026, the "
        "expected delivery date (0 days late)."
    )

    assert calculate("Was the delivery late?", records).answer == (
        "No. The delivery was not late (0 days): delivered on October 9, "
        "2026, the expected delivery date."
    )


@pytest.mark.parametrize(
    "expected, actual, late",
    [
        ("January 31, 2027", "February 2, 2027", "2 days late"),
        ("December 31, 2026", "January 2, 2027", "2 days late"),
        ("February 28, 2028", "March 1, 2028", "2 days late"),
        ("February 28, 2027", "March 1, 2027", "1 day late"),
        ("March 1, 2028", "February 28, 2028", "2 days early"),
    ],
)
def test_month_year_and_leap_year_boundaries(expected, actual, late):

    result = calculate(
        "What were the expected and actual delivery dates?",
        dated(expected, actual),
    )

    assert result.answer == (
        f"The expected delivery date was {expected} and the actual delivery "
        f"date was {actual}: {late}."
    )


def test_other_date_formats():

    result = calculate(
        "How many days late was the delivery?",
        dated("2026-10-03", "Oct. 5, 2026"),
    )

    assert result.answer.startswith("The delivery was 2 days late")


@pytest.mark.parametrize(
    "records, field",
    [
        (dated(None, "October 5, 2026"), "expected delivery date"),
        (dated("October 3, 2026", None), "actual delivery date"),
    ],
)
def test_missing_dates(records, field):

    abstains(
        calculate("Was the shipment delivered on time?", records),
        field,
        "was not found",
    )


def test_conflicting_delivery_dates():

    records = [
        bol(48, expected="October 3, 2026"),
        pod(48, date="October 5, 2026", source="POD_A.pdf"),
        pod(48, date="October 6, 2026", source="POD_B.pdf"),
    ]

    abstains(
        calculate("How many days late was the delivery?", records),
        "different values for the actual delivery date",
        "October 5, 2026 (POD_A.pdf page 1), October 6, 2026 (POD_B.pdf page 1)",
    )


def test_unreadable_date():

    abstains(
        calculate(
            "How many days late was the delivery?",
            dated("February 30, 2026", "March 2, 2026"),
        ),
        "'February 30, 2026'",
        "could not be read",
    )


def test_dates_are_not_confused():

    # BOL pickup date, POD "Expected Delivery Date" and invoice billing
    # and due dates must not be used as the expected or actual date
    records = [
        bol(48, expected="October 3, 2026", pickup="October 2, 2026"),
        pod(48, date="October 5, 2026",
            extra="Expected Delivery Date: October 1, 2026"),
        invoice(SAMPLE_ITEMS, "1,430.00"),
    ]

    result = calculate("How many days late was the delivery?", records)

    assert result.answer.startswith("The delivery was 2 days late")
    assert cited(result) == [
        ("BOL_TEST.pdf", "Expected Delivery Date: October 3, 2026"),
        ("POD_TEST.pdf", "Delivery Date: October 5, 2026"),
    ]


@pytest.mark.parametrize(
    "question",
    [
        "How many days passed between pickup and delivery?",
        "How long did delivery take after pickup in days?",
        "How many days was the shipment in transit?",
    ],
)
def test_days_between_pickup_and_delivery(question):

    result = calculate(
        question,
        dated("October 9, 2026", "October 8, 2026", pickup="October 6, 2026"),
    )

    assert result.answer == (
        "2 days passed between pickup and delivery: picked up on October 6, "
        "2026 and delivered on October 8, 2026."
    )
    assert cited(result) == [
        ("BOL_TEST.pdf", "Pickup Date: October 6, 2026"),
        ("POD_TEST.pdf", "Delivery Date: October 8, 2026"),
    ]


def test_pickup_and_delivery_edge_cases():

    question = "How many days passed between pickup and delivery?"

    assert calculate(
        question, dated(None, "October 6, 2026", pickup="October 6, 2026")
    ).answer == (
        "0 days passed between pickup and delivery: the shipment was picked "
        "up and delivered on October 6, 2026."
    )

    abstains(
        calculate(question, dated(None, "October 5, 2026", pickup="October 6, 2026")),
        "is before the pickup date",
    )

    abstains(
        calculate(question, dated("October 9, 2026", "October 8, 2026")),
        "pickup date",
        "was not found",
    )


@pytest.mark.parametrize(
    "question",
    [
        "What was the delivery date?",
        "What was the expected delivery date?",
        "When is the invoice due?",
        "What is the trailer license plate number?",
    ],
)
def test_questions_that_are_not_date_calculations(question):

    assert calculate(question, dated("October 3, 2026", "October 5, 2026")) is None


ON_TIME = (
    "The shipment was delivered on time: on October 9, 2026, the expected "
    "delivery date (0 days late)."
)

EARLY = (
    "The shipment was delivered 1 day early: on October 8, 2026, 1 day "
    "before the expected delivery date of October 9, 2026."
)

LATE = (
    "The shipment was delivered 2 days late: on October 11, 2026, 2 days "
    "after the expected delivery date of October 9, 2026."
)


@pytest.mark.parametrize(
    "question",
    [
        "Was the shipment delivered on time or early?",
        "Was the shipment delivered early or on time?",
    ],
)
@pytest.mark.parametrize(
    "actual, expected_answer",
    [
        ("October 9, 2026", "Yes. " + ON_TIME),
        ("October 8, 2026", "Yes. " + EARLY),
        ("October 11, 2026", "No. " + LATE),
    ],
)
def test_on_time_or_early(question, actual, expected_answer):

    # The same day was answered "No. The delivery was not early (0 days)"
    assert calculate(question, dated("October 9, 2026", actual)).answer == expected_answer


@pytest.mark.parametrize(
    "question",
    [
        "Was the shipment late or on time?",
        "Was the shipment delivered early or late?",
        "Was the shipment early, on time or late?",
    ],
)
@pytest.mark.parametrize(
    "actual, statement",
    [
        ("October 9, 2026", ON_TIME),
        ("October 8, 2026", EARLY),
        ("October 11, 2026", LATE),
    ],
)
def test_either_or_timing_is_answered_with_the_result(question, actual, statement):

    # The same day was answered "No. The delivery was not late (0 days)"
    assert calculate(question, dated("October 9, 2026", actual)).answer == statement


def test_single_timing_questions_keep_their_meaning():

    same_day = dated("October 9, 2026", "October 9, 2026")

    assert calculate("Was the shipment early?", same_day).answer == (
        "No. The delivery was not early (0 days): delivered on October 9, "
        "2026, the expected delivery date."
    )

    # Negated: the result, without a Yes or No that could be misread
    assert calculate(
        "Was the shipment not delivered on time?",
        dated("October 9, 2026", "October 11, 2026"),
    ).answer == LATE

    # "after the expected date" is a delivery-timing question
    assert calculate(
        "How many days after the expected date was it delivered?",
        dated("October 9, 2026", "October 11, 2026"),
    ).answer.startswith("The delivery was 2 days late")


@pytest.mark.parametrize(
    "question",
    [
        "Was the shipment more than 2 days late?",
        "Was the delivery late by more than 1 day?",
        "Was the shipment at least 1 day late?",
    ],
)
def test_timing_thresholds_are_not_judged(question):

    # Was answered "Yes. The delivery was 1 day late" to "more than 2
    # days late?": the threshold is not checked, so no Yes or No
    result = calculate(question, dated("October 9, 2026", "October 10, 2026"))

    assert result.answer == (
        "The delivery was 1 day late: delivered on October 10, 2026, 1 day "
        "after the expected delivery date of October 9, 2026."
    )


@pytest.mark.parametrize(
    "question",
    [
        "Was the pickup on time?",
        "Was the shipment picked up late?",
        "Was the shipment picked up early?",
        "Was the pickup delayed?",
        "Was the shipment picked up and delivered on time?",
    ],
)
def test_pickup_timing_is_never_answered_with_delivery_dates(question):

    # Was answered "No. The shipment was delivered 2 days late: ..."
    result = calculate(
        question,
        dated("October 3, 2026", "October 5, 2026", pickup="October 1, 2026"),
    )

    abstains(
        result,
        "I cannot answer whether the pickup was on time: pickup timing is "
        "not calculated",
    )
    assert "2 days late" not in result.answer


def test_unrecognised_date_format_is_never_skipped():

    question = "Was the shipment delivered on time?"

    # It used to be ignored, and the other POD's date was used without
    # reporting a conflict
    abstains(
        calculate(
            question,
            [
                bol(48, expected="October 3, 2026"),
                pod(48, date="10/05/2026", source="POD_SCAN.pdf"),
                pod(48, date="October 6, 2026"),
            ],
        ),
        "the actual delivery date (POD Delivery Date) '10/05/2026' "
        "(POD_SCAN.pdf page 1) could not be read",
    )

    abstains(
        calculate(question, dated("October 3, 2026", "10/05/2026")),
        "'10/05/2026'",
        "could not be read",
    )

    abstains(
        calculate(
            "How many days passed between pickup and delivery?",
            dated(None, "October 8, 2026", pickup="06.10.2026"),
        ),
        "pickup date (BOL Pickup Date) '06.10.2026'",
        "could not be read",
    )

    # A value that is not a date at all is no date value
    abstains(calculate(question, dated("October 3, 2026", "Pending")), "was not found")


@pytest.mark.parametrize(
    "written",
    ["October 5 2026", "5 October 2026", "October 5th, 2026", "Oct 5, 2026", "2026-10-05"],
)
def test_unambiguous_date_forms_are_read(written):

    result = calculate(
        "How many days late was the delivery?",
        dated("October 3, 2026", written),
    )

    assert result.answer == (
        f"The delivery was 2 days late: delivered on {written}, 2 days after "
        "the expected delivery date of October 3, 2026."
    )


@pytest.mark.parametrize(
    "label",
    [
        "Promised Delivery Date",
        "Required Delivery Date",
        "Scheduled Delivery Date",
        "Target Delivery Date",
        "Requested Delivery Date",
    ],
)
def test_qualified_delivery_dates_are_not_the_actual_date(label):

    question = "Was the shipment delivered on time?"
    expected = "October 3, 2026"

    abstains(
        calculate(question, [bol(48, expected=expected),
                             pod(48, extra=f"{label}: October 3, 2026")]),
        "actual delivery date",
        "was not found",
    )

    result = calculate(
        question,
        [bol(48, expected=expected),
         pod(48, date="October 5, 2026", extra=f"{label}: October 3, 2026")],
    )

    assert result.answer.startswith("No. The shipment was delivered 2 days late")
    assert cited(result)[1] == ("POD_TEST.pdf", "Delivery Date: October 5, 2026")


# ============================================================
# QUANTITY AND DELIVERY TIMING IN ONE QUESTION
# ============================================================

@pytest.mark.parametrize(
    "delivered, actual, expected_answer",
    [
        (48, "October 10, 2026",
         "No. The POD records 48 pieces delivered, matching the 48 pieces on "
         "the BOL. The shipment was delivered 2 days late: on October 10, "
         "2026, 2 days after the expected delivery date of October 8, 2026."),
        (45, "October 8, 2026",
         "No. The POD records 45 pieces delivered, 3 fewer than the 48 "
         "pieces on the BOL (a shortage of 3). The shipment was delivered on "
         "time: on October 8, 2026, the expected delivery date (0 days late)."),
        (48, "October 7, 2026",
         "Yes. The POD records 48 pieces delivered, matching the 48 pieces "
         "on the BOL. The shipment was delivered 1 day early: on October 7, "
         "2026, 1 day before the expected delivery date of October 8, 2026."),
    ],
)
def test_all_pieces_on_time_needs_both_parts(delivered, actual, expected_answer):

    # Was answered from the piece counts only: "Yes. ... matching ..."
    # even when the delivery was late
    records = [bol(48, expected="October 8, 2026"), pod(delivered, date=actual)]

    for question in (
        "Were all pieces delivered on time?",
        "Was the shipment delivered complete and on time?",
    ):

        result = calculate(question, records)

        assert result.answer == expected_answer, question
        assert cited(result) == [
            ("BOL_TEST.pdf", "Number of Pieces: 48"),
            ("POD_TEST.pdf", f"Number of Pieces Delivered: {delivered}"),
            ("BOL_TEST.pdf", "Expected Delivery Date: October 8, 2026"),
            ("POD_TEST.pdf", f"Delivery Date: {actual}"),
        ]


def test_all_pieces_on_time_with_a_missing_field_abstains():

    question = "Were all pieces delivered on time?"

    abstains(
        calculate(question, [bol(48), pod(48, date="October 8, 2026")]),
        "expected delivery date",
        "was not found",
    )

    abstains(
        calculate(question, [bol(48, expected="October 8, 2026"),
                             pod(None, date="October 8, 2026")]),
        "delivered piece count",
        "was not found",
    )


# ============================================================
# INVOICE LINE ITEMS VS TOTAL
# ============================================================

QUESTION = "Do the invoice line items add up to the total invoice amount?"


def test_invoice_that_does_not_add_up():

    result = calculate(QUESTION, [invoice(SAMPLE_ITEMS, "1,550.00")])

    assert result.answer == (
        "No. The invoice line items (Base Freight $1,200.00 + Fuel Surcharge "
        "$180.00 + Detention Charge $0.00 + Other Accessorial Charges $50.00) "
        "add up to $1,430.00, but the Total Invoice Amount is $1,550.00, a "
        "difference of $120.00."
    )
    assert [text for _, text in cited(result)] == [
        "Base Freight: $1,200.00",
        "Fuel Surcharge: $180.00",
        "Detention Charge: $0.00",
        "Other Accessorial Charges: $50.00",
        "Total Invoice Amount: $1,550.00",
    ]


def test_invoice_that_adds_up_with_cents():

    items = {
        "Base Freight": "0.10",
        "Fuel Surcharge": "0.20",
        "Detention Charge": "0.00",
        "Other Accessorial Charges": "1,000.05",
    }

    result = calculate(QUESTION, [invoice(items, "1,000.35")])

    assert result.answer.startswith("Yes.")
    assert "add up to $1,000.35, which equals" in result.answer


def test_identical_invoice_copies():

    result = calculate(
        QUESTION,
        [
            invoice(SAMPLE_ITEMS, "1,550.00", source="INVOICE_A.pdf"),
            invoice(SAMPLE_ITEMS, "1,550.00", source="INVOICE_COPY.pdf"),
        ],
    )

    assert result.status == ANSWERED
    assert {source for source, _ in cited(result)} == {"INVOICE_A.pdf", "INVOICE_COPY.pdf"}


def test_two_different_invoices():

    abstains(
        calculate(
            QUESTION,
            [
                invoice(SAMPLE_ITEMS, "1,550.00", source="INVOICE_A.pdf"),
                invoice(SAMPLE_ITEMS, "1,430.00", source="INVOICE_B.pdf"),
            ],
        ),
        "different values for the invoice Total Invoice Amount",
    )


def test_unknown_charge_in_another_chunk_of_the_invoice():

    # The first chunk is complete on its own; an extra charge in the
    # second chunk of the same invoice must still stop the sum
    second_chunk = record(
        "invoice", "Lumper Fee: $75.00 Signature: A. Clerk", "INVOICE_TEST.pdf", page=2
    )

    abstains(
        calculate(QUESTION, [invoice(SAMPLE_ITEMS, "1,505.00"), second_chunk]),
        "not known line items: $75.00",
    )


def test_unknown_charge_with_the_same_amount_as_a_line_item():

    abstains(
        calculate(
            QUESTION,
            [invoice(SAMPLE_ITEMS, "1,480.00", extra="Lumper Fee: $50.00")],
        ),
        "not known line items: $50.00",
    )


@pytest.mark.parametrize(
    "items, total, field",
    [
        ({k: v for k, v in SAMPLE_ITEMS.items() if k != "Fuel Surcharge"},
         "1,250.00", "invoice Fuel Surcharge"),
        (SAMPLE_ITEMS, None, "invoice Total Invoice Amount"),
    ],
)
def test_incomplete_invoice(items, total, field):

    abstains(calculate(QUESTION, [invoice(items, total)]), field, "was not found")


def test_invoice_question_variants():

    for question in (
        "Does the sum of the invoice line items match the invoice total?",
        "Do the invoice charges add up to the total?",
    ):
        assert calculate(question, [invoice(SAMPLE_ITEMS, "1,550.00")]).answer.startswith("No.")


@pytest.mark.parametrize(
    "charge, shown",
    [
        ("Lumper Fee: 120.00", "120.00"),
        ("Lumper Fee: 120", "120"),
        ("Lumper Fee: USD 120.00", "USD 120.00"),
        ("Lumper 120.00", "120.00"),
    ],
)
def test_unknown_charge_without_a_dollar_sign(charge, shown):

    # Only "$" amounts were found, so this was answered "No. ... a
    # difference of $120.00" although the charges do add up
    abstains(
        calculate(QUESTION, [invoice(SAMPLE_ITEMS, "1,550.00", extra=charge)]),
        f"not known line items: {shown} (INVOICE_TEST.pdf page 1)",
    )


def test_unknown_charge_without_a_dollar_sign_in_other_chunks_and_invoices():

    second_chunk = record(
        "invoice", "Lumper Fee: 120.00 Signature: A. Clerk", "INVOICE_TEST.pdf", page=2
    )

    abstains(
        calculate(QUESTION, [invoice(SAMPLE_ITEMS, "1,550.00"), second_chunk]),
        "not known line items: 120.00 (INVOICE_TEST.pdf page 2)",
    )

    supplement = record(
        "invoice",
        "SUPPLEMENTAL INVOICE Invoice Number: INV-SHJ-2026-901-S Layover: 150",
        "INVOICE_SUPPLEMENT.pdf",
    )

    abstains(
        calculate(QUESTION, [invoice(SAMPLE_ITEMS, "1,430.00"), supplement]),
        "not known line items: 150 (INVOICE_SUPPLEMENT.pdf page 1)",
    )


@pytest.mark.parametrize(
    "summary",
    [
        "Amount Due: $1,430.00",
        "Balance Due: 1,430.00",
        "Total Amount Due: $1,430.00",
        "Subtotal: $1,430.00 Amount Due: $1,430.00",
    ],
)
def test_summary_amount_repeating_the_total_is_not_a_charge(summary):

    # Was reported as an unknown charge, so the check abstained
    result = calculate(QUESTION, [invoice(SAMPLE_ITEMS, "1,430.00", extra=summary)])

    assert result.answer == (
        "Yes. The invoice line items (Base Freight $1,200.00 + Fuel "
        "Surcharge $180.00 + Detention Charge $0.00 + Other Accessorial "
        "Charges $50.00) add up to $1,430.00, which equals the Total "
        "Invoice Amount of $1,430.00."
    )
    assert [text for _, text in cited(result)][-1] == "Total Invoice Amount: $1,430.00"


def test_subtotal_and_amount_due_are_never_added():

    result = calculate(
        QUESTION,
        [invoice(SAMPLE_ITEMS, "1,550.00",
                 extra="Subtotal: $1,430.00 Amount Due: $1,550.00")],
    )

    assert result.answer.endswith(
        "add up to $1,430.00, but the Total Invoice Amount is $1,550.00, a "
        "difference of $120.00."
    )


def test_summary_amount_that_is_neither_total_nor_sum():

    abstains(
        calculate(QUESTION, [invoice(SAMPLE_ITEMS, "1,430.00",
                                     extra="Amount Due: $1,000.00")]),
        "the invoice's \"Amount Due: $1,000.00\" (INVOICE_TEST.pdf page 1) is "
        "neither the Total Invoice Amount ($1,430.00) nor the sum of the "
        "line items ($1,430.00)",
    )


def test_numbers_that_are_not_charges():

    result = calculate(
        QUESTION,
        [invoice(SAMPLE_ITEMS, "1,430.00",
                 extra="Total Weight: 12,500 lbs Pieces: 48 Fuel Rate: 15.00% "
                       "PRO Number: 77812")],
    )

    assert result.status == ANSWERED
    assert result.answer.startswith("Yes.")
