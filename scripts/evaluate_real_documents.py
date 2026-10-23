"""
Evaluate structured extraction on real documents we have permission to process.

    python scripts/evaluate_real_documents.py template   [--dir eval_real]
    python scripts/evaluate_real_documents.py validate   [--dir eval_real]
    python scripts/evaluate_real_documents.py coverage   [--dir eval_real]
    python scripts/evaluate_real_documents.py freeze     [--dir eval_real]
    python scripts/evaluate_real_documents.py evaluate   [--dir eval_real] [--split development|heldout] [--show-values]

The directory (default eval_real/, git-ignored) holds the confidential
documents, the manifest and the human-made labels; see
docs/real_world_evaluation/PROTOCOL.md. This script contains no documents and no
labels. Without verified documents it reports NOT MEASURED, never a number.

Reports are written to <dir>/reports/ with values reduced to shapes. --show-values
prints real values to the console only (never to a file) for a person who is
allowed to see them.

Exit codes: 0 done, 2 the directory or a file cannot be used.
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import real_eval as ev  # noqa: E402


def directory(args):
    return Path(args.dir) if Path(args.dir).is_absolute() else PROJECT_ROOT / args.dir


def command_template(root):
    ev.check_privacy(root)

    for name in ("documents", "reports"):
        (root / name).mkdir(parents=True, exist_ok=True)

    wrote = []

    for name, content in (("manifest.json", {"dataset": "real-documents", "documents": []}), ("labels.json", {"labels_version": "1", "documents": {}})):
        target = root / name

        if not target.exists():
            target.write_text(json.dumps(content, indent=1), encoding="utf-8")
            wrote.append(name)

    print(f"{root} is ready (wrote: {', '.join(wrote) or 'nothing, it already existed'}).")
    print("Next: put permitted PDFs in documents/, register them in manifest.json and label them in labels.json (see PROTOCOL.md).")


def command_validate(root):
    dataset = ev.load(root)
    print(f"{len(dataset.manifest.get('documents', []))} document(s) in the manifest, {len(dataset.documents)} valid.")

    for item in dataset.excluded:
        print(f"  EXCLUDED {item['id']}: {item['reason']}")

    for split in ev.SPLITS:
        dataset.scorable(split)
        print(f"{split}: {sum(1 for e in dataset.documents.values() if e['split'] == split)} valid, {len(dataset.unscored)} without usable labels")

        for item in dataset.unscored:
            print(f"  NOT SCORED {item['id']}: {item['reason']}")


def command_coverage(root):
    cover = ev.coverage(ev.load(root))
    print(f"{cover['verified_documents']} verified document(s). Enough for a meaningful first report: {'yes' if cover['enough'] else 'NO'}.")

    for row in cover["rows"]:
        print(f"  [{'ok' if row['met'] else 'MISSING'}] {row['need']}: have {row['have']}, want {row['want']}")


def command_freeze(root):
    record = ev.freeze(root)
    print(f"Held-out set frozen: {len(record['heldout_ids'])} document(s), extractor {record['extractor_version']}.")
    print("From now on a score on it is independent only while the labels, the manifest and the extractor stay as they are.")


def command_evaluate(root, args):
    report = ev.evaluate(root, args.split)
    text = ev.render(report, show_values=False)
    stamp = ev.now_text().replace(":", "").replace("-", "")[:15]
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    (reports / f"real_eval_{args.split}_{stamp}.txt").write_text(text, encoding="utf-8")
    (reports / f"real_eval_{args.split}_{stamp}.json").write_text(json.dumps(ev.to_json(report), indent=1), encoding="utf-8")

    print(ev.render(report, show_values=args.show_values))
    print(f"\nReport files (values redacted): {reports}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Real-document extraction evaluation.")
    parser.add_argument("command", choices=["template", "validate", "coverage", "freeze", "evaluate"])
    parser.add_argument("--dir", default=ev.DEFAULT_DIR, help="the evaluation directory (default: eval_real, git-ignored)")
    parser.add_argument("--split", choices=ev.SPLITS, default="development")
    parser.add_argument("--show-values", action="store_true", help="print real values to the console (never written to a file)")
    args = parser.parse_args(argv)
    root = directory(args)

    try:
        if args.command == "template":
            command_template(root)
        elif args.command == "evaluate":
            command_evaluate(root, args)
        else:
            {"validate": command_validate, "coverage": command_coverage, "freeze": command_freeze}[args.command](root)
    except ev.EvalError as error:
        print(f"Cannot continue: {error}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
