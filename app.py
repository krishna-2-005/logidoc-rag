import sys
from pathlib import Path

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
# IMPORT PROJECT MODULES
# ============================================================

from config import (
    RAW_DATA_DIR,
    CHROMA_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    LLM_MODEL,
)

from vector_store import build_vector_database

from rag import ask_question

from reconciliation import reconcile_shipment


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
        font-weight: 700;
        margin-bottom: 0px;
    }

    .subtitle {
        font-size: 17px;
        color: #666;
        margin-top: 0px;
        margin-bottom: 25px;
    }

    .status-card {
        padding: 18px;
        border-radius: 12px;
        border: 1px solid #ddd;
        margin-bottom: 15px;
    }

    .section-title {
        font-size: 24px;
        font-weight: 650;
        margin-top: 15px;
        margin-bottom: 15px;
    }

    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# HEADER
# ============================================================

st.markdown(
    '<div class="main-title">📦 LogiDoc-RAG</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="subtitle">'
    "AI-powered logistics document intelligence and shipment reconciliation"
    "</div>",
    unsafe_allow_html=True,
)


# ============================================================
# SESSION STATE
# ============================================================

if "documents_processed" not in st.session_state:
    st.session_state.documents_processed = False


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

    process_button = st.button(
        "Process Documents",
        type="primary",
        use_container_width=True,
    )

    if process_button:

        if not uploaded_files:

            st.warning(
                "Please upload at least one PDF."
            )

        else:

            try:

                RAW_DATA_DIR.mkdir(
                    parents=True,
                    exist_ok=True
                )

                # ------------------------------------------------
                # Remove previous PDFs
                # ------------------------------------------------

                for pdf_file in RAW_DATA_DIR.glob("*.pdf"):
                    pdf_file.unlink()

                # ------------------------------------------------
                # Save uploaded PDFs
                # ------------------------------------------------

                for uploaded_file in uploaded_files:

                    destination = (
                        RAW_DATA_DIR
                        / Path(uploaded_file.name).name
                    )

                    with open(
                        destination,
                        "wb"
                    ) as file:

                        file.write(
                            uploaded_file.getbuffer()
                        )

                # ------------------------------------------------
                # Build vector database
                # ------------------------------------------------

                with st.spinner(
                    "Extracting, chunking and indexing documents..."
                ):

                    build_vector_database()

                st.session_state.documents_processed = True

                st.success(
                    f"{len(uploaded_files)} document(s) processed."
                )

            except Exception as e:

                st.error(
                    f"Processing failed: {e}"
                )


# ============================================================
# SIDEBAR SYSTEM STATUS
# ============================================================


# ============================================================
# MAIN TABS
# ============================================================

tab1, tab2 = st.tabs(
    [
        "💬 Logistics Assistant",
        "📊 Shipment Reconciliation",
    ]
)


# ============================================================
# TAB 1 — LOGISTICS ASSISTANT
# ============================================================

with tab1:

    st.markdown(
        '<div class="section-title">'
        "Ask your logistics documents"
        "</div>",
        unsafe_allow_html=True,
    )

    st.caption(
        "Ask questions about BOL, POD and Invoice documents."
    )

    question = st.text_input(
        "Logistics question",
        placeholder=(
            "Example: How many pieces were delivered?"
        ),
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
                        question
                    )

                st.subheader("Answer")

                st.success(answer)

                # ------------------------------------------------
                # Sources
                # ------------------------------------------------

                st.subheader("Sources")

                metadatas = (
                    results
                    .get("metadatas", [[]])[0]
                    or []
                )

                seen_sources = set()

                for metadata in metadatas:

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

                    if key not in seen_sources:

                        st.write(
                            f"📄 {source} "
                            f"(Page {page}, "
                            f"Type: {document_type})"
                        )

                        seen_sources.add(key)

            except Exception as e:

                st.error(
                    f"Question processing failed: {e}"
                )


# ============================================================
# TAB 2 — RECONCILIATION
# ============================================================

