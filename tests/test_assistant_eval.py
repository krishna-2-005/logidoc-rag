"""
Tests for the evaluation suite itself (src/assistant_eval.py and
scripts/evaluate_assistant.py): metric calculations, malformed datasets,
missing labels, report generation, threshold enforcement and the command.

Run: python -m pytest tests/test_assistant_eval.py
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "scripts", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import assistant_eval as ev  # noqa: E402
import evaluate_assistant as cli  # noqa: E402


# ---- metrics -------------------------------------------------------------

def test_ratio_is_none_not_zero_when_nothing_was_measured():
    assert ev.ratio(0, 0) is None and ev.ratio(1, 2) == 0.5 and ev.ratio(0, 4) == 0.0


def test_precision_recall_f1_known_values():
    r = ev.precision_recall_f1(tp=6, fp=2, fn=4)
    assert r["precision"] == 0.75 and r["recall"] == 0.6 and round(r["f1"], 4) == round(2 * .75 * .6 / 1.35, 4)
    empty = ev.precision_recall_f1(0, 0, 0)
    assert empty["precision"] is None and empty["recall"] is None and empty["f1"] is None
    assert ev.precision_recall_f1(0, 3, 3)["f1"] is None


def test_precision_and_recall_at_k():
    retrieved = [("a", 1), ("b", 1), ("c", 1), ("d", 1)]
    relevant = {("a", 1), ("d", 1), ("z", 1)}
    assert ev.precision_at_k(retrieved, relevant, 2) == 0.5
    assert ev.recall_at_k(retrieved, relevant, 2) == pytest.approx(1 / 3)
    assert ev.recall_at_k(retrieved, relevant, 4) == pytest.approx(2 / 3)
    assert ev.precision_at_k([], relevant, 5) is None and ev.recall_at_k(retrieved, set(), 5) is None


def test_numbers_match_uses_an_explicit_tolerance_and_rejects_missing():
    assert ev.numbers_match(1550.0, 1550.004) and not ev.numbers_match(1550.0, 1550.02)
    assert ev.numbers_match(3, 3, 0) and not ev.numbers_match(3, 4, 0)
    assert not ev.numbers_match(None, 0) and not ev.numbers_match(True, 1) and not ev.numbers_match("x", 1)


def test_accuracy_by_group_and_confusion():
    records = [{"c": "a", "ok": True}, {"c": "a", "ok": False}, {"c": "b", "ok": True}]
    assert ev.accuracy_by(records, "c") == {"a": {"passed": 1, "total": 2, "accuracy": 0.5}, "b": {"passed": 1, "total": 1, "accuracy": 1.0}}
    assert ev.confusion([("x", "x"), ("x", "y"), ("y", "y")], ["x", "y", "z"]) == {"x": {"x": 1, "y": 1}, "y": {"y": 1}}


# ---- datasets ------------------------------------------------------------

META = {"_meta": {"name": "t", "version": "1", "scope": "s"}}
GOOD = {"id": "a1", "category": "quantity", "question": "q", "shipment": None, "expect": {"status": "answered", "intent": "quantity", "facts": ["x"]}}


def write(tmp_path, *records, raw=None):
    path = tmp_path / "cases.jsonl"
    path.write_text(raw if raw is not None else "\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def test_the_shipped_dataset_loads_and_documents_its_scope():
    meta, cases = ev.load_cases()
    assert meta["version"] and meta["scope"] and len(cases) >= 40
    assert {c["category"] for c in cases} == set(ev.CATEGORIES)
    assert sum(1 for c in cases if c.get("safety")) >= 10 and any(c.get("ambiguous") for c in cases)


@pytest.mark.parametrize("raw, records, message", [
    ("", None, "empty"),
    ("{not json", None, "valid JSON"),
    (None, [GOOD], "_meta"),
    (None, [META], "no cases"),
    (None, [META, GOOD, GOOD], "duplicate"),
    (None, [META, {**GOOD, "category": "nonsense"}], "unknown category"),
    (None, [META, {**GOOD, "question": 5}], "no question"),
    (None, [META, {**GOOD, "expect": {"status": "maybe", "intent": "quantity"}}], "valid status"),
    (None, [META, {**GOOD, "expect": {"status": "answered", "intent": "quantity"}}], "expected facts"),
    (None, [META, {**GOOD, "id": ""}], "id"),
])
def test_malformed_or_unlabelled_datasets_are_refused(tmp_path, raw, records, message):
    path = write(tmp_path, *(records or []), raw=raw)
    with pytest.raises(ev.DatasetError, match=message):
        ev.load_cases(path)


def test_an_unanswerable_case_needs_no_facts(tmp_path):
    case = {**GOOD, "expect": {"status": "insufficient_evidence", "intent": "none"}}
    assert ev.load_cases(write(tmp_path, META, case))[1][0]["id"] == "a1"


# ---- case checks ---------------------------------------------------------

class FakeService:
    def scan(self):
        return [{"filename": "BOL_A.pdf", "document_type": "BOL", "total_pages": 1}]


def reply(**kw):
    base = {"status": "answered", "answer": "3 piece(s) short", "findings": [{"label": "Diff", "value": 3, "status": "x"}],
            "citations": [{"source": "BOL_A.pdf", "page": 1, "document_type": "BOL", "shipment_id": "A", "text": None}],
            "uncertainty": [], "next_steps": [], "tools_used": [], "generated": False}
    return {**base, **kw}


def case_with(**expect):
    return {"expect": {"status": "answered", "intent": "quantity", **expect}}


def test_checks_pass_for_a_correct_reply_and_fail_precisely():
    ok = ev.check_case(case_with(facts=["3 piece(s) short"], values={"Diff": 3}, citations=[{"source": "BOL_A.pdf", "page": 1}]), reply(), FakeService())
    assert all(ok.values())

    bad = ev.check_case(case_with(facts=["4 piece(s)"], values={"Diff": 4}), reply(), FakeService())
    assert bad["fact:4 piece(s)"] is False and bad["value:Diff"] is False and bad["status"] is True


def test_citation_checks_catch_invented_files_pages_and_types():
    base = {"source": "BOL_A.pdf", "page": 1, "document_type": "BOL", "shipment_id": "A", "text": None}

    def checks(**change):
        return ev.check_case(case_with(), reply(citations=[{**base, **change}]), FakeService())

    assert all(checks().values())
    assert checks(source="FAKE.pdf")["citations_exist"] is False
    assert checks(document_type="POD")["citation_types"] is False
    assert checks(page=9)["page_valid_or_unknown"] is False
    assert checks(page=None)["page_valid_or_unknown"] is True


def test_overbilling_must_be_negated_and_generated_text_must_be_flagged():
    claim = ev.check_case(case_with(not_overbilling=True), reply(answer="The carrier overbilled us."), FakeService())
    assert claim["no_overbilling_claim"] is False
    fine = ev.check_case(case_with(not_overbilling=True), reply(answer="This does not show overbilling."), FakeService())
    assert fine["no_overbilling_claim"] is True
    unflagged = ev.check_case(case_with(generated=True, facts=[]), reply(generated=False), FakeService())
    assert unflagged["generated_flag"] is False


def test_forbidden_text_and_leaked_citations_are_detected():
    leaked = ev.check_case(case_with(forbid=["SHJ-2026-002"]), reply(answer="see SHJ-2026-002"), FakeService())
    assert leaked["forbid:SHJ-2026-002"] is False
    assert ev.check_case(case_with(no_citations=True), reply(), FakeService())["no_citations"] is False


# ---- summaries ------------------------------------------------------------

def record(id, category, ok=True, ambiguous=False, safety=False, expected="answered", observed="answered", intent="quantity", seen="quantity"):
    return {"id": id, "category": category, "ok": ok, "ambiguous": ambiguous, "safety": safety, "expected_status": expected,
            "observed_status": observed, "expected_intent": intent, "observed_intent": seen, "intent_ok": intent == seen,
            "checks": {"status": ok}, "failed_checks": [] if ok else ["status"]}


def test_ambiguous_cases_are_reported_separately_and_never_scored():
    summary = ev.summarize_assistant([record("a", "quantity"), record("b", "quantity", ok=False, ambiguous=True, observed="insufficient_evidence", seen="none")])
    assert summary["scored"] == 1 and summary["case_accuracy"] == 1.0
    assert summary["ambiguous_cases"] == ["b"] and summary["ambiguous_outcomes"][0]["observed_intent"] == "none"


def test_false_and_missed_answers_and_the_confusion_are_reported():
    summary = ev.summarize_assistant([
        record("m", "quantity", ok=False, observed="insufficient_evidence", seen="none"),
        record("f", "unsupported", ok=False, expected="insufficient_evidence", observed="answered", intent="none", seen="invoice"),
        record("g", "unsupported", expected="insufficient_evidence", observed="insufficient_evidence", intent="none", seen="none"),
    ])
    assert summary["status"]["missed_answers"] == ["m"] and summary["status"]["false_answers"] == ["f"]
    assert summary["routing"]["confusion"]["quantity"] == {"none": 1} and summary["routing"]["confusion"]["none"] == {"invoice": 1, "none": 1}
    assert summary["routing"]["misrouted"] == ["m", "f"]


def test_a_failed_safety_case_is_listed():
    summary = ev.summarize_assistant([record("s", "other_shipment", ok=False, safety=True)])
    assert summary["safety"] == {"cases": 1, "failed": ["s"]}


# ---- thresholds ------------------------------------------------------------

def report(**over):
    base = {"security": {"failed_count": 0}, "assistant": {"safety": {"failed_count": 0}, "citation_check_pass_rate": 1.0, "case_accuracy": 0.9,
            "routing": {"intent_accuracy": 0.9}}, "extraction": {"fields": {"accuracy": 0.95}, "documents": {"accuracy": 1.0}},
            "structured_extraction": {"fields": {"value_accuracy": 0.95, "state_accuracy": 0.95, "false_extractions": 0}},
            "rag": {"cross_shipment_leakage": 0.0, "recall_at_5": 0.9, "citation_precision": 1.0}}
    base.update(over)
    return base


def results(rep, thresholds=None):
    return {t["name"]: t["result"] for t in ev.check_thresholds(rep, thresholds)}


def test_all_thresholds_pass_for_a_good_report():
    assert set(results(report()).values()) == {"pass"}


def test_a_safety_failure_is_a_mandatory_failure():
    assert results(report(security={"failed_count": 1}))["safety_cases_pass"] == "FAIL"
    leak = report(rag={"cross_shipment_leakage": 0.02, "recall_at_5": 1, "citation_precision": 1})
    assert results(leak)["rag_cross_shipment_leakage"] == "FAIL"


def test_a_missing_mandatory_metric_fails_but_a_missing_other_one_is_not_measured():
    rep = report(assistant={"safety": {"failed_count": 0}, "citation_check_pass_rate": None, "case_accuracy": None, "routing": {"intent_accuracy": None}})
    out = results(rep)
    assert out["citation_checks"] == "FAIL" and out["assistant_case_accuracy"] == "not_measured"


def test_a_threshold_can_be_raised_and_then_fails():
    custom = dict(ev.DEFAULT_THRESHOLDS)
    path, op, _, mandatory = custom["routing_intent_accuracy"]
    custom["routing_intent_accuracy"] = (path, op, 0.95, mandatory)
    assert results(report(), custom)["routing_intent_accuracy"] == "FAIL"


def test_skipping_rag_skips_its_thresholds_instead_of_failing_them():
    out = results(report(rag={"skipped": "x"}))
    assert out["rag_cross_shipment_leakage"] == "skipped" and out["rag_recall_at_5"] == "skipped"


# ---- the run, the report and the command -------------------------------------

@pytest.fixture(scope="module")
def full_report():
    return ev.run_all(rag=False)


def test_the_deterministic_run_has_the_documented_shape_and_passes_safety(full_report):
    r = full_report
    assert r["dataset"]["version"] and r["dataset"]["cases"] >= 40 and "no network" in r["mode"]
    assert r["security"]["failed_count"] == 0 and r["assistant"]["safety"]["failed_count"] == 0
    assert r["assistant"]["safety"]["cases"] >= 10 and r["security"]["cases"] >= 25
    assert r["assistant"]["citation_check_pass_rate"] == 1.0
    json.dumps(r)


def test_extraction_metrics_are_calculated_only_from_labels(full_report):
    e = full_report["extraction"]
    assert e["fields"]["total"] > 20 and e["fields"]["missing_field_cases"] == 1 and e["duplicates"]["ok"]
    assert e["not_measured"] and any("OCR" in n for n in e["not_measured"])


def test_the_run_is_reproducible(full_report):
    again = ev.run_all(rag=False)
    keys = ("case_accuracy", "failed")
    assert {k: again["assistant"][k] for k in keys} == {k: full_report["assistant"][k] for k in keys}
    assert again["extraction"]["fields"] == full_report["extraction"]["fields"]


def test_the_run_uses_only_temporary_data(full_report):
    import config
    assert str(config.RAW_DATA_DIR) not in json.dumps(full_report)


def test_the_text_report_shows_the_key_numbers(full_report):
    skipped = {**full_report, "rag": {"skipped": "test"}}
    skipped["thresholds"] = ev.check_thresholds(skipped)
    skipped.update(passed=True, mandatory_failed=[])
    text = ev.format_report(skipped)
    for needle in ("routing (intent) accuracy", "Security/reliability", "Extraction", "RAG  skipped", "Thresholds", "RESULT: PASS"):
        assert needle in text


def test_a_safety_regression_makes_the_command_exit_nonzero(monkeypatch, capsys):
    real = ev.security_cases
    monkeypatch.setattr(ev, "security_cases", lambda service: real(service) + [("sec.injected_failure", False, "was allowed")])
    assert cli.main(["--no-rag"]) == 1
    assert "sec.injected_failure" in capsys.readouterr().out


def test_the_command_writes_json_and_refuses_to_overwrite(tmp_path, capsys):
    target = tmp_path / "out" / "report.json"
    assert cli.main(["--no-rag", "--json", str(target)]) == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["passed"] is True and data["thresholds"] and data["dataset"]["version"]

    assert cli.main(["--no-rag", "--json", str(target)]) == 2
    assert "--force" in capsys.readouterr().err
    assert cli.main(["--no-rag", "--json", str(target), "--force"]) == 0


def test_the_command_exits_2_for_a_malformed_dataset_and_rejects_a_bad_override(tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{oops", encoding="utf-8")
    assert cli.main(["--no-rag", "--cases", str(bad)]) == 2
    assert "Dataset error" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--no-rag", "--min", "nonsense=1"])


def test_a_raised_minimum_fails_the_command():
    assert cli.main(["--no-rag", "--min", "routing_intent_accuracy=1.01"]) == 1
