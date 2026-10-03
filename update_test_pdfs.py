import pymupdf
from pathlib import Path

RAW_DIR = Path(r"D:\LogiDoc-RAG\data\raw")


def replace_text(pdf_path, replacements):
    doc = pymupdf.open(pdf_path)

    for page in doc:
        for old_text, new_text in replacements:
            areas = page.search_for(old_text)

            for rect in areas:
                page.add_redact_annot(
                    rect,
                    text=new_text,
                    fontsize=11
                )

        page.apply_redactions()

    doc.save(
        str(pdf_path).replace(".pdf", "_updated.pdf")
    )

    doc.close()


# ---------------------------------------------------------
# BOL
# ---------------------------------------------------------

bol = RAW_DIR / "BOL_SHJ-2026-001.pdf"

replace_text(
    bol,
    [
        ("48", "48"),
        ("October 3, 2026", "October 3, 2026"),
    ]
)


# ---------------------------------------------------------
# POD
# ---------------------------------------------------------

pod = RAW_DIR / "POD_SHJ-2026-001.pdf"

replace_text(
    pod,
    [
        ("48", "45"),
        ("October 3, 2026", "October 5, 2026"),
    ]
)


# ---------------------------------------------------------
# INVOICE
# ---------------------------------------------------------

invoice = RAW_DIR / "INVOICE_SHJ-2026-001.pdf"

replace_text(
    invoice,
    [
        ("$1,430.00", "$1,550.00"),
    ]
)


print()
print("=" * 60)
print("TEST PDFs CREATED")
print("=" * 60)
print()
print("Created:")
print("BOL_SHJ-2026-001_updated.pdf")
print("POD_SHJ-2026-001_updated.pdf")
print("INVOICE_SHJ-2026-001_updated.pdf")
print()