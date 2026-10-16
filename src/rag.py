import sys
import re
import time
from pathlib import Path


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# THIRD-PARTY IMPORTS
# ============================================================

import chromadb
import ollama

from source_evidence import (
    create_evidence,
    format_evidence,
)

from rag_grounding import (
    ground_answer,
    is_abstention,
)

from rag_scope import (
    SCOPE_ABSTAINED,
    SCOPE_CLARIFICATION,
    SCOPE_OK,
    group_records_by_shipment,
    records_in_scope,
    resolve_scope,
)

from shipment_identity import identifier_key

from shipment_calculations import (
    ANSWERED as CALCULATION_ANSWERED,
    calculate,
)


# ============================================================
# ANSWER STATUS
# ============================================================

STATUS_ANSWERED = "answered"
STATUS_ABSTAINED = SCOPE_ABSTAINED
STATUS_CLARIFICATION = SCOPE_CLARIFICATION

NOT_FOUND_MESSAGE = (
    "I could not find this information in the logistics documents."
)

UNVERIFIED_MESSAGE = (
    "I could not verify an answer to this question in the documents "
    "in scope, so I am not answering it."
)

LLM_UNAVAILABLE_MESSAGE = (
    "I could not generate an answer because the language model is "
    "not available."
)


# ============================================================
# CONFIG
# ============================================================

from config import (
    CHROMA_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    LLM_MODEL,
    TOP_K,
)


# ============================================================
# LOGIDOC-RAG
#
# Hybrid Retrieval
# Exact Logistics Extraction
# Source Evidence
# Shipment-Aware Answers
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
# MODEL CALLS
#
# Kept as separate functions so tests and the offline evaluation
# can replace them without Ollama.
# ============================================================

def embed_query(text):

    return ollama.embeddings(
        model=EMBEDDING_MODEL,
        prompt=text
    )["embedding"]


def call_llm(prompt):

    response = ollama.chat(
        model=LLM_MODEL,
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ]
    )

    return (
        response
        .get("message", {})
        .get("content", "")
        .strip()
    )


# ============================================================
# GET ALL DOCUMENTS
# ============================================================

def get_all_documents():

    collection = get_collection()

    data = collection.get(
        include=[
            "documents",
            "metadatas",
        ]
    )

    documents = (
        data.get(
            "documents",
            []
        )
        or []
    )

    metadatas = (
        data.get(
            "metadatas",
            []
        )
        or []
    )

    return documents, metadatas


# ============================================================
# NORMALIZE TEXT
# ============================================================

def normalize_text(text):

    if not text:
        return ""

    text = str(text)

    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ============================================================
# RAW TEXT NORMALIZATION
# ============================================================

def normalize_raw_text(text):

    if not text:
        return ""

    return (
        str(text)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


# ============================================================
# DOCUMENT TYPE
# ============================================================

def get_document_type(
    metadata,
    document
):

    document_type = str(
        metadata.get(
            "document_type",
            ""
        )
    ).strip().lower()

    if document_type:
        return document_type

    document_lower = (
        document or ""
    ).lower()

    if (
        "proof of delivery"
        in document_lower
    ):
        return "pod"

    if (
        "bill of lading"
        in document_lower
    ):
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

    question_lower = (
        question or ""
    ).lower()

    document_lower = (
        document or ""
    ).lower()

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
        "bill of lading",
        "expected",
        "actual",
        "on time",
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

    overlap = (
        question_words
        .intersection(
            document_words
        )
    )

    score += (
        len(overlap) * 3
    )

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

    if (
        (
            "deliver" in question_lower
            or "delivery" in question_lower
            or "on time" in question_lower
        )
        and document_type == "pod"
    ):
        score += 25

    if (
        "expected" in question_lower
        and document_type == "bol"
    ):
        score += 25

    return score


# ============================================================
# HYBRID SEARCH
# ============================================================

def search_documents(
    question,
    scope=None,
    records=None
):
    """
    Hybrid keyword + vector search limited to the question's scope.

    scope: from rag_scope.resolve_scope(); None searches every chunk.
    records: the records the scope allows (rag_scope.records_in_scope);
    loaded unscoped when omitted.
    """

    started = time.perf_counter()

    if records is None:
        records = get_document_records()

    allowed_keys = (
        set(scope["keys"])
        if scope and scope.get("keys")
        else None
    )

    # --------------------------------------------------------
    # VECTOR SEARCH (metadata filter, then re-checked)
    # --------------------------------------------------------

    vector_documents = []
    vector_metadatas = []

    if records:

        collection = get_collection()

        query = {
            "query_embeddings": [
                embed_query(question)
            ],
            "n_results": max(
                1,
                min(
                    max(TOP_K, 5),
                    collection.count()
                )
            ),
        }

        if allowed_keys:

            query["where"] = {
                "shipment_id": {
                    "$in": scope["shipment_ids"]
                }
            }

        vector_results = collection.query(**query)

        for document, metadata in zip(
            (vector_results.get("documents") or [[]])[0] or [],
            (vector_results.get("metadatas") or [[]])[0] or [],
        ):

            # Never let a chunk outside the scope through, even
            # if the metadata filter matched it
            if (
                allowed_keys
                and identifier_key(
                    (metadata or {}).get("shipment_id")
                ) not in allowed_keys
            ):
                continue

            vector_documents.append(document)
            vector_metadatas.append(metadata)

    # --------------------------------------------------------
    # KEYWORD SEARCH (in-scope records only)
    # --------------------------------------------------------

    scored_documents = []

    for record in records:

        document = record["document"]
        metadata = record["metadata"]

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

    scored_documents.sort(
        key=lambda item: item[0],
        reverse=True
    )

    # --------------------------------------------------------
    # COMBINE RESULTS
    # --------------------------------------------------------

    combined = []

    seen = set()

    # Keyword results first
    for (
        score,
        document,
        metadata
    ) in scored_documents:

        if score <= 0:
            continue

        key = (
            metadata.get("source"),
            metadata.get("page"),
            metadata.get("chunk")
        )

        if key in seen:
            continue

        combined.append(
            {
                "document": document,
                "metadata": metadata,
                "score": score,
            }
        )

        seen.add(key)

    # Vector results second
    for (
        document,
        metadata
    ) in zip(
        vector_documents,
        vector_metadatas
    ):

        key = (
            metadata.get("source"),
            metadata.get("page"),
            metadata.get("chunk")
        )

        if key in seen:
            continue

        combined.append(
            {
                "document": document,
                "metadata": metadata,
                "score": 0,
            }
        )

        seen.add(key)

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
        ],
        "retrieval_seconds": (
            time.perf_counter()
            - started
        ),
    }


