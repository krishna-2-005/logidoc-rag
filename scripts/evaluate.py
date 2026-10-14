"""
LogiDoc-RAG evaluation on the fixed question set.

Indexes the two fixture shipments (tests/fixtures/shipment_SHJ-2026-001
and shipment_SHJ-2026-002) into a temporary folder and ChromaDB index,
asks every question in tests/eval/questions.jsonl through
rag.ask_question() with its selected shipment, and reports:

    retrieval recall@5       share of expected source pages among the
                             first 5 retrieved chunks
    answer match             exact, numeric or date match, where an
                             answer is expected
    citation precision       share of cited source pages that are
                             expected sources
    abstention accuracy      abstained (or asked for clarification)
                             exactly when expected
    cross-shipment leakage   questions with a selected shipment whose
                             retrieved chunks, citations or model
                             prompt include another shipment
    latency                  retrieval and generation, separately

Modes:
    offline (default)  deterministic lexical embeddings and NO language
                       model. Questions the system would send to the
                       model are reported as "not measured offline"
                       and left out of the answer, citation and
                       abstention metrics instead of being scored.
    --ollama           the embedding and language models from
                       config.py through Ollama; every metric is
                       measured.

The live data/raw folder and chroma_db index are never used.

Run from the project root:
    python scripts/evaluate.py
    python scripts/evaluate.py --ollama
    python scripts/evaluate.py --json eval_report.json
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import rag
from config import EMBEDDING_MODEL, LLM_MODEL
from fake_embeddings import lexical_embedding
from ingestion_manager import ingest_files
from isolation import SAMPLE_SHIPMENT_DIR, sample_workspace
from rag_grounding import DATE_PATTERN, normalize_date, numbers_in
from shipment_identity import identifier_key
from vector_store import create_embedding


QUESTIONS_FILE = PROJECT_ROOT / "tests" / "eval" / "questions.jsonl"

SECOND_SHIPMENT_DIR = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "shipment_SHJ-2026-002"
)

TEMP_PREFIX = "logidoc_eval_"

TOP_K = 5

TARGETS = {
    "cross_shipment_leakage": ("<=", 0.0),
    "retrieval_recall_at_5": (">=", 0.90),
    "citation_precision": (">=", 0.90),
    "abstention_accuracy": (">=", 0.75),
}


# ============================================================
# QUESTIONS AND ANSWER MATCHING
# ============================================================

def load_questions(path=QUESTIONS_FILE):

    with open(path, encoding="utf-8") as file:

        return [
            json.loads(line)
            for line in file
            if line.strip()
        ]


def normalize_words(text):

    return " ".join(
        "".join(
            character.lower() if character.isalnum() else " "
            for character in (text or "")
        ).split()
    )


def answer_matches(answer, question):
    """
    Exact, numeric or date match by answer_type.
    """

    expected = question["expected_answer"]
    answer_type = question["answer_type"]

    if answer_type in ("number", "money"):

        return float(expected) in numbers_in(answer)

    if answer_type == "date":

        return normalize_date(expected) in {
            normalize_date(date)
            for date in DATE_PATTERN.findall(answer)
        }

    if answer_type == "yes_no":

        words = normalize_words(answer).split()

        return bool(words) and words[0] == expected.lower()

    return normalize_words(expected) in normalize_words(answer)


# ============================================================
# MODEL CALLS
# ============================================================

class NotMeasuredOffline(Exception):
    pass


class RecordingLLM:
    """
    Records every prompt. Offline it raises instead of answering, so
    such questions are marked as not measured.
    """

    def __init__(self, delegate=None):

        self.delegate = delegate
        self.prompts = []

    def __call__(self, prompt):

        self.prompts.append(prompt)

        if self.delegate is None:
            raise NotMeasuredOffline()

        return self.delegate(prompt)


# ============================================================
# EVALUATION
# ============================================================

def source_pages(items):

    return [
        (item.get("source"), item.get("page"))
        for item in items
    ]


def evaluate_question(question, llm, shipment_ids, offline):

    llm.prompts.clear()

    answer, results = rag.ask_question(
        question["question"],
        shipment_id=question["shipment"]
    )

    used_llm = bool(llm.prompts)
    measured = not (offline and used_llm)

    retrieved_metadata = results["metadatas"][0][:TOP_K]
    retrieved = source_pages(retrieved_metadata)
    cited = source_pages(results["evidence"])
    expected = {
        (item["source"], item["page"])
        for item in question["expected_sources"]
    }

    record = {
        "id": question["id"],
        "category": question["category"],
        "shipment": question["shipment"],
        "status": results["status"],
        "method": results.get("answer_method"),
        "answer": answer,
        "measured": measured,
        "retrieved": retrieved,
        "cited": cited,
        "retrieval_seconds": results.get("retrieval_seconds"),
        "generation_seconds": (
            results.get("generation_seconds") if used_llm and not offline else None
        ),
        "generation_error": None if offline else results.get("generation_error"),
        "unverified_answer": results.get("unverified_answer"),
    }

    # Retrieval recall@5 and recall@1 (answerable questions only)
    if not question["expect_abstain"] and expected:

        record["recall_at_5"] = len(expected & set(retrieved)) / len(expected)
        record["hit_at_1"] = bool(retrieved[:1]) and retrieved[0] in expected

    # Cross-shipment leakage (questions with a selected shipment)
    if question["shipment"]:

        selected_key = identifier_key(question["shipment"])

        touched = {
            identifier_key(item.get("shipment_id"))
            for item in retrieved_metadata + results["evidence"]
        }

        other_ids = [
            shipment_id
            for shipment_id in shipment_ids
            if identifier_key(shipment_id) != selected_key
        ]

        record["leak"] = bool(
            touched - {selected_key}
            or any(
                other in prompt
                for prompt in llm.prompts
                for other in other_ids
            )
        )

    if measured:

        abstained = results["status"] != rag.STATUS_ANSWERED

        record["abstention_correct"] = abstained == question["expect_abstain"]

        if not question["expect_abstain"]:
            record["answer_correct"] = (
                not abstained and answer_matches(answer, question)
            )

        if not abstained:
            record["citations_correct"] = sum(
                1 for page in cited if page in expected
            )
            record["citations_total"] = len(cited)

    return record


def summarize(records):

    def mean(values):

        values = [value for value in values if value is not None]

        return (sum(values) / len(values), len(values)) if values else (None, 0)

    recall, recall_n = mean(record.get("recall_at_5") for record in records)
    hit1, _ = mean(
        float(record["hit_at_1"]) for record in records if "hit_at_1" in record
    )

    leak, leak_n = mean(
        float(record["leak"]) for record in records if "leak" in record
    )

    answer, answer_n = mean(
        float(record["answer_correct"])
        for record in records
        if "answer_correct" in record
    )

    abstention, abstention_n = mean(
        float(record["abstention_correct"])
        for record in records
        if "abstention_correct" in record
    )

    cited_correct = sum(record.get("citations_correct", 0) for record in records)
    cited_total = sum(record.get("citations_total", 0) for record in records)

    retrieval_times = [
        record["retrieval_seconds"]
        for record in records
        if record["retrieval_seconds"] is not None
    ]

    generation_times = [
        record["generation_seconds"]
        for record in records
        if record["generation_seconds"] is not None
    ]

    def latency(times):

        if not times:
            return None

        ordered = sorted(times)

        return {
            "count": len(times),
            "mean_seconds": statistics.mean(times),
            "p95_seconds": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
        }

    return {
        "questions": len(records),
        "not_measured_offline": sum(1 for record in records if not record["measured"]),
        "retrieval_recall_at_5": recall,
        "retrieval_recall_at_5_n": recall_n,
        "retrieval_hit_at_1": hit1,
        "answer_match": answer,
        "answer_match_n": answer_n,
        "citation_precision": cited_correct / cited_total if cited_total else None,
        "citation_precision_n": cited_total,
        "abstention_accuracy": abstention,
        "abstention_accuracy_n": abstention_n,
        "cross_shipment_leakage": leak,
        "cross_shipment_leakage_n": leak_n,
        "retrieval_latency": latency(retrieval_times),
        "generation_latency": latency(generation_times),
    }


def check_targets(summary):

    outcome = {}

    for metric, (operator, target) in TARGETS.items():

        value = summary[metric]

        if value is None:
            outcome[metric] = "not measured"
        elif operator == ">=":
            outcome[metric] = "pass" if value >= target else "FAIL"
        else:
            outcome[metric] = "pass" if value <= target else "FAIL"

    return outcome


def run(offline):

    embed = lexical_embedding if offline else create_embedding

    llm = RecordingLLM(None if offline else rag.call_llm)

    with sample_workspace(TEMP_PREFIX, copy_samples=False) as (raw_dir, chroma_dir):

        indexed = ingest_files(
            sorted(SAMPLE_SHIPMENT_DIR.glob("*.pdf"))
            + sorted(SECOND_SHIPMENT_DIR.glob("*.pdf")),
            raw_dir,
            chroma_dir,
            embed_text=embed
        )

        if indexed["files_added"] != 6:
            raise SystemExit(f"Indexing the fixtures failed: {indexed['errors']}")

        shipment_ids = sorted({
            outcome["shipment_id"]
            for outcome in indexed["files"]
        })

        original = (rag.embed_query, rag.call_llm)
        rag.embed_query = embed
        rag.call_llm = llm

        try:
            records = [
                evaluate_question(question, llm, shipment_ids, offline)
                for question in load_questions()
            ]
        finally:
            rag.embed_query, rag.call_llm = original

    return records


# ============================================================
# REPORT
# ============================================================

def percent(value):

    return "n/a" if value is None else f"{value:.2f}"


def print_report(mode, records, summary, targets):

    print("=" * 78)
    print(f"LOGIDOC-RAG EVALUATION ({mode})")
    print("=" * 78)

    for record in records:

        flags = []

        if not record["measured"]:
            flags.append("needs model: not measured offline")
        if record.get("leak"):
            flags.append("LEAK")
        if record.get("answer_correct") is False:
            flags.append("wrong answer")
        if record.get("abstention_correct") is False:
            flags.append("wrong abstention")

        print(
            f"{record['id']:4} {record['category'][:12]:12} "
            f"{record['status']:13} {str(record['method']):6} "
            f"{'; '.join(flags) or 'ok'}"
        )

    print("-" * 78)

    print(
        f"Retrieval recall@5     {percent(summary['retrieval_recall_at_5'])}"
        f"  (n={summary['retrieval_recall_at_5_n']}; hit@1 "
        f"{percent(summary['retrieval_hit_at_1'])})"
    )
    print(
        f"Answer match           {percent(summary['answer_match'])}"
        f"  (n={summary['answer_match_n']})"
    )
    print(
        f"Citation precision     {percent(summary['citation_precision'])}"
        f"  (n={summary['citation_precision_n']} citations)"
    )
    print(
        f"Abstention accuracy    {percent(summary['abstention_accuracy'])}"
        f"  (n={summary['abstention_accuracy_n']})"
    )
    print(
        f"Cross-shipment leakage {percent(summary['cross_shipment_leakage'])}"
        f"  (n={summary['cross_shipment_leakage_n']})"
    )

    for name in ("retrieval_latency", "generation_latency"):

        latency = summary[name]

        print(
            f"{name.replace('_', ' ').capitalize():22} "
            + (
                "n/a"
                if latency is None
                else f"mean {latency['mean_seconds'] * 1000:.0f} ms, "
                f"p95 {latency['p95_seconds'] * 1000:.0f} ms "
                f"(n={latency['count']})"
            )
        )

    if summary["not_measured_offline"]:
        print(
            f"\n{summary['not_measured_offline']} question(s) need the language "
            "model and are not measured offline; run with --ollama."
        )

    print("\nAcceptance targets:")

    for metric, result in targets.items():
        operator, target = TARGETS[metric]
        print(f"  {metric:24} {operator} {target:<5} {result}")

    print("=" * 78)


def main():

    parser = argparse.ArgumentParser(
        description="Evaluate LogiDoc-RAG on the fixed question set."
    )

    parser.add_argument(
        "--ollama",
        action="store_true",
        help=f"use Ollama ({EMBEDDING_MODEL}, {LLM_MODEL}) instead of the offline mode",
    )

    parser.add_argument("--json", help="also write the full results to this file")

    arguments = parser.parse_args()

    offline = not arguments.ollama
    mode = "offline: lexical embeddings, no language model" if offline else (
        f"Ollama: {EMBEDDING_MODEL} + {LLM_MODEL}"
    )

    started = time.perf_counter()

    records = run(offline)
    summary = summarize(records)
    targets = check_targets(summary)

    print_report(mode, records, summary, targets)
    print(f"Total time {time.perf_counter() - started:.1f} s")

    if arguments.json:

        with open(arguments.json, "w", encoding="utf-8") as file:

            json.dump(
                {
                    "mode": mode,
                    "summary": summary,
                    "targets": targets,
                    "questions": records,
                },
                file,
                indent=2,
            )


if __name__ == "__main__":

    main()
