"""
LogiDoc-RAG document completeness and ingestion quality

Reads the PDFs of the raw document folder (the same folder the
reconciliation reads) one by one and reports, per shipment, whether
the documents needed for a freight reconciliation are present and
usable. A freight reconciliation needs a BOL, an invoice and a POD.

Each file gets a status from what the extraction actually did:

    parsed     text was extracted from every page
    partial    some pages had no extractable text (scanned pages?)
    no_text    the PDF opened but no page has extractable text (an
               image-only scan; OCR is not supported)
    empty      the PDF opened and has no pages
    unreadable the PDF could not be opened or analysed (the error is
               kept)

A usable document is a parsed or partial one with a document type and
a shipment ID found in its content. A file that is not usable has no
shipment ID, so it can never be attributed to a shipment: it is listed
separately, never counted as a shipment's document, and never claimed
to be a shipment's missing one. Documents are not merged across
shipments: shipment IDs are compared with identifier_key.

Readiness per shipment:

    ready       a usable BOL, invoice and POD are all present
    incomplete  at least one of them is missing; the register says which,
                and what is needed to make the shipment auditable

"Ready" means the documents are there. It does not mean the audit will
find nothing: values can still be missing, conflicting or unreadable
(NEEDS REVIEW). Warnings (not blocking) note partial extraction, more
than one document of a type, and byte-identical files.
"""

import hashlib
from pathlib import Path

import pymupdf

from audit_analytics import cell, write_csv
from chunker import read_chunks
from document_loader import load_document
from shipment_identity import identifier_key


REQUIRED_TYPES = ("BOL", "INVOICE", "POD")

PARSED = "parsed"
PARTIAL = "partial"
NO_TEXT = "no_text"
EMPTY = "empty"
UNREADABLE = "unreadable"

USABLE = (PARSED, PARTIAL)

STATUS_TEXT = {
    PARSED: "Text extracted from every page.",
    PARTIAL: "Some pages have no extractable text (scanned pages?).",
    NO_TEXT: "No extractable text (image-only scan; OCR is not supported).",
    EMPTY: "The PDF has no pages.",
    UNREADABLE: "The PDF could not be opened or analysed.",
}

READY = "ready"
INCOMPLETE = "incomplete"

COLUMNS = (
    "shipment_id", "readiness", "bol", "invoice", "pod", "missing",
    "documents", "indexed_documents", "warnings", "next_step",
)


# ============================================================
# FILES
# ============================================================

def scan_file(path, loader=load_document):
    """
    One PDF: its status from what extraction did, and, when it is
    usable, the document type and shipment ID found in its content.
    """

    path = Path(path)

    data = path.read_bytes()

    entry = {
        "filename": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "status": UNREADABLE,
        "error": None,
        "total_pages": None,
        "text_pages": None,
        "document_type": None,
        "shipment_id": None,
        "shipment_key": None,
    }

    try:

        document = pymupdf.open(stream=data, filetype="pdf")

        try:
            total = len(document)
            text_pages = sum(1 for page in document if page.get_text("text").strip())
        finally:
            document.close()

    except Exception as error:
        entry["error"] = f"{type(error).__name__}: {error}"[:200]
        return entry

    entry["total_pages"], entry["text_pages"] = total, text_pages

    if total == 0:
        entry["status"] = EMPTY
        return entry

    if text_pages == 0:
        entry["status"] = NO_TEXT
        return entry

    try:
        loaded = loader(path)
    except Exception as error:
        entry["error"] = f"{type(error).__name__}: {error}"[:200]
        return entry

    entry["status"] = PARSED if text_pages == total else PARTIAL
    entry["document_type"] = str(loaded.get("document_type") or "UNKNOWN").upper()

    shipment_id = (loaded.get("identifiers") or {}).get("shipment_id")
    entry["shipment_id"] = str(shipment_id).strip() if shipment_id else None
    entry["shipment_key"] = identifier_key(shipment_id)

    return entry


