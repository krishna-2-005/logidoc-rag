import sys
import re
from pathlib import Path
from datetime import datetime


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

    all_documents, all_metadatas = (
        get_all_documents()
    )

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
# SHIPMENT ID EXTRACTION
# ============================================================

def extract_shipment_id(question):

    match = re.search(
        r"\bSHJ-\d{4}-\d+\b",
        question or "",
        re.IGNORECASE
    )

    if not match:
        return None

    return (
        match.group(0)
        .upper()
    )


# ============================================================
# FILTER RECORDS BY SHIPMENT
# ============================================================

def filter_records_by_shipment(
    records,
    shipment_id
):

    if not shipment_id:
        return records

    filtered = []

    for record in records:

        metadata_shipment = str(
            record["metadata"].get(
                "shipment_id",
                ""
            )
        ).upper()

        document_upper = (
            record["document"]
            or ""
        ).upper()

        if (
            shipment_id
            == metadata_shipment
            or shipment_id
            in document_upper
        ):

            filtered.append(
                record
            )

    if filtered:
        return filtered

    return records


# ============================================================
# EXTRACT DATE
# ============================================================

def extract_date(
    text,
    patterns
):

    if not text:
        return None

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            value = (
                match.group(1)
                .strip()
            )

            try:

                parsed = datetime.strptime(
                    value,
                    "%B %d, %Y"
                )

                return (
                    parsed,
                    value
                )

            except ValueError:
                continue

    return None


# ============================================================
# GET EXPECTED DELIVERY DATE
# ============================================================

def get_expected_delivery_date(
    records
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

        result = extract_date(
            record["normalized"],
            patterns
        )

        if result:
            return (
                result[0],
                result[1],
                record
            )

    return None


# ============================================================
# GET ACTUAL DELIVERY DATE
# ============================================================

def get_actual_delivery_date(
    records
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

        result = extract_date(
            record["normalized"],
            patterns
        )

        if result:
            return (
                result[0],
                result[1],
                record
            )

    return None


# ============================================================
# EXTRACT EXACT ANSWER
# ============================================================

def extract_exact_answer(
    question,
    results
):

    question_lower = (
        question or ""
    ).lower()

    records = get_document_records()

    if not records:
        return None

    # --------------------------------------------------------
    # SHIPMENT FILTER
    # --------------------------------------------------------

    shipment_id = (
        extract_shipment_id(
            question
        )
    )

    records = filter_records_by_shipment(
        records,
        shipment_id
    )

    # --------------------------------------------------------
    # DELIVERY ON TIME
    # --------------------------------------------------------

    on_time_question = (
        (
            "on time"
            in question_lower
        )
        or (
            "on-time"
            in question_lower
        )
        or (
            "delivered on time"
            in question_lower
        )
        or (
            "delivery on time"
            in question_lower
        )
        or (
            "late"
            in question_lower
        )
    )

    if on_time_question:

        expected = (
            get_expected_delivery_date(
                records
            )
        )

        actual = (
            get_actual_delivery_date(
                records
            )
        )

        if expected and actual:

            expected_date = expected[0]
            expected_text = expected[1]

            actual_date = actual[0]
            actual_text = actual[1]

            difference = (
                actual_date
                - expected_date
            ).days

            if difference <= 0:

                return (
                    f"Yes. The shipment was delivered "
                    f"on {actual_text}, which was on or "
                    f"before the expected delivery date "
                    f"of {expected_text}."
                )

            return (
                f"No. The shipment was delivered "
                f"on {actual_text}, which was "
                f"{difference} days after the expected "
                f"delivery date of {expected_text}."
            )

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
    # --------------------------------------------------------

    if (
        "delivery date"
        in question_lower
        or "when was it delivered"
        in question_lower
        or "when was the shipment delivered"
        in question_lower
        or "when was shipment delivered"
        in question_lower
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
    answer
):

    question_lower = (
        question or ""
    ).lower()

    records = get_document_records()

    if not records:
        return None

    shipment_id = (
        extract_shipment_id(
            question
        )
    )

    records = filter_records_by_shipment(
        records,
        shipment_id
    )

    # --------------------------------------------------------
    # ON-TIME EVIDENCE
    # --------------------------------------------------------

    if (
        "on time" in question_lower
        or "on-time" in question_lower
        or "delivered on time"
        in question_lower
        or "late" in question_lower
    ):

        expected = (
            get_expected_delivery_date(
                records
            )
        )

        actual = (
            get_actual_delivery_date(
                records
            )
        )

        evidence_items = []

        if expected:

            expected_record = expected[2]

            evidence_items.append(
                make_evidence(
                    expected_record,
                    f"Expected Delivery Date: "
                    f"{expected[1]}"
                )
            )

        if actual:

            actual_record = actual[2]

            evidence_items.append(
                make_evidence(
                    actual_record,
                    f"Delivery Date: "
                    f"{actual[1]}"
                )
            )

        if evidence_items:
            return evidence_items

    # --------------------------------------------------------
    # FIELD PATTERNS
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
        "delivery date"
        in question_lower
        or "when was it delivered"
        in question_lower
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
# CLEAN RETRIEVED EVIDENCE
# ============================================================

def build_retrieved_evidence(
    results,
    limit=3
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

    evidence_items = []

    for document, metadata in zip(
        documents[:limit],
        metadatas[:limit]
    ):

        document_type = (
            metadata.get(
                "document_type",
                "Unknown"
            )
        )

        normalized = normalize_text(
            document
        )

        # ----------------------------------------------------
        # Instead of displaying the entire PDF chunk,
        # extract a useful sentence-sized evidence snippet.
        # ----------------------------------------------------

        evidence_text = normalized

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
                document_type=document_type,
                shipment_id=metadata.get(
                    "shipment_id"
                ),
            )
        )

    return evidence_items


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
    # NO DOCUMENTS
    # --------------------------------------------------------

    if not documents:

        return (
            "I could not find this information "
            "in the logistics documents."
        )

    # --------------------------------------------------------
    # EXACT EXTRACTION FIRST
    # --------------------------------------------------------

    exact_answer = (
        extract_exact_answer(
            question,
            results
        )
    )

    if exact_answer:

        results["evidence"] = (
            find_exact_evidence(
                question,
                exact_answer
            )
        )

        return exact_answer

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
12. For yes/no questions, provide the reason
    or supporting fact from the documents.
13. Prefer one or two clear sentences.

LOGISTICS DOCUMENT CONTEXT:

{context}

USER QUESTION:

{question}

ANSWER:
"""

    try:

        response = ollama.chat(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ]
        )

        answer = (
            response
            .get("message", {})
            .get("content", "")
            .strip()
        )

    except Exception:

        answer = (
            "I could not generate an answer from "
            "the logistics documents."
        )

    answer = ensure_professional_answer(
        answer,
        question
    )

    results["evidence"] = (
        build_retrieved_evidence(
            results
        )
    )

    return answer


# ============================================================
# ASK QUESTION
# ============================================================

def ask_question(question):

    question = (
        question or ""
    ).strip()

    if not question:

        return (
            "Please enter a logistics question.",
            {
                "documents": [[]],
                "metadatas": [[]],
                "evidence": None,
            }
        )

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