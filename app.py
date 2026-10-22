import sys
import re
from pathlib import Path

import pandas as pd
import streamlit as st


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# PROJECT IMPORTS
# ============================================================

from config import CHROMA_DIR, RAW_DATA_DIR
from rag import ask_question, call_llm, get_all_documents
from reconciliation import reconcile_shipment
from intelligence_service import analyze_shipment
from ingestion_manager import (
    STATUS_ADDED,
    STATUS_CHANGED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_INCONSISTENT,
)
from consistency_report import check_destination
from display_text import cited_fields, escape_markdown_dollars, money_text
from audit_export import NOT_AVAILABLE, ExportError, audit_csv, export_filename
from audit_pdf_export import audit_pdf, pdf_filename
import config
import audit_workflow_ui
import auth_ui
import authz
import exception_ui
import ingestion_service
import ingestion_ui
import reconciliation
import trends_ui
from audit_history import HistoryStore
from exception_workflow import ExceptionWorkflow
from chunker import describe_problems, read_chunks
from document_completeness import scan_folder
from audit_analytics import (
    DISCREPANCY,
    METRICS,
    REVIEW,
    build_exceptions,
    build_snapshot,
    exceptions_csv,
    filter_rows,
    kpis,
    register_csv,
    register_rows,
    source_fingerprint,
    with_reviews,
)
from review_store import (
    DISMISSED,
    STATUS_LABELS,
    STATUSES,
    Policy,
    ReviewError,
    ReviewStore,
)
from shipment_identity import belongs_to_shipment, identifier_key


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="LogiDoc-RAG",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# CUSTOM CSS
# ============================================================

