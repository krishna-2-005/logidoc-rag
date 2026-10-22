"""
Tests for the structured-extraction evaluation (assistant_eval.evaluate_structured):
metrics come only from labelled fields, a wrong label is scored as wrong, and
the unmeasured fields and the scope limits are reported.

Run: python -m pytest tests/test_structured_eval.py
"""

import copy
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import assistant_eval as ev  # noqa: E402


@pytest.fixture(scope="module")
def labels():
    return json.loads(ev.STRUCTURED_LABELS_FILE.read_text(encoding="utf-8"))


def run(labels, tmp_path):
    return ev.evaluate_structured(labels, tmp_path / "w")


def test_the_dataset_covers_all_four_types_and_every_state(labels):
    states = {item["state"] for doc in labels["documents"] for item in doc["labels"]}
    types = {doc["document_type"] for doc in labels["documents"]}

    assert {"BOL", "POD", "INVOICE", "RATE_CONFIRMATION"} <= types
    assert states == {"EXTRACTED", "MISSING", "UNPARSEABLE", "UNREADABLE", "UNSUPPORTED", "CONFLICTING"}
    assert "synthetic" in labels["scope"].lower() and "not evidence" in labels["scope"].lower()


def test_the_extractor_meets_its_own_labels(labels, tmp_path):
    report = run(labels, tmp_path)
    fields = report["fields"]

    assert fields["failed"] == [] and fields["false_extractions"] == 0 and fields["value_errors"] == 0
    assert fields["state_accuracy"] == fields["value_accuracy"] == fields["page_attribution"] == 1.0
    assert fields["labelled"] > 80 and fields["non_extracted_cases"] > 15 and report["line_items"]["recall"] == 1.0


def test_a_wrong_value_label_is_counted_as_a_failure_not_ignored(labels, tmp_path):
    wrong = copy.deepcopy(labels)
    wrong["documents"][2]["labels"][8]["value"] = 1.0        # the invoice total of SHJ-2026-001

    fields = run(wrong, tmp_path)["fields"]

    assert fields["value_accuracy"] < 1.0 and fields["false_extractions"] == 1 and fields["value_errors"] == 1
    assert any("invoice_total" in item for item in fields["failed"])


def test_a_wrong_state_label_and_a_wrong_page_label_are_failures(labels, tmp_path):
    wrong = copy.deepcopy(labels)
    wrong["documents"][2]["labels"][9]["state"] = "EXTRACTED"        # currency: really MISSING
    wrong["documents"][2]["labels"][9]["value"] = "USD"
    wrong["documents"][0]["labels"][1]["page"] = 3                      # carrier page

    report = run(wrong, tmp_path)

    assert report["fields"]["state_accuracy"] < 1.0 and report["fields"]["page_attribution"] < 1.0


def test_unlabelled_fields_are_listed_not_scored(labels, tmp_path):
    report = run(labels, tmp_path)

    assert "RATE_CONFIRMATION.weight" in report["not_measured"] and "POD.damage_report" in report["not_measured"]
    assert report["fields"]["labelled"] == sum(len(d["labels"]) for d in labels["documents"])
    assert "synthetic" in report["scope"].lower()


def test_the_report_is_json_and_part_of_the_full_run(tmp_path):
    report = ev.run_all(rag=False)

    json.dumps(report)
    assert report["structured_extraction"]["fields"]["labelled"] > 80
    names = {t["name"]: t for t in report["thresholds"]}
    assert {"structured_value_accuracy", "structured_state_accuracy", "structured_false_extractions"} <= set(names)
    assert not any(names[n]["mandatory"] for n in names if n.startswith("structured"))
    assert "Structured extraction" in ev.format_report(report)
    assert report["assistant"]["safety"]["failed_count"] == 0