def readable_chunks(raw_dir, loader=load_document):
    """
    chunker.read_chunks() with the names of the files that gave no
    chunks (unreadable, or with no extractable text) instead of their
    details: the chunks of every usable PDF of the folder, and the names
    of the others. The completeness scan reports why a file was skipped.
    """

    chunks, problems = read_chunks(raw_dir, loader=loader)

    return chunks, [item["filename"] for item in problems]


def scan_folder(raw_dir, loader=load_document):
    """
    scan_file for every PDF of the folder, sorted by name; an empty
    list when the folder is missing or has no PDFs.
    """

    raw_dir = Path(raw_dir)

    if not raw_dir.exists():
        return []

    return [scan_file(path, loader) for path in sorted(raw_dir.glob("*.pdf"))]


# ============================================================
# REGISTER
# ============================================================

def usable(entry):

    return entry["status"] in USABLE and bool(entry["shipment_key"])


def duplicate_groups(files):
    """
    Groups of files with identical content (same SHA-256), as lists of
    file names.
    """

    groups = {}

    for entry in files:
        groups.setdefault(entry["sha256"], []).append(entry["filename"])

    return sorted(sorted(names) for names in groups.values() if len(names) > 1)


def next_step(shipment_id, missing, present, unattributed):
    """
    What is needed to make an incomplete shipment auditable.
    """

    if not missing:
        return "All required documents are present."

    needs = ", ".join(
        {"BOL": "BOL", "INVOICE": "invoice", "POD": "POD"}[doc_type] for doc_type in missing
    )

    have = ", ".join(
        {"BOL": "BOL", "INVOICE": "invoice", "POD": "POD"}[doc_type] for doc_type in present
    )

    text = (
        f"Upload the {needs} for {shipment_id} (the documents must show "
        f"Shipment Number {shipment_id})."
    )

    if have:
        text += f" Already present: {have}."

    if unattributed:
        text += (
            f" {len(unattributed)} file(s) could not be used and have no shipment ID "
            f"({', '.join(unattributed[:3])}{'...' if len(unattributed) > 3 else ''}); "
            "if one of them is the missing document, replace it with a readable "
            "copy that shows the shipment number."
        )

    return text


