"""
LogiDoc-RAG shipment-scoped retrieval and grounding tests.

Both sample shipments (tests/fixtures/shipment_SHJ-2026-001 and
shipment_SHJ-2026-002) are indexed together in a temporary folder and
ChromaDB index with deterministic offline embeddings
(tests/fake_embeddings.py). The language model is replaced by small
fakes, so these tests need no Ollama and are fully repeatable.

Run from the project root:
    python -m pytest tests/test_rag_scoping.py
"""

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import chromadb

import config
import rag
from fake_embeddings import lexical_embedding
from ingestion_manager import ingest_files
from isolation import SAMPLE_SHIPMENT_DIR, sample_workspace
from rag_scope import indexed_shipments
from test_ingestion_manager import patched


# ============================================================
# TEST DATA
# ============================================================

TEMP_PREFIX = "logidoc_ragtest_"

SECOND_SHIPMENT_DIR = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "shipment_SHJ-2026-002"
)

A = "SHJ-2026-001"
B = "SHJ-2026-002"

A_FILES = {
    "BOL_SHJ-2026-001.pdf",
    "INVOICE_SHJ-2026-001.pdf",
    "POD_SHJ-2026-001.pdf",
}

B_FILES = {
    "BOL_SHJ-2026-002.pdf",
    "INVOICE_SHJ-2026-002.pdf",
    "POD_SHJ-2026-002.pdf",
}


# ============================================================
# FIXTURES AND HELPERS
# ============================================================

class FakeLLM:
    """
    Stand-in for rag.call_llm that records every prompt.
    reply: a string, or a function of the prompt.
    """

    def __init__(self, reply):

        self.reply = reply
        self.prompts = []

    def __call__(self, prompt):

        self.prompts.append(prompt)

        return self.reply(prompt) if callable(self.reply) else self.reply


def faithful_receiver(prompt):
    """
    Answers 'who received' only from the context it was given,
    like a model that follows the instructions.
    """

    match = re.search(r"Received By:\s*([A-Z][a-z]+ [A-Z][a-z]+)", prompt)

    if not match:
        return "I could not find this information in the logistics documents."

    return f"{match.group(1)} received the shipment."


@pytest.fixture(scope="module")
def two_shipments():
    """
    Both sample shipments indexed together, with every project path
    and the query embedder pointed at the temporary workspace.
    """

    with sample_workspace(TEMP_PREFIX, copy_samples=False) as (raw_dir, chroma_dir):

        indexed = ingest_files(
            sorted(SAMPLE_SHIPMENT_DIR.glob("*.pdf"))
            + sorted(SECOND_SHIPMENT_DIR.glob("*.pdf")),
            raw_dir,
            chroma_dir,
            embed_text=lexical_embedding
        )

        assert indexed["files_added"] == 6, indexed["errors"]

        with patched(rag, "embed_query", lexical_embedding):
            yield raw_dir, chroma_dir


@pytest.fixture
def llm(two_shipments):
    """
    A fake model that abstains unless a test sets its reply.
    """

    fake = FakeLLM(
        "I could not find this information in the logistics documents."
    )

    with patched(rag, "call_llm", fake):
        yield fake


def ask(question, shipment=None):

    return rag.ask_question(question, shipment_id=shipment)


def retrieved(results):

    return results["metadatas"][0]


def cited(results):

    return {
        (item["source"], item["page"], item["shipment_id"])
        for item in results["evidence"]
    }


def shipments_touched(results):

    return (
        {metadata.get("shipment_id") for metadata in retrieved(results)}
        | {item["shipment_id"] for item in results["evidence"]}
    )


# ============================================================
# BOTH SHIPMENTS INDEXED
# ============================================================

def test_both_shipments_indexed(two_shipments):

    records = rag.get_document_records()

    shipments, unidentified = indexed_shipments(records)

    assert shipments == {"SHJ2026001": [A], "SHJ2026002": [B]}
    assert unidentified == 0
    assert {record["metadata"]["source"] for record in records} == A_FILES | B_FILES


# ============================================================
# ISOLATION
# ============================================================

QUESTIONS = [
    "How many pieces were delivered?",
    "What is the total invoice amount?",
    "Who was the carrier?",
    "What was the delivery date?",
    "What was the expected delivery date?",
    "Was the shipment delivered on time?",
    "Who received the shipment?",
    "What equipment was used?",
]


