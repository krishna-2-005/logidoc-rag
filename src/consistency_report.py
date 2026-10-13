"""
LogiDoc-RAG Consistency Report

Read-only check of a destination: the raw PDF folder, the ChromaDB
index and the ingestion journal (ingestion_journal). It never
deletes, repairs, reindexes or overwrites anything, and it does not
create the index or the collection if they are missing.

Note: opening an existing ChromaDB index lets ChromaDB rewrite some
of its own internal files, even for reads. The stored vectors,
documents and metadata are not changed.

Each issue is a dict:

    type          one of the ISSUE_* constants below
    severity      "error" | "warning" | "info"
    certainty     "confirmed" | "uncertain"
    file          PDF filename or vector source, if any
    sha256        content hash, when known
    vector_ids    affected vector IDs
    operation_id  related journal operation, if any
    explanation   plain-language description
    details       extra structured data

A missing journal record never counts as proof that nothing
happened: vectors from a full rebuild, or from before the journal
existed, have no records.

Run from the project root (read-only):
    python src/consistency_report.py
    python src/consistency_report.py --raw-dir <folder> --chroma-dir <folder>
"""

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import chromadb


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import CHROMA_DIR, COLLECTION_NAME, RAW_DATA_DIR

from ingestion_journal import (
    EVENT_ROLLBACK_FAILED,
    read_journal,
    summarize_operations,
)
from ingestion_manager import (
    HASH_METADATA_KEY,
    INDEX_DIFFERENT,
    INDEX_MISSING,
    INDEX_STATUS_TEXT,
    INDEX_UNVERIFIED,
    INDEX_VERIFIED,
    calculate_sha256,
    index_status,
)


# ============================================================
# ISSUE TYPES
# ============================================================

ISSUE_INTERRUPTED = "interrupted_operation"
ISSUE_ROLLBACK_FAILED = "rollback_failed"
ISSUE_ORPHAN_VECTORS = "orphan_vectors"
ISSUE_PDF_NOT_INDEXED = "pdf_not_indexed"
ISSUE_CONTENT_MISMATCH = "content_mismatch"
ISSUE_DUPLICATE_CONTENT = "duplicate_content"
ISSUE_DUPLICATE_VECTOR_SETS = "duplicate_vector_sets"
ISSUE_PARTIAL_FILE = "leftover_partial_file"
ISSUE_UNVERIFIED = "unverified_vectors"
ISSUE_UNVERIFIED_WITHOUT_PDF = "unverified_vectors_without_pdf"
ISSUE_JOURNAL_LINE = "journal_unreadable_line"
ISSUE_UNREADABLE_FILE = "unreadable_file"
ISSUE_INDEX_UNAVAILABLE = "index_unavailable"
ISSUE_RAW_FOLDER_MISSING = "raw_folder_missing"

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# Temporary copies written by ingestion_manager.store_file
PART_PATTERN = re.compile(
    r"^\.(?P<original>.+)\.(?P<token>[0-9a-f]{32})\.part$"
)


# ============================================================
# READING (no writes)
# ============================================================

def make_issue(
    issue_type,
    severity,
    certainty,
    explanation,
    file=None,
    sha256=None,
    vector_ids=(),
    operation_id=None,
    **details
):

    return {
        "type": issue_type,
        "severity": severity,
        "certainty": certainty,
        "file": file,
        "sha256": sha256,
        "vector_ids": sorted(vector_ids),
        "operation_id": operation_id,
        "explanation": explanation,
        "details": details,
    }


def read_raw_folder(raw_dir, issues):
    """
    Hash every PDF and leftover .part file in the raw folder.
    """

    pdfs = []
    parts = []

    if not raw_dir.is_dir():

        issues.append(
            make_issue(
                ISSUE_RAW_FOLDER_MISSING,
                "info",
                "confirmed",
                f"The raw folder {raw_dir} does not exist.",
            )
        )

        return pdfs, parts

    pdf_paths = sorted(
        raw_dir.glob("*.pdf"),
        key=lambda path: path.name.casefold()
    )

    part_paths = sorted(
        (
            path
            for path in raw_dir.iterdir()
            if path.is_file() and PART_PATTERN.match(path.name)
        ),
        key=lambda path: path.name.casefold()
    )

    for paths, target in ((pdf_paths, pdfs), (part_paths, parts)):

        for path in paths:

            try:

                target.append(
                    {
                        "name": path.name,
                        "sha256": calculate_sha256(path),
                        "size": path.stat().st_size,
                    }
                )

            except Exception as error:

                issues.append(
                    make_issue(
                        ISSUE_UNREADABLE_FILE,
                        "error",
                        "confirmed",
                        f"{path.name} could not be read: {error}",
                        file=path.name,
                    )
                )

    return pdfs, parts


