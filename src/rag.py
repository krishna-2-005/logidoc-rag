import sys
import re
from pathlib import Path

import chromadb
import ollama


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(
    0,
    str(PROJECT_ROOT)
)


# ============================================================
# CONFIG
# ============================================================

from config import (
    CHROMA_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    LLM_MODEL,
    TOP_K
)


# ============================================================
# LOGIDOC-RAG
# HYBRID RETRIEVAL + EXACT LOGISTICS EXTRACTION
# ============================================================


# ============================================================
# GET CHROMA COLLECTION
# ============================================================

def get_collection():

    client = chromadb.PersistentClient(
        path=str(CHROMA_DIR)
    )

    return client.get_collection(
        name=COLLECTION_NAME
    )


# ============================================================
# GET ALL DOCUMENTS
# ============================================================

def get_all_documents():

    collection = get_collection()

    data = collection.get(
        include=[
            "documents",
            "metadatas"
        ]
    )

    documents = data.get(
        "documents",
        []
    ) or []

    metadatas = data.get(
        "metadatas",
        []
    ) or []

    return documents, metadatas


# ============================================================
# NORMALIZE TEXT
# ============================================================

def normalize_text(text):

    if not text:
        return ""

    # Convert different line endings
    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    # Replace all whitespace/newlines with one space
    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ============================================================
# GET DOCUMENT TYPE
# ============================================================

def get_document_type(metadata, document):

    document_type = str(
        metadata.get(
            "document_type",
            ""
        )
    ).lower()

    if document_type:
        return document_type

    document_lower = document.lower()

    if "proof of delivery" in document_lower:
        return "pod"

    if "bill of lading" in document_lower:
        return "bol"

    if "invoice" in document_lower:
        return "invoice"

    return "unknown"


# ============================================================
# KEYWORD SCORE
# ============================================================

def keyword_score(
    question,
    document,
    metadata
):

    question_lower = question.lower()

    document_lower = document.lower()

    score = 0

    keywords = [
        "shipment",
        "shipment id",
        "pieces",
        "piece",
        "delivered",
        "delivery",
        "pod",
        "proof of delivery",
        "invoice",
        "amount",
        "total",
        "charge",
        "destination",
        "origin",
        "carrier",
        "weight",
        "damage",
        "status",
        "date",
        "received",
        "signature",
        "bol",
        "bill of lading"
    ]

    question_words = set(
        re.findall(
            r"\b[a-zA-Z0-9]+\b",
            question_lower
        )
    )

    document_words = set(
        re.findall(
            r"\b[a-zA-Z0-9]+\b",
            document_lower
        )
    )

    overlap = question_words.intersection(
        document_words
    )

    score += len(overlap) * 3

    for keyword in keywords:

        if (
            keyword in question_lower
            and keyword in document_lower
        ):
            score += 10

    document_type = str(
        metadata.get(
            "document_type",
            ""
        )
    ).lower()

    if (
        "pod" in question_lower
        and document_type == "pod"
    ):
        score += 50

    if (
        "invoice" in question_lower
        and document_type == "invoice"
    ):
        score += 50

    if (
        "bol" in question_lower
        and document_type == "bol"
    ):
        score += 50

    return score


# ============================================================
# HYBRID SEARCH
# ============================================================

def search_documents(question):

    collection = get_collection()

    # --------------------------------------------------------
    # VECTOR SEARCH
    # --------------------------------------------------------

    embedding_response = ollama.embeddings(
        model=EMBEDDING_MODEL,
        prompt=question
    )

    vector_results = collection.query(
        query_embeddings=[
            embedding_response["embedding"]
        ],
        n_results=max(
            TOP_K,
            5
        )
    )

    vector_documents = (
        vector_results.get(
            "documents",
            [[]]
        )[0]
        or []
    )

    vector_metadatas = (
        vector_results.get(
            "metadatas",
            [[]]
        )[0]
        or []
    )

    # --------------------------------------------------------
    # GET ALL DOCUMENTS
    # --------------------------------------------------------

    all_documents, all_metadatas = get_all_documents()

    scored_documents = []

    for document, metadata in zip(
        all_documents,
        all_metadatas
    ):

        score = keyword_score(
            question,
            document,
            metadata
        )

        scored_documents.append(
            (
                score,
                document,
                metadata
            )
        )

    # Highest keyword score first
    scored_documents.sort(
        key=lambda x: x[0],
        reverse=True
    )

    # --------------------------------------------------------
    # COMBINE RESULTS
    # --------------------------------------------------------

    combined = []

    seen = set()

    # First keyword results
    for (
        score,
        document,
        metadata
    ) in scored_documents:

        if score <= 0:
            continue

        key = (
            metadata.get(
                "source"
            ),
            metadata.get(
                "page"
            ),
            metadata.get(
                "chunk"
            )
        )

        if key not in seen:

            combined.append(
                {
                    "document": document,
                    "metadata": metadata,
                    "score": score
                }
            )

            seen.add(key)

    # Then vector results
    for (
        document,
        metadata
    ) in zip(
        vector_documents,
        vector_metadatas
    ):

        key = (
            metadata.get(
                "source"
            ),
            metadata.get(
                "page"
            ),
            metadata.get(
                "chunk"
            )
        )

        if key not in seen:

            combined.append(
                {
                    "document": document,
                    "metadata": metadata,
                    "score": 0
                }
            )

            seen.add(key)

    # Keep context manageable
    combined = combined[
        :max(
            TOP_K,
            5
        )
    ]

    documents = [
        item["document"]
        for item in combined
    ]

    metadatas = [
        item["metadata"]
        for item in combined
    ]

    return {
        "documents": [
            documents
        ],
        "metadatas": [
            metadatas
        ]
    }