st.markdown(
    """
    <style>

    .main-title {
        font-size: 42px;
        font-weight: 750;
        margin-bottom: 0px;
    }

    .subtitle {
        font-size: 16px;
        color: #8b8f98;
        margin-top: 4px;
        margin-bottom: 28px;
    }

    .section-title {
        font-size: 26px;
        font-weight: 700;
        margin-top: 10px;
        margin-bottom: 10px;
    }

    .intelligence-card {
        padding: 18px;
        border-radius: 12px;
        border: 1px solid #30333b;
        background: #15171d;
        margin-bottom: 12px;
    }

    .exception-text {
        font-size: 14px;
        line-height: 1.6;
    }

    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# SIGN-IN
# ============================================================

# The acting user. In required mode nothing below this line runs for a
# visitor who is not signed in (auth_ui.gate stops the script).
PRINCIPAL = auth_ui.gate()


# ============================================================
# SESSION STATE
# ============================================================

if "documents_processed" not in st.session_state:
    st.session_state.documents_processed = False

if "selected_shipment" not in st.session_state:
    st.session_state.selected_shipment = "All Shipments"

if "ingestion_result" not in st.session_state:
    st.session_state.ingestion_result = None


# ============================================================
# SAFE DOCUMENT DATA
# ============================================================

def get_indexed_data():

    try:

        documents, metadatas = get_all_documents()

        if documents is None:
            documents = []

        if metadatas is None:
            metadatas = []

        if not isinstance(documents, list):
            documents = list(documents)

        if not isinstance(metadatas, list):
            metadatas = list(metadatas)

        return documents, metadatas

    except Exception:

        return [], []


# ============================================================
# SHIPMENT INFORMATION
# ============================================================

def get_shipment_information():

    documents, metadatas = get_indexed_data()

    shipment_map = {}

    for metadata in metadatas:

        if not isinstance(metadata, dict):
            continue

        shipment_id = str(
            metadata.get("shipment_id", "")
        ).strip()

        if not shipment_id:
            continue

        document_type = str(
            metadata.get("document_type", "")
        ).upper()

        source = str(
            metadata.get("source", "")
        )

        if shipment_id not in shipment_map:

            shipment_map[shipment_id] = {
                "documents": set(),
                "types": set(),
            }

        if source:
            shipment_map[shipment_id]["documents"].add(source)

        if document_type:
            shipment_map[shipment_id]["types"].add(
                document_type
            )

    return shipment_map


# ============================================================
# TEXT HELPERS
# ============================================================

def normalize_text(text):

    if text is None:
        return ""

    text = str(text)

    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def clean_display_text(text, max_length=420):

    text = normalize_text(text)

    if not text:
        return ""

    if len(text) > max_length:
        return text[:max_length].rstrip() + "..."

    return text


# ============================================================
# PRECISE Q&A EVIDENCE
# ============================================================

def extract_precise_evidence(text, question=""):

    clean = normalize_text(text)

    if not clean:
        return ""

    # Evidence that already lists the exact fields an answer used
    # (a calculation's line items, counts and dates) is shown in
    # full, one field per line
    fields = cited_fields(clean)

    if fields:
        return "\n".join(fields)

    question_lower = question.lower()

    patterns = []

    # Quantity

    if (
        "how many pieces" in question_lower
        or "pieces delivered" in question_lower
        or "number of pieces" in question_lower
        or "quantity delivered" in question_lower
    ):

        patterns.extend(
            [
                (
                    r"Number\s+of\s+Pieces\s+Delivered\s*:\s*(\d+)",
                    "Number of Pieces Delivered"
                ),
                (
                    r"Pieces\s+Delivered\s*:\s*(\d+)",
                    "Pieces Delivered"
                ),
                (
                    r"Number\s+of\s+Pieces\s*:\s*(\d+)",
                    "Number of Pieces"
                ),
            ]
        )

    # Delivery dates

    if (
        "delivered on time" in question_lower
        or "delivery date" in question_lower
        or "actual delivery" in question_lower
        or "expected delivery" in question_lower
    ):

        patterns.extend(
            [
                (
                    r"Expected\s+Delivery\s+Date\s*:\s*"
                    r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    "Expected Delivery Date"
                ),
                (
                    r"Delivery\s+Date\s*:\s*"
                    r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    "Delivery Date"
                ),
                (
                    r"Actual\s+Delivery\s+Date\s*:\s*"
                    r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    "Actual Delivery Date"
                ),
            ]
        )

    # Invoice

    if (
        "invoice" in question_lower
        or "amount" in question_lower
        or "freight" in question_lower
        or "charge" in question_lower
        or "cost" in question_lower
    ):

        patterns.extend(
            [
                (
                    r"Total\s+Invoice\s+Amount\s*:\s*"
                    r"\$?\s*[\d,]+(?:\.\d{2})?",
                    None
                ),
                (
                    r"Invoice\s+Total\s*:\s*"
                    r"\$?\s*[\d,]+(?:\.\d{2})?",
                    None
                ),
                (
                    r"Base\s+Freight\s*:\s*"
                    r"\$?\s*[\d,]+(?:\.\d{2})?",
                    None
                ),
            ]
        )

    # Carrier

    if "carrier" in question_lower:

        patterns.append(
            (
                r"Carrier\s*:\s*[A-Za-z0-9 .&_-]+",
                None
            )
        )

    # Route

    if (
        "origin" in question_lower
        or "destination" in question_lower
        or "route" in question_lower
    ):

        patterns.extend(
            [
                (
                    r"Origin\s*:\s*[A-Za-z0-9 ,.-]+",
                    None
                ),
                (
                    r"Destination\s*:\s*[A-Za-z0-9 ,.-]+",
                    None
                ),
            ]
        )

    for pattern, label in patterns:

        match = re.search(
            pattern,
            clean,
            re.IGNORECASE
        )

        if not match:
            continue

        if label:

            return (
                f"{label}: "
                f"{match.group(1).strip()}"
            )

        return match.group(0).strip()

    return clean_display_text(clean)


# ============================================================
# SOURCE EVIDENCE UI
# ============================================================

def show_source_evidence(
    evidence,
    question=""
):

    if not evidence:

        st.info(
            "No exact source evidence was identified for this answer."
        )

        return

    if not isinstance(evidence, list):
        evidence = [evidence]

    st.subheader("Source Evidence")

    for index, item in enumerate(
        evidence,
        start=1
    ):

        if not isinstance(item, dict):
            continue

        source = item.get(
            "source",
            "Unknown"
        )

        page = item.get(
            "page",
            "Unknown"
        )

        document_type = item.get(
            "document_type",
            "Unknown"
        )

        shipment_id = item.get(
            "shipment_id",
            ""
        )

        raw_text = item.get(
            "text",
            ""
        )

        precise_text = extract_precise_evidence(
            raw_text,
            question
        )

        if not precise_text:
            precise_text = clean_display_text(
                raw_text
            )

        with st.container(border=True):

            st.markdown(
                f"**Evidence {index}**"
            )

            col1, col2, col3 = st.columns(3)

            with col1:

                st.caption("SOURCE")
                st.write(str(source))

            with col2:

                st.caption("PAGE")
                st.write(str(page))

            with col3:

                st.caption("TYPE")
                st.write(str(document_type))

            if shipment_id:

                st.caption("SHIPMENT")
                st.write(str(shipment_id))

            st.caption("EVIDENCE")

            st.markdown(
                "  \n".join(
                    f"**{escape_markdown_dollars(line)}**"
                    for line in precise_text.splitlines()
                    if line.strip()
                )
            )

            pdf_path = (
                RAW_DATA_DIR
                / Path(str(source)).name
            )

            if pdf_path.exists():

                with st.expander(
                    "📄 View Original PDF"
                ):

                    try:

                        st.pdf(
                            str(pdf_path),
                            height=650
                        )

                    except Exception as pdf_error:

                        st.error(
                            f"PDF viewer error: {pdf_error}"
                        )


# ============================================================
# RECONCILIATION EVIDENCE
# ============================================================

# The invoice fields the invoice check adds up
INVOICE_LINE_LABELS = [
    "Base Freight",
    "Fuel Surcharge",
    "Detention Charge",
    "Other Accessorial Charges",
]

# Evidence labels (build_reconciliation_evidence) behind each
# exception category
EVIDENCE_LABELS = {
    "Quantity": ["BOL Pieces", "Delivered Pieces"],
    "Delivery": ["Expected Delivery", "Actual Delivery"],
    "Invoice": INVOICE_LINE_LABELS + ["Invoice Total"],
}


def line_item_pattern(label):

    return (
        r"\s+".join(label.split())
        + r"\s*:\s*(\$?\s*[\d,]+(?:\.\d{2})?)"
    )


def display_value(value):

    return "N/A" if value is None else value


def build_reconciliation_evidence(result):

    shipment_id = str(
        result.get(
            "shipment_id",
            ""
        )
    ).upper()

    documents, metadatas = get_indexed_data()

    evidence = []

    patterns = [

        (
            "BOL",
            [
                (
                    r"Number\s+of\s+Pieces\s*:\s*(\d+)",
                    "Number of Pieces"
                ),
                (
                    r"Pieces\s*:\s*(\d+)",
                    "Pieces"
                ),
                (
                    r"Quantity\s*:\s*(\d+)",
                    "Quantity"
                ),
            ],
            "BOL Pieces",
        ),

        (
            "POD",
            [
                (
                    r"Number\s+of\s+Pieces\s+Delivered\s*:\s*(\d+)",
                    "Number of Pieces Delivered"
                ),
                (
                    r"Pieces\s+Delivered\s*:\s*(\d+)",
                    "Pieces Delivered"
                ),
                (
                    r"Quantity\s+Delivered\s*:\s*(\d+)",
                    "Quantity Delivered"
                ),
            ],
            "Delivered Pieces",
        ),

        (
            "BOL",
            [
                (
                    r"Expected\s+Delivery\s+Date\s*:\s*"
                    r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    "Expected Delivery Date"
                ),
                (
                    r"Expected\s+Delivery\s*:\s*"
                    r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    "Expected Delivery"
                ),
            ],
            "Expected Delivery",
        ),

        (
            "POD",
            [
                (
                    r"Delivery\s+Date\s*:\s*"
                    r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    "Delivery Date"
                ),
                (
                    r"Actual\s+Delivery\s+Date\s*:\s*"
                    r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    "Actual Delivery Date"
                ),
            ],
            "Actual Delivery",
        ),

        (
            "INVOICE",
            [
                (
                    r"Total\s+Invoice\s+Amount\s*:\s*"
                    r"(\$?\s*[\d,]+(?:\.\d{2})?)",
                    "Total Invoice Amount"
                ),
                (
                    r"Invoice\s+Total\s*:\s*"
                    r"(\$?\s*[\d,]+(?:\.\d{2})?)",
                    "Invoice Total"
                ),
            ],
            "Invoice Total",
        ),

        # The line items the invoice check adds up
        *[
            (
                "INVOICE",
                [
                    (
                        line_item_pattern(label),
                        label
                    ),
                ],
                label,
            )
            for label in INVOICE_LINE_LABELS
        ],
    ]

    seen = set()

    for document, metadata in zip(
        documents,
        metadatas
    ):

        if not isinstance(metadata, dict):
            continue

        # A chunk belongs to the shipment named in its metadata; the
        # text decides only when the metadata has no shipment ID
        if not belongs_to_shipment(
            metadata,
            document,
            shipment_id
        ):
            continue

        document_type = str(
            metadata.get(
                "document_type",
                ""
            )
        ).upper()

        clean_document = normalize_text(
            document
        )

        for (
            expected_type,
            type_patterns,
            label
        ) in patterns:

            if document_type != expected_type:
                continue

            match = None
            matched_label = None

            for pattern, field_label in type_patterns:

                match = re.search(
                    pattern,
                    clean_document,
                    re.IGNORECASE
                )

                if match:

                    matched_label = field_label
                    break

            if not match:
                continue

            source = metadata.get(
                "source",
                "Unknown"
            )

            page = metadata.get(
                "page",
                "Unknown"
            )

            key = (
                source,
                page,
                label
            )

            if key in seen:
                continue

            value = match.group(
                match.lastindex
            ).strip()

            evidence_text = (
                f"{matched_label}: {value}"
            )

            evidence.append(
                {
                    "label": label,
                    "source": source,
                    "page": page,
                    "document_type": metadata.get(
                        "document_type",
                        "Unknown"
                    ),
                    "shipment_id": metadata.get(
                        "shipment_id"
                    ),
                    "text": evidence_text,
                }
            )

            seen.add(key)

    return evidence


# ============================================================
# RECONCILIATION EVIDENCE UI
# ============================================================

def show_reconciliation_evidence(
    evidence,
    key_prefix=None
):

    if not evidence:

        st.info(
            "No source evidence was found for the reconciliation values."
        )

        return

    st.subheader("Source Evidence")

    grouped = {}

    for item in evidence:

        source = item.get(
            "source",
            "Unknown"
        )

        if source not in grouped:
            grouped[source] = []

        grouped[source].append(item)

    for source, items in grouped.items():

        first = items[0]

        with st.container(border=True):

            st.markdown(
                f"**📄 {source}**"
            )

            st.caption(
                f"Document Type: "
                f"{first.get('document_type', 'Unknown')}"
                f"  •  Shipment: "
                f"{first.get('shipment_id', 'Unknown')}"
            )

            for item in items:

                st.markdown(
                    f"**{item.get('label', 'Evidence')}:** "
                    f"{item.get('text', '')}"
                )

                st.caption(
                    f"Page {item.get('page', 'Unknown')}"
                )

            pdf_path = (
                RAW_DATA_DIR
                / Path(str(source)).name
            )

            if pdf_path.exists():

                with st.expander(
                    "📄 View Original PDF"
                ):

                    try:

                        # A unique key lets the same PDF appear
                        # more than once on the page
                        st.pdf(
                            str(pdf_path),
                            height=650,
                            key=(
                                f"{key_prefix}_{source}"
                                if key_prefix
                                else None
                            )
                        )

                    except Exception as pdf_error:

                        st.error(
                            f"PDF viewer error: {pdf_error}"
                        )


# ============================================================
# EXCEPTION DETAILS UI
# ============================================================

def show_exception_details(
    intelligence
):

    st.markdown("#### Exception Details")

    exceptions = intelligence.get(
        "exceptions",
        []
    )

    if not exceptions:

        st.success(
            "No exceptions detected for this shipment."
        )

        return

    quantity = intelligence.get(
        "quantity",
        {}
    )

    delivery = intelligence.get(
        "delivery",
        {}
    )

    invoice = intelligence.get(
        "invoice",
        {}
    )

    # Per exception category: card title, the evidence labels
    # produced by build_reconciliation_evidence(), and the
    # calculation values from the intelligence result
    details = {

        "Quantity": {
            "title": (
                "Quantity "
                f"{str(quantity.get('status', '')).title()}"
            ),
            "labels": [
                "BOL Pieces",
                "Delivered Pieces",
            ],
            "calculation": [
                ("Expected", quantity.get("bol_pieces")),
                ("Delivered", quantity.get("delivered_pieces")),
                ("Difference", quantity.get("difference")),
            ],
        },

        "Delivery": {
            "title": "Delivery Delay",
            "labels": [
                "Expected Delivery",
                "Actual Delivery",
            ],
            "calculation": [
                ("Expected Date", delivery.get("expected")),
                ("Actual Date", delivery.get("actual")),
                (
                    "Delay",
                    f"{delivery.get('delay_days', 0)} day(s)"
                ),
            ],
        },

        "Invoice": {
            "title": "Invoice Total Mismatch",
            "labels": EVIDENCE_LABELS["Invoice"],
            "calculation": [
                (
                    "Line Item Sum",
                    money_text(invoice.get("line_item_sum"))
                ),
                (
                    "Invoice Total",
                    money_text(invoice.get("total"))
                ),
                (
                    "Difference",
                    money_text(invoice.get("difference"))
                ),
            ],
        },
    }

    evidence = build_reconciliation_evidence(
        intelligence
    )

    for index, exception in enumerate(
        exceptions
    ):

        category = exception.get(
            "category",
            "Other"
        )

        severity = str(
            exception.get(
                "severity",
                "UNKNOWN"
            )
        ).upper()

        detail = details.get(
            category,
            {}
        )

        # Data that could not be verified shows the evidence it was
        # read from
        if category == "Data Review":

            detail = {
                "title": (
                    f"{exception.get('reviewed_category', 'Data')} "
                    "Needs Review"
                ),
                "labels": EVIDENCE_LABELS.get(
                    exception.get("reviewed_category"),
                    []
                ),
            }

        icon = (
            "🔴"
            if severity == "HIGH"
            else "🟠"
        )

        with st.container(border=True):

            st.markdown(
                f"**{icon} {detail.get('title', category)}**"
                f"  •  Severity: {severity}"
            )

            st.write(
                exception.get(
                    "message",
                    ""
                )
            )

            calculation = detail.get(
                "calculation",
                []
            )

            if calculation:

                columns = st.columns(
                    len(calculation)
                )

                for column, (label, value) in zip(
                    columns,
                    calculation
                ):

                    with column:

                        st.caption(label.upper())

                        st.write(
                            str(value)
                            if value is not None
                            else "N/A"
                        )

            items = [
                item
                for item in evidence
                if item.get("label")
                in detail.get("labels", [])
            ]

            if items:

                show_reconciliation_evidence(
                    items,
                    key_prefix=f"exception_{index}"
                )


# ============================================================
# SHIPMENT INTELLIGENCE UI
# ============================================================

def show_shipment_intelligence(shipment_id=None):

    st.subheader("Shipment Intelligence")

    try:

        intelligence = analyze_shipment(
            shipment_id
        )

    except Exception as error:

        st.error(
            f"Shipment intelligence failed: {error}"
        )

        return None

    if not isinstance(intelligence, dict):

        st.error(
            "Shipment intelligence returned an unexpected result."
        )

        return None

    if not intelligence.get(
        "success",
        False
    ):

        st.error(
            "Shipment intelligence failed: "
            f"{intelligence.get('error', 'Unknown error')}"
        )

        return None

    # --------------------------------------------------------
    # analyze_shipment() returns nested sections:
    #   documents -> found / expected / completeness
    #   quantity  -> status / difference
    #   delivery  -> status / delay_days
    # --------------------------------------------------------

    shipment_status = intelligence.get(
        "shipment_status",
        "UNKNOWN"
    )

    exceptions = intelligence.get(
        "exceptions",
        []
    )

    documents_info = intelligence.get(
        "documents",
        {}
    )

    documents = (
        f"{documents_info.get('found', 0)}/"
        f"{documents_info.get('expected', 0)}"
    )

    completeness = documents_info.get(
        "completeness",
        0
    )

    quantity = intelligence.get(
        "quantity",
        {}
    )

    quantity_status = quantity.get(
        "status",
        "UNKNOWN"
    )

    quantity_difference = display_value(
        quantity.get(
            "difference",
            0
        )
    )

    invoice_check = (
        intelligence.get("invoice") or {}
    ).get(
        "status",
        "UNKNOWN"
    )

    delivery = intelligence.get(
        "delivery",
        {}
    )

    delivery_status = delivery.get(
        "status",
        "UNKNOWN"
    )

    delay_days = delivery.get(
        "delay_days",
        0
    )

    invoice_total = intelligence.get(
        "invoice_total"
    )

    if invoice_total is None:

        invoice_total = "N/A"

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    if str(shipment_status).upper() == "EXCEPTION":

        st.error(
            f"⚠️ Shipment Status: {shipment_status}"
        )

    elif str(shipment_status).upper() == "NEEDS REVIEW":

        st.warning(
            f"⚠️ Shipment Status: {shipment_status}. Some values "
            "could not be verified; see the details below."
        )

    else:

        st.success(
            f"Shipment Status: {shipment_status}"
        )

    # --------------------------------------------------------
    # KPI CARDS
    # --------------------------------------------------------

    col1, col2, col3, col4 = st.columns(4)

    with col1:

        st.metric(
            "Exceptions",
            len(exceptions)
        )

    with col2:

        st.metric(
            "Documents",
            documents
        )

    with col3:

        st.metric(
            "Completeness",
            f"{completeness}%"
        )

    with col4:

        st.metric(
            "Invoice Total",
            (
                f"${invoice_total:,.2f}"
                if isinstance(
                    invoice_total,
                    (int, float)
                )
                else str(invoice_total)
            )
        )

    # --------------------------------------------------------
    # OPERATIONAL STATUS
    # --------------------------------------------------------

    st.markdown("#### Operational Status")

    col1, col2, col3, col4 = st.columns(4)

    with col4:

        st.metric(
            "Invoice Check",
            invoice_check
        )

    with col1:

        st.metric(
            "Quantity Status",
            quantity_status
        )

        if str(quantity_status).upper() == "SHORTAGE":

            st.warning(
                f"Shortage detected: "
                f"{quantity_difference} pieces"
            )

    with col2:

        st.metric(
            "Delivery Status",
            delivery_status
        )

        if str(delivery_status).upper() == "DELAYED":

            st.warning(
                f"Delivery delayed by "
                f"{delay_days} day(s)"
            )

    with col3:

        st.metric(
            "Quantity Difference",
            quantity_difference
        )

    show_exception_details(
        intelligence
    )

    return intelligence


# ============================================================
# ALL SHIPMENTS INTELLIGENCE UI
# ============================================================

def show_all_shipments_intelligence(shipment_ids):

    st.subheader("Shipment Intelligence")

    if not shipment_ids:

        st.info(
            "No shipments are indexed yet."
        )

        return []

    # One analysis per shipment, so documents from
    # different shipments are never combined
    rows = []

    for shipment_id in shipment_ids:

        try:

            intelligence = analyze_shipment(
                shipment_id
            )

        except Exception as error:

            intelligence = {
                "success": False,
                "error": str(error),
            }

        if not intelligence.get(
            "success",
            False
        ):

            rows.append(
                {
                    "Shipment": shipment_id,
                    "Status": "ERROR",
                    "Exceptions": None,
                    "Documents": "",
                    "Completeness": "",
                    "Quantity": "",
                    "Delivery": "",
                    "Invoice Total": str(
                        intelligence.get(
                            "error",
                            "Unknown error"
                        )
                    ),
                }
            )

            continue

        documents_info = intelligence.get(
            "documents",
            {}
        )

        invoice_total = intelligence.get(
            "invoice_total"
        )

        rows.append(
            {
                "Shipment": shipment_id,
                "Status": intelligence.get(
                    "shipment_status",
                    "UNKNOWN"
                ),
                "Exceptions": intelligence.get(
                    "exception_count",
                    0
                ),
                "Documents": (
                    f"{documents_info.get('found', 0)}/"
                    f"{documents_info.get('expected', 0)}"
                ),
                "Completeness": (
                    f"{documents_info.get('completeness', 0)}%"
                ),
                "Quantity": intelligence.get(
                    "quantity",
                    {}
                ).get(
                    "status",
                    "UNKNOWN"
                ),
                "Delivery": intelligence.get(
                    "delivery",
                    {}
                ).get(
                    "status",
                    "UNKNOWN"
                ),
                "Invoice Total": (
                    f"${invoice_total:,.2f}"
                    if isinstance(
                        invoice_total,
                        (int, float)
                    )
                    else "N/A"
                ),
            }
        )

    col1, col2, col3 = st.columns(3)

    with col1:

        st.metric(
            "Shipments Analyzed",
            len(rows)
        )

    with col2:

        st.metric(
            "Shipments With Exceptions",
            sum(
                1
                for row in rows
                if row["Status"] == "EXCEPTION"
            )
        )

    with col3:

        st.metric(
            "Total Exceptions",
            sum(
                row["Exceptions"] or 0
                for row in rows
            )
        )

    st.dataframe(
        rows,
        hide_index=True,
    )

    st.caption(
        "Select a shipment in the sidebar to see its "
        "exception details and source evidence."
    )

    return rows


# ============================================================
# UPLOAD RESULT UI
# ============================================================

INGESTION_STATUS_DISPLAY = {
    STATUS_ADDED: ("✅", "Added"),
    STATUS_DUPLICATE: ("⏭️", "Duplicate, skipped"),
    STATUS_CHANGED: ("⚠️", "Changed, not applied"),
    STATUS_INCONSISTENT: ("⚠️", "Inconsistent, needs attention"),
    STATUS_FAILED: ("❌", "Failed"),
}


def show_ingestion_result(result):

    if not result:
        return

    st.markdown("**Last upload**")

    st.caption(
        f"Added {result['files_added']} · "
        f"Duplicates {result['duplicates_skipped']} · "
        f"Changed {result['changed_detected']} · "
        f"Inconsistent {result['inconsistent_detected']} · "
        f"Failed {result['files_failed']}"
    )

    # Success only for files that were indexed and stored
    if result["files_added"]:

        st.success(
            f"{result['files_added']} file(s) indexed and stored."
        )

    if result["files_failed"]:

        st.error(
            f"{result['files_failed']} file(s) failed. Nothing was "
            "stored or indexed for them."
        )

    attention = (
        result["changed_detected"]
        + result["inconsistent_detected"]
    )

    if attention:

        st.warning(
            f"{attention} file(s) need attention. Existing "
            "documents were not changed."
        )

    for outcome in result["files"]:

        icon, label = INGESTION_STATUS_DISPLAY.get(
            outcome["status"],
            ("•", outcome["status"])
        )

        st.markdown(
            f"{icon} **{outcome['filename']}**: {label}"
        )

        detail = outcome["error"] or outcome["message"]

        if detail:
            st.caption(detail)

        if outcome["action"]:
            st.caption(f"Next step: {outcome['action']}")

    for error in result["rollback_errors"]:
        st.error(f"Rollback problem: {error}")

    for warning in result["warnings"]:
        st.warning(warning)


# ============================================================
# DIAGNOSTICS UI
# ============================================================

def show_consistency_report(report):

    summary = report["summary"]

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("Raw PDFs", summary["raw_pdfs"])

    with col2:
        st.metric("Verified Documents", summary["verified_documents"])

    with col3:
        st.metric("Unverified Documents", summary["unverified_documents"])

    with col4:
        st.metric("Vectors", summary["vectors"])

    st.caption(
        "Unverified documents were indexed by a full rebuild, which "
        "records no content hash; they are linked to their PDF by "
        "filename only."
    )

    if summary["healthy"]:

        st.success(
            "No problems found."
            + (
                f" {summary['info']} informational note(s) below."
                if summary["info"]
                else ""
            )
        )

    elif summary["errors"]:

        st.error(
            f"{summary['errors']} error(s) and {summary['warnings']} "
            "warning(s) found. Nothing was changed; review each "
            "issue below."
        )

    else:

        st.warning(
            f"{summary['warnings']} warning(s) found. Nothing was "
            "changed; review each issue below."
        )

    if not report["issues"]:
        return

    st.dataframe(
        [
            {
                "Severity": issue["severity"].upper(),
                "Certainty": issue["certainty"],
                "Issue": issue["type"].replace("_", " "),
                "File": issue["file"] or "",
                "SHA-256": (issue["sha256"] or "")[:12],
                "Vectors": len(issue["vector_ids"]),
                "Explanation": issue["explanation"],
            }
            for issue in report["issues"]
        ],
        hide_index=True,
    )

    with st.expander(
        "Issue details (vector IDs, hashes, journal operations)"
    ):
        st.json(report["issues"], expanded=False)


# ============================================================
# HEADER
# ============================================================

st.markdown(
    '<div class="main-title">📦 LogiDoc-RAG</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="subtitle">'
    'AI-powered logistics document intelligence and shipment reconciliation'
    '</div>',
    unsafe_allow_html=True,
)


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header("Document Processing")

    uploaded_files = st.file_uploader(
        "Upload logistics PDFs",
        type=["pdf"],
        accept_multiple_files=True,
        help="Upload BOL, POD and Invoice documents.",
    )

    can_ingest = PRINCIPAL.can(authz.INGESTION_WRITE)

    process_button = st.button(
        "Process Documents",
        type="primary",
        use_container_width=True,
        disabled=not can_ingest,
        help=None if can_ingest else "Only administrators can add documents.",
    )

    if process_button:

        if not uploaded_files:

            st.warning(
                "Please upload at least one PDF."
            )

        else:

            try:

                # Validated, incremental and non-destructive: existing
                # PDFs and vectors are kept, every file gets a status
                # in the ingestion register, transient failures are
                # retried. The service makes no st.* calls, so
                # Streamlit cannot stop it half-way; the result is
                # saved before the next st.* call.
                with st.spinner(
                    "Checking, indexing and storing documents..."
                ):

                    locations = config.ingestion_locations(RAW_DATA_DIR)

                    summary = ingestion_service.process_files(
                        PRINCIPAL,
                        [
                            ingestion_service.Upload(
                                uploaded.name,
                                bytes(uploaded.getbuffer())
                            )
                            for uploaded in uploaded_files
                        ],
                        RAW_DATA_DIR,
                        CHROMA_DIR,
                        ingestion_ui.open_register(RAW_DATA_DIR),
                        locations["quarantine"],
                    )

                    st.session_state.ingestion_result = (
                        ingestion_ui.legacy_view(summary)
                    )

                if summary["corpus_changed"]:

                    st.session_state.documents_processed = True

                    # Everything cached from the documents is stale
                    st.cache_data.clear()

                    # Reload shipments and documents from the index
                    st.rerun()

            except Exception as error:

                st.session_state.ingestion_result = None

                st.error(
                    f"Processing stopped: {error}. Run the "
                    "consistency check in the Diagnostics tab to "
                    "review the document store."
                )

    show_ingestion_result(
        st.session_state.ingestion_result
    )


# ============================================================
# LOAD DASHBOARD DATA
# ============================================================

shipment_information = (
    get_shipment_information()
)

shipment_ids = sorted(
    shipment_information.keys()
)


# ============================================================
# SHIPMENT SELECTOR
# ============================================================

with st.sidebar:

    st.divider()

    st.header("Shipment")

    shipment_options = (
        ["All Shipments"]
        + shipment_ids
    )

    # A shipment chosen elsewhere (Audit Analytics) that is no longer
    # indexed falls back to All Shipments
    if st.session_state.get("shipment_selector") not in shipment_options:
        st.session_state.pop("shipment_selector", None)

    selected_shipment = st.selectbox(
        "Select shipment",
        shipment_options,
        key="shipment_selector",
    )

    st.session_state.selected_shipment = (
        selected_shipment
    )

    if selected_shipment != "All Shipments":

        info = shipment_information.get(
            selected_shipment,
            {}
        )

        documents_count = len(
            info.get(
                "documents",
                set()
            )
        )

        types = sorted(
            info.get(
                "types",
                set()
            )
        )

        st.caption(
            f"Documents: {documents_count}"
        )

        st.caption(
            "Types: "
            + (
                ", ".join(types)
                if types
                else "Unknown"
            )
        )


# ============================================================
# DASHBOARD COUNTS
# ============================================================

documents, metadatas = (
    get_indexed_data()
)

unique_sources = set()
unique_types = set()

for metadata in metadatas:

    if not isinstance(metadata, dict):
        continue

    source = metadata.get("source")
    document_type = metadata.get("document_type")

    if source:
        unique_sources.add(
            str(source)
        )

    if document_type:
        unique_types.add(
            str(document_type).upper()
        )


# ============================================================
# DASHBOARD
# ============================================================

st.subheader(
    "Shipment Intelligence Dashboard"
)

st.caption(
    "Monitor indexed logistics documents, shipments and reconciliation activity."
)

col1, col2, col3, col4 = st.columns(4)

with col1:

    st.metric(
        "Active Shipment",
        selected_shipment
    )

with col2:

    st.metric(
        "Shipments Indexed",
        len(shipment_ids)
    )

with col3:

    st.metric(
        "Documents",
        len(unique_sources)
    )

with col4:

    st.metric(
        "Document Types",
        len(unique_types)
    )

st.divider()


# ============================================================
# AUDIT ANALYTICS
# ============================================================

def ingestion_problems():
    """
    The files offered for ingestion that could not be ingested and have
    not been resolved (ingestion_service.audit_problems), as a hashable
    tuple of (file name, status, reason). The audit holds them against
    every shipment: a file that was not ingested might belong to it.
    """

    try:
        found = ingestion_service.open_problems(
            config.ingestion_locations(RAW_DATA_DIR)["register"]
        )
    except Exception:
        return ()

    return tuple(
        (item["filename"], item["status"], item["error"])
        for item in found
    )


def reconcile_with_ingestion_problems(shipment_id):
    """
    reconcile_shipment, holding the files that could not be ingested
    against the shipment like the unreadable files of the folder: it is
    never clean while one might belong to it. Without such files this is
    exactly reconcile_shipment(shipment_id).
    """

    offered = ingestion_problems()

    if not offered:
        return reconcile_shipment(shipment_id)

    chunks, raw_problems = reconciliation.load_document_chunks()

    return reconcile_shipment(
        shipment_id,
        chunks=chunks,
        unreadable_files=ingestion_service.merge_problems(
            raw_problems,
            [
                {"filename": name, "status": status, "error": error}
                for name, status, error in offered
            ],
        ),
    )


@st.cache_data(show_spinner=False, max_entries=4)
def load_audit_snapshot(raw_dir, fingerprint, offered_problems=()):
    """
    Every shipment reconciled from one reading of the raw PDFs.
    Cached per folder, fingerprint (file names, sizes and times) and the
    open ingestion errors, so it is recomputed as soon as a document
    changes or an ingestion problem appears or is resolved.
    """

    # A PDF that cannot be used does not stop every audit: it is
    # reported, and every shipment's result knows about it (it cannot
    # be clean while an unreadable file might belong to it). So are the
    # files that could not be ingested at all.
    chunks, problems = read_chunks(raw_dir)

    problems = ingestion_service.merge_problems(
        problems,
        [
            {"filename": name, "status": status, "error": error}
            for name, status, error in offered_problems
        ],
    )

    return build_snapshot(chunks, reconcile_shipment, unreadable_files=problems)


@st.cache_data(show_spinner=False, max_entries=4)
def load_document_scan(raw_dir, fingerprint):
    """
    Every PDF of the raw folder scanned for the completeness register,
    cached per folder and fingerprint like the audit snapshot.
    """

    return scan_folder(raw_dir)


def amount(value):

    return NOT_AVAILABLE if value is None else money_text(value)


def shown_value(value):

    return NOT_AVAILABLE if value is None else str(value)


def open_in_reconciliation(shipment_option):

    st.session_state["shipment_selector"] = shipment_option


def reset_analytics_filters():

    for key in (
        "an_search", "an_carriers", "an_statuses", "an_categories",
        "an_invoice", "an_delay", "an_review",
        "an_kinds", "an_severities", "an_review_statuses",
    ):
        st.session_state.pop(key, None)


def show_analytics_kpis(metrics):

    columns = st.columns(4)

    columns[0].metric("Identified shipments", metrics["identified"], help=METRICS["identified"])
    columns[1].metric("Reconciled", metrics["reconciled"], help=METRICS["reconciled"])
    columns[2].metric("With discrepancies", metrics["with_discrepancies"], help=METRICS["with_discrepancies"])
    columns[3].metric("Need manual review", metrics["needs_review"], help=METRICS["needs_review"])

    columns = st.columns(4)

    columns[0].metric("No discrepancies", metrics["no_discrepancies"], help=METRICS["no_discrepancies"])

    columns[1].metric(
        "Total invoice amount",
        amount(metrics["invoice_total"]),
        help=(
            f"{METRICS['invoice_total']} Covers {metrics['invoice_total_covered']} "
            f"of {metrics['reconciled']} reconciled shipment(s)."
        ),
    )

    columns[2].metric(
        "Invoice variance",
        amount(metrics["invoice_variance"]),
        help=(
            f"{METRICS['invoice_variance']} Net (signed): "
            f"{amount(metrics['invoice_variance_net'])}. Covers "
            f"{metrics['invoice_variance_covered']} of {metrics['reconciled']} "
            "reconciled shipment(s)."
        ),
    )

    columns[3].metric(
        "% reconciled with discrepancies",
        NOT_AVAILABLE if metrics["discrepancy_rate"] is None else f"{metrics['discrepancy_rate']}%",
        help=METRICS["discrepancy_rate"],
    )


def show_shipment_audit(row, result, shipment_options):
    """
    One shipment's reconciliation, from the analytics snapshot.
    """

    shipment_id = row["shipment_id"]

    if not row["reconciled"]:
        st.error(f"{shipment_id} could not be reconciled: {row['error']}")
        return

    status = row["final_status"]

    if status == "DISCREPANCIES DETECTED":
        st.error(f"{shipment_id}: {status}")
    elif status == "NEEDS REVIEW":
        st.warning(f"{shipment_id}: {status} (some values could not be verified)")
    else:
        st.success(f"{shipment_id}: {status}")

    quantity = result.get("quantity") or {}
    delivery = result.get("delivery") or {}
    invoice = result.get("invoice") or {}

    columns = st.columns(2)

    with columns[0]:

        st.markdown("**Quantity**")
        st.table(pd.DataFrame(
            [
                ["BOL expected", shown_value(quantity.get("bol"))],
                ["POD delivered", shown_value(quantity.get("pod"))],
                ["Difference (BOL - POD)", shown_value(quantity.get("difference"))],
                ["Status", shown_value(quantity.get("status"))],
            ],
            columns=["Field", "Value"],
        ))

    with columns[1]:

        st.markdown("**Delivery**")
        st.table(pd.DataFrame(
            [
                ["Expected", shown_value(delivery.get("expected"))],
                ["Actual", shown_value(delivery.get("actual"))],
                ["Days late (+) / early (-)", shown_value(delivery.get("days_late"))],
                ["Status", shown_value(delivery.get("status"))],
            ],
            columns=["Field", "Value"],
        ))

    st.markdown("**Invoice**")

    st.table(pd.DataFrame(
        [[label, amount(value)] for label, value in invoice.get("line_items") or []]
        + [
            ["Line-item sum", amount(invoice.get("line_item_sum"))],
            ["Stated invoice total", amount(invoice.get("total"))],
            ["Difference (total - line items)", amount(invoice.get("difference"))],
            ["Status", shown_value(invoice.get("status"))],
        ],
        columns=["Item", "Amount"],
    ))

    for item in result.get("discrepancies") or []:
        st.warning(
            f"**{item.get('category')}**: "
            + escape_markdown_dollars(str(item.get("description") or ""))
        )

    for item in result.get("review_items") or []:
        st.warning(
            f"**{item.get('category')}** needs review: "
            + escape_markdown_dollars(str(item.get("reason") or ""))
        )

    evidence = [
        {
            "Audit": category,
            "Source": item.get("source"),
            "Page": shown_value(item.get("page")),
            "Type": item.get("document_type"),
            "Shipment": item.get("shipment_id"),
            "Field text": item.get("text"),
        }
        for category in ("Quantity", "Delivery", "Invoice")
        for item in (result.get(category.lower()) or {}).get("evidence") or []
    ]

    st.markdown("**Evidence**")

    if evidence:
        st.dataframe(pd.DataFrame(evidence), hide_index=True, use_container_width=True)
    else:
        st.caption("No evidence was cited.")

    csv_column, pdf_column, navigation_column = st.columns(3)

    try:

        with csv_column:
            st.download_button(
                "Shipment audit (CSV)",
                data=audit_csv(result, shipment_id),
                file_name=export_filename(result),
                mime="text/csv",
                on_click="ignore",
                key="an_shipment_csv",
            )

        with pdf_column:
            st.download_button(
                "Shipment audit (PDF)",
                data=audit_pdf(result, shipment_id),
                file_name=pdf_filename(result),
                mime="application/pdf",
                on_click="ignore",
                key="an_shipment_pdf",
            )

    except ExportError as export_error:
        st.caption(f"Export unavailable: {export_error}")

    # The sidebar lists the shipments of the search index
    option = next(
        (
            option for option in shipment_options
            if option != "All Shipments"
            and identifier_key(option) == row["shipment_key"]
        ),
        None,
    )

    with navigation_column:

        if option:
            st.button(
                "Select for Reconciliation",
                on_click=open_in_reconciliation,
                args=(option,),
                key="an_open_reconciliation",
                help="Selects this shipment in the sidebar; then open the Shipment Reconciliation tab.",
            )
        else:
            st.caption("This shipment is not in the search index, so it cannot be selected in the sidebar.")


def review_policy():
    """
    The workflow rules set on the server (config.MIN_NOTE_LENGTH,
    config.REQUIRE_APPROVAL).
    """

    return Policy(
        min_note=config.MIN_NOTE_LENGTH,
        require_approval=config.REQUIRE_APPROVAL,
    )


def assignable_users():
    """
    Who an exception can be assigned to: the active accounts when users
    sign in, any name in local development mode (None).
    """

    if not PRINCIPAL.authenticated:
        return None

    return [
        user["username"]
        for user in auth_ui.open_store().users()
        if user["active"]
    ]


def show_exception_review(store, exceptions, prefix="an"):
    """
    Review state, history and the form to change it, for one exception.
    prefix keeps the widget keys apart when the widget is shown in
    more than one tab.

    A signed-in user always gets the full workflow (exception_ui): roles,
    required notes and evidence, assignment, approval, and their own
    account recorded on every change. The quick form below, which takes a
    free-typed reviewer name, exists only in local development mode.
    """

    if PRINCIPAL.authenticated:

        if store is None:
            st.info("The review database is unavailable; review changes cannot be saved.")
            return

        exception_ui.show_review(
            PRINCIPAL, ExceptionWorkflow(store, assignable_users), store,
            exceptions, prefix=prefix, assignable=assignable_users(),
        )

        return

    labels = {
        f"{item['shipment_id']} · {item['category']} ({item['kind']})": item
        for item in exceptions
    }

    choice = st.selectbox("Exception", list(labels), key=f"{prefix}_exception")
    exception = labels[choice]

    st.markdown(escape_markdown_dollars(exception["description"]))

    if exception["needs_recheck"]:
        st.warning(
            "Reopened: the audited values changed, or the exception came "
            "back, after it had been resolved or dismissed."
        )

    for reference in exception["evidence"]:
        st.caption(escape_markdown_dollars(reference))

    if store is None:
        st.info("The review database is unavailable; review changes cannot be saved.")
        return

    with st.form(f"{prefix}_review_form", clear_on_submit=True):

        status = st.selectbox(
            "Review state",
            STATUSES,
            index=STATUSES.index(exception["review_status"]),
            format_func=lambda value: STATUS_LABELS[value],
        )

        reviewer = st.text_input("Reviewer (optional)")
        note = st.text_area("Note (required to dismiss)")

        submitted = st.form_submit_button("Save review state")

    if submitted:

        try:
            store.set_status(exception, status, reviewer=reviewer, note=note)
        except ReviewError as error:
            st.error(str(error))
        else:
            st.session_state[f"{prefix}_review_saved"] = (
                f"{choice}: {STATUS_LABELS[status]}"
            )
            st.rerun()

    if st.session_state.get(f"{prefix}_review_saved"):
        st.success(f"Saved: {st.session_state.pop(f'{prefix}_review_saved')}")

    history = store.history(exception["exception_key"])

    if history:
        with st.expander(f"Review history ({len(history)})"):
            st.dataframe(
                pd.DataFrame(history)[
                    ["created_at", "from_status", "to_status", "reviewer", "note"]
                ],
                hide_index=True,
                use_container_width=True,
            )


def show_audit_analytics(shipment_options):

    fingerprint = source_fingerprint(RAW_DATA_DIR)

    if not fingerprint:
        st.info("No PDFs in the document folder yet. Upload documents in the sidebar.")
        return

    try:
        with st.spinner("Reconciling every shipment..."):
            snapshot = load_audit_snapshot(str(RAW_DATA_DIR), fingerprint, ingestion_problems())
    except Exception as error:
        st.error(f"Audit analytics could not be computed: {error}")
        return

    rows = register_rows(snapshot)

    if snapshot["unidentified_sources"]:
        st.warning(
            f"{len(snapshot['unidentified_sources'])} document(s) have no shipment "
            "ID and are not included in any shipment: "
            + ", ".join(snapshot["unidentified_sources"])
        )

    if snapshot["unreadable_files"]:
        st.warning(
            f"{len(snapshot['unreadable_files'])} file(s) could not be used and belong to "
            "no shipment: " + describe_problems(snapshot["unreadable_files"])
            + ". No shipment is shown as clean while they cannot be ruled out."
        )

    if not rows:
        st.info("No documents with a shipment ID were found.")
        return

    exceptions = build_exceptions(snapshot)

    try:
        store = ReviewStore(config.REVIEW_DB_PATH, review_policy())
        reviews = store.sync(exceptions)
    except Exception as error:
        store, reviews = None, {}
        st.warning(
            f"Review database unavailable ({error}). Exceptions are shown as "
            "open and review changes cannot be saved."
        )

    exceptions = with_reviews(exceptions, reviews)
    results = {item["shipment_key"]: item["result"] for item in snapshot["shipments"]}

    st.caption(
        "Source: the PDFs in the document folder, reconciled like the "
        "Shipment Reconciliation tab. Missing or unverifiable values are "
        "never counted as 0 or as clean."
    )

    show_analytics_kpis(kpis(rows))

    # --------------------------------------------------------
    # Charts
    # --------------------------------------------------------

    open_exceptions = [
        item for item in exceptions
        if item["review_status"] not in ("resolved", "dismissed")
    ]

    chart_status, chart_exceptions = st.columns(2)

    with chart_status:
        st.markdown("**Final audit status**")
        st.bar_chart(pd.Series([row["final_status"] for row in rows]).value_counts())

    with chart_exceptions:
        st.markdown("**Unresolved exceptions by category**")
        if open_exceptions:
            st.bar_chart(pd.Series([item["category"] for item in open_exceptions]).value_counts())
        else:
            st.caption("No unresolved exceptions.")

    # --------------------------------------------------------
    # Shipment audit register
    # --------------------------------------------------------

    st.subheader("Shipment audit register")

    filter_columns = st.columns(4)

    search = filter_columns[0].text_input("Shipment ID", key="an_search")
    carriers = filter_columns[1].multiselect(
        "Carrier",
        sorted({row["carrier"] or NOT_AVAILABLE for row in rows}),
        key="an_carriers",
    )
    statuses = filter_columns[2].multiselect(
        "Final status",
        sorted({row["final_status"] for row in rows}),
        key="an_statuses",
    )
    categories = filter_columns[3].multiselect(
        "Discrepancy category",
        sorted({category for row in rows for category in row["discrepancy_categories"]}),
        key="an_categories",
    )

    flag_columns = st.columns(4)

    invoice_mismatch = flag_columns[0].checkbox("Invoice mismatch", key="an_invoice")
    delivery_delayed = flag_columns[1].checkbox("Delivery delayed", key="an_delay")
    needs_review = flag_columns[2].checkbox("Needs review", key="an_review")
    flag_columns[3].button("Reset filters", on_click=reset_analytics_filters)

    filtered = filter_rows(
        rows, search, carriers, statuses, categories,
        invoice_mismatch, delivery_delayed, needs_review,
    )

    st.caption(
        f"{len(filtered)} of {len(rows)} shipment(s). A blank amount is NOT "
        "AVAILABLE, not 0."
    )

    if filtered:

        st.dataframe(
            pd.DataFrame([
                {
                    "Shipment": row["shipment_id"],
                    "Carrier": shown_value(row["carrier"]),
                    "Origin": shown_value(row["origin"]),
                    "Destination": shown_value(row["destination"]),
                    "Quantity": shown_value(row["quantity_status"]),
                    "Delivery": shown_value(row["delivery_status"]),
                    "Invoice": shown_value(row["invoice_status"]),
                    "Final status": row["final_status"],
                    "Invoice total": row["invoice_total"],
                    "Invoice difference": row["invoice_difference"],
                    "Discrepancies": row["discrepancy_count"],
                    "Review items": row["review_count"],
                }
                for row in filtered
            ]),
            hide_index=True,
            use_container_width=True,
            column_config={
                "Invoice total": st.column_config.NumberColumn(format="dollar"),
                "Invoice difference": st.column_config.NumberColumn(format="dollar"),
            },
        )

        st.download_button(
            "Download register (CSV)",
            data=register_csv(filtered),
            file_name="freight_audit_register.csv",
            mime="text/csv",
            on_click="ignore",
            key="an_register_csv",
        )

    else:
        st.info("No shipments match these filters.")

    # --------------------------------------------------------
    # Drill-down
    # --------------------------------------------------------

    st.subheader("Shipment details")

    if filtered:

        detail = st.selectbox(
            "Shipment",
            [row["shipment_id"] for row in filtered],
            key="an_detail",
        )

        row = next(item for item in filtered if item["shipment_id"] == detail)

        show_shipment_audit(row, results[row["shipment_key"]], shipment_options)

    # --------------------------------------------------------
    # Exception register (shipments of the filtered register)
    # --------------------------------------------------------

    st.subheader("Exception register")

    st.caption(
        "Detected discrepancies and values that need review, for the "
        "shipments listed above. A review state records what was done; "
        "it does not change the audit. Showing an exception does not "
        "resolve it."
    )

    keys = {row["shipment_key"] for row in filtered}
    scoped = [item for item in exceptions if item["shipment_key"] in keys]

    exception_filters = st.columns(3)

    kinds = exception_filters[0].multiselect("Type", [DISCREPANCY, REVIEW], key="an_kinds")
    severities = exception_filters[1].multiselect("Severity", ["HIGH", "MEDIUM"], key="an_severities")
    review_statuses = exception_filters[2].multiselect(
        "Review state", list(STATUSES),
        format_func=lambda value: STATUS_LABELS[value],
        key="an_review_statuses",
    )

    shown = [
        item for item in scoped
        if (not kinds or item["kind"] in kinds)
        and (not severities or item["severity"] in severities)
        and (not review_statuses or item["review_status"] in review_statuses)
    ]

    if not scoped:
        st.success("No exceptions for these shipments.")
        return

    st.caption(f"{len(shown)} of {len(scoped)} exception(s).")

    if shown:

        st.dataframe(
            pd.DataFrame([
                {
                    "Shipment": item["shipment_id"],
                    "Type": item["kind"],
                    "Category": item["category"],
                    "Severity": item["severity"],
                    "Description": item["description"],
                    "Difference": shown_value(item["difference"]),
                    "Unit": item["unit"] or "",
                    "Review state": STATUS_LABELS[item["review_status"]]
                    + (" (reopened)" if item["needs_recheck"] else ""),
                    "Reviewer": item["reviewer"] or "",
                    "Evidence": len(item["evidence"]),
                }
                for item in shown
            ]),
            hide_index=True,
            use_container_width=True,
        )

        st.download_button(
            "Download exceptions (CSV)",
            data=exceptions_csv(shown),
            file_name="freight_audit_exceptions.csv",
            mime="text/csv",
            on_click="ignore",
            key="an_exceptions_csv",
        )

        st.markdown("**Review an exception**")
        show_exception_review(store, shown)

    else:
        st.info("No exceptions match these filters.")


def show_audit_operations():
    """
    The Audit Operations tab: the business modules do the work (see
    src/audit_workflow_ui.py); this only loads their inputs.
    """

    fingerprint = source_fingerprint(RAW_DATA_DIR)

    if not fingerprint:
        st.info("No PDFs in the document folder yet. Upload documents in the sidebar.")
        return

    try:
        with st.spinner("Reading and reconciling every shipment..."):
            snapshot = load_audit_snapshot(str(RAW_DATA_DIR), fingerprint, ingestion_problems())
            files = load_document_scan(str(RAW_DATA_DIR), fingerprint)
    except Exception as error:
        st.error(f"Audit operations could not be loaded: {error}")
        return

    # The search index, when it can be read: which files chat can see
    _, metadatas = get_indexed_data()

    indexed_sources = {
        str(metadata.get("source"))
        for metadata in metadatas
        if isinstance(metadata, dict) and metadata.get("source")
    } or None

    review_store = history = review_error = history_error = None

    try:
        review_store = ReviewStore(config.REVIEW_DB_PATH, review_policy())
    except Exception as error:
        review_error = str(error)

    try:
        history = HistoryStore(config.HISTORY_DB_PATH)
    except Exception as error:
        history_error = str(error)

    audit_workflow_ui.render(
        snapshot, files, indexed_sources, review_store, history,
        show_exception_review, llm=call_llm,
        review_error=review_error, history_error=history_error,
        principal=PRINCIPAL,
    )


def show_exception_queue():
    """
    The Exception Queue tab: the exceptions of every shipment with their
    assignee, state and age, and the workflow actions the signed-in user
    may take (src/exception_ui.py).
    """

    fingerprint = source_fingerprint(RAW_DATA_DIR)

    if not fingerprint:
        st.info("No PDFs in the document folder yet. Upload documents in the sidebar.")
        return

    try:
        with st.spinner("Reconciling every shipment..."):
            snapshot = load_audit_snapshot(str(RAW_DATA_DIR), fingerprint, ingestion_problems())

        store = ReviewStore(config.REVIEW_DB_PATH, review_policy())
        reviews = store.sync(build_exceptions(snapshot))
    except Exception as error:
        st.error(f"The exception queue could not be loaded: {error}")
        return

    exceptions = with_reviews(build_exceptions(snapshot), reviews)

    exception_ui.show_queue(
        PRINCIPAL, ExceptionWorkflow(store, assignable_users), store, exceptions,
        assignable=assignable_users(),
    )


def show_historical_trends():
    """
    The Historical Trends tab (src/trends_ui.py): only what the recorded
    audits say, separate from the current-state analytics.
    """

    try:
        history = HistoryStore(config.HISTORY_DB_PATH)
    except Exception as error:
        st.error(f"The audit history is unavailable: {error}")
        return

    trends_ui.show_trends(PRINCIPAL, history)


def show_ingestion_tab():

    ingestion_ui.show_ingestion(PRINCIPAL, RAW_DATA_DIR, CHROMA_DIR)


def show_administration():

    stores = {}

    if PRINCIPAL.can(authz.ADMIN_MAINTENANCE):
        stores["exception review history"] = lambda: ReviewStore(
            config.REVIEW_DB_PATH, review_policy()
        ).verify_integrity()

    auth_ui.show_admin(PRINCIPAL, stores)


# ============================================================
# MAIN TABS
# ============================================================

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10 = st.tabs(
    [
        "💬 Logistics Assistant",
        "📊 Shipment Reconciliation",
        "🧠 Shipment Intelligence",
        "🩺 Diagnostics",
        "📈 Audit Analytics",
        "🧭 Audit Operations",
        "📥 Exception Queue",
        "📉 Historical Trends",
        "🗂️ Ingestion",
        "⚙️ Administration",
    ]
)


# ============================================================
# TAB 1 — LOGISTICS ASSISTANT
# ============================================================

with tab1:

    st.markdown(
        '<div class="section-title">'
        'Ask Your Logistics Documents'
        '</div>',
        unsafe_allow_html=True,
    )

    if selected_shipment == "All Shipments":

        st.info(
            "Scope: All Shipments (unscoped). Every indexed shipment "
            "is searched. Select a shipment in the sidebar to limit "
            "answers to its documents."
        )

    else:

        st.info(
            f"Selected shipment: {selected_shipment}"
        )

        st.caption(
            "Answers use only the documents of this shipment."
        )

    question = st.text_input(
        "Logistics question",
        placeholder=(
            "Example: How many pieces were delivered?"
        ),
        key="logistics_question",
    )

    ask_button = st.button(
        "Ask Question",
        type="primary",
    )

    if ask_button:

        if not question.strip():

            st.warning(
                "Please enter a question."
            )

        else:

            try:

                with st.spinner(
                    "Searching logistics documents..."
                ):

                    answer, results = ask_question(
                        question,
                        shipment_id=(
                            None
                            if selected_shipment == "All Shipments"
                            else selected_shipment
                        )
                    )

                st.subheader("Answer")

                answered = (
                    results.get("status") == "answered"
                )

                answer_text = str(
                    answer or ""
                ).strip()

                # Abstentions and clarification requests are not
                # shown as successful answers
                if answered and answer_text:

                    st.success(
                        escape_markdown_dollars(answer_text)
                    )

                else:

                    st.warning(
                        escape_markdown_dollars(answer_text)
                        or "No answer was generated from the indexed documents."
                    )

                scope = results.get("scope") or {}

                if scope.get("label"):

                    st.caption(
                        f"Scope: {scope['label']}"
                    )

                # How the answer was produced, as rag reports it
                answer_method = results.get("answer_method")

                if answer_method == "exact":

                    st.caption(
                        "Method: deterministic (read or calculated from "
                        "document fields, without the language model)"
                    )

                elif answer_method == "llm":

                    st.caption(
                        "Method: language model (generated from the "
                        "retrieved passages)"
                    )

                # Citations: only the sources supporting the answer
                if answered:

                    show_source_evidence(
                        results.get("evidence"),
                        question
                    )

                metadatas_result = []

                if isinstance(
                    results,
                    dict
                ):

                    raw_metadatas = results.get(
                        "metadatas",
                        []
                    )

                    if (
                        isinstance(
                            raw_metadatas,
                            list
                        )
                        and raw_metadatas
                        and isinstance(
                            raw_metadatas[0],
                            list
                        )
                    ):

                        metadatas_result = (
                            raw_metadatas[0]
                        )

                    elif isinstance(
                        raw_metadatas,
                        list
                    ):

                        metadatas_result = (
                            raw_metadatas
                        )

                seen_sources = set()

                # What was searched, kept apart from the citations
                with st.expander(
                    "Searched passages (search results, not citations)"
                ):

                    for metadata in metadatas_result:

                        if not isinstance(
                            metadata,
                            dict
                        ):
                            continue

                        source = metadata.get(
                            "source",
                            "Unknown"
                        )

                        page = metadata.get(
                            "page",
                            "Unknown"
                        )

                        document_type = metadata.get(
                            "document_type",
                            "Unknown"
                        )

                        key = (
                            source,
                            page,
                            document_type
                        )

                        if key in seen_sources:
                            continue

                        st.write(
                            f"📄 {source} "
                            f"(Page {page}, "
                            f"Type: {document_type}, "
                            f"Shipment: {metadata.get('shipment_id') or 'none'})"
                        )

                        seen_sources.add(key)

            except Exception as error:

                st.error(
                    f"Question processing failed: {error}"
                )


# ============================================================
# TAB 2 — SHIPMENT RECONCILIATION
# ============================================================

with tab2:

    st.markdown(
        '<div class="section-title">'
        'Shipment Reconciliation'
        '</div>',
        unsafe_allow_html=True,
    )

    st.caption(
        "Compare BOL, POD and Invoice information to identify shipment discrepancies."
    )

    run_reconciliation = st.button(
        "Run Reconciliation",
        type="primary",
    )

    if run_reconciliation:

        try:

            with st.spinner(
                "Reconciling shipment documents..."
            ):

                result = reconcile_with_ingestion_problems(
                    None
                    if selected_shipment == "All Shipments"
                    else selected_shipment
                )

            if not isinstance(
                result,
                dict
            ):

                st.error(
                    "Reconciliation returned an unexpected result."
                )

            elif not result:

                st.error(
                    "No shipment documents found."
                )

            elif not result.get(
                "success",
                False
            ):

                st.error(
                    result.get(
                        "error",
                        "Reconciliation failed."
                    )
                )

            else:

                shipment_id = result.get(
                    "shipment_id",
                    "Unknown"
                )

                st.subheader(
                    f"Shipment: {shipment_id}"
                )

                # ------------------------------------------------
                # Quantity
                # ------------------------------------------------

                quantity = result.get(
                    "quantity",
                    {}
                )

                if not isinstance(
                    quantity,
                    dict
                ):
                    quantity = {}

                col1, col2, col3 = st.columns(3)

                with col1:

                    st.metric(
                        "BOL Pieces",
                        display_value(quantity.get("bol"))
                    )

                with col2:

                    st.metric(
                        "Delivered Pieces",
                        display_value(quantity.get("pod"))
                    )

                with col3:

                    st.metric(
                        "Difference",
                        display_value(quantity.get("difference"))
                    )

                if quantity.get(
                    "status"
                ) == "MISMATCH":

                    difference = quantity.get("difference", 0)

                    st.error(
                        "⚠️ Quantity discrepancy: "
                        f"{abs(difference)} pieces "
                        + (
                            "(shortage)."
                            if difference > 0
                            else "(over-delivery)."
                        )
                    )

                elif quantity.get(
                    "status"
                ) == "MATCH":

                    st.success(
                        "Quantity matches."
                    )

                else:

                    st.warning(
                        "Quantity needs review: "
                        + escape_markdown_dollars(
                            quantity.get("reason")
                            or "a piece count could not be verified"
                        )
                    )

                # ------------------------------------------------
                # Delivery
                # ------------------------------------------------

                st.subheader(
                    "Delivery"
                )

                delivery = result.get(
                    "delivery",
                    {}
                )

                if not isinstance(
                    delivery,
                    dict
                ):
                    delivery = {}

                col1, col2 = st.columns(2)

                with col1:

                    st.metric(
                        "Expected Delivery",
                        display_value(delivery.get("expected"))
                    )

                with col2:

                    st.metric(
                        "Actual Delivery",
                        display_value(delivery.get("actual"))
                    )

                delivery_status = delivery.get("status")
                days_late = delivery.get("days_late")

                if delivery_status == "DELAYED":

                    st.warning(
                        "⚠️ Delivery date discrepancy: delivered "
                        f"{days_late} day(s) after the expected "
                        "delivery date."
                    )

                elif delivery_status == "EARLY":

                    st.success(
                        f"Delivered {abs(days_late)} day(s) early. "
                        "An early delivery is not a discrepancy."
                    )

                elif delivery_status == "ON TIME":

                    st.success(
                        "Delivered on the expected delivery date."
                    )

                else:

                    st.warning(
                        "Delivery timing needs review: "
                        + escape_markdown_dollars(
                            delivery.get("reason")
                            or "a delivery date could not be verified"
                        )
                    )

                # ------------------------------------------------
                # Route
                # ------------------------------------------------

                st.subheader(
                    "Shipment Route"
                )

                route = result.get(
                    "route",
                    {}
                )

                if not isinstance(
                    route,
                    dict
                ):
                    route = {}

                col1, col2 = st.columns(2)

                with col1:

                    st.caption("ORIGIN")

                    st.write(
                        route.get(
                            "origin",
                            "N/A"
                        )
                    )

                with col2:

                    st.caption("DESTINATION")

                    st.write(
                        route.get(
                            "destination",
                            "N/A"
                        )
                    )

                # ------------------------------------------------
                # Carrier
                # ------------------------------------------------

                st.subheader(
                    "Carrier"
                )

                carrier = result.get(
                    "carrier",
                    {}
                )

                if not isinstance(
                    carrier,
                    dict
                ):
                    carrier = {}

                col1, col2, col3 = st.columns(3)

                with col1:

                    st.caption("BOL")

                    st.write(
                        carrier.get(
                            "bol",
                            "N/A"
                        )
                    )

                with col2:

                    st.caption("INVOICE")

                    st.write(
                        carrier.get(
                            "invoice",
                            "N/A"
                        )
                    )

                with col3:

                    st.caption("POD")

                    st.write(
                        carrier.get(
                            "pod",
                            "N/A"
                        )
                    )

                # ------------------------------------------------
                # Invoice
                # ------------------------------------------------

                st.subheader(
                    "Invoice"
                )

                invoice = result.get(
                    "invoice",
                    {}
                )

                if not isinstance(
                    invoice,
                    dict
                ):
                    invoice = {}

                # Every line item the invoice check adds up, then the
                # sum, the stated total and the difference
                col1, col2, col3, col4 = st.columns(4)

                for column, label, key in (
                    (col1, "Base Freight", "base_freight"),
                    (col2, "Fuel Surcharge", "fuel_surcharge"),
                    (col3, "Detention Charge", "detention_charge"),
                    (col4, "Other Charges", "other_charges"),
                ):

                    with column:

                        st.metric(
                            label,
                            money_text(invoice.get(key))
                        )

                col1, col2, col3 = st.columns(3)

                with col1:

                    st.metric(
                        "Line Item Sum",
                        money_text(invoice.get("line_item_sum"))
                    )

                with col2:

                    st.metric(
                        "Invoice Total",
                        money_text(invoice.get("total"))
                    )

                with col3:

                    st.metric(
                        "Difference",
                        money_text(invoice.get("difference"))
                    )

                invoice_status = invoice.get("status")

                if invoice_status == "MATCH":

                    st.success(
                        escape_markdown_dollars(
                            "Invoice line items "
                            f"({invoice.get('breakdown')}) add up to "
                            f"${invoice.get('line_item_sum'):,.2f}, "
                            "which equals the Total Invoice Amount."
                        )
                    )

                elif invoice_status == "MISMATCH":

                    st.error(
                        escape_markdown_dollars(
                            "⚠️ Invoice line items "
                            f"({invoice.get('breakdown')}) add up to "
                            f"${invoice.get('line_item_sum'):,.2f}, but "
                            "the Total Invoice Amount is "
                            f"${invoice.get('total'):,.2f}, a "
                            "difference of "
                            f"${abs(invoice.get('difference')):,.2f}."
                        )
                    )

                else:

                    st.warning(
                        "Invoice needs review: "
                        + escape_markdown_dollars(
                            invoice.get("reason")
                            or "the invoice could not be verified"
                        )
                    )

                # ------------------------------------------------
                # Evidence
                # ------------------------------------------------

                reconciliation_evidence = (
                    build_reconciliation_evidence(
                        result
                    )
                )

                show_reconciliation_evidence(
                    reconciliation_evidence
                )

                # ------------------------------------------------
                # Final Result
                # ------------------------------------------------

                st.subheader(
                    "Reconciliation Result"
                )

                discrepancies = result.get(
                    "discrepancies",
                    []
                )

                if discrepancies:

                    st.error(
                        "⚠️ DISCREPANCIES DETECTED"
                    )

                    for discrepancy in discrepancies:

                        if not isinstance(
                            discrepancy,
                            dict
                        ):
                            continue

                        category = discrepancy.get(
                            "category",
                            "Issue"
                        )

                        description = discrepancy.get(
                            "description",
                            ""
                        )

                        st.warning(
                            f"**{category}** — "
                            f"{description}"
                        )

                review_items = result.get(
                    "review_items",
                    []
                )

                if review_items and not discrepancies:

                    st.warning(
                        "⚠️ NEEDS REVIEW: some values could not be "
                        "verified."
                    )

                for item in review_items:

                    st.warning(
                        f"**{item.get('category')}** — needs review: "
                        + escape_markdown_dollars(
                            str(item.get("reason", ""))
                        )
                    )

                if not discrepancies and not review_items:

                    st.success(
                        "✅ NO DISCREPANCIES"
                    )

                if result.get("excluded_unidentified"):

                    st.caption(
                        f"{result['excluded_unidentified']} document "
                        "chunk(s) without a shipment ID were not used."
                    )

                if result.get("unreadable_files"):

                    st.warning(
                        f"{len(result['unreadable_files'])} file(s) in the document folder "
                        "could not be used and belong to no shipment: "
                        + ", ".join(result["unreadable_files"])
                    )

                # ------------------------------------------------
                # Audit export (the selected shipment only)
                # ------------------------------------------------

                if selected_shipment == "All Shipments":

                    st.caption(
                        "Select a shipment in the sidebar to export "
                        "its freight audit as CSV or PDF."
                    )

                else:

                    csv_column, pdf_column = st.columns(2)

                    with csv_column:

                        try:

                            st.download_button(
                                "Download freight audit (CSV)",
                                data=audit_csv(result, selected_shipment),
                                file_name=export_filename(result),
                                mime="text/csv",
                                # Downloading must not rerun the app: that
                                # would clear this result and the PDF button
                                on_click="ignore",
                            )

                        except ExportError as export_error:

                            st.caption(
                                f"Audit export unavailable: {export_error}"
                            )

                    # The same reconciliation result as the CSV
                    with pdf_column:

                        try:

                            st.download_button(
                                "Download freight audit (PDF)",
                                data=audit_pdf(result, selected_shipment),
                                file_name=pdf_filename(result),
                                mime="application/pdf",
                                on_click="ignore",
                            )

                        except ExportError as export_error:

                            st.caption(
                                f"PDF report unavailable: {export_error}"
                            )

                        except Exception as pdf_error:

                            st.caption(
                                f"PDF report could not be created: {pdf_error}"
                            )

        except Exception as error:

            st.error(
                f"Reconciliation failed: {error}"
            )


# ============================================================
# TAB 3 — SHIPMENT INTELLIGENCE
# ============================================================

with tab3:

    st.markdown(
        '<div class="section-title">'
        'Shipment Intelligence'
        '</div>',
        unsafe_allow_html=True,
    )

    st.caption(
        "Operational analysis of shipment completeness, quantity, delivery and financial status."
    )

    run_intelligence = st.button(
        "Run Shipment Intelligence",
        type="primary",
    )

    if run_intelligence:

        if selected_shipment == "All Shipments":

            show_all_shipments_intelligence(
                shipment_ids
            )

        else:

            show_shipment_intelligence(
                selected_shipment
            )


# ============================================================
# TAB 4 — DIAGNOSTICS (read-only)
# ============================================================

with tab4:

    st.markdown(
        '<div class="section-title">'
        'Document Store Diagnostics'
        '</div>',
        unsafe_allow_html=True,
    )

    st.caption(
        "Read-only check of the raw PDF folder, the vector index "
        "and the ingestion journal. Nothing is changed, repaired "
        "or reindexed."
    )

    run_diagnostics = st.button(
        "Run Consistency Check",
        type="primary",
    )

    if run_diagnostics:

        try:

            with st.spinner(
                "Checking documents, vectors and journal..."
            ):

                diagnostics = check_destination(
                    RAW_DATA_DIR,
                    CHROMA_DIR
                )

        except Exception as error:

            st.error(
                f"Consistency check failed: {error}"
            )

        else:

            show_consistency_report(diagnostics)


# ============================================================
# TAB 5 — AUDIT ANALYTICS
# ============================================================

with tab5:

    st.markdown(
        '<div class="section-title">'
        'Freight Audit Analytics'
        '</div>',
        unsafe_allow_html=True,
    )

    st.caption(
        "Every identified shipment reconciled, with KPIs, an audit "
        "register, shipment details and an exception register with "
        "review states."
    )

    if st.button("Load audit analytics", type="primary"):
        st.session_state["analytics_loaded"] = True

    if st.session_state.get("analytics_loaded"):
        show_audit_analytics(shipment_options)
    else:
        st.info(
            "Load the analytics to reconcile every shipment in the "
            "document folder. Results are reused until a document changes."
        )


# ============================================================
# TAB 6 — AUDIT OPERATIONS
# ============================================================

with tab6:

    st.markdown(
        '<div class="section-title">'
        'Audit Operations'
        '</div>',
        unsafe_allow_html=True,
    )

    st.caption(
        "Document readiness, financial exposure, evidence-backed "
        "findings, review, audit history and reports for each shipment."
    )

    if st.button("Load audit operations", type="primary"):
        st.session_state["operations_loaded"] = True

    if st.session_state.get("operations_loaded"):
        show_audit_operations()
    else:
        st.info(
            "Load the operations view to read every document and reconcile "
            "every shipment. Results are reused until a document changes."
        )


# ============================================================
# TAB 7 — EXCEPTION QUEUE
# ============================================================

with tab7:

    st.markdown(
        '<div class="section-title">'
        'Exception Queue'
        '</div>',
        unsafe_allow_html=True,
    )

    st.caption(
        "Assign exceptions, review them, and resolve or dismiss them with "
        "notes and evidence. Reviewing never changes the audit result."
    )

    if st.button("Load exception queue", type="primary"):
        st.session_state["queue_loaded"] = True

    if st.session_state.get("queue_loaded"):
        show_exception_queue()
    else:
        st.info(
            "Load the queue to reconcile every shipment and list its exceptions."
        )


# ============================================================
# TAB 8 — HISTORICAL TRENDS
# ============================================================

with tab8:

    st.markdown(
        '<div class="section-title">'
        'Historical Trends'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Load historical trends", type="primary"):
        st.session_state["trends_loaded"] = True

    if st.session_state.get("trends_loaded"):
        show_historical_trends()
    else:
        st.info(
            "Load the trends to read the recorded audit history."
        )


# ============================================================
# TAB 9 — INGESTION
# ============================================================

with tab9:

    st.markdown(
        '<div class="section-title">'
        'Document Ingestion'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Load ingestion register", type="primary"):
        st.session_state["ingestion_loaded"] = True

    if st.session_state.get("ingestion_loaded"):
        show_ingestion_tab()
    else:
        st.info(
            "Load the register to see the status of every file offered "
            "for ingestion and the open errors."
        )


# ============================================================
# TAB 10 — ADMINISTRATION
# ============================================================

with tab10:

    st.markdown(
        '<div class="section-title">'
        'Administration'
        '</div>',
        unsafe_allow_html=True,
    )

    show_administration()


# ============================================================
# FOOTER
# ============================================================

st.divider()

st.caption(
    "LogiDoc-RAG • Logistics Document Intelligence"
)