def read_index(chroma_dir, collection_name, issues):
    """
    Read every vector's ID, source, content hash and text.

    Returns None if there is no index; it is never created.
    """

    if not (Path(chroma_dir) / "chroma.sqlite3").is_file():

        issues.append(
            make_issue(
                ISSUE_INDEX_UNAVAILABLE,
                "warning",
                "confirmed",
                f"No ChromaDB index was found in {chroma_dir}. "
                "Index-related checks were skipped.",
            )
        )

        return None

    try:

        collection = chromadb.PersistentClient(
            path=str(chroma_dir)
        ).get_collection(
            name=collection_name
        )

        data = collection.get(
            include=["metadatas", "documents"]
        )

    except Exception as error:

        issues.append(
            make_issue(
                ISSUE_INDEX_UNAVAILABLE,
                "warning",
                "confirmed",
                f"The collection '{collection_name}' could not be "
                f"read: {error}. Index-related checks were skipped.",
            )
        )

        return None

    vectors = []

    for vector_id, metadata, document in zip(
        data["ids"],
        data["metadatas"],
        data["documents"],
    ):

        metadata = metadata or {}

        vectors.append(
            {
                "id": vector_id,
                "source": str(metadata.get("source") or ""),
                "sha256": metadata.get(HASH_METADATA_KEY) or None,
                "document": document or "",
            }
        )

    return vectors


# ============================================================
# REPORT
# ============================================================

