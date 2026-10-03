from pathlib import Path
import streamlit as st


# ============================================================
# LOGIDOC-RAG
# PHASE 2 - PDF VIEWER TEST
# ============================================================

st.set_page_config(
    page_title="LogiDoc-RAG PDF Viewer",
    page_icon="📄",
    layout="wide"
)

st.title("📄 LogiDoc-RAG — Document Viewer")
st.caption("Phase 2: Source Evidence → Original PDF")


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"


# ============================================================
# FIND PDF DOCUMENTS
# ============================================================

pdf_files = sorted(
    RAW_DATA_DIR.glob("*.pdf")
)


if not pdf_files:

    st.error(
        f"No PDF files found in:\n{RAW_DATA_DIR}"
    )

    st.stop()


# ============================================================
# DOCUMENT SELECTOR
# ============================================================

selected_pdf = st.selectbox(
    "Select a logistics document",
    pdf_files,
    format_func=lambda path: path.name
)


# ============================================================
# DOCUMENT INFORMATION
# ============================================================

st.subheader("Document Information")

col1, col2 = st.columns(2)

with col1:
    st.write(
        f"**File:** {selected_pdf.name}"
    )

with col2:
    st.write(
        f"**Location:** `{selected_pdf}`"
    )


# ============================================================
# PDF VIEWER
# ============================================================

st.subheader("Original PDF")

try:

    st.pdf(
        str(selected_pdf),
        height=750
    )

except Exception as error:

    st.error(
        f"Unable to display PDF: {error}"
    )

    st.info(
        'If the error mentions "streamlit-pdf", '
        'run: pip install "streamlit[pdf]"'
    )