def register(files, indexed_sources=None):
    """
    The completeness register of scan_folder() files.

    Returns {"shipments": [...], "unidentified": [...], "unusable":
    [...], "duplicates": [[names]]}. indexed_sources: the file names in
    the search index (rag/ChromaDB), when known; a shipment's
    indexed_documents then counts its documents found there, and a
    usable file that is not indexed is flagged.
    """

    files = list(files)
    indexed = None if indexed_sources is None else set(indexed_sources)

    unusable = [entry for entry in files if entry["status"] not in USABLE]
    unidentified = [
        entry for entry in files
        if entry["status"] in USABLE and not entry["shipment_key"]
    ]

    unattributed = [entry["filename"] for entry in unusable + unidentified]

    shipments = {}

    for entry in files:
        if usable(entry):
            shipments.setdefault(entry["shipment_key"], []).append(entry)

    duplicates = duplicate_groups(files)
    duplicated = {name for group in duplicates for name in group}

    rows = []

    for key in sorted(shipments):

        members = shipments[key]

        shipment_id = sorted({entry["shipment_id"] for entry in members}, key=lambda name: (len(name), name))[0]

        by_type = {doc_type: [e["filename"] for e in members if e["document_type"] == doc_type]
                   for doc_type in REQUIRED_TYPES}

        missing = [doc_type for doc_type in REQUIRED_TYPES if not by_type[doc_type]]
        present = [doc_type for doc_type in REQUIRED_TYPES if by_type[doc_type]]

        other = [e["filename"] for e in members if e["document_type"] not in REQUIRED_TYPES]

        warnings = []

        partial = [e["filename"] for e in members if e["status"] == PARTIAL]

        if partial:
            warnings.append("Partial extraction (pages without text): " + ", ".join(partial))

        for doc_type in REQUIRED_TYPES:
            if len(by_type[doc_type]) > 1:
                warnings.append(
                    f"{len(by_type[doc_type])} {doc_type} documents: " + ", ".join(by_type[doc_type])
                    + " (conflicting values need review in the audit)"
                )

        copies = sorted(e["filename"] for e in members if e["filename"] in duplicated)

        if copies:
            warnings.append("Identical file content: " + ", ".join(copies))

        if other:
            warnings.append(
                "Documents that are not a BOL, invoice or POD (not used by the audit): "
                + ", ".join(other)
            )

        not_indexed = (
            [e["filename"] for e in members if e["filename"] not in indexed] if indexed is not None else []
        )

        if not_indexed:
            warnings.append(
                "Not in the search index (chat cannot see them): " + ", ".join(not_indexed)
            )

        # The same files the audit holds against every shipment: their
        # shipment is unknown, so none can be ruled out
        if unusable:
            warnings.append(
                f"{len(unusable)} file(s) could not be used and cannot be ruled out as "
                "belonging to this shipment (the audit result is NEEDS REVIEW at best): "
                + ", ".join(f"{e['filename']} ({e['status']})" for e in unusable)
            )

        rows.append({
            "shipment_id": shipment_id,
            "shipment_key": key,
            "readiness": INCOMPLETE if missing else READY,
            "documents": len(members),
            "indexed_documents": (
                None if indexed is None else sum(e["filename"] in indexed for e in members)
            ),
            "files": [e["filename"] for e in members],
            "by_type": by_type,
            "missing": missing,
            "partial_extraction": bool(partial),
            "duplicate_files": bool(copies),
            "multiple_of_a_type": any(len(names) > 1 for names in by_type.values()),
            "warnings": warnings,
            "attention": bool(warnings),
            "next_step": next_step(shipment_id, missing, present, unattributed),
        })

    return {
        "shipments": rows,
        "unidentified": [
            {"filename": e["filename"], "document_type": e["document_type"],
             "message": "No shipment ID found in the document: it is not part of any shipment."}
            for e in unidentified
        ],
        "unusable": [
            {"filename": e["filename"], "status": e["status"],
             "message": STATUS_TEXT[e["status"]], "error": e["error"],
             "pages": e["total_pages"], "pages_with_text": e["text_pages"]}
            for e in unusable
        ],
        "duplicates": duplicates,
    }


def filter_shipments(
    rows,
    search="",
    incomplete_only=False,
    missing_types=(),
    extraction_issues=False,
    attention_only=False,
):
    """
    The register rows matching every filter given. search matches the
    shipment ID regardless of case, spaces and dashes; missing_types
    keeps shipments missing any of the given types; extraction_issues
    keeps shipments with a partially extracted document.
    """

    search_key = identifier_key(search) if search and search.strip() else None

    def matches(row):

        if search_key and search_key not in row["shipment_key"]:
            return False

        if incomplete_only and row["readiness"] != INCOMPLETE:
            return False

        if missing_types and not set(missing_types) & set(row["missing"]):
            return False

        if extraction_issues and not row["partial_extraction"]:
            return False

        if attention_only and not row["attention"]:
            return False

        return True

    return [row for row in rows if matches(row)]


def register_csv(rows):
    """
    The register rows given as CSV (UTF-8 with a byte-order mark,
    every cell formula-protected).
    """

    return write_csv(COLUMNS, [
        [
            cell(row["shipment_id"]), row["readiness"],
            "present" if row["by_type"]["BOL"] else "missing",
            "present" if row["by_type"]["INVOICE"] else "missing",
            "present" if row["by_type"]["POD"] else "missing",
            ", ".join(row["missing"]), str(row["documents"]),
            cell(row["indexed_documents"]), " | ".join(row["warnings"]), row["next_step"],
        ]
        for row in rows
    ])
