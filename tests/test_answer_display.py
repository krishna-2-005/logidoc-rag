"""
LogiDoc-RAG answer display tests.

The display helpers (src/display_text.py) and the Logistics Assistant
tab of the real dashboard (app.py), driven with Streamlit's AppTest
against the temporary two-shipment index of test_rag_scoping (offline
embeddings, fake language model). The production data/raw folder and
chroma_db index are never used.

Checks that answers with several dollar amounts keep every "$" (an
unescaped pair would be rendered as LaTeX), that calculation evidence
shows every field it cites, and that the answer method is shown.

Run from the project root:
    python -m pytest tests/test_answer_display.py
"""

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from streamlit.testing.v1 import AppTest

from display_text import cited_fields, escape_markdown_dollars
from ingestion_manager import ingest_files
from isolation import sample_workspace
from fake_embeddings import lexical_embedding
from test_ingestion_manager import make_pdf

# Fixtures: both sample shipments in a temporary index, fake model
from test_rag_scoping import A, B, TEMP_PREFIX, llm, two_shipments  # noqa: F401


APP_FILE = PROJECT_ROOT / "app.py"

DETERMINISTIC = (
    "Method: deterministic (read or calculated from document fields, "
    "without the language model)"
)

LANGUAGE_MODEL = "Method: language model (generated from the retrieved passages)"

R07 = (
    "No. The invoice line items (Base Freight $1,200.00 + Fuel Surcharge "
    "$180.00 + Detention Charge $0.00 + Other Accessorial Charges $50.00) "
    "add up to $1,430.00, but the Total Invoice Amount is $1,550.00, a "
    "difference of $120.00."
)

R08 = (
    "Yes. The invoice line items (Base Freight $2,400.00 + Fuel Surcharge "
    "$360.00 + Detention Charge $0.00 + Other Accessorial Charges $115.50) "
    "add up to $2,875.50, which equals the Total Invoice Amount of "
    "$2,875.50."
)


# ============================================================
# DISPLAY HELPERS
# ============================================================

def test_every_dollar_sign_is_escaped_once():

    assert escape_markdown_dollars("$1,200.00 + $180.00") == r"\$1,200.00 + \$180.00"

    # Already escaped: unchanged, so escaping twice changes nothing
    assert escape_markdown_dollars(r"\$5") == r"\$5"
    assert escape_markdown_dollars(escape_markdown_dollars(R07)) == escape_markdown_dollars(R07)

    # Text without "$" is unchanged
    assert escape_markdown_dollars("45 pieces were delivered.") == "45 pieces were delivered."
    assert escape_markdown_dollars(None) == ""


def test_cited_field_lists_are_recognised():

    assert cited_fields(
        "Base Freight: $1,200.00 | Total Invoice Amount: $1,550.00"
    ) == ["Base Freight: $1,200.00", "Total Invoice Amount: $1,550.00"]

    # A single field or a passage of document text is not a field list
    assert cited_fields("Number of Pieces Delivered: 45") is None
    assert cited_fields(
        "PROOF OF DELIVERY Shipment Number: SHJ-2026-001 Carrier: "
        "SwiftLine Logistics Delivery Date: October 5, 2026"
    ) is None
    assert cited_fields("Carrier: SwiftLine | and some text without a field") is None


# ============================================================
# THE LOGISTICS ASSISTANT TAB
# ============================================================

def ask_in_app(question, shipment):
    """
    Open the dashboard, select the shipment, ask the question and
    return what the Logistics Assistant tab shows.
    """

    at = AppTest.from_file(str(APP_FILE), default_timeout=240)
    at.run()

    if shipment != "All Shipments":
        at.sidebar.selectbox[0].select(shipment).run()

    at.text_input(key="logistics_question").input(question)
    [button for button in at.button if button.label == "Ask Question"][0].click().run()

    assert not at.exception, [str(item.value) for item in at.exception]

    tab = at.tabs[0]
    markdown = [str(item.value) for item in tab.markdown]

    return {
        "success": [str(item.value) for item in tab.success],
        "warning": [str(item.value) for item in tab.warning],
        "captions": [str(item.value) for item in tab.caption],
        # The bold EVIDENCE text of each card
        "evidence": [
            text for text in markdown
            if text.startswith("**") and not text.startswith("**Evidence ")
        ],
        "markdown": markdown,
    }


def shown(markdown_text):
    """
    What Markdown shows for escaped text: "\\$" is a literal "$".
    """

    return markdown_text.replace("\\$", "$")


def assert_dollars_escaped(text):

    # Every "$" escaped exactly once: none bare, none doubly escaped
    assert not re.search(r"(?<!\\)\$", text), text
    assert "\\\\$" not in text, text