with tab2:

    st.markdown(
        '<div class="section-title">'
        "Shipment Reconciliation"
        "</div>",
        unsafe_allow_html=True,
    )

    st.caption(
        "Compare BOL, POD and Invoice data to detect shipment discrepancies."
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

                result = reconcile_shipment()

            if not result:

                st.error(
                    "No shipment documents found."
                )

            elif not result.get("success"):

                st.error(
                    result.get(
                        "error",
                        "Reconciliation failed."
                    )
                )

            else:

                # ------------------------------------------------
                # Shipment Header
                # ------------------------------------------------

                st.subheader(
                    f"Shipment: {result['shipment_id']}"
                )

                # ------------------------------------------------
                # Quantity Metrics
                # ------------------------------------------------

                quantity = result["quantity"]

                col1, col2, col3 = st.columns(3)

                with col1:

                    st.metric(
                        "BOL Pieces",
                        quantity["bol"]
                    )

                with col2:

                    st.metric(
                        "Delivered Pieces",
                        quantity["pod"]
                    )

                with col3:

                    st.metric(
                        "Difference",
                        quantity["difference"]
                    )

                if quantity["status"] == "MISMATCH":

                    st.error(
                        f"⚠️ Quantity discrepancy: "
                        f"{quantity['difference']} pieces."
                    )

                else:

                    st.success(
                        "Quantity matches."
                    )

                # ------------------------------------------------
                # Delivery
                # ------------------------------------------------

                st.subheader(
                    "Delivery"
                )

                delivery = result["delivery"]

                col1, col2 = st.columns(2)

                with col1:

                    st.metric(
                        "Expected Delivery",
                        delivery["expected"]
                    )

                with col2:

                    st.metric(
                        "Actual Delivery",
                        delivery["actual"]
                    )

                if delivery["status"] == "MISMATCH":

                    st.warning(
                        "⚠️ Delivery date discrepancy detected."
                    )

                else:

                    st.success(
                        "Delivery date matches."
                    )

                # ------------------------------------------------
                # Route
                # ------------------------------------------------

                st.subheader(
                    "Shipment Route"
                )

                route = result["route"]

                col1, col2 = st.columns(2)

                with col1:

                    st.write(
                        "**Origin**"
                    )

                    st.write(
                        route["origin"]
                    )

                with col2:

                    st.write(
                        "**Destination**"
                    )

                    st.write(
                        route["destination"]
                    )

                # ------------------------------------------------
                # Carrier
                # ------------------------------------------------

                st.subheader(
                    "Carrier"
                )

                carrier = result["carrier"]

                st.write(
                    f"**BOL:** {carrier['bol']}"
                )

                st.write(
                    f"**Invoice:** {carrier['invoice']}"
                )

                st.write(
                    f"**POD:** {carrier['pod']}"
                )

                # ------------------------------------------------
                # Invoice
                # ------------------------------------------------

                st.subheader(
                    "Invoice"
                )

                invoice = result["invoice"]

                col1, col2, col3, col4 = st.columns(4)

                with col1:

                    st.metric(
                        "Base Freight",
                        f"${invoice['base_freight']:,.2f}"
                        if invoice["base_freight"] is not None
                        else "N/A"
                    )

                with col2:

                    st.metric(
                        "Fuel Surcharge",
                        f"${invoice['fuel_surcharge']:,.2f}"
                        if invoice["fuel_surcharge"] is not None
                        else "N/A"
                    )

                with col3:

                    st.metric(
                        "Other Charges",
                        f"${invoice['other_charges']:,.2f}"
                        if invoice["other_charges"] is not None
                        else "N/A"
                    )

                with col4:

                    st.metric(
                        "Invoice Total",
                        f"${invoice['total']:,.2f}"
                        if invoice["total"] is not None
                        else "N/A"
                    )

                # ------------------------------------------------
                # Final Status
                # ------------------------------------------------

                st.subheader(
                    "Reconciliation Result"
                )

                if result["discrepancies"]:

                    st.error(
                        "⚠️ DISCREPANCIES DETECTED"
                    )

                    for discrepancy in result[
                        "discrepancies"
                    ]:

                        st.warning(
                            f"**{discrepancy['category']}** — "
                            f"{discrepancy['description']}"
                        )

                else:

                    st.success(
                        "✅ NO DISCREPANCIES"
                    )

        except Exception as e:

            st.error(
                f"Reconciliation failed: {e}"
            )


# ============================================================
# FOOTER
# ============================================================

st.divider()

st.caption(
    "LogiDoc-RAG • Logistics Document Intelligence"
)