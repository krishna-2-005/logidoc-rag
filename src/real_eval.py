"""
LogiDoc-RAG real-document extraction evaluation

Measures document_extraction against documents we have permission to process
and a human-made ground truth, kept in a separate, access-controlled local
directory (default: eval_real/, git-ignored). It is a harness: it contains no
documents and no labels, and it never reports a number it cannot support.

Layout of the evaluation directory

    manifest.json       one entry per document: type, layout family, split,
                        source permission, redaction, properties, limitations
    labels.json         the ground truth, written by a person (see
                        docs/real_world_evaluation/PROTOCOL.md)
    documents/          the PDFs (read only: nothing here is ever written to)
    freeze.json         written by freeze(): the held-out set, locked
    heldout_runs.jsonl  one line per held-out evaluation (version, date)
    reports/            reports (values redacted by default)

Rules the code enforces, not just documents

    permission     a document without a recorded permission basis, who
                   confirmed it and when is excluded and listed
    integrity      a document whose SHA-256 differs from the manifest is excluded
    verification   labels count only when labelled_by and verified_by are two
                   different people and the labeller attests that the extractor's
                   output was not seen; anything else is listed, not scored
    privacy        the directory must be outside any git repository or ignored by
                   it; reports and failure examples name documents by id only and
                   show values only as shapes ("9999-99-99", "Aaaa 99") unless
                   a person asks to see them on the console
    independence   the held-out split can be evaluated only after freeze(); the
                   report says it is independent only if the labels, the manifest
                   and the extractor are unchanged since the freeze and the set
                   has not been scored by another extractor version. Otherwise it
                   is a development score and says so
    denominators   every metric carries k, n and a Wilson 95% interval; a metric
                   with fewer than MIN_N labelled cases is "not measured"

What this cannot say: a score on a sample of documents is not the accuracy of
the extractor on all freight documents. The report states the sample.
"""

import hashlib
import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import document_extraction as de

DEFAULT_DIR = "eval_real"
MIN_N = 10                               # fewer labelled cases than this: not measured
SPLITS = ("development", "heldout")
DOCUMENT_TYPES = de.DOCUMENT_TYPES + ("OTHER",)
PERMISSION_BASES = ("own_document", "customer_consent", "licensed", "public_domain", "synthetic")
REDACTION = ("none", "partial", "full")
STATUSES = (de.S_EXTRACTED, de.S_PARTIAL, de.S_NEEDS_OCR, de.S_UNREADABLE)
ARITHMETIC = ("match", "mismatch", "unchecked")

# Properties a document may have; coverage asks for a minimum of each
PROPERTIES = (
    "multi_page", "table_layout", "credit", "previous_balance", "tax", "fuel_surcharge", "foreign_currency",
    "unusual_charge_description", "repeated_fields", "conflicting_values", "missing_fields", "ambiguous_signature",
    "image_only", "encrypted", "malformed", "partial_pages",
)

# Judgement, not statistics: the least that makes a first report worth reading
COVERAGE = {
    "documents_per_type": 20, "layout_families_per_type": 4, "heldout_share": 0.3,
    "multi_page": 5, "table_layout": 5, "credit": 3, "previous_balance": 3, "tax": 5, "fuel_surcharge": 5, "foreign_currency": 3,
    "unusual_charge_description": 3, "repeated_fields": 3, "conflicting_values": 5, "missing_fields": 5, "ambiguous_signature": 3,
    "image_only": 5, "encrypted": 2, "malformed": 3, "partial_pages": 2,
}


class EvalError(ValueError):
    """The evaluation directory or a file in it cannot be used."""


class PrivacyError(EvalError):
    """Evaluating here could put confidential documents into a repository."""


def now_text():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvalError(f"{Path(path).name} is missing") from error
    except json.JSONDecodeError as error:
        raise EvalError(f"{Path(path).name} is not valid JSON: {error.msg} (line {error.lineno})") from error


# ============================================================
# STATISTICS AND REDACTION
# ============================================================

def wilson(k, n, z=1.96):
    """The 95% Wilson interval of k successes in n; None for n = 0."""

    if n == 0:
        return None

    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator

    return (max(0.0, centre - spread), min(1.0, centre + spread))


def prop(k, n, minimum=None):
    """A proportion with its denominator, interval and whether it is measured at all."""

    minimum = MIN_N if minimum is None else minimum
    measured = n >= minimum
    interval = wilson(k, n) if measured else None

    return {"k": k, "n": n, "rate": (k / n) if measured else None, "ci95": list(interval) if interval else None, "measured": measured}


