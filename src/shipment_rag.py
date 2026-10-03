from pathlib import Path
import sys
import ollama

# Add project root to Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import LLM_MODEL
from shipment_search import search_shipment


# ============================================================
# Build Context
# ============================================================

def build_context(results):
    """
    Convert retrieved shipment documents into
    a structured context for the LLM.
    """

    context_parts = []

    for index, result in enumerate(
        results,
        start=1
    ):

        metadata = result.get(
            "metadata",
            {}
        )

        source = metadata.get(
            "source",
            "Unknown"
        )

        document_type = metadata.get(
            "document_type",
            "Unknown"
        )

        page = metadata.get(
            "page",
            "Unknown"
        )

        text = result.get(
            "text",
            ""
        )

        context_parts.append(
            f"""
SOURCE {index}
Document: {source}
Document Type: {document_type}
Page: {page}

{text}
"""
        )

    return "\n".join(
        context_parts
    )


# ============================================================
# Generate Answer
# ============================================================

def generate_shipment_answer(
    shipment_identifier,
    question
):
    """
    Answer a logistics question using only
    documents belonging to the selected shipment.
    """

    # --------------------------------------------------------
    # Retrieve shipment-specific documents
    # --------------------------------------------------------

    search_result = search_shipment(
        shipment_identifier,
        question
    )

    shipment_id = search_result.get(
        "shipment_id"
    )

    results = search_result.get(
        "results",
        []
    )

    # --------------------------------------------------------
    # Shipment not found
    # --------------------------------------------------------

    if not shipment_id:

        return {
            "shipment_id": None,
            "answer": (
                "I could not find a shipment matching "
                f"'{shipment_identifier}'."
            ),
            "sources": []
        }

    # --------------------------------------------------------
    # No documents found
    # --------------------------------------------------------

    if not results:

        return {
            "shipment_id": shipment_id,
            "answer": (
                "No relevant documents were found "
                "for this shipment."
            ),
            "sources": []
        }

    # --------------------------------------------------------
    # Build context
    # --------------------------------------------------------

    context = build_context(
        results
    )

    # --------------------------------------------------------
    # Strict logistics prompt
    # --------------------------------------------------------

    prompt = f"""
You are a logistics document assistant.

You are answering a question about ONE specific shipment.

Shipment ID:
{shipment_id}

User Question:
{question}

Use ONLY the information provided in the shipment
documents below.

Do not use outside knowledge.

Do not invent information.

Do not guess.

Do not assume that two fields are the same unless
the documents explicitly support it.

If multiple documents contain different values,
clearly mention the difference and identify the
document where each value came from.

For quantities, preserve the exact terminology
used by the document.

For example:
- "Number of Pieces" is different from
  "Number of Pieces Delivered".
- Do not silently treat them as the same value.

Give a direct and concise answer.

SHIPMENT DOCUMENTS
==================

{context}

==================

Answer the user's question using only the
shipment documents above.
"""

    # --------------------------------------------------------
    # Call Ollama
    # --------------------------------------------------------

    response = ollama.chat(
        model=LLM_MODEL,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    answer = response[
        "message"
    ][
        "content"
    ]

    # --------------------------------------------------------
    # Prepare source information
    # --------------------------------------------------------

    sources = []

    for result in results:

        metadata = result.get(
            "metadata",
            {}
        )

        sources.append(
            {
                "source": metadata.get(
                    "source"
                ),
                "document_type": metadata.get(
                    "document_type"
                ),
                "page": metadata.get(
                    "page"
                )
            }
        )

    return {
        "shipment_id": shipment_id,
        "answer": answer,
        "sources": sources
    }


# ============================================================
# Display Answer
# ============================================================

def print_shipment_answer(
    shipment_identifier,
    question
):
    """
    Run shipment-aware RAG and display
    the final answer with sources.
    """

    print("\n" + "=" * 70)
    print("SHIPMENT-AWARE RAG")
    print("=" * 70)

    print(
        f"\nRequested identifier: "
        f"{shipment_identifier}"
    )

    print(
        f"Question: "
        f"{question}"
    )

    result = generate_shipment_answer(
        shipment_identifier,
        question
    )

    print(
        f"\nResolved shipment: "
        f"{result['shipment_id']}"
    )

    print("\nANSWER")
    print("-" * 70)

    print(
        result["answer"]
    )

    print("\nSOURCES")
    print("-" * 70)

    if result["sources"]:

        for index, source in enumerate(
            result["sources"],
            start=1
        ):

            print(
                f"{index}. "
                f"{source['source']} "
                f"-> "
                f"{source['document_type']} "
                f"(Page {source['page']})"
            )

    else:

        print(
            "No sources available."
        )

    print("\n" + "=" * 70)
    print(
        "SHIPMENT-AWARE RAG COMPLETED"
    )
    print("=" * 70)


# ============================================================
# Tests
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # Test 1
    # Shipment ID + delivery quantity
    # --------------------------------------------------------

    print_shipment_answer(
        "SHJ-2026-001",
        "How many pieces were delivered?"
    )

    # --------------------------------------------------------
    # Test 2
    # Invoice number + invoice amount
    # --------------------------------------------------------

    print_shipment_answer(
        "INV-SHJ-2026-001",
        "What is the total invoice amount?"
    )