# ============================================================
# GET DOCUMENT RECORDS
# ============================================================

def get_document_records():

    all_documents, all_metadatas = (
        get_all_documents()
    )

    records = []

    for document, metadata in zip(
        all_documents,
        all_metadatas
    ):

        records.append(
            {
                "document": document,
                "metadata": metadata,
                "type": get_document_type(
                    metadata,
                    document
                ),
                "normalized": normalize_text(
                    document
                ),
                "raw": normalize_raw_text(
                    document
                ),
            }
        )

    return records


# ============================================================
# SHIPMENT SCOPE
#
# Which records a question may use is decided by
# rag_scope.resolve_scope() before extraction and retrieval.
# The functions below only ever see in-scope records.
# ============================================================

def is_comparison_question(question_lower):
    """
    Questions comparing values ("how many were missing between the
    BOL and the POD", "do the line items add up", "were more than 40
    pieces delivered") are not answered by the single-field
    extractors below.
    """

    return bool(
        re.search(
            r"\b(?:missing|short(?:age)?|difference|between|compare|"
            r"add up|sum|match(?:es)?|consistent|all of|all the|"
            r"more|fewer|less|than|exceeds?|exceeded|at least|at most)\b",
            question_lower
        )
    )


# ============================================================
# EXTRACT EXACT ANSWER
#
# Calculations (delivery timing, quantity differences, invoice
# totals) are answered by shipment_calculations before this runs.
# ============================================================