def check_destination(
    raw_dir,
    chroma_dir,
    collection_name=COLLECTION_NAME
):
    """
    Build the read-only consistency report for a destination.
    """

    raw_dir = Path(raw_dir)
    chroma_dir = Path(chroma_dir)

    issues = []

    pdfs, parts = read_raw_folder(raw_dir, issues)
    journal = read_journal(raw_dir)
    vectors = read_index(chroma_dir, collection_name, issues)

    index_available = vectors is not None
    vectors = vectors or []

    # --------------------------------------------------------
    # Lookups
    # --------------------------------------------------------

    raw_by_name = {
        pdf["name"].casefold(): pdf
        for pdf in pdfs
    }

    raw_by_hash = {}

    for pdf in pdfs:
        raw_by_hash.setdefault(pdf["sha256"], []).append(pdf["name"])

    sources = {}

    for vector in vectors:

        entry = sources.setdefault(
            vector["source"].casefold(),
            {
                "source": vector["source"],
                "ids": [],
                "hashes": set(),
                "documents": [],
            }
        )

        entry["ids"].append(vector["id"])
        entry["hashes"].add(vector["sha256"])
        entry["documents"].append(vector["document"])

    hashed = {}

    for vector in vectors:

        if vector["sha256"]:

            entry = hashed.setdefault(
                vector["sha256"],
                {"ids": [], "sources": set()}
            )

            entry["ids"].append(vector["id"])
            entry["sources"].add(vector["source"])

    existing_ids = {vector["id"] for vector in vectors}

    # Same shape as ingestion_manager.scan_destination, so both
    # modules judge "indexed" with the same index_status()
    state = {
        "indexed_sources": {
            key: entry["hashes"]
            for key, entry in sources.items()
        }
    }

    # --------------------------------------------------------
    # Journal
    # --------------------------------------------------------

    for problem in journal["problems"]:

        issues.append(
            make_issue(
                ISSUE_JOURNAL_LINE,
                "warning",
                "confirmed",
                f"Journal line {problem['line']} could not be read "
                f"({problem['reason']}) and was ignored. If it was an "
                "operation record, that operation cannot be checked.",
                text=problem["text"],
            )
        )

    explained_hashes = set()

    for operation_id, operation in summarize_operations(
        journal["records"]
    ).items():

        begin = operation["begin"]
        end = operation["end"]

        if not begin:
            continue

        planned = list(begin.get("vector_ids") or [])
        content_hash = begin.get("sha256")
        filename = begin.get("filename")

        if end and end["event"] != EVENT_ROLLBACK_FAILED:
            continue

        if not index_available:

            issues.append(
                make_issue(
                    ISSUE_INTERRUPTED if not end else ISSUE_ROLLBACK_FAILED,
                    "warning",
                    "uncertain",
                    f"Operation {operation_id} for {filename} did not "
                    "finish cleanly, and the index could not be read "
                    "to check what remains.",
                    file=filename,
                    sha256=content_hash,
                    operation_id=operation_id,
                )
            )

            continue

        remaining = [
            vector_id
            for vector_id in planned
            if vector_id in existing_ids
        ]

        if remaining:
            explained_hashes.add(content_hash)

        if end:

            issues.append(
                make_issue(
                    ISSUE_ROLLBACK_FAILED,
                    "error" if remaining else "info",
                    "confirmed",
                    (
                        f"Ingesting {filename} failed and the rollback "
                        f"also failed; {len(remaining)} of its "
                        f"{len(planned)} vector(s) are still in the "
                        "index without a stored PDF."
                        if remaining
                        else
                        f"Ingesting {filename} failed and the rollback "
                        "reported an error, but none of its vectors "
                        "are in the index any more."
                    ),
                    file=filename,
                    sha256=content_hash,
                    vector_ids=remaining,
                    operation_id=operation_id,
                    rollback_error=end.get("error"),
                )
            )

            continue

        pdf_present = content_hash in raw_by_hash

        complete_copies = [
            part["name"]
            for part in parts
            if part["sha256"] == content_hash
        ]

        if remaining and not pdf_present:

            state_name = "orphan_vectors"
            severity = "error"
            certainty = "confirmed"
            text = (
                f"The operation wrote {len(remaining)} vector(s) for "
                f"{filename} but stopped before the PDF was stored, "
                "so these vectors have no PDF."
            )

        elif remaining and len(remaining) < len(planned):

            state_name = "partial_vectors"
            severity = "error"
            certainty = "confirmed"
            text = (
                f"Only {len(remaining)} of {len(planned)} vector(s) "
                f"for {filename} are in the index."
            )

        elif remaining:

            state_name = "completed_without_commit"
            severity = "warning"
            certainty = "uncertain"
            text = (
                f"All vectors and the PDF for {filename} are present "
                "and match, so the operation probably completed, but "
                "its commit record is missing (the process stopped "
                "right after storing the PDF)."
            )

        elif pdf_present:

            state_name = "pdf_without_vectors"
            severity = "error"
            certainty = "confirmed"
            text = (
                f"The PDF for {filename} is stored but none of the "
                "operation's vectors are in the index."
            )

        else:

            state_name = "nothing_remaining"
            severity = "info"
            certainty = "confirmed"
            text = (
                f"The operation for {filename} stopped before "
                "anything was kept: neither its vectors nor its PDF "
                "are present."
            )

        if complete_copies:
            text += (
                " A complete copy of the PDF was left as "
                f"{', '.join(complete_copies)}."
            )

        issues.append(
            make_issue(
                ISSUE_INTERRUPTED,
                severity,
                certainty,
                f"Operation {operation_id} did not finish. {text}",
                file=filename,
                sha256=content_hash,
                vector_ids=remaining,
                operation_id=operation_id,
                state=state_name,
                planned_vectors=len(planned),
                started=begin.get("time"),
            )
        )

    # --------------------------------------------------------
    # Vectors with a content hash but no matching PDF
    # --------------------------------------------------------

    for content_hash, entry in sorted(hashed.items()):

        if (
            content_hash in raw_by_hash
            or content_hash in explained_hashes
        ):
            continue

        # A PDF with that name but other content is reported
        # as a content mismatch instead
        if any(
            source.casefold() in raw_by_name
            for source in entry["sources"]
        ):
            continue

        source_names = ", ".join(sorted(entry["sources"]))

        issues.append(
            make_issue(
                ISSUE_ORPHAN_VECTORS,
                "error",
                "confirmed",
                f"{len(entry['ids'])} vector(s) for {source_names} "
                "have no PDF with that content in the raw folder. The "
                "journal has no unfinished operation for them, so the "
                "cause is unknown: the PDF may have been removed after "
                "indexing, or the vectors predate the journal.",
                file=source_names,
                sha256=content_hash,
                vector_ids=entry["ids"],
                cause="unknown",
            )
        )

    # --------------------------------------------------------
    # Raw PDFs against the index
    # --------------------------------------------------------

    verified = 0
    unverified = 0

    for pdf in pdfs if index_available else []:

        status = index_status(pdf["name"], pdf["sha256"], state)

        entry = sources.get(pdf["name"].casefold(), {})

        if status == INDEX_VERIFIED:

            verified += 1

        elif status == INDEX_UNVERIFIED:

            unverified += 1

            issues.append(
                make_issue(
                    ISSUE_UNVERIFIED,
                    "info",
                    "uncertain",
                    f"The vectors for {pdf['name']} come from a full "
                    "rebuild and record no content hash. They are "
                    "linked to the PDF by filename only, so it cannot "
                    "be confirmed that they match its current content.",
                    file=pdf["name"],
                    sha256=pdf["sha256"],
                    vector_ids=entry["ids"],
                )
            )

        elif status == INDEX_MISSING:

            elsewhere = sorted(
                hashed.get(pdf["sha256"], {}).get("sources", ())
            )

            issues.append(
                make_issue(
                    ISSUE_PDF_NOT_INDEXED,
                    "error",
                    "confirmed",
                    f"{pdf['name']} is in the raw folder but has no "
                    "vectors in the index."
                    + (
                        f" The same content is indexed as "
                        f"{', '.join(elsewhere)}."
                        if elsewhere
                        else ""
                    ),
                    file=pdf["name"],
                    sha256=pdf["sha256"],
                    indexed_as=elsewhere,
                )
            )

        elif status == INDEX_DIFFERENT:

            issues.append(
                make_issue(
                    ISSUE_CONTENT_MISMATCH,
                    "error",
                    "confirmed",
                    f"The vectors indexed for {pdf['name']} were made "
                    "from different content than the PDF now in the "
                    "raw folder.",
                    file=pdf["name"],
                    sha256=pdf["sha256"],
                    vector_ids=entry["ids"],
                    indexed_hashes=sorted(
                        h for h in entry["hashes"] if h
                    ),
                )
            )

    # --------------------------------------------------------
    # The same content under several filenames
    # --------------------------------------------------------

    for content_hash, names in sorted(raw_by_hash.items()):

        if len(names) < 2:
            continue

        statuses = [
            {
                "filename": name,
                "index_status": (
                    index_status(name, content_hash, state)
                    if index_available
                    else "unknown"
                ),
            }
            for name in names
        ]

        issues.append(
            make_issue(
                ISSUE_DUPLICATE_CONTENT,
                "warning",
                "confirmed",
                f"The same content is stored under {len(names)} "
                "filenames: "
                + ", ".join(
                    f"{item['filename']} "
                    f"({INDEX_STATUS_TEXT.get(item['index_status'], item['index_status'])})"
                    for item in statuses
                )
                + ".",
                file=", ".join(names),
                sha256=content_hash,
                files=statuses,
            )
        )

    # --------------------------------------------------------
    # Duplicate vector sets (identical extracted text under
    # several sources, e.g. from a full rebuild)
    # --------------------------------------------------------

    signatures = {}

    for entry in sources.values():

        signature = hashlib.sha256(
            "\x00".join(sorted(entry["documents"])).encode("utf-8")
        ).hexdigest()

        signatures.setdefault(signature, []).append(entry)

    for entries in signatures.values():

        if len(entries) < 2:
            continue

        names = sorted(
            (entry["source"] for entry in entries),
            key=str.casefold
        )

        file_hashes = {
            raw_by_name.get(name.casefold(), {}).get("sha256")
            for name in names
        }

        same_files = None not in file_hashes and len(file_hashes) == 1

        issues.append(
            make_issue(
                ISSUE_DUPLICATE_VECTOR_SETS,
                "warning",
                "confirmed" if same_files else "uncertain",
                f"{len(names)} sources have identical indexed text, so "
                "the index holds a separate vector set for each: "
                f"{', '.join(names)}."
                + (
                    " Their PDFs are byte-for-byte identical."
                    if same_files
                    else " Their PDFs could not all be compared, so "
                    "they may be different files with the same text."
                ),
                file=", ".join(names),
                vector_ids=[
                    vector_id
                    for entry in entries
                    for vector_id in entry["ids"]
                ],
                sources=names,
            )
        )

    # --------------------------------------------------------
    # Full-rebuild vectors without their PDF
    # --------------------------------------------------------

    for key, entry in sorted(sources.items()):

        if entry["hashes"] != {None} or key in raw_by_name:
            continue

        issues.append(
            make_issue(
                ISSUE_UNVERIFIED_WITHOUT_PDF,
                "warning",
                "uncertain",
                f"The index has {len(entry['ids'])} vector(s) for "
                f"{entry['source']} from a full rebuild, but the PDF "
                "is not in the raw folder. They record no content "
                "hash, so their content and origin cannot be checked.",
                file=entry["source"],
                vector_ids=entry["ids"],
            )
        )

    # --------------------------------------------------------
    # Leftover partial copies
    # --------------------------------------------------------

    interrupted_hashes = {
        issue["sha256"]: issue["operation_id"]
        for issue in issues
        if issue["type"] == ISSUE_INTERRUPTED
    }

    for part in parts:

        original = PART_PATTERN.match(part["name"]).group("original")

        operation_id = interrupted_hashes.get(part["sha256"])

        issues.append(
            make_issue(
                ISSUE_PARTIAL_FILE,
                "warning",
                "confirmed" if operation_id else "uncertain",
                f"{part['name']} is a leftover temporary copy of "
                f"{original} ({part['size']} bytes) from an "
                "interrupted storage step. "
                + (
                    f"It is a complete copy from operation "
                    f"{operation_id}."
                    if operation_id
                    else
                    "No unfinished operation matches its content, so "
                    "it may be incomplete."
                ),
                file=part["name"],
                sha256=part["sha256"],
                operation_id=operation_id,
                original=original,
            )
        )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    issues.sort(
        key=lambda issue: (
            SEVERITY_ORDER[issue["severity"]],
            issue["type"],
            str(issue["file"]).casefold(),
        )
    )

    def count(field, value):

        return sum(
            1
            for issue in issues
            if issue[field] == value
        )

    summary = {
        "raw_pdfs": len(pdfs),
        "partial_files": len(parts),
        "index_available": index_available,
        "vectors": len(vectors),
        "indexed_sources": len(sources),
        "verified_documents": verified,
        "unverified_documents": unverified,
        "journal_records": len(journal["records"]),
        "journal_unreadable_lines": len(journal["problems"]),
        "unfinished_operations": count("type", ISSUE_INTERRUPTED),
        "errors": count("severity", "error"),
        "warnings": count("severity", "warning"),
        "info": count("severity", "info"),
        "uncertain": count("certainty", "uncertain"),
    }

    summary["healthy"] = (
        summary["errors"] == 0
        and summary["warnings"] == 0
    )

    return {
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "destination": {
            "raw_dir": str(raw_dir),
            "chroma_dir": str(chroma_dir),
            "collection": collection_name,
            "journal": journal["path"],
        },
        "summary": summary,
        "issues": issues,
    }