@pytest.mark.parametrize("selected, other, files", [(A, B, A_FILES), (B, A, B_FILES)])
@pytest.mark.parametrize("question", QUESTIONS)
def test_selected_shipment_never_sees_the_other(llm, selected, other, files, question):

    llm.reply = faithful_receiver

    answer, results = ask(question, selected)

    assert shipments_touched(results) <= {selected}, (
        f"{question!r} for {selected} touched {shipments_touched(results)}"
    )

    assert {item["source"] for item in results["evidence"]} <= files

    assert {metadata["source"] for metadata in retrieved(results)} <= files

    # The model never sees the other shipment's text
    for prompt in llm.prompts:
        assert other not in prompt


@pytest.mark.parametrize(
    "selected, expected",
    [
        (A, "45 pieces were delivered."),
        (B, "60 pieces were delivered."),
    ],
)
def test_question_without_id_uses_the_selected_shipment(llm, selected, expected):

    answer, results = ask("How many pieces were delivered?", selected)

    assert answer == expected
    assert results["status"] == "answered"
    assert results["scope"]["label"] == f"Shipment {selected} (selected)"
    assert llm.prompts == []


def test_vector_search_is_filtered_by_shipment(two_shipments):

    # A query with no keyword overlap: only vector results come back
    scope = rag.resolve_scope("zzz qqq", A, rag.get_document_records())

    results = rag.search_documents(
        "zzz qqq",
        scope,
        rag.records_in_scope(rag.get_document_records(), scope)
    )

    assert retrieved(results)
    assert {metadata["shipment_id"] for metadata in retrieved(results)} == {A}


# ============================================================
# EMPTY, AMBIGUOUS AND CONFLICTING SCOPES
# ============================================================

def test_selected_shipment_without_documents(llm):

    answer, results = ask("How many pieces were delivered?", "SHJ-2026-999")

    assert results["status"] == "abstained"
    assert "No documents are indexed for shipment SHJ-2026-999" in answer
    assert results["evidence"] == []
    assert retrieved(results) == []
    assert llm.prompts == []


def test_unscoped_question_where_shipments_disagree(llm):

    answer, results = ask("How many pieces were delivered?")

    assert results["status"] == "clarification"
    assert "different answers" in answer
    assert A in answer and B in answer
    assert "45" not in answer and "60" not in answer
    assert results["evidence"] == []


def test_unscoped_question_where_shipments_agree(llm):

    answer, results = ask("What was the delivery status?")

    assert results["status"] == "answered"
    assert answer == "The delivery status was Delivered."
    assert {item["source"] for item in results["evidence"]} == {
        "POD_SHJ-2026-001.pdf",
        "POD_SHJ-2026-002.pdf",
    }


def test_unscoped_question_naming_a_shipment(llm):

    answer, results = ask("How many pieces were delivered for SHJ-2026-002?")

    assert answer == "60 pieces were delivered."
    assert results["scope"]["source"] == "question"
    assert shipments_touched(results) == {B}


def test_question_naming_another_shipment_asks_for_clarification(llm):

    answer, results = ask("How many pieces were delivered for SHJ-2026-002?", A)

    assert results["status"] == "clarification"
    assert B in answer and A in answer
    assert results["evidence"] == []
    assert llm.prompts == []


def test_reference_found_only_in_another_shipment(llm):

    answer, results = ask("What is the total on invoice INV-SHJ-2026-002?", A)

    assert results["status"] == "clarification"
    assert results["evidence"] == []


def test_reference_found_in_no_document(llm):

    answer, results = ask("Is PO 12345 listed on the invoice?", A)

    assert results["status"] == "abstained"
    assert "PO 12345" in answer
    assert results["evidence"] == []


# ============================================================
# EQUIVALENT SHIPMENT IDS
# ============================================================

@pytest.mark.parametrize(
    "selected",
    ["SHJ-2026-001", "shj-2026-001", "SHJ 2026 001", " shj2026001 ", "Shj_2026_001"],
)
def test_equivalent_selected_ids(llm, selected):

    answer, results = ask("How many pieces were delivered?", selected)

    assert answer == "45 pieces were delivered."
    assert shipments_touched(results) == {A}


@pytest.mark.parametrize(
    "question",
    [
        "How many pieces were delivered for shj 2026 001?",
        "How many pieces were delivered for SHJ2026001?",
        "How many pieces were delivered for Shj-2026-001?",
    ],
)
def test_equivalent_ids_in_the_question(llm, question):

    # Same shipment as selected: no conflict
    answer, results = ask(question, A)
    assert answer == "45 pieces were delivered."

    # Unscoped: the question selects the shipment
    answer, results = ask(question)
    assert answer == "45 pieces were delivered."
    assert shipments_touched(results) == {A}