@pytest.mark.parametrize(
    "question, shipment, expected, evidence",
    [
        (
            "Do the invoice line items add up to the total invoice amount?", A, R07,
            "**Base Freight: $1,200.00**  \n**Fuel Surcharge: $180.00**  \n"
            "**Detention Charge: $0.00**  \n**Other Accessorial Charges: $50.00**  \n"
            "**Total Invoice Amount: $1,550.00**",
        ),
        (
            "Do the invoice line items add up to the total invoice amount?", B, R08,
            "**Base Freight: $2,400.00**  \n**Fuel Surcharge: $360.00**  \n"
            "**Detention Charge: $0.00**  \n**Other Accessorial Charges: $115.50**  \n"
            "**Total Invoice Amount: $2,875.50**",
        ),
    ],
)
def test_invoice_sum_keeps_every_dollar_sign_and_line_item(llm, question, shipment, expected, evidence):

    # Was shown as "Base Freight 1,200.00 + ... add up to 1,430.00":
    # the dollar signs were read as LaTeX, and the evidence card
    # showed only the Total Invoice Amount
    page = ask_in_app(question, shipment)

    assert len(page["success"]) == 1
    assert_dollars_escaped(page["success"][0])
    assert shown(page["success"][0]) == expected

    assert len(page["evidence"]) == 1
    assert_dollars_escaped(page["evidence"][0])
    assert shown(page["evidence"][0]) == evidence

    assert DETERMINISTIC in page["captions"]
    assert llm.prompts == []


def test_quantity_and_timing_evidence_shows_counts_and_dates(llm):

    # Was shown with the piece counts only
    page = ask_in_app("Were all pieces delivered on time?", A)

    assert shown(page["success"][0]).startswith("No. The POD records 45 pieces delivered")
    assert page["evidence"] == [
        "**Number of Pieces: 48**  \n**Expected Delivery Date: October 3, 2026**",
        "**Number of Pieces Delivered: 45**  \n**Delivery Date: October 5, 2026**",
    ]
    assert {"BOL_SHJ-2026-001.pdf", "POD_SHJ-2026-001.pdf"} <= set(page["markdown"])
    assert DETERMINISTIC in page["captions"]


def test_single_field_answers_display_as_before(llm):

    page = ask_in_app("How many pieces were delivered?", B)

    assert page["success"] == ["60 pieces were delivered."]
    assert page["evidence"] == ["**Number of Pieces Delivered: 60**"]
    assert "Scope: Shipment SHJ-2026-002 (selected)" in page["captions"]
    assert DETERMINISTIC in page["captions"]

    # Shipment isolation in the display: no SHJ-2026-001 source
    assert not [text for text in page["markdown"] if "SHJ-2026-001" in text]


def test_model_answer_with_several_amounts(llm):

    reply = "The base freight was $1,200.00 and the fuel surcharge was $180.00."
    llm.reply = reply

    page = ask_in_app("Which charges were billed for freight and fuel?", A)

    assert llm.prompts
    assert len(page["success"]) == 1
    assert_dollars_escaped(page["success"][0])
    assert shown(page["success"][0]) == reply
    assert LANGUAGE_MODEL in page["captions"]


def test_abstention_with_several_amounts_is_a_warning(llm):

    with sample_workspace(TEMP_PREFIX + "display_", copy_samples=False) as (raw_dir, chroma_dir):

        invoice = make_pdf(
            raw_dir.parent / "INVOICE_SHJ-2026-304.pdf",
            "FREIGHT INVOICE",
            ["Invoice Number: INV-SHJ-2026-304", "",
             "Shipment Number: SHJ-2026-304", "",
             "Base Freight:", "$1,000.00", "Fuel Surcharge:", "$100.00",
             "Detention Charge:", "$0.00", "Other Accessorial Charges:",
             "$25.00", "Total Invoice Amount:", "$1,125.00",
             "Amount Due:", "$1,000.00", "", "End of invoice"]
        )

        ingest_files([invoice], raw_dir, chroma_dir, embed_text=lexical_embedding)

        page = ask_in_app(
            "Do the invoice line items add up to the total invoice amount?",
            "SHJ-2026-304"
        )

    assert page["success"] == []
    assert len(page["warning"]) == 1
    assert_dollars_escaped(page["warning"][0])
    assert shown(page["warning"][0]) == (
        "I cannot verify the invoice total: the invoice's \"Amount Due: "
        "$1,000.00\" (INVOICE_SHJ-2026-304.pdf page 1) is neither the Total "
        "Invoice Amount ($1,125.00) nor the sum of the line items "
        "($1,125.00)."
    )
    assert page["evidence"] == []
    assert DETERMINISTIC in page["captions"]
    assert llm.prompts == []


def test_clarification_has_no_method(llm):

    page = ask_in_app("How many pieces were missing?", "All Shipments")

    assert page["success"] == []
    assert "different answers" in page["warning"][0]
    assert not [caption for caption in page["captions"] if caption.startswith("Method:")]