def shape(value, limit=32):
    """
    A value reduced to its shape: letters become a/A, digits 9, other characters
    stay. It shows what kind of value was found without showing the value.
    """

    if value is None:
        return "none"

    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}: {shape(item, limit)}" for key, item in value.items()) + "}"

    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(shape(item, limit) for item in value[:3]) + (", ..." if len(value) > 3 else "") + "]"

    text = str(value)
    out = "".join("9" if c.isdigit() else ("A" if c.isupper() else ("a" if c.isalpha() else c)) for c in text[:limit])

    return out + ("..." if len(text) > limit else "")


def shown(value, show_values):
    return json.dumps(value, ensure_ascii=False, default=str)[:60] if show_values else shape(value)


# ============================================================
# PRIVACY
# ============================================================

def check_privacy(root):
    """
    Refuses a directory that lies inside a git repository without being ignored
    by it: confidential documents must not be one `git add .` from a commit.
    """

    root = Path(root).resolve()
    probe = root / "manifest.json"

    inside = subprocess.run(["git", "-C", str(root if root.exists() else root.parent), "rev-parse", "--show-toplevel"], capture_output=True, text=True)

    if inside.returncode != 0:
        return                                      # not in a repository

    ignored = subprocess.run(["git", "-C", inside.stdout.strip(), "check-ignore", "-q", str(probe)], capture_output=True)

    if ignored.returncode != 0:
        raise PrivacyError(
            f"{root} is inside a git repository and is not ignored by it. Add it to .gitignore or use a directory outside the repository; "
            "confidential documents must never be committable."
        )


# ============================================================
# LOADING AND VALIDATION
# ============================================================

