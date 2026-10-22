"""
LogiDoc-RAG assistant / extraction / RAG evaluation (deterministic).

    python scripts/evaluate_assistant.py
    python scripts/evaluate_assistant.py --json reports/eval.json
    python scripts/evaluate_assistant.py --no-rag                 # skip the slower RAG part
    python scripts/evaluate_assistant.py --min routing_intent_accuracy=0.8

No network, no language model and no production data are used: the corpus is
built in a temporary folder from tests/fixtures and synthetic PDFs. Exit code
0 = every mandatory safety check and every threshold passed; 1 = a failure;
2 = the datasets are malformed. See "Evaluation suite" in the README.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT, ROOT / "src", ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import assistant_eval as ev


def parse_minimums(items):
    thresholds = dict(ev.DEFAULT_THRESHOLDS)

    for item in items or []:
        name, _, value = item.partition("=")

        if name not in thresholds or not value:
            raise SystemExit(f"--min needs NAME=VALUE with NAME one of: {', '.join(thresholds)}")

        path, operator, _, mandatory = thresholds[name]
        thresholds[name] = (path, operator, float(value), mandatory)

    return thresholds


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deterministic evaluation of the assistant, extraction and RAG.")
    parser.add_argument("--cases", action="append", help="a cases file (repeatable; default: v1 baseline, v2 additions and v3 holdout)")
    parser.add_argument("--labels", default=str(ev.LABELS_FILE))
    parser.add_argument("--structured-labels", default=str(ev.STRUCTURED_LABELS_FILE))
    parser.add_argument("--json", help="write the machine-readable report here (an existing file is not overwritten without --force)")
    parser.add_argument("--force", action="store_true", help="allow overwriting the --json file")
    parser.add_argument("--no-rag", action="store_true")
    parser.add_argument("--min", action="append", help="override a threshold, e.g. routing_intent_accuracy=0.8")
    args = parser.parse_args(argv)

    target = Path(args.json) if args.json else None

    if target and target.exists() and not args.force:
        print(f"{target} exists; pass --force to overwrite it.", file=sys.stderr)
        return 2

    try:
        thresholds = parse_minimums(args.min)
        report = ev.run_all(args.cases or ev.DEFAULT_CASE_FILES, args.labels, rag=not args.no_rag, structured_file=args.structured_labels)
    except ev.DatasetError as error:
        print(f"Dataset error: {error}", file=sys.stderr)
        return 2

    report["thresholds"] = ev.check_thresholds(report, thresholds)
    report["passed"] = not any(t["result"] == "FAIL" for t in report["thresholds"])
    report["mandatory_failed"] = [t["name"] for t in report["thresholds"] if t["mandatory"] and t["result"] == "FAIL"]

    print(ev.format_report(report))

    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nJSON report: {target}")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