# ============================================================
# CONSOLE REPORT
# ============================================================

def print_consistency_report(report):

    summary = report["summary"]

    print("=" * 70)
    print("CONSISTENCY REPORT (read-only)")
    print("=" * 70)

    print(f"Raw folder      : {report['destination']['raw_dir']}")
    print(f"Index folder    : {report['destination']['chroma_dir']}")
    print(f"Raw PDFs        : {summary['raw_pdfs']}")
    print(f"Vectors         : {summary['vectors']}")
    print(f"Verified docs   : {summary['verified_documents']}")
    print(f"Unverified docs : {summary['unverified_documents']}")
    print(f"Journal records : {summary['journal_records']}")
    print(
        f"Issues          : {summary['errors']} error(s), "
        f"{summary['warnings']} warning(s), {summary['info']} info"
    )

    for issue in report["issues"]:

        print(
            f"- [{issue['severity'].upper()}, {issue['certainty']}] "
            f"{issue['type']}: {issue['explanation']}"
        )

    print("=" * 70)


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description=(
            "Read-only consistency check of a LogiDoc-RAG "
            "destination. Nothing is changed."
        )
    )

    parser.add_argument("--raw-dir", default=str(RAW_DATA_DIR))
    parser.add_argument("--chroma-dir", default=str(CHROMA_DIR))

    arguments = parser.parse_args()

    consistency_report = check_destination(
        arguments.raw_dir,
        arguments.chroma_dir
    )

    print_consistency_report(consistency_report)

    sys.exit(0 if consistency_report["summary"]["healthy"] else 1)