# ============================================================
# EXACT LOGISTICS FIELD EXTRACTION
# ============================================================

def extract_exact_answer(
    question,
    results
):
    """
    Deterministic extraction for structured logistics fields.

    IMPORTANT:
    This searches ALL documents in ChromaDB instead of only
    the semantic-search results.

    Therefore:
        "How many pieces were delivered?"

    will still find:

        Number of Pieces Delivered: 48

    even if vector search ranks another document first.
    """

    question_lower = question.lower()

    # ========================================================
    # GET ALL INDEXED DOCUMENTS
    # ========================================================

    all_documents, all_metadatas = get_all_documents()

    if not all_documents:
        return None

    # ========================================================
    # BUILD DOCUMENT RECORDS
    # ========================================================

    records = []

    for document, metadata in zip(
        all_documents,
        all_metadatas
    ):

        normalized = normalize_text(
            document
        )

        document_type = get_document_type(
            metadata,
            document
        )

        # Line breaks kept, for fields whose value ends at a newline
        raw = document.replace(
            "\r\n",
            "\n"
        ).replace(
            "\r",
            "\n"
        )

        records.append(
            {
                "text": normalized,
                "raw": raw,
                "metadata": metadata,
                "type": document_type
            }
        )

    # ========================================================
    # DETECT REQUESTED DOCUMENT TYPE
    # ========================================================

    preferred_types = []

    # "delivered" / "delivery" questions are answered by the POD,
    # not the BOL (BOL holds shipped pieces and expected dates)
    if (
        "pod" in question_lower
        or "proof of delivery" in question_lower
        or "deliver" in question_lower
    ):
        preferred_types.append("pod")

    if "invoice" in question_lower:
        preferred_types.append("invoice")

    if (
        "bol" in question_lower
        or "bill of lading" in question_lower
    ):
        preferred_types.append("bol")

    # ========================================================
    # SORT DOCUMENTS
    # ========================================================

    if preferred_types:

        records.sort(
            key=lambda record:
            (
                0
                if record["type"]
                in preferred_types
                else 1
            )
        )

    # ========================================================
    # SHIPMENT ID FROM QUESTION
    # ========================================================

    shipment_match = re.search(
        r"\bSHJ-\d{4}-\d+\b",
        question,
        re.IGNORECASE
    )

    shipment_id = None

    if shipment_match:

        shipment_id = (
            shipment_match
            .group(0)
            .upper()
        )

    # ========================================================
    # FILTER BY SHIPMENT ID IF PROVIDED
    # ========================================================

    if shipment_id:

        shipment_records = []

        for record in records:

            metadata_shipment = str(
                record["metadata"].get(
                    "shipment_id",
                    ""
                )
            ).upper()

            if (
                shipment_id
                in record["text"].upper()
                or shipment_id
                == metadata_shipment
            ):

                shipment_records.append(
                    record
                )

        if shipment_records:

            records = shipment_records

    # ========================================================
    # 1. NUMBER OF PIECES DELIVERED
    # ========================================================

    if (
        "piece" in question_lower
        and (
            "how many" in question_lower
            or "number" in question_lower
            or "quantity" in question_lower
        )
    ):

        patterns = [

            r"Number\s+of\s+Pieces\s+Delivered\s*:\s*(\d+)",

            r"Number\s+of\s+Pieces\s*:\s*(\d+)",

            r"Pieces\s+Delivered\s*:\s*(\d+)",

            r"Pieces\s+Delivered\s+Count\s*:\s*(\d+)",

            r"Quantity\s+Delivered\s*:\s*(\d+)",

            r"Delivered\s+Pieces\s*:\s*(\d+)"
        ]

        for record in records:

            text = record["text"]

            for pattern in patterns:

                match = re.search(
                    pattern,
                    text,
                    re.IGNORECASE
                )

                if match:

                    number = match.group(1)

                    return (
                        f"{number} pieces were delivered."
                    )

    # ========================================================
    # 2. TOTAL INVOICE AMOUNT
    # ========================================================

    if (
        "invoice" in question_lower
        and (
            "total" in question_lower
            or "amount" in question_lower
        )
    ):

        patterns = [

            r"Total\s+Invoice\s+Amount\s*:\s*\$?\s*([\d,]+(?:\.\d{2})?)",

            r"Total\s+Amount\s*:\s*\$?\s*([\d,]+(?:\.\d{2})?)",

            r"Invoice\s+Total\s*:\s*\$?\s*([\d,]+(?:\.\d{2})?)"
        ]

        for record in records:

            text = record["text"]

            for pattern in patterns:

                match = re.search(
                    pattern,
                    text,
                    re.IGNORECASE
                )

                if match:

                    amount = match.group(1)

                    return (
                        f"The total invoice amount is "
                        f"${amount}."
                    )

    # ========================================================
    # 3. DELIVERY LOCATION
    # ========================================================

    if (
        "where" in question_lower
        and (
            "delivered" in question_lower
            or "delivery" in question_lower
        )
    ):

        # Company on the first line, city/state on the next
        patterns = [

            r"Delivery\s+Location\s*:\s*"
            r"([^\n]+?)[ \t]*\n\s*([^\n]+)"
        ]

        for record in records:

            text = record["raw"]

            for pattern in patterns:

                match = re.search(
                    pattern,
                    text,
                    re.IGNORECASE
                )

                if match:

                    company = (
                        match.group(1)
                        .strip()
                    )

                    location = (
                        match.group(2)
                        .strip()
                    )

                    return (
                        f"The shipment was delivered "
                        f"to {company} in {location}."
                    )

    # ========================================================
    # 4. DELIVERY STATUS
    # ========================================================

    if (
        "delivery status" in question_lower
        or "status of delivery" in question_lower
        or (
            "what" in question_lower
            and "status" in question_lower
        )
    ):

        pattern = (
            r"Delivery\s+Status\s*:\s*"
            r"([A-Za-z ]+)"
        )

        for record in records:

            match = re.search(
                pattern,
                record["raw"],
                re.IGNORECASE
            )

            if match:

                status = (
                    match.group(1)
                    .strip()
                )

                return (
                    f"The delivery status was "
                    f"{status}."
                )

    # ========================================================
    # 5. DELIVERY DATE
    # ========================================================

    if (
        "delivery date" in question_lower
        or "when was it delivered" in question_lower
        or "when was the shipment delivered" in question_lower
        or "when was shipment delivered" in question_lower
    ):

        pattern = (
            r"Delivery\s+Date\s*:\s*"
            r"([A-Za-z]+\s+\d{1,2},\s+\d{4})"
        )

        for record in records:

            match = re.search(
                pattern,
                record["text"],
                re.IGNORECASE
            )

            if match:

                date = (
                    match.group(1)
                    .strip()
                )

                return (
                    f"The delivery date was "
                    f"{date}."
                )

    # ========================================================
    # 6. CARRIER
    # ========================================================

    if (
        "carrier" in question_lower
        and (
            "who" in question_lower
            or "what" in question_lower
            or "carrier" in question_lower
        )
    ):

        pattern = (
            r"Carrier\s*:\s*"
            r"([^\n]+)"
        )

        for record in records:

            match = re.search(
                pattern,
                record["raw"],
                re.IGNORECASE
            )

            if match:

                carrier = (
                    match.group(1)
                    .strip()
                )

                return (
                    f"The carrier was "
                    f"{carrier}."
                )

    # ========================================================
    # 7. DAMAGE REPORT
    # ========================================================

    if (
        "damage" in question_lower
        and (
            "report" in question_lower
            or "damage" in question_lower
        )
    ):

        pattern = (
            r"Damage\s+Report\s*:\s*"
            r"(.+?)(?=\s+Receiver\s+Comments|\s+Signature|\s*$)"
        )

        for record in records:

            match = re.search(
                pattern,
                record["text"],
                re.IGNORECASE
            )

            if match:

                damage = (
                    match.group(1)
                    .strip()
                )

                return (
                    f"Damage report: {damage}"
                )

    # ========================================================
    # 8. RECEIVER COMMENTS
    # ========================================================

    if (
        "receiver" in question_lower
        and (
            "comment" in question_lower
            or "condition" in question_lower
        )
    ):

        pattern = (
            r"Receiver\s+Comments\s*:\s*"
            r"(.+?)(?=\s+Signature|\s*$)"
        )

        for record in records:

            match = re.search(
                pattern,
                record["text"],
                re.IGNORECASE
            )

            if match:

                comments = (
                    match.group(1)
                    .strip()
                )

                return (
                    f"Receiver comments: "
                    f"{comments}"
                )

    # ========================================================
    # 9. SIGNATURE
    # ========================================================

    if "signature" in question_lower:

        pattern = (
            r"Signature\s*:\s*"
            r"([A-Za-z .'-]+)"
        )

        for record in records:

            match = re.search(
                pattern,
                record["text"],
                re.IGNORECASE
            )

            if match:

                signature = (
                    match.group(1)
                    .strip()
                )

                return (
                    f"The shipment was signed by "
                    f"{signature}."
                )

    # ========================================================
    # NO EXACT ANSWER
    # ========================================================

    return None


