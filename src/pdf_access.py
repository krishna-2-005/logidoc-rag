"""
LogiDoc-RAG: one PyMuPDF document at a time in this process.

PyMuPDF does not support multithreaded use (its own documentation; it sets
MuPDF up single-threaded when imported), and the API works on PDFs from
several threads: uploads in their worker thread, every other route in the
shared thread pool. Every place that opens a PyMuPDF document holds PDF_LOCK
for the document's whole life, from open to close:

    document_validation.validate_pdf     checking an upload
    document_loader.extract_pdf          reading the text to index
    document_extraction.extract_document structured extraction
    document_completeness.scan_file      the completeness register
    audit_pdf_export.audit_pdf           writing the PDF report

Measured on this project, threads gave PDF work no speed-up (600 parses:
2.8 s on one thread, 4.2 s on eight), so the lock costs next to nothing.

Lock order: hold PDF_LOCK only around PDF work and never acquire the
ingestion lock (ingestion_manager.INGESTION_LOCK) while holding it.
Ingestion takes PDF_LOCK inside its own lock, so the other order could
deadlock. It is re-entrant, so a PDF step inside another is safe.
"""

import threading


PDF_LOCK = threading.RLock()