class Dataset:
    def __init__(self, root, manifest, labels):
        self.root, self.manifest, self.labels = Path(root), manifest, labels
        self.documents, self.excluded = {}, []
        self.manifest_sha = sha256_file(self.root / "manifest.json")
        self.labels_sha = sha256_file(self.root / "labels.json")
        self.validate()

    def exclude(self, doc_id, reason):
        self.excluded.append({"id": doc_id, "reason": reason})

    def validate(self):
        seen = set()

        for entry in self.manifest.get("documents", []):
            doc_id = entry.get("id")

            if not isinstance(doc_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", doc_id or ""):
                self.exclude(str(doc_id), "the id must be 1-40 letters, digits, dots, dashes or underscores")
                continue

            if doc_id in seen:
                self.exclude(doc_id, "duplicate id")
                continue

            seen.add(doc_id)
            problem = self.problem_with(entry)

            if problem:
                self.exclude(doc_id, problem)
            else:
                self.documents[doc_id] = entry

    def problem_with(self, entry):
        permission = entry.get("source_permission") or {}
        redaction = entry.get("redaction") or {}

        if entry.get("document_type") not in DOCUMENT_TYPES:
            return f"document_type must be one of {list(DOCUMENT_TYPES)}"

        if entry.get("split") not in SPLITS:
            return f"split must be one of {list(SPLITS)}"

        if not entry.get("layout_family"):
            return "layout_family is required (a pseudonymous label, never a customer or carrier name)"

        if permission.get("basis") not in PERMISSION_BASES or not permission.get("confirmed_by") or not permission.get("confirmed_on"):
            return "source_permission needs a basis, who confirmed it and when: a document without a recorded permission is not processed"

        if redaction.get("status") not in REDACTION:
            return f"redaction.status must be one of {list(REDACTION)}"

        unknown = [item for item in entry.get("properties", []) if item not in PROPERTIES]

        if unknown:
            return f"unknown properties: {unknown}"

        path = self.root / str(entry.get("file", ""))

        if not str(entry.get("file", "")).startswith("documents/") or ".." in Path(entry["file"]).parts or not path.is_file():
            return "file must be a path under documents/ that exists"

        if sha256_file(path) != entry.get("sha256"):
            return "the file's SHA-256 differs from the manifest: it changed since it was registered"

        return None

    def label_problem(self, doc_id):
        """Why the labels of a document do not count; None when they do."""

        label = (self.labels.get("documents") or {}).get(doc_id)

        if not label:
            return "no labels"

        if not label.get("labelled_by") or not label.get("verified_by"):
            return "labels need a labeller and an independent verifier"

        if label["labelled_by"] == label["verified_by"]:
            return "the labeller and the verifier are the same person: not independently verified"

        if label.get("labelled_without_extractor_output") is not True:
            return "the labeller did not attest that the extractor's output was not seen"

        for item in label.get("fields", []):
            if item.get("state") not in de.STATES or "field" not in item:
                return f"a label has an unknown state or no field: {shape(item.get('field'))}"

            if item["state"] == de.EXTRACTED and "value" not in item:
                return "an EXTRACTED label has no value"

        return None

    def scorable(self, split):
        """[(id, entry, labels)] of valid, verified documents in the split; the rest are listed in self.unscored."""

        self.unscored = []
        result = []

        for doc_id, entry in self.documents.items():
            if entry["split"] != split:
                continue

            problem = self.label_problem(doc_id)

            if problem:
                self.unscored.append({"id": doc_id, "reason": problem})
            else:
                result.append((doc_id, entry, self.labels["documents"][doc_id]))

        return result


def load(root):
    root = Path(root)
    check_privacy(root)

    return Dataset(root, read_json(root / "manifest.json"), read_json(root / "labels.json"))


# ============================================================
# COVERAGE: what data a meaningful report needs
# ============================================================

def coverage(dataset):
    """What the verified documents cover against COVERAGE, and what is still missing."""

    verified = {doc_id: entry for doc_id, entry in dataset.documents.items() if dataset.label_problem(doc_id) is None}
    rows = []

    for doc_type in de.DOCUMENT_TYPES:
        items = [entry for entry in verified.values() if entry["document_type"] == doc_type]
        families = {entry["layout_family"] for entry in items}
        heldout = sum(1 for entry in items if entry["split"] == "heldout")

        rows.append({"need": f"{doc_type}: verified documents", "have": len(items), "want": COVERAGE["documents_per_type"]})
        rows.append({"need": f"{doc_type}: layout families", "have": len(families), "want": COVERAGE["layout_families_per_type"]})
        rows.append({"need": f"{doc_type}: held-out share", "have": round(heldout / len(items), 2) if items else 0.0, "want": COVERAGE["heldout_share"]})

    counts = Counter(prop_name for entry in verified.values() for prop_name in entry.get("properties", []))

    for name in PROPERTIES:
        rows.append({"need": f"documents with {name.replace('_', ' ')}", "have": counts.get(name, 0), "want": COVERAGE[name]})

    for row in rows:
        row["met"] = row["have"] >= row["want"]

    return {"verified_documents": len(verified), "rows": rows, "enough": all(row["met"] for row in rows)}


# ============================================================
# FREEZING THE HELD-OUT SET
# ============================================================

def freeze(root):
    """
    Locks the held-out set: records the manifest, the labels, the held-out ids and
    the extractor version at this moment. Refuses unless every held-out document
    is valid and verified, and refuses to overwrite an earlier freeze.
    """

    dataset = load(root)
    path = dataset.root / "freeze.json"

    if path.exists():
        raise EvalError("the held-out set is already frozen; a new set needs a new directory (a second freeze would hide earlier use)")

    heldout = [doc_id for doc_id, entry in dataset.documents.items() if entry["split"] == "heldout"]
    problems = {doc_id: dataset.label_problem(doc_id) for doc_id in heldout if dataset.label_problem(doc_id)}

    if not heldout:
        raise EvalError("there are no valid held-out documents to freeze")

    if problems:
        raise EvalError("held-out documents without verified labels: " + ", ".join(sorted(problems)))

    record = {"frozen_on": now_text(), "manifest_sha256": dataset.manifest_sha, "labels_sha256": dataset.labels_sha,
              "extractor_version": de.EXTRACTOR_VERSION, "heldout_ids": sorted(heldout)}
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")

    return record


def independence(dataset, split):
    """(independent, [reasons]): whether a score on this split can be called independent performance."""

    if split != "heldout":
        return False, ["the development split is used to improve the extractor, so its score is not independent"]

    path = dataset.root / "freeze.json"

    if not path.exists():
        return False, ["the held-out set has not been frozen"]

    frozen, reasons = read_json(path), []
    runs = dataset.root / "heldout_runs.jsonl"
    earlier = [json.loads(line) for line in runs.read_text(encoding="utf-8").splitlines() if line.strip()] if runs.exists() else []

    if frozen["manifest_sha256"] != dataset.manifest_sha or frozen["labels_sha256"] != dataset.labels_sha:
        reasons.append("the manifest or the labels changed after the freeze")

    if frozen["extractor_version"] != de.EXTRACTOR_VERSION:
        reasons.append("the extractor changed since the freeze (it cannot be shown that it was not tuned on this set): report it as a development score")

    if {item["extractor_version"] for item in earlier} - {de.EXTRACTOR_VERSION}:
        reasons.append("this held-out set was already scored by other extractor versions, so it has guided development")

    return not reasons, reasons


# ============================================================
# SCORING
# ============================================================

def kind_of(doc_type, field):
    spec = de.SCHEMAS.get(doc_type, {}).get(field)

    return spec.kind if spec else "text"


def comparable(kind, value):
    """A value in a form that can be compared for an exact match."""

    if isinstance(value, bool) or value is None:
        return value

    if isinstance(value, (int, float)):
        return round(float(value), 2)

    if isinstance(value, dict):
        out = {key: comparable(kind, item) for key, item in value.items()}
        return json.dumps(out, sort_keys=True, default=str)

    if isinstance(value, list):
        return json.dumps(value, default=str)

    return str(value)


def normalized(kind, value):
    """Like comparable, with text compared without case and spacing differences."""

    out = comparable(kind, value)

    return " ".join(out.split()).casefold() if isinstance(out, str) else out


def arithmetic_of(extraction):
    total = extraction.fields.get("invoice_total")

    if total is None or total.state != de.EXTRACTED:
        return "unchecked"

    codes = {issue["code"] for issue in de.validate_shipment([extraction])}

    if "invoice_arithmetic_mismatch" in codes:
        return "mismatch"

    return "unchecked" if "invoice_arithmetic_unchecked" in codes else "match"


def score(dataset, split, extract=de.extract_document):
    """
    Scores every valid, verified document of the split. Returns the records and
    the metrics; no value or file name is kept beyond what the failure examples
    need (and those are redacted when rendered).
    """

    items = dataset.scorable(split)
    records, documents, crashes = [], [], []
    line_tp = line_fp = line_fn = 0

    for doc_id, entry, label in items:
        doc_type, family = entry["document_type"], entry["layout_family"]

        try:
            extraction = extract(dataset.root / entry["file"])
        except Exception as error:  # noqa: BLE001 - extraction must never raise; a crash is a finding
            crashes.append({"id": doc_id, "error": type(error).__name__})
            continue

        documents.append({
            "id": doc_id, "type": doc_type, "family": family, "properties": entry.get("properties", []),
            "type_ok": extraction.document_type == doc_type,
            "expected_status": label.get("document_status"), "observed_status": extraction.status,
            "expected_complete": label.get("expected_complete"), "observed_complete": extraction.complete,
            "expected_arithmetic": label.get("arithmetic"), "observed_arithmetic": arithmetic_of(extraction) if doc_type == de.INVOICE else None,
        })

        for item in label.get("fields", []):
            name, expected = item["field"], item["state"]
            observed = extraction.fields.get(name)
            state = observed.state if observed else "ABSENT"
            kind = kind_of(doc_type, name)
            value_exact = value_norm = page_ok = None

            if expected == de.EXTRACTED:
                same = state == de.EXTRACTED
                value_exact = same and comparable(kind, observed.value) == comparable(kind, item["value"])
                value_norm = same and normalized(kind, observed.value) == normalized(kind, item["value"])

                if "page" in item:
                    page_ok = bool(value_exact) and observed.page == item["page"]

            records.append({
                "doc": doc_id, "type": doc_type, "family": family, "field": name, "kind": kind, "expected": expected, "observed": state,
                # A field the extractor has no entry for on this document type is unsupported, not a wrong state
                "state_ok": state == expected or (state == "ABSENT" and expected == de.UNSUPPORTED), "value_exact": value_exact, "value_norm": value_norm, "page_ok": page_ok,
                "false_extraction": state == de.EXTRACTED and expected != de.EXTRACTED,
                "value_error": state == de.EXTRACTED and expected == de.EXTRACTED and value_exact is False,
                "page_unknown": bool(item.get("page_unknown")),
                "invented_page": bool(state == de.EXTRACTED and item.get("page_unknown") and observed.page is not None),
                "expected_value": item.get("value"), "observed_value": observed.value if observed else None,
                "expected_page": item.get("page"), "observed_page": observed.page if observed else None,
            })

        if "line_items" in label:
            wanted = {(" ".join(str(d).split()).casefold(), round(float(a), 2)) for d, a in label["line_items"]}
            found = {(" ".join(i.description.split()).casefold(), round(float(i.amount), 2)) for i in extraction.line_items}
            line_tp += len(wanted & found)
            line_fp += len(found - wanted)
            line_fn += len(wanted - found)

    return metrics(records, documents, crashes, (line_tp, line_fp, line_fn), dataset, split, len(items))


def group(records, key):
    out = defaultdict(list)

    for record in records:
        out[key(record)].append(record)

    return dict(sorted(out.items(), key=lambda pair: str(pair[0])))


def metrics(records, documents, crashes, lines, dataset, split, n_documents):
    extracted = [r for r in records if r["expected"] == de.EXTRACTED]
    not_extracted = [r for r in records if r["expected"] != de.EXTRACTED]

    def value_rate(rows, field="value_exact"):
        return prop(sum(bool(r[field]) for r in rows), len(rows))

    by_type = {t: {"exact": value_rate(rows), "labelled_fields": len(rows)} for t, rows in group(extracted, lambda r: r["type"]).items()}
    by_field = {f"{t}.{f}": value_rate(rows) for (t, f), rows in group(extracted, lambda r: (r["type"], r["field"])).items()}
    by_kind = {k: value_rate(rows) for k, rows in group(extracted, lambda r: r["kind"]).items()}
    by_family = {f"{t}/{fam}": value_rate(rows) for (t, fam), rows in group(extracted, lambda r: (r["type"], r["family"])).items()}

    currency_rows = [r for r in records if r["field"] == "currency"]
    expected_code = [r for r in currency_rows if r["expected"] == de.EXTRACTED]
    expected_none = [r for r in currency_rows if r["expected"] != de.EXTRACTED]
    conflicts = [r for r in records if r["expected"] == de.CONFLICTING]
    observed_conflicts = [r for r in records if r["observed"] == de.CONFLICTING]
    missing = [r for r in records if r["expected"] == de.MISSING]
    paged = [r for r in extracted if r["page_ok"] is not None]
    unknown_page_claims = [r for r in records if r["page_unknown"] and r["observed"] == de.EXTRACTED]

    confusion = Counter((r["expected"], r["observed"]) for r in records)

    docs_with_status = [d for d in documents if d["expected_status"]]
    docs_with_review = [d for d in documents if d["expected_complete"] is not None]
    unsafe = [d for d in docs_with_review if d["expected_complete"] is False]
    arithmetic = [d for d in documents if d["expected_arithmetic"] and d["observed_arithmetic"]]
    mismatch_expected = [d for d in arithmetic if d["expected_arithmetic"] == "mismatch"]
    mismatch_observed = [d for d in arithmetic if d["observed_arithmetic"] == "mismatch"]

    failure_properties = {}

    for name in ("image_only", "encrypted", "malformed"):
        rows = [d for d in documents if name in d["properties"] and d["expected_status"]]
        failure_properties[name] = {"status_correct": prop(sum(d["expected_status"] == d["observed_status"] for d in rows), len(rows)),
                                    "documents": len(rows)}

    tp, fp, fn = lines

    return {
        "split": split, "documents_scored": len(documents), "documents_selected": n_documents, "crashes": crashes,
        "excluded": dataset.excluded, "unscored": dataset.unscored,
        "labelled_fields": len(records),
        "exact_match": value_rate(extracted), "normalized_text_match": value_rate(extracted, "value_norm"),
        "by_document_type": by_type, "by_field": by_field, "by_kind": by_kind, "by_layout_family": by_family,
        "numeric_parsing": prop(sum(bool(r["value_exact"]) for r in extracted if r["kind"] in ("money", "count", "weight")), sum(1 for r in extracted if r["kind"] in ("money", "count", "weight"))),
        "date_parsing": prop(sum(bool(r["value_exact"]) for r in extracted if r["kind"] == "date"), sum(1 for r in extracted if r["kind"] == "date")),
        "currency": {
            "correct_code": prop(sum(bool(r["value_exact"]) for r in expected_code), len(expected_code)),
            "assumed_when_not_stated": prop(sum(r["observed"] == de.EXTRACTED for r in expected_none), len(expected_none)),
        },
        "conflict_detection": {"recall": prop(sum(r["observed"] == de.CONFLICTING for r in conflicts), len(conflicts)),
                               "false_conflict": prop(sum(r["expected"] != de.CONFLICTING for r in observed_conflicts), len(observed_conflicts))},
        "false_extraction": prop(sum(r["false_extraction"] for r in not_extracted), len(not_extracted)),
        "wrong_value_shown_as_extracted": prop(sum(r["value_error"] for r in extracted), len(extracted)),
        "missing_detection": prop(sum(r["observed"] == de.MISSING for r in missing), len(missing)),
        "state_accuracy": prop(sum(r["state_ok"] for r in records), len(records)),
        "state_confusion": {f"{a}->{b}": c for (a, b), c in sorted(confusion.items())},
        "provenance": {"page_correct": prop(sum(bool(r["page_ok"]) for r in paged), len(paged)),
                       "invented_pages": prop(sum(r["invented_page"] for r in unknown_page_claims), len(unknown_page_claims))},
        "document_type": prop(sum(d["type_ok"] for d in documents), len(documents)),
        "document_status": prop(sum(d["expected_status"] == d["observed_status"] for d in docs_with_status), len(docs_with_status)),
        "review_state": {"agreement": prop(sum(d["expected_complete"] == d["observed_complete"] for d in docs_with_review), len(docs_with_review)),
                         "false_completion": prop(sum(bool(d["observed_complete"]) for d in unsafe), len(unsafe))},
        "invoice_arithmetic": {"agreement": prop(sum(d["expected_arithmetic"] == d["observed_arithmetic"] for d in arithmetic), len(arithmetic)),
                               "mismatch_recall": prop(sum(d["observed_arithmetic"] == "mismatch" for d in mismatch_expected), len(mismatch_expected)),
                               "mismatch_precision": prop(sum(d["expected_arithmetic"] == "mismatch" for d in mismatch_observed), len(mismatch_observed))},
        "line_items": {"precision": prop(tp, tp + fp), "recall": prop(tp, tp + fn)},
        "failure_properties": failure_properties,
        "_records": records,
    }


# ============================================================
# HELD-OUT BOOKKEEPING AND REPORT
# ============================================================

def record_heldout_run(dataset, result):
    line = {"at": now_text(), "extractor_version": de.EXTRACTOR_VERSION, "documents": result["documents_scored"]}

    with open(dataset.root / "heldout_runs.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def evaluate(root, split="development"):
    """
    Loads, scores and, for the held-out split, checks independence and records the
    run. Returns {result, coverage, independent, reasons, version}.
    """

    dataset = load(root)

    if split == "heldout" and not (dataset.root / "freeze.json").exists():
        raise EvalError("the held-out split can be scored only after freeze(): freeze it first, so its use is on record")

    independent, reasons = independence(dataset, split)
    result = score(dataset, split)
    cover = coverage(dataset)

    if split == "heldout":
        record_heldout_run(dataset, result)

    return {"result": result, "coverage": cover, "independent": independent, "reasons": reasons, "extractor_version": de.EXTRACTOR_VERSION,
            "dataset": {"documents_in_manifest": len(dataset.manifest.get("documents", [])), "valid": len(dataset.documents), "excluded": dataset.excluded}}


def fmt(metric):
    if metric["n"] == 0:
        return "not measured (0 labelled cases)"

    if not metric["measured"]:
        return f"not measured: {metric['k']} of {metric['n']} (fewer than {MIN_N} labelled cases)"

    low, high = metric["ci95"]

    return f"{metric['rate'] * 100:.1f}% ({metric['k']} of {metric['n']}; 95% interval {low * 100:.0f}-{high * 100:.0f}%)"


def failures(records, limit=25):
    """The first failures: wrong state, wrong value, wrong page, unsupported shown as valid."""

    out = [r for r in records if not r["state_ok"] or r["value_exact"] is False or r["page_ok"] is False or r["invented_page"]]

    return out[:limit], len(out)


def render(report, show_values=False):
    result, cover = report["result"], report["coverage"]
    lines = ["LogiDoc-RAG real-document extraction evaluation", ""]

    if result["documents_scored"] == 0:
        lines += [
            "RESULT: NOT MEASURED. No valid, independently verified document was scored. No real-world accuracy is reported or implied.",
            f"  extractor version: {report['extractor_version']}", "",
        ]
    else:
        lines += [
            f"Split: {result['split']}   documents scored: {result['documents_scored']}   labelled fields: {result['labelled_fields']}   extractor {report['extractor_version']}",
            "Independent performance: " + ("YES (frozen held-out set, unchanged extractor, first scoring)" if report["independent"] else "NO"),
            *[f"  - {reason}" for reason in report["reasons"]], "",
            "This is a score on THIS sample of documents. It is not the accuracy of the extractor on freight documents in general.", "",
            f"Exact-match accuracy (typed values): {fmt(result['exact_match'])}",
            f"  text compared without case or spacing: {fmt(result['normalized_text_match'])}",
            f"  numeric parsing (amounts, counts, weights): {fmt(result['numeric_parsing'])}",
            f"  date parsing: {fmt(result['date_parsing'])}",
            "By document type:", *[f"  {t:<18} {fmt(m['exact'])}" for t, m in result["by_document_type"].items()],
            "By field:", *[f"  {f:<42} {fmt(m)}" for f, m in result["by_field"].items()],
            "By layout family:", *[f"  {f:<42} {fmt(m)}" for f, m in result["by_layout_family"].items()], "",
            f"Currency: correct code {fmt(result['currency']['correct_code'])}",
            f"          assumed when the document does not state one {fmt(result['currency']['assumed_when_not_stated'])}",
            f"Conflicts: detected {fmt(result['conflict_detection']['recall'])}; reported but not real {fmt(result['conflict_detection']['false_conflict'])}",
            f"False extraction (a field that is missing, unreadable, unparseable, unsupported or conflicting shown as extracted): {fmt(result['false_extraction'])}",
            f"Wrong value shown as extracted: {fmt(result['wrong_value_shown_as_extracted'])}",
            f"Missing-field detection: {fmt(result['missing_detection'])}",
            f"State accuracy (all labelled fields): {fmt(result['state_accuracy'])}",
            f"Provenance: page correct {fmt(result['provenance']['page_correct'])}; page given where the label says the page is unknown {fmt(result['provenance']['invented_pages'])}",
            f"Document type recognised: {fmt(result['document_type'])}",
            f"Document status (extracted, partial, needs OCR, unreadable): {fmt(result['document_status'])}",
            f"Review state: agreement {fmt(result['review_state']['agreement'])}; documents that should need review shown as complete {fmt(result['review_state']['false_completion'])}",
            f"Invoice arithmetic: agreement {fmt(result['invoice_arithmetic']['agreement'])}; mismatch recall {fmt(result['invoice_arithmetic']['mismatch_recall'])}; mismatch precision {fmt(result['invoice_arithmetic']['mismatch_precision'])}",
            f"Line items: precision {fmt(result['line_items']['precision'])}; recall {fmt(result['line_items']['recall'])}",
            "Malformed, encrypted and scanned files (status handled correctly):",
            *[f"  {name:<11} {fmt(m['status_correct'])}" for name, m in result["failure_properties"].items()],
            f"Crashes: {len(result['crashes'])} {[c['id'] for c in result['crashes']]}", "",
        ]

        shown_failures, total = failures(result["_records"])
        lines += [f"Failure examples ({len(shown_failures)} of {total}; values {'SHOWN' if show_values else 'shown as shapes only'}):"]
        lines += [f"  {r['doc']} {r['type']}.{r['field']}: expected {r['expected']} {shown(r['expected_value'], show_values)} p{r['expected_page']}, "
                  f"observed {r['observed']} {shown(r['observed_value'], show_values)} p{r['observed_page']}" for r in shown_failures]
        lines += ["State confusion (expected->observed): " + ", ".join(f"{k}: {v}" for k, v in result["state_confusion"].items()), ""]

    lines += [f"Excluded from the manifest ({len(result['excluded'])}): " + "; ".join(f"{e['id']}: {e['reason'][:70]}" for e in result["excluded"][:10]),
              f"Valid but not scored ({len(result['unscored'])}): " + "; ".join(f"{e['id']}: {e['reason'][:60]}" for e in result["unscored"][:10]), "",
              f"Coverage: {cover['verified_documents']} verified document(s). Enough for a meaningful first report: {'yes' if cover['enough'] else 'NO'}.",
              "  " + "\n  ".join(f"[{'ok' if row['met'] else 'MISSING'}] {row['need']}: have {row['have']}, want {row['want']}" for row in cover["rows"] if not row["met"])]

    return "\n".join(lines)


def to_json(report):
    """The report as data: no values, no file names, no raw records."""

    clean = json.loads(json.dumps(report, default=str))
    clean["result"].pop("_records", None)

    return clean
