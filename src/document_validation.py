"""
LogiDoc-RAG file validation

Decides, before anything is indexed or stored, whether an uploaded file
can be ingested, and says why not in words a person can act on. It only
reads the bytes it is given: it never writes, moves or deletes a file.

Result of validate_pdf():

    ok            True when the file can be ingested (text on every
                  page, or on some pages: see warnings)
    status        None when ok, otherwise "failed" (the file is broken
                  and a corrected copy is needed) or "unsupported" (the
                  file is not something this system can read)
    code          a short stable code (CODES) for the reason
    message       the reason
    action        what to do about it
    pages, text_pages, empty_pages
                  what the PDF holds, when it could be opened
    warnings      codes of problems that do not block ingestion

Image-only PDFs (scans without a text layer) are "unsupported" with the
code needs_ocr. There is no OCR in this installation, and none is
pretended: nothing is extracted, nothing is indexed, and the file is
reported as requiring OCR. A PDF with text on some pages only is ok,
with the warning partial_text and the pages without text listed.
"""

import re
from pathlib import Path

import pymupdf

import pdf_access


MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_PAGES = 400
MAX_NAME_LENGTH = 150

FAILED = "failed"
UNSUPPORTED = "unsupported"

BAD_FILENAME = "bad_filename"
UNSUPPORTED_TYPE = "unsupported_type"
EMPTY_FILE = "empty_file"
TOO_LARGE = "too_large"
NOT_A_PDF = "not_a_pdf"
CORRUPTED = "corrupted"
PASSWORD_PROTECTED = "password_protected"
NO_PAGES = "no_pages"
TOO_MANY_PAGES = "too_many_pages"
NEEDS_OCR = "needs_ocr"

PARTIAL_TEXT = "partial_text"

CODES = {
    BAD_FILENAME: "The file name is not allowed.",
    UNSUPPORTED_TYPE: "Only PDF files can be ingested.",
    EMPTY_FILE: "The file is empty (0 bytes).",
    TOO_LARGE: "The file is larger than the upload limit.",
    NOT_A_PDF: "The content is not a PDF, whatever the file name says.",
    CORRUPTED: "The PDF is damaged and cannot be opened.",
    PASSWORD_PROTECTED: "The PDF is password-protected.",
    NO_PAGES: "The PDF has no pages.",
    TOO_MANY_PAGES: "The PDF has more pages than the limit.",
    NEEDS_OCR: "The PDF has no extractable text (an image-only scan). OCR is not available.",
    PARTIAL_TEXT: "Some pages have no extractable text (scanned pages?).",
}

ACTIONS = {
    BAD_FILENAME: "Rename the file with letters, digits, spaces, '.', '_' or '-' (at most 150 characters) and upload it again.",
    UNSUPPORTED_TYPE: "Convert the document to a text-based PDF and upload that.",
    EMPTY_FILE: "Export the document again and upload the new file.",
    TOO_LARGE: "Split or compress the PDF below the limit and upload it again.",
    NOT_A_PDF: "Open the file to check what it is; export it as a real PDF and upload that.",
    CORRUPTED: "Re-export or re-download the PDF from its source, then upload the corrected file.",
    PASSWORD_PROTECTED: "Remove the password in a PDF tool, then upload the unprotected copy.",
    NO_PAGES: "Export the document again; this PDF contains no pages.",
    TOO_MANY_PAGES: "Upload the relevant pages as a separate PDF.",
    NEEDS_OCR: (
        "This installation has no OCR, so the document cannot be read. Run it through an OCR tool "
        "(so it has a text layer) and upload the result, or key its values into a text-based PDF."
    ),
    PARTIAL_TEXT: "Check the pages without text; if they matter, OCR the document and replace it.",
}

SAFE_NAME = re.compile(r"^\w[\w ._()\-]*$")

# Device names Windows cannot use as file names
RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"com{n}" for n in range(1, 10)} | {f"lpt{n}" for n in range(1, 10)}


