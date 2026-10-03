"""
Source Evidence Utilities

Phase 2:
Provides a standard structure for tracking the exact source document,
page, and text used as evidence for a RAG answer.
"""


def create_evidence(
    source,
    page,
    text,
    document_type=None,
    shipment_id=None,
    score=None,
):
    """
    Create a standardized evidence record.

    Parameters:
        source: PDF filename
        page: PDF page number
        text: Evidence text
        document_type: BOL / POD / INVOICE / etc.
        shipment_id: Shipment identifier
        score: Retrieval score, if available

    Returns:
        dict containing source evidence information.
    """

    evidence = {
        "source": source,
        "page": page,
        "text": text,
        "document_type": document_type,
        "shipment_id": shipment_id,
        "score": score,
    }

    return evidence


def format_evidence(evidence):
    """
    Convert an evidence dictionary into a readable format.
    """

    source = evidence.get("source", "Unknown source")
    page = evidence.get("page", "Unknown page")
    document_type = evidence.get("document_type", "Unknown")
    shipment_id = evidence.get("shipment_id")
    text = evidence.get("text", "").strip()

    lines = [
        f"Source: {source}",
        f"Page: {page}",
        f"Document Type: {document_type}",
    ]

    if shipment_id:
        lines.append(f"Shipment ID: {shipment_id}")

    if text:
        lines.append(f"Evidence: {text}")

    return "\n".join(lines)


def print_evidence(evidence):
    """
    Print evidence in a readable CLI format.
    """

    print("=" * 70)
    print("SOURCE EVIDENCE")
    print("=" * 70)
    print(format_evidence(evidence))
    print("=" * 70)


if __name__ == "__main__":

    # Simple Phase 2 test
    test_evidence = create_evidence(
        source="POD_SHJ-2026-001.pdf",
        page=1,
        text="Number of Pieces Delivered: 45",
        document_type="POD",
        shipment_id="SHJ-2026-001",
    )

    print_evidence(test_evidence)