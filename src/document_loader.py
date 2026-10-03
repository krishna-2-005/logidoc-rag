from pathlib import Path
import sys
import pymupdf

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import RAW_DATA_DIR


def classify_document(filename):
    """
    Identify the logistics document type from its filename.
    """

    filename_upper = filename.upper()

    if filename_upper.startswith("BOL_"):
        return "BOL"

    if filename_upper.startswith("POD_"):
        return "POD"

    if filename_upper.startswith("INVOICE_"):
        return "INVOICE"

    if filename_upper.startswith("RATE_CONFIRMATION_"):
        return "RATE_CONFIRMATION"

    return "UNKNOWN"


def extract_pdf(pdf_path):
    """
    Extract text from a PDF page by page.
    """

    document = pymupdf.open(pdf_path)

    pages = []

    for page_number, page in enumerate(document, start=1):

        # sort=True returns text in visual reading order (top to bottom),
        # so values stay next to their labels even if the PDF was edited
        text = page.get_text("text", sort=True).strip()

        if text:
            pages.append(
                {
                    "page": page_number,
                    "text": text
                }
            )

    document.close()

    return pages


def load_document(pdf_path):
    """
    Load one logistics PDF and return structured document data.
    """

    pdf_path = Path(pdf_path)

    document_type = classify_document(pdf_path.name)

    pages = extract_pdf(pdf_path)

    return {
        "filename": pdf_path.name,
        "path": str(pdf_path),
        "document_type": document_type,
        "page_count": len(pages),
        "pages": pages
    }


def load_all_documents():
    """
    Load all PDF documents from the raw data directory.
    """

    documents = []

    pdf_files = sorted(RAW_DATA_DIR.glob("*.pdf"))

    for pdf_file in pdf_files:

        document = load_document(pdf_file)

        documents.append(document)

    return documents


if __name__ == "__main__":

    print("=" * 60)
    print("LogiDoc-RAG Document Loader Test")
    print("=" * 60)

    documents = load_all_documents()

    print(f"\nDocuments found: {len(documents)}")

    for document in documents:

        print("\n" + "-" * 60)

        print(f"File          : {document['filename']}")
        print(f"Type          : {document['document_type']}")
        print(f"Pages         : {document['page_count']}")

        for page in document["pages"]:

            print(f"\nPage {page['page']}")

            print(page["text"][:500])

    print("\n" + "=" * 60)
    print("Document loading completed successfully.")
    print("=" * 60)