def outcome(ok, status=None, code=None, **details):

    return {
        "ok": ok, "status": status, "code": code,
        "message": CODES.get(code) if code else None,
        "action": ACTIONS.get(code) if code else None,
        "pages": None, "text_pages": None, "empty_pages": [], "warnings": [],
        **details,
    }


def reject(status, code, **details):

    return outcome(False, status, code, **details)


def check_filename(name):
    """
    None when the name is allowed, otherwise the code. A name is a bare
    file name: no directory part, no control characters, no leading dot
    or dash, a .pdf extension.
    """

    name = str(name or "")

    if not name or name != Path(name).name or "/" in name or "\\" in name:
        return BAD_FILENAME

    if len(name) > MAX_NAME_LENGTH or any(ord(char) < 32 for char in name):
        return BAD_FILENAME

    if not SAFE_NAME.match(name) or ".." in name or name.endswith((" ", ".")):
        return BAD_FILENAME

    if name.split(".")[0].strip().casefold() in RESERVED_NAMES:
        return BAD_FILENAME

    if not name.lower().endswith(".pdf"):
        return UNSUPPORTED_TYPE

    return None


def validate_pdf(name, data, max_bytes=MAX_FILE_BYTES, max_pages=MAX_PAGES):
    """
    Validates an uploaded file given its name and its bytes.
    """

    problem = check_filename(name)

    if problem == UNSUPPORTED_TYPE:
        return reject(UNSUPPORTED, UNSUPPORTED_TYPE)

    if problem:
        return reject(FAILED, problem)

    if not data:
        return reject(FAILED, EMPTY_FILE)

    if len(data) > max_bytes:
        return reject(
            FAILED, TOO_LARGE,
            message=f"The file is {len(data) / 1024 / 1024:.1f} MB; the limit is {max_bytes / 1024 / 1024:.0f} MB.",
        )

    if b"%PDF-" not in data[:1024]:
        return reject(UNSUPPORTED, NOT_A_PDF)

    with pdf_access.PDF_LOCK:

        try:
            document = pymupdf.open(stream=data, filetype="pdf")
        except Exception as error:
            return reject(FAILED, CORRUPTED, error=f"{type(error).__name__}: {error}"[:200])

        try:

            if document.needs_pass or document.is_encrypted:
                return reject(FAILED, PASSWORD_PROTECTED)

            total = len(document)

            if total == 0:
                return reject(FAILED, NO_PAGES, pages=0)

            if total > max_pages:
                return reject(
                    FAILED, TOO_MANY_PAGES, pages=total,
                    message=f"The PDF has {total} pages; the limit is {max_pages}.",
                )

            try:
                with_text = [bool(page.get_text("text").strip()) for page in document]
            except Exception as error:
                return reject(FAILED, CORRUPTED, pages=total, error=f"{type(error).__name__}: {error}"[:200])

        finally:
            document.close()

    empty_pages = [number for number, has in enumerate(with_text, start=1) if not has]
    text_pages = total - len(empty_pages)

    if text_pages == 0:
        return reject(UNSUPPORTED, NEEDS_OCR, pages=total, text_pages=0, empty_pages=empty_pages)

    result = outcome(True, pages=total, text_pages=text_pages, empty_pages=empty_pages)

    if empty_pages:
        result["warnings"].append(PARTIAL_TEXT)

    return result


def describe(validation):
    """
    The reason of a rejected file (or the warnings of an accepted one)
    as one line.
    """

    if validation["ok"]:
        return "; ".join(CODES[code] for code in validation["warnings"]) or None

    text = validation["message"] or "The file cannot be ingested."

    if validation["empty_pages"] and validation["code"] == NEEDS_OCR:
        text += f" ({validation['pages']} page(s) without text.)"

    if validation.get("error"):
        text += f" ({validation['error']})"

    return text