def extract_exact_answer(
    question,
    results,
    records=None
):
    """
    Deterministic answer for supported field questions, from the
    given records only (the question's scope). Without records,
    every indexed record is used.
    """

    question_lower = (
        question or ""
    ).lower()

    if records is None:
        records = get_document_records()

    if not records:
        return None

    # --------------------------------------------------------
    # COMPARISONS ARE NOT SINGLE-FIELD QUESTIONS
    # --------------------------------------------------------

    if is_comparison_question(question_lower):
        return None

    # --------------------------------------------------------
    # NUMBER OF PIECES DELIVERED
    # --------------------------------------------------------

    if (
        "piece" in question_lower
        and (
            "how many"
            in question_lower
            or "number"
            in question_lower
            or "quantity"
            in question_lower
        )
        and (
            "deliver"
            in question_lower
            or "received"
            in question_lower
        )
    ):

        patterns = [

            r"Number\s+of\s+Pieces\s+Delivered"
            r"\s*:\s*(\d+)",

            r"Pieces\s+Delivered"
            r"\s*:\s*(\d+)",

            r"Pieces\s+Delivered\s+Count"
            r"\s*:\s*(\d+)",

            r"Quantity\s+Delivered"
            r"\s*:\s*(\d+)",

            r"Delivered\s+Pieces"
            r"\s*:\s*(\d+)",
        ]

        for record in records:

            if record["type"] != "pod":
                continue

            for pattern in patterns:

                match = re.search(
                    pattern,
                    record["normalized"],
                    re.IGNORECASE
                )

                if match:

                    number = (
                        match.group(1)
                    )

                    return (
                        f"{number} pieces were delivered."
                    )

    # --------------------------------------------------------
    # BOL PIECES
    # --------------------------------------------------------

    if (
        "piece" in question_lower
        and (
            "bol" in question_lower
            or "bill of lading"
            in question_lower
        )
    ):

        patterns = [

            r"Number\s+of\s+Pieces"
            r"\s*:\s*(\d+)",

            r"Pieces"
            r"\s*:\s*(\d+)",
        ]

        for record in records:

            if record["type"] != "bol":
                continue

            for pattern in patterns:

                match = re.search(
                    pattern,
                    record["normalized"],
                    re.IGNORECASE
                )

                if match:

                    number = (
                        match.group(1)
                    )

                    return (
                        f"The BOL lists "
                        f"{number} pieces."
                    )

    # --------------------------------------------------------
    # TOTAL INVOICE AMOUNT
    # --------------------------------------------------------

    if (
        "invoice" in question_lower
        and (
            "total" in question_lower
            or "amount" in question_lower
        )
    ):

        patterns = [

            r"Total\s+Invoice\s+Amount"
            r"\s*:\s*\$?\s*"
            r"([\d,]+(?:\.\d{2})?)",

            r"Total\s+Amount"
            r"\s*:\s*\$?\s*"
            r"([\d,]+(?:\.\d{2})?)",

            r"Invoice\s+Total"
            r"\s*:\s*\$?\s*"
            r"([\d,]+(?:\.\d{2})?)",
        ]

        for record in records:

            if record["type"] != "invoice":
                continue

            for pattern in patterns:

                match = re.search(
                    pattern,
                    record["normalized"],
                    re.IGNORECASE
                )

                if match:

                    amount = (
                        match.group(1)
                    )

                    return (
                        f"The total invoice amount "
                        f"is ${amount}."
                    )

    # --------------------------------------------------------
    # DELIVERY LOCATION
    # --------------------------------------------------------

    if (
        "where" in question_lower
        and (
            "delivered"
            in question_lower
            or "delivery"
            in question_lower
        )
    ):

        pattern = (
            r"Delivery\s+Location\s*:\s*"
            r"(.+?)(?=\s+Delivery\s+Date\s*:)"
        )

        for record in records:

            if record["type"] != "pod":
                continue

            match = re.search(
                pattern,
                record["normalized"],
                re.IGNORECASE
            )

            if match:

                location = (
                    match.group(1)
                    .strip()
                )

                return (
                    f"The shipment was delivered "
                    f"to {location}."
                )

    # --------------------------------------------------------
    # DELIVERY STATUS
    # --------------------------------------------------------

    if (
        "delivery status"
        in question_lower
        or "status of delivery"
        in question_lower
        or (
            "what" in question_lower
            and "status" in question_lower
        )
    ):

        pattern = (
            r"Delivery\s+Status\s*:\s*"
            r"(.+?)(?=\s+Damage\s+Report\s*:)"
        )

        for record in records:

            if record["type"] != "pod":
                continue

            match = re.search(
                pattern,
                record["normalized"],
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

    # --------------------------------------------------------
    # DELIVERY DATE
    # ("expected delivery date" is answered from the BOL below)
    # --------------------------------------------------------

    if (
        (
            "delivery date"
            in question_lower
            or "when was it delivered"
            in question_lower
            or "when was the shipment delivered"
            in question_lower
            or "when was shipment delivered"
            in question_lower
        )
        and "expected" not in question_lower
    ):

        patterns = [

            r"Delivery\s+Date\s*:\s*"
            r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",

            r"Actual\s+Delivery\s+Date\s*:\s*"
            r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        ]

        for record in records:

            if record["type"] != "pod":
                continue

            for pattern in patterns:

                match = re.search(
                    pattern,
                    record["normalized"],
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

    # --------------------------------------------------------
    # EXPECTED DELIVERY DATE
    # --------------------------------------------------------

    if (
        "expected delivery"
        in question_lower
        or "expected date"
        in question_lower
    ):

        patterns = [

            r"Expected\s+Delivery\s+Date\s*:\s*"
            r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",

            r"Expected\s+Delivery\s*:\s*"
            r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        ]

        for record in records:

            if record["type"] != "bol":
                continue

            for pattern in patterns:

                match = re.search(
                    pattern,
                    record["normalized"],
                    re.IGNORECASE
                )

                if match:

                    date = (
                        match.group(1)
                        .strip()
                    )

                    return (
                        f"The expected delivery date "
                        f"was {date}."
                    )

    # --------------------------------------------------------
    # CARRIER
    # --------------------------------------------------------

    if "carrier" in question_lower:

        pattern = (
            r"Carrier\s*:\s*"
            r"(.+?)(?=\s+(?:Delivery|Origin|Destination|"
            r"Expected|Number|Invoice|$))"
        )

        for record in records:

            match = re.search(
                pattern,
                record["normalized"],
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

    # --------------------------------------------------------
    # DAMAGE REPORT
    # --------------------------------------------------------

    if "damage" in question_lower:

        pattern = (
            r"Damage\s+Report\s*:\s*"
            r"(.+?)(?=\s+Receiver\s+Comments|"
            r"\s+Signature|$)"
        )

        for record in records:

            if record["type"] != "pod":
                continue

            match = re.search(
                pattern,
                record["normalized"],
                re.IGNORECASE
            )

            if match:

                damage = (
                    match.group(1)
                    .strip()
                )

                return (
                    f"Damage report: "
                    f"{damage}"
                )

    # --------------------------------------------------------
    # RECEIVER COMMENTS
    # --------------------------------------------------------

    if (
        "receiver" in question_lower
        and (
            "comment"
            in question_lower
            or "condition"
            in question_lower
        )
    ):

        pattern = (
            r"Receiver\s+Comments\s*:\s*"
            r"(.+?)(?=\s+Signature|$)"
        )

        for record in records:

            if record["type"] != "pod":
                continue

            match = re.search(
                pattern,
                record["normalized"],
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

    # --------------------------------------------------------
    # SIGNATURE
    # --------------------------------------------------------

    if "signature" in question_lower:

        pattern = (
            r"Signature\s*:\s*"
            r"([A-Za-z .'-]+)"
        )

        for record in records:

            if record["type"] != "pod":
                continue

            match = re.search(
                pattern,
                record["normalized"],
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

    # --------------------------------------------------------
    # NO EXACT ANSWER
    # --------------------------------------------------------

    return None


# ============================================================
# SOURCE EVIDENCE
# ============================================================

def make_evidence(
    record,
    evidence_text
):

    metadata = record["metadata"]

    return create_evidence(
        source=metadata.get(
            "source",
            "Unknown"
        ),
        page=metadata.get(
            "page",
            "Unknown"
        ),
        text=normalize_text(
            evidence_text
        ),
        document_type=metadata.get(
            "document_type",
            record["type"]
        ),
        shipment_id=metadata.get(
            "shipment_id"
        ),
    )


# ============================================================
# FIND EXACT EVIDENCE
# ============================================================

def find_exact_evidence(
    question,
    answer,
    records=None
):
    """
    The source text behind a deterministic answer, from the given
    records only (the question's scope).
    """

    question_lower = (
        question or ""
    ).lower()

    if records is None:
        records = get_document_records()

    if not records:
        return None

    # --------------------------------------------------------
    # FIELD PATTERNS
    # (calculations cite their own fields; see
    # shipment_calculations)
    # --------------------------------------------------------

    field_patterns = []

    # Delivered pieces
    if (
        "piece" in question_lower
        and (
            "deliver"
            in question_lower
            or "received"
            in question_lower
        )
    ):

        field_patterns.append(
            (
                "pod",
                [
                    r"Number\s+of\s+Pieces\s+Delivered"
                    r"\s*:\s*\d+",

                    r"Pieces\s+Delivered"
                    r"\s*:\s*\d+",

                    r"Quantity\s+Delivered"
                    r"\s*:\s*\d+",

                    r"Delivered\s+Pieces"
                    r"\s*:\s*\d+",
                ]
            )
        )

    # BOL pieces
    if (
        "piece" in question_lower
        and (
            "bol" in question_lower
            or "bill of lading"
            in question_lower
        )
    ):

        field_patterns.append(
            (
                "bol",
                [
                    r"Number\s+of\s+Pieces"
                    r"\s*:\s*\d+",
                ]
            )
        )

    # Delivery location (the POD, not the BOL consignee)
    if (
        "where" in question_lower
        and (
            "delivered"
            in question_lower
            or "delivery"
            in question_lower
        )
    ):

        field_patterns.append(
            (
                "pod",
                [
                    r"Delivery\s+Location\s*:\s*"
                    r".+?(?=\s+Delivery\s+Date\s*:)"
                ]
            )
        )

    # Invoice total
    if (
        "invoice" in question_lower
        and (
            "total" in question_lower
            or "amount"
            in question_lower
        )
    ):

        field_patterns.append(
            (
                "invoice",
                [
                    r"Total\s+Invoice\s+Amount"
                    r"\s*:\s*\$?\s*"
                    r"[\d,]+(?:\.\d{2})?",

                    r"Total\s+Amount"
                    r"\s*:\s*\$?\s*"
                    r"[\d,]+(?:\.\d{2})?",
                ]
            )
        )

    # Delivery date
    if (
        (
            "delivery date"
            in question_lower
            or "when was it delivered"
            in question_lower
        )
        and "expected" not in question_lower
    ):

        field_patterns.append(
            (
                "pod",
                [
                    r"Delivery\s+Date\s*:\s*"
                    r"[A-Za-z]+\s+\d{1,2},\s+\d{4}"
                ]
            )
        )

    # Expected delivery
    if (
        "expected delivery"
        in question_lower
    ):

        field_patterns.append(
            (
                "bol",
                [
                    r"Expected\s+Delivery\s+Date"
                    r"\s*:\s*"
                    r"[A-Za-z]+\s+\d{1,2},\s+\d{4}"
                ]
            )
        )

    # Delivery status
    if (
        "delivery status"
        in question_lower
        or "status of delivery"
        in question_lower
    ):

        field_patterns.append(
            (
                "pod",
                [
                    r"Delivery\s+Status\s*:\s*"
                    r".+?(?=\s+Damage\s+Report|$)"
                ]
            )
        )

    # Carrier
    if "carrier" in question_lower:

        field_patterns.append(
            (
                None,
                [
                    r"Carrier\s*:\s*"
                    r".+?(?=\s+(?:Delivery|Origin|"
                    r"Destination|Expected|Number|$))"
                ]
            )
        )

    # Damage
    if "damage" in question_lower:

        field_patterns.append(
            (
                "pod",
                [
                    r"Damage\s+Report\s*:\s*"
                    r".+?(?=\s+Receiver\s+Comments|"
                    r"\s+Signature|$)"
                ]
            )
        )

    # Receiver comments
    if (
        "receiver" in question_lower
        and (
            "comment"
            in question_lower
            or "condition"
            in question_lower
        )
    ):

        field_patterns.append(
            (
                "pod",
                [
                    r"Receiver\s+Comments\s*:\s*"
                    r".+?(?=\s+Signature|$)"
                ]
            )
        )

    # Signature
    if "signature" in question_lower:

        field_patterns.append(
            (
                "pod",
                [
                    r"Signature\s*:\s*"
                    r"[A-Za-z .'-]+"
                ]
            )
        )

    # --------------------------------------------------------
    # SEARCH EXACT SUPPORTING TEXT
    # --------------------------------------------------------

    for preferred_type, patterns in (
        field_patterns
    ):

        candidates = records

        if preferred_type:

            typed = [
                record
                for record in records
                if record["type"]
                == preferred_type
            ]

            if typed:
                candidates = typed

        for record in candidates:

            text = record["normalized"]

            for pattern in patterns:

                match = re.search(
                    pattern,
                    text,
                    re.IGNORECASE
                )

                if match:

                    evidence_text = (
                        match.group(0)
                        .strip()
                    )

                    return make_evidence(
                        record,
                        evidence_text
                    )

    return None


# ============================================================
# SUPPORTING EVIDENCE
#
# Only chunks that support the answer are cited
# (rag_grounding.ground_answer), never every retrieved chunk.
# ============================================================

def build_supporting_evidence(
    documents,
    metadatas,
    indexes
):

    evidence_items = []

    for index in indexes:

        metadata = metadatas[index] or {}

        evidence_text = normalize_text(
            documents[index]
        )

        if len(evidence_text) > 350:
            evidence_text = (
                evidence_text[:350]
                + "..."
            )

        evidence_items.append(
            create_evidence(
                source=metadata.get(
                    "source",
                    "Unknown"
                ),
                page=metadata.get(
                    "page",
                    "Unknown"
                ),
                text=evidence_text,
                document_type=metadata.get(
                    "document_type",
                    "Unknown"
                ),
                shipment_id=metadata.get(
                    "shipment_id"
                ),
            )
        )

    return evidence_items


def scope_shipment_ids(metadatas):
    """
    Shipment IDs of the given chunks. Naming them in an answer is not
    evidence, so grounding ignores them.
    """

    return sorted({
        str(metadata.get("shipment_id")).strip()
        for metadata in metadatas
        if metadata and metadata.get("shipment_id")
    })


def supported_exact_evidence(
    question,
    answer,
    records
):
    """
    Evidence for a deterministic answer, kept only where it supports
    the answer; otherwise the in-scope chunks that do.
    """

    ignore_ids = scope_shipment_ids(
        [record["metadata"] for record in records]
    )

    evidence = find_exact_evidence(
        question,
        answer,
        records
    )

    if evidence and not isinstance(evidence, list):
        evidence = [evidence]

    if evidence:

        supported, indexes, _ = ground_answer(
            answer,
            [item["text"] for item in evidence],
            ignore_ids
        )

        if supported:
            return [evidence[index] for index in indexes]

    supported, indexes, _ = ground_answer(
        answer,
        [record["normalized"] for record in records],
        ignore_ids
    )

    if not supported:
        return []

    return build_supporting_evidence(
        [record["document"] for record in records],
        [record["metadata"] for record in records],
        indexes
    )


# ============================================================
# ENSURE PROFESSIONAL ANSWER
# ============================================================

def ensure_professional_answer(
    answer,
    question
):

    if not answer:

        return (
            "I could not find enough information "
            "in the logistics documents to answer "
            "this question."
        )

    answer = normalize_text(
        answer
    )

    normalized = answer.lower().strip()

    # --------------------------------------------------------
    # SINGLE-WORD YES
    # --------------------------------------------------------

    if normalized in {
        "yes",
        "yes.",
        "yes!",
    }:

        return (
            "Yes. The available logistics documents "
            "support the condition described in the "
            "question."
        )

    # --------------------------------------------------------
    # SINGLE-WORD NO
    # --------------------------------------------------------

    if normalized in {
        "no",
        "no.",
        "no!",
    }:

        return (
            "No. The available logistics documents "
            "do not support the condition described "
            "in the question."
        )

    # --------------------------------------------------------
    # EXTREMELY SHORT ANSWER
    # --------------------------------------------------------

    words = answer.split()

    if len(words) <= 2:

        return (
            f"{answer.rstrip('.!?')}. "
            "The available logistics documents "
            "provide the supporting information "
            "for this response."
        )

    return answer


# ============================================================
# GENERATE LLM ANSWER
# ============================================================

def finish_answer(
    results,
    answer,
    status,
    method=None,
    evidence=None
):

    results["status"] = status
    results["answer_method"] = method
    results["evidence"] = evidence or []

    return answer


def finish_calculation(
    results,
    calculations
):
    """
    Answer from shipment_calculations results, one per shipment in
    scope. Several shipments must agree exactly, otherwise the user
    is asked to choose one; an abstention says why the calculation
    could not be done. A shipment that cannot be calculated is
    reported as such, not as a disagreement.
    """

    if len(calculations) > 1:

        unable = [
            f"{shipment_name(key, group)}: "
            f"{calculation.reason or calculation.answer}"
            for key, (calculation, group) in calculations.items()
            if calculation.status != CALCULATION_ANSWERED
        ]

        able = [
            shipment_name(key, group)
            for key, (calculation, group) in calculations.items()
            if calculation.status == CALCULATION_ANSWERED
        ]

        if unable and not able:

            return finish_answer(
                results,
                "I cannot calculate this for any shipment in scope ("
                + "; ".join(unable) + ").",
                STATUS_ABSTAINED,
                "exact"
            )

        if unable:

            return finish_answer(
                results,
                f"This can be calculated for {', '.join(able)}, but not "
                f"for {'; '.join(unable)}. Select a shipment in the "
                "sidebar, or include the shipment ID in the question.",
                STATUS_CLARIFICATION
            )

        answers = {
            calculation.answer
            for calculation, _ in calculations.values()
        }

        if len(answers) > 1:

            names = ", ".join(
                shipment_name(key, group)
                for key, (_, group) in calculations.items()
            )

            return finish_answer(
                results,
                "The documents of different shipments give different "
                f"answers to this question ({names}). Select a shipment "
                "in the sidebar, or include the shipment ID in the "
                "question.",
                STATUS_CLARIFICATION
            )

    calculation, _ = next(iter(calculations.values()))

    if calculation.status != CALCULATION_ANSWERED:

        return finish_answer(
            results,
            calculation.answer,
            STATUS_ABSTAINED,
            "exact"
        )

    # One evidence item per document, listing the fields used
    by_record = {}

    for calculation_result, _ in calculations.values():

        for item in calculation_result.evidence:

            by_record.setdefault(
                id(item.record),
                (item.record, [])
            )[1].append(item.text)

    evidence = [
        make_evidence(record, " | ".join(dict.fromkeys(texts)))
        for record, texts in by_record.values()
    ]

    # The cited field texts must contain the facts the answer states
    supported, _, _ = ground_answer(
        calculation.answer,
        [item["text"] for item in evidence],
        scope_shipment_ids(
            [record["metadata"] for record, _ in by_record.values()]
        )
    )

    if not supported:

        return finish_answer(
            results,
            UNVERIFIED_MESSAGE,
            STATUS_ABSTAINED,
            "exact"
        )

    return finish_answer(
        results,
        calculation.answer,
        STATUS_ANSWERED,
        "exact",
        evidence
    )


def shipment_name(key, records):

    if key is None:
        return "documents without a shipment ID"

    return str(
        records[0]["metadata"].get("shipment_id")
    ).strip()


def generate_answer(
    question,
    results,
    records=None,
    scope=None
):
    """
    Answer from the in-scope records and retrieved chunks only.

    Sets results["status"] ("answered", "abstained" or
    "clarification"), results["answer_method"] ("exact" or "llm")
    and results["evidence"] (only the sources supporting the answer).
    """

    if records is None:
        records = get_document_records()

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

    results.setdefault("generation_seconds", None)

    # --------------------------------------------------------
    # NO DOCUMENTS
    # --------------------------------------------------------

    if not documents:

        return finish_answer(
            results,
            NOT_FOUND_MESSAGE,
            STATUS_ABSTAINED
        )

    groups = group_records_by_shipment(records)

    # --------------------------------------------------------
    # CALCULATIONS FIRST (delivery timing, quantity differences,
    # invoice totals), per shipment; the arithmetic is never left
    # to the language model
    # --------------------------------------------------------

    calculations = {}

    for key, group in groups.items():

        calculation = calculate(
            question,
            group
        )

        if calculation:
            calculations[key] = (calculation, group)

    if calculations:

        return finish_calculation(
            results,
            calculations
        )

    # --------------------------------------------------------
    # EXACT EXTRACTION, per shipment, so that values from
    # different shipments are never mixed
    # --------------------------------------------------------

    exact_answers = {}

    for key, group in groups.items():

        exact_answer = extract_exact_answer(
            question,
            results,
            group
        )

        if exact_answer:
            exact_answers[key] = (exact_answer, group)

    if exact_answers:

        distinct = {
            exact_answer
            for exact_answer, _ in exact_answers.values()
        }

        if len(distinct) > 1:

            names = ", ".join(
                shipment_name(key, group)
                for key, (_, group) in exact_answers.items()
            )

            return finish_answer(
                results,
                "The documents of different shipments give different "
                f"answers to this question ({names}). Select a shipment "
                "in the sidebar, or include the shipment ID in the "
                "question.",
                STATUS_CLARIFICATION
            )

        exact_answer = distinct.pop()

        evidence = []

        for _, group in exact_answers.values():

            evidence.extend(
                supported_exact_evidence(
                    question,
                    exact_answer,
                    group
                )
            )

        if not evidence:

            return finish_answer(
                results,
                UNVERIFIED_MESSAGE,
                STATUS_ABSTAINED,
                "exact"
            )

        # Unscoped with several shipments: name the one answering
        if len(groups) > 1 and len(exact_answers) == 1:

            key, (_, group) = next(iter(exact_answers.items()))

            exact_answer = (
                f"{exact_answer} "
                f"(Shipment: {shipment_name(key, group)})"
            )

        return finish_answer(
            results,
            exact_answer,
            STATUS_ANSWERED,
            "exact",
            evidence
        )

    # --------------------------------------------------------
    # BUILD CONTEXT
    # --------------------------------------------------------

    context_parts = []

    for document, metadata in zip(
        documents,
        metadatas
    ):

        context_parts.append(
            f"""
SOURCE: {metadata.get("source", "Unknown")}
DOCUMENT TYPE: {metadata.get("document_type", "Unknown")}
SHIPMENT ID: {metadata.get("shipment_id", "Unknown")}
PAGE: {metadata.get("page", "Unknown")}

DOCUMENT CONTENT:
{document}
"""
        )

    context = "\n".join(
        context_parts
    )

    scope_label = (
        scope["label"]
        if scope
        else "all indexed shipments"
    )

    # --------------------------------------------------------
    # LLM PROMPT
    # --------------------------------------------------------

    prompt = f"""
You are LOGIDOC-RAG, a professional logistics
document intelligence assistant.

Answer the user's question using ONLY the
provided logistics documents.

STRICT RULES:

1. Do not use outside knowledge.
2. Do not invent information.
3. Do not guess.
4. Do not assume missing information.
5. Carefully read all document context.
6. If the answer exists in the documents,
   answer it directly.
7. If the answer does not exist, say:

I could not find this information in the
logistics documents.

8. Keep answers concise and professional.
9. Always provide at least one complete sentence.
10. NEVER answer only:
    Yes.
11. NEVER answer only:
    No.
12. For yes/no questions, start the answer
    with Yes or No, then give the reason or
    supporting fact from the documents.
13. Prefer one or two clear sentences.
14. The documents come from: {scope_label}.
    If they belong to more than one shipment,
    name the shipment for every fact.

LOGISTICS DOCUMENT CONTEXT:

{context}

USER QUESTION:

{question}

ANSWER:
"""

    started = time.perf_counter()

    try:

        answer = call_llm(prompt)

    except Exception as error:

        results["generation_seconds"] = (
            time.perf_counter()
            - started
        )

        results["generation_error"] = str(error)

        return finish_answer(
            results,
            LLM_UNAVAILABLE_MESSAGE,
            STATUS_ABSTAINED,
            "llm"
        )

    results["generation_seconds"] = (
        time.perf_counter()
        - started
    )

    answer = ensure_professional_answer(
        answer,
        question
    )

    if is_abstention(answer):

        return finish_answer(
            results,
            NOT_FOUND_MESSAGE,
            STATUS_ABSTAINED,
            "llm"
        )

    # --------------------------------------------------------
    # The answer must be supported by the retrieved chunks;
    # only the supporting ones are cited
    # --------------------------------------------------------

    supported, indexes, reason = ground_answer(
        answer,
        [normalize_text(document) for document in documents],
        scope_shipment_ids(metadatas)
    )

    results["grounding"] = reason

    if not supported:

        results["unverified_answer"] = answer

        return finish_answer(
            results,
            UNVERIFIED_MESSAGE,
            STATUS_ABSTAINED,
            "llm"
        )

    return finish_answer(
        results,
        answer,
        STATUS_ANSWERED,
        "llm",
        build_supporting_evidence(
            documents,
            metadatas,
            indexes
        )
    )


# ============================================================
# ASK QUESTION
# ============================================================

def ask_question(
    question,
    shipment_id=None
):
    """
    Answer a question from the indexed documents.

    shipment_id: the shipment selected in the UI. None or
    "All Shipments" searches every shipment (unscoped), unless the
    question names a shipment.

    Returns (answer, results):
        status       "answered", "abstained" or "clarification"
        scope        what was searched (rag_scope.resolve_scope)
        evidence     only the sources that support the answer
        documents,   the searched chunks (search results, not
        metadatas    citations)
        retrieval_seconds, generation_seconds
    """

    results = {
        "documents": [[]],
        "metadatas": [[]],
        "evidence": [],
        "status": STATUS_ABSTAINED,
        "answer_method": None,
        "scope": None,
        "retrieval_seconds": None,
        "generation_seconds": None,
    }

    question = (
        question or ""
    ).strip()

    if not question:

        return (
            "Please enter a logistics question.",
            results
        )

    records = get_document_records()

    scope = resolve_scope(
        question,
        shipment_id,
        records
    )

    results["scope"] = scope

    if scope["status"] != SCOPE_OK:

        results["status"] = scope["status"]

        return (
            scope["message"],
            results
        )

    scoped_records = records_in_scope(
        records,
        scope
    )

    if not scoped_records:

        return (
            "No indexed documents are available for this question.",
            results
        )

    results.update(
        search_documents(
            question,
            scope,
            scoped_records
        )
    )

    answer = generate_answer(
        question,
        results,
        scoped_records,
        scope
    )

    return (
        answer,
        results
    )


# ============================================================
# MAIN CLI
# ============================================================

def main():

    print(
        "=" * 70
    )

    print(
        "LOGIDOC-RAG"
    )

    print(
        "Hybrid Logistics Document Intelligence"
    )

    print(
        "=" * 70
    )

    question = input(
        "\nAsk a logistics question: "
    ).strip()

    if not question:

        print(
            "\nPlease enter a question."
        )

        return

    print(
        "\nSearching logistics documents...\n"
    )

    try:

        answer, results = (
            ask_question(
                question
            )
        )

    except Exception as error:

        print(
            "\nERROR:"
        )

        print(
            error
        )

        return

    # --------------------------------------------------------
    # ANSWER
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # SOURCE EVIDENCE
    # --------------------------------------------------------

    evidence = (
        results.get(
            "evidence"
        )
    )

    if evidence:

        print(
            "\n" + "=" * 70
        )

        print(
            "SOURCE EVIDENCE"
        )

        print(
            "=" * 70
        )

        if isinstance(
            evidence,
            list
        ):

            for item in evidence:

                print(
                    format_evidence(
                        item
                    )
                )

                print(
                    "-" * 70
                )

        else:

            print(
                format_evidence(
                    evidence
                )
            )

    # --------------------------------------------------------
    # SOURCES
    # --------------------------------------------------------

    print(
        "\n" + "=" * 70
    )

    print(
        "SEARCHED PASSAGES (search results, not citations)"
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

        if key in seen_sources:
            continue

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