def test_equivalent_id_of_another_shipment_still_conflicts(llm):

    answer, results = ask("How many pieces were delivered for shj 2026 002?", A)

    assert results["status"] == "clarification"


# ============================================================
# LEGACY VECTORS WITHOUT SHIPMENT METADATA
# ============================================================

def test_legacy_vectors_without_shipment_metadata(llm):

    # A separate index with both shipments plus two chunks that have
    # no usable shipment_id. Their text claims shipment SHJ-2026-001
    # with different values, which must never be trusted.
    with sample_workspace(TEMP_PREFIX + "legacy_", copy_samples=False) as (raw_dir, chroma_dir):

        ingest_files(
            sorted(SAMPLE_SHIPMENT_DIR.glob("*.pdf"))
            + sorted(SECOND_SHIPMENT_DIR.glob("*.pdf")),
            raw_dir,
            chroma_dir,
            embed_text=lexical_embedding
        )

        legacy_text = (
            "PROOF OF DELIVERY Shipment Number: SHJ-2026-001 "
            "Number of Pieces Delivered: 99 Delivery Status: Lost "
            "Damage Report: Crushed."
        )

        other_text = (
            "PROOF OF DELIVERY Shipment Number: SHJ-2026-777 "
            "Number of Pieces Delivered: 7"
        )

        chromadb.PersistentClient(path=str(chroma_dir)).get_collection(
            config.COLLECTION_NAME
        ).add(
            ids=["UNKNOWN_SHIPMENT_0", "LEGACY_1"],
            embeddings=[lexical_embedding(legacy_text), lexical_embedding(other_text)],
            documents=[legacy_text, other_text],
            metadatas=[
                # As build_vector_database writes a missing ID
                {"source": "legacy_scan.pdf", "document_type": "POD",
                 "shipment_id": "", "page": 1, "chunk": 0},
                # Very old vector without the field at all
                {"source": "legacy_other.pdf", "document_type": "POD",
                 "page": 1, "chunk": 0},
            ],
        )

        answer, results = ask("How many pieces were delivered?", A)

        assert answer == "45 pieces were delivered."
        assert results["scope"]["excluded_unidentified"] == 2
        assert "legacy_scan.pdf" not in {m["source"] for m in retrieved(results)}
        assert "legacy_scan.pdf" not in {e["source"] for e in results["evidence"]}

        answer, results = ask("What was the damage report?", A)
        assert "Crushed" not in answer

        # Only unidentified chunks mention this shipment: never used
        answer, results = ask("How many pieces were delivered?", "SHJ-2026-777")
        assert results["status"] == "abstained"
        assert "have no shipment ID" in answer

        # Unscoped: the unidentified chunk is a third, disagreeing source
        answer, results = ask("How many pieces were delivered?")
        assert results["status"] == "clarification"
        assert "without a shipment ID" in answer


# ============================================================
# CITATIONS
# ============================================================

def test_deterministic_answer_cites_its_source(llm):

    answer, results = ask("What is the total invoice amount?", A)

    assert answer == "The total invoice amount is $1,550.00."
    assert cited(results) == {("INVOICE_SHJ-2026-001.pdf", 1, A)}
    assert "1,550.00" in results["evidence"][0]["text"]
    assert len(retrieved(results)) > len(results["evidence"])


@pytest.mark.parametrize(
    "reply, expected_source",
    [
        # The model names the shipment (with U+2011 dashes, as the
        # real model does); that must not make every chunk "support"
        ("The fuel surcharge on the invoice for Shipment SHJ‑2026‑001 "
         "was $180.00.", "INVOICE_SHJ-2026-001.pdf"),
        ("The SHJ-2026-001 shipment had a total weight of 12,500 lbs.",
         "BOL_SHJ-2026-001.pdf"),
        ("The equipment used for shipment SHJ‑2026‑001 was a "
         "53‑foot dry‑van.", "BOL_SHJ-2026-001.pdf"),
        ("The invoice number is INV-SHJ-2026-001, shown on the invoice "
         "for shipment SHJ-2026-001.", "INVOICE_SHJ-2026-001.pdf"),
    ],
)
def test_naming_the_shipment_is_not_evidence(llm, reply, expected_source):

    llm.reply = reply

    answer, results = ask("Tell me about the shipment paperwork.", A)

    assert results["status"] == "answered"
    assert cited(results) == {(expected_source, 1, A)}


def test_unicode_dashes_in_question_ids(llm):

    answer, results = ask("How many pieces were delivered for SHJ‑2026‑001?")

    assert answer == "45 pieces were delivered."

    answer, results = ask("How many pieces were delivered for SHJ‑2026‑002?", A)

    assert results["status"] == "clarification"


def test_delivery_location_cites_the_pod_not_the_bol_consignee(llm):

    # The BOL consignee line contains the same company name, but only
    # the POD's Delivery Location supports "delivered to"
    answer, results = ask("Where was the shipment delivered?", B)

    assert "Peach State Fabrication" in answer
    assert cited(results) == {("POD_SHJ-2026-002.pdf", 1, B)}


def test_on_time_answer_cites_both_dates(llm):

    answer, results = ask("Was the shipment delivered on time?", A)

    assert answer.startswith("No.")
    assert "2 days after" in answer
    assert cited(results) == {
        ("BOL_SHJ-2026-001.pdf", 1, A),
        ("POD_SHJ-2026-001.pdf", 1, A),
    }


def test_generated_answer_cites_only_supporting_chunks(llm):

    llm.reply = faithful_receiver

    answer, results = ask("Who received the shipment?", B)

    assert results["status"] == "answered"
    assert results["answer_method"] == "llm"
    assert answer == "Sarah Whitfield received the shipment."
    assert cited(results) == {("POD_SHJ-2026-002.pdf", 1, B)}
    assert len(retrieved(results)) > 1


# ============================================================
# UNSUPPORTED ANSWERS ABSTAIN
# ============================================================

@pytest.mark.parametrize(
    "reply",
    [
        "The driver's phone number is 555-0199.",
        "The cargo was delivered by drone on October 12, 2026.",
        "The trailer license plate is TX 4471 ZQ.",
        "It was delivered by drone.",
    ],
)
def test_unsupported_answer_abstains(llm, reply):

    llm.reply = reply

    answer, results = ask("What is the driver's phone number?", A)

    assert results["status"] == "abstained"
    assert answer == rag.UNVERIFIED_MESSAGE
    assert results["evidence"] == []
    assert results["unverified_answer"] == reply


def test_model_abstention_is_reported_as_abstention(llm):

    answer, results = ask("What temperature was the cargo kept at?", A)

    assert results["status"] == "abstained"
    assert answer == rag.NOT_FOUND_MESSAGE
    assert results["evidence"] == []


# ============================================================
# EXISTING DETERMINISTIC ANSWERS
# ============================================================

@pytest.mark.parametrize(
    "question, selected, expected",
    [
        ("How many pieces were delivered?", A, "45 pieces were delivered."),
        ("How many pieces does the BOL list?", A, "The BOL lists 48 pieces."),
        ("What is the total invoice amount?", B, "The total invoice amount is $2,875.50."),
        ("What was the delivery date?", A, "The delivery date was October 5, 2026."),
        ("What was the expected delivery date?", A, "The expected delivery date was October 3, 2026."),
        ("Who was the carrier?", B, "The carrier was Lakeshore Freight Lines."),
        ("What was the damage report?", A, "Damage report: No visible damage reported."),
        ("What were the receiver comments?", B, "Receiver comments: All beams received and counted."),
        ("Whose signature is on the proof of delivery?", B, "The shipment was signed by Sarah Whitfield."),
    ],
)
def test_deterministic_answers(llm, question, selected, expected):

    answer, results = ask(question, selected)

    assert answer == expected
    assert results["answer_method"] == "exact"
    assert results["evidence"]
    assert llm.prompts == []


@pytest.mark.parametrize(
    "question",
    [
        "How many pieces were missing between the BOL and the POD?",
        "Do the invoice line items add up to the total invoice amount?",
    ],
)
def test_comparison_questions_are_not_single_field_answers(llm, question):

    # Previously answered "The BOL lists 48 pieces." and
    # "The total invoice amount is $1,550.00."
    answer, results = ask(question, A)

    assert results["answer_method"] == "llm"
    assert llm.prompts


def test_plate_is_not_a_late_delivery_question(llm):

    # "plate" contains "late"; it must not trigger the on-time answer
    answer, results = ask("What is the trailer license plate number?", A)

    assert results["answer_method"] == "llm"
    assert results["status"] == "abstained"