# ============================================================
# GENERATE LLM ANSWER
# ============================================================

def generate_answer(
    question,
    results
):

    documents = (
        results.get(
            "documents",
            [[]]
        )[0]
        or []
    )

    metadatas = (
        results.get(
            "metadatas",
            [[]]
        )[0]
        or []
    )

    # --------------------------------------------------------
    # NO RETRIEVED DOCUMENTS
    # --------------------------------------------------------

    if not documents:

        return (
            "I could not find this information "
            "in the logistics documents."
        )

    # --------------------------------------------------------
    # FIRST: EXACT EXTRACTION
    # --------------------------------------------------------

    exact_answer = extract_exact_answer(
        question,
        results
    )

    if exact_answer:

        return exact_answer

    # --------------------------------------------------------
    # BUILD LLM CONTEXT
    # --------------------------------------------------------

    context_parts = []

    for document, metadata in zip(
        documents,
        metadatas
    ):

        context_parts.append(
            f"""
SOURCE: {metadata.get('source', 'Unknown')}
DOCUMENT TYPE: {metadata.get('document_type', 'Unknown')}
SHIPMENT ID: {metadata.get('shipment_id', 'Unknown')}
PAGE: {metadata.get('page', 'Unknown')}
CHUNK: {metadata.get('chunk', 'Unknown')}

DOCUMENT CONTENT:
{document}
"""
        )

    context = "\n".join(
        context_parts
    )

    # --------------------------------------------------------
    # LLM PROMPT
    # --------------------------------------------------------

    prompt = f"""
You are LOGIDOC-RAG, a logistics document assistant.

You MUST answer using ONLY the provided logistics document
context.

STRICT RULES:

1. Do NOT use outside knowledge.
2. Do NOT invent information.
3. Do NOT guess.
4. Do NOT assume information.
5. Carefully read all provided context.
6. If the answer is explicitly present, answer it directly.
7. If the answer is not present, say exactly:

I could not find this information in the logistics documents.

8. Do not calculate or infer a value unless the calculation
   is directly supported by the provided documents.
9. Keep the answer concise and professional.

LOGISTICS DOCUMENT CONTEXT:

{context}

USER QUESTION:

{question}

ANSWER:
"""

    response = ollama.chat(
        model=LLM_MODEL,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    answer = (
        response["message"]["content"]
        .strip()
    )

    return answer


# ============================================================
# ASK QUESTION
# ============================================================

def ask_question(question):

    results = search_documents(
        question
    )

    answer = generate_answer(
        question,
        results
    )

    return (
        answer,
        results
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("LOGIDOC-RAG")
    print("=" * 70)

    question = input(
        "\nAsk a logistics question: "
    ).strip()

    if not question:

        print(
            "Please enter a question."
        )

        return

    print(
        "\nSearching logistics documents...\n"
    )

    answer, results = ask_question(
        question
    )

    # ========================================================
    # ANSWER
    # ========================================================

    print(
        "=" * 70
    )

    print(
        "ANSWER"
    )

    print(
        "=" * 70
    )

    print(
        answer
    )

    # ========================================================
    # SOURCES
    # ========================================================

    print(
        "\n" + "=" * 70
    )

    print(
        "SOURCES"
    )

    print(
        "=" * 70
    )

    metadatas = (
        results.get(
            "metadatas",
            [[]]
        )[0]
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

            print(
                f"- {source} "
                f"(Page {page}, "
                f"Type: {document_type})"
            )

            seen_sources.add(
                key
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()