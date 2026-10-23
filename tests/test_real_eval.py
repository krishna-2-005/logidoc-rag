"""
Tests of the real-document evaluation HARNESS (src/real_eval.py).

Every document here is a synthetic self-test fixture written by the test. These
tests prove the metric arithmetic, the rejection rules, the independence rules
and the privacy guarantees. They produce no real-world accuracy figure and none
should be read from them.

Run: python -m pytest tests/test_real_eval.py
"""

import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_extraction as de  # noqa: E402
import evaluate_real_documents as cli  # noqa: E402
import real_eval as ev  # noqa: E402

SENTINEL = "ZQX-SENTINEL-5520"
PERMISSION = {"basis": "synthetic", "confirmed_by": "test", "confirmed_on": "2026-01-01"}
REDACTION = {"status": "none", "method": "not needed: synthetic"}


REAL_CHECK_PRIVACY = ev.check_privacy


@pytest.fixture(autouse=True)
def small_threshold(monkeypatch):
    """
    The self-tests use small samples (the real threshold has its own tests) and
    work in temporary folders, which may sit inside some parent repository (a
    home directory under git, for one): the privacy guard is exercised by its own
    tests below, not by every test.
    """

    monkeypatch.setattr(ev, "MIN_N", 1)
    monkeypatch.setattr(ev, "check_privacy", lambda root: None)


def write_pdf(path, pages):
    document = pymupdf.open()

    for text in pages:
        page = document.new_page()

        if text:
            for index, line in enumerate(text.split("\n")):
                page.insert_text((40, 50 + index * 14), line, fontsize=9)

    document.save(str(path))
    document.close()


def text(title, shipment, pairs):
    lines = [title, f"Shipment Number: {shipment}"]

    for label, value in pairs:
        lines += [f"{label}:", value]

    return "\n".join(lines)


def invoice(shipment, *extra, total="$110.00", items=(("Base Freight", "$100.00"), ("Fuel Surcharge", "$10.00"))):
    return text("FREIGHT INVOICE", shipment, [("Invoice Number", f"INV-{shipment[-3:]}"), *items, *extra, ("Total Invoice Amount", total)])


def field(name, state, value=None, page=None, **extra):
    item = {"field": name, "state": state, **extra}

    if value is not None:
        item["value"] = value

    if page is not None:
        item["page"] = page

    return item


def build(tmp_path, specs, labelled_by="a", verified_by="b", attested=True):
    """
    specs: [{id, type, family, split, pages | bytes | image_only | encrypted, properties,
             fields, status, complete, arithmetic, line_items, verified}].
    Returns the evaluation directory.
    """

    root = Path(tmp_path) / "eval_dir"
    (root / "documents").mkdir(parents=True)
    manifest, labels = [], {}

    for spec in specs:
        path = root / "documents" / f"{spec['id']}.pdf"

        if "bytes" in spec:
            path.write_bytes(spec["bytes"])
        elif spec.get("image_only"):
            document = pymupdf.open()
            picture = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40))
            picture.clear_with(180)
            document.new_page().insert_image(pymupdf.Rect(50, 50, 150, 150), pixmap=picture)
            document.save(str(path))
            document.close()
        elif spec.get("encrypted"):
            document = pymupdf.open()
            document.new_page().insert_text((40, 60), "x")
            document.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
            document.close()
        else:
            write_pdf(path, spec["pages"])

        manifest.append({
            "id": spec["id"], "file": f"documents/{spec['id']}.pdf", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "document_type": spec.get("type", "INVOICE"), "layout_family": spec.get("family", "family-01"), "split": spec.get("split", "development"),
            "source_permission": spec.get("permission", PERMISSION), "redaction": REDACTION, "properties": spec.get("properties", []), "limitations": "self-test fixture",
        })

        if spec.get("verified", True):
            label = {"labelled_by": labelled_by, "verified_by": verified_by, "verified_on": "2026-01-02", "labelled_without_extractor_output": attested,
                     "document_status": spec.get("status", "extracted"), "fields": spec.get("fields", [])}

            for key in ("complete", "arithmetic", "line_items"):
                if key in spec:
                    label["expected_complete" if key == "complete" else key] = spec[key]

            labels[spec["id"]] = label

    (root / "manifest.json").write_text(json.dumps({"dataset": "self-test", "documents": manifest}), encoding="utf-8")
    (root / "labels.json").write_text(json.dumps({"documents": labels}), encoding="utf-8")

    return root


def run(root, split="development"):
    return ev.evaluate(root, split)["result"]


# ---- statistics and redaction ------------------------------------------------------------------

def test_wilson_interval_and_the_not_measured_threshold(monkeypatch):
    low, high = ev.wilson(5, 10)

    assert round(low, 3) == 0.237 and round(high, 3) == 0.763 and ev.wilson(0, 0) is None

    monkeypatch.setattr(ev, "MIN_N", 10)
    small, enough = ev.prop(3, 9), ev.prop(9, 10)

    assert small["measured"] is False and small["rate"] is None and small["ci95"] is None and (small["k"], small["n"]) == (3, 9)
    assert enough["measured"] is True and enough["rate"] == 0.9 and "not measured: 3 of 9" in ev.fmt(small) and "9 of 10" in ev.fmt(enough)
    assert ev.fmt(ev.prop(0, 0)) == "not measured (0 labelled cases)"


@pytest.mark.parametrize("value", ["INV-20458", "2026-10-03", "Acme Industrial Components", 1550.5, {"name": "Jane Roe", "address_lines": ["12 Dock Rd"]}])
def test_shapes_never_contain_the_value(value):
    shaped = ev.shape(value)
    original = json.dumps(value)

    assert not any(chunk in shaped for chunk in ("INV", "Acme", "Jane", "Dock", "2026", "20458", "1550"))
    assert len(shaped) <= len(original) + 8 and any(c in shaped for c in "9aA")


# ---- privacy ----------------------------------------------------------------------------------

def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_a_directory_inside_a_repository_must_be_ignored_by_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    inside = repo / "eval_real"
    inside.mkdir()

    with pytest.raises(ev.PrivacyError, match="not ignored"):
        REAL_CHECK_PRIVACY(inside)

    (repo / ".gitignore").write_text("eval_real/\n", encoding="utf-8")
    REAL_CHECK_PRIVACY(inside)                                # ignored: fine


def test_a_directory_outside_any_repository_is_allowed(tmp_path, monkeypatch):
    real_run = subprocess.run

    def not_a_repository(command, *args, **kwargs):
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 128, "", "fatal: not a git repository")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(ev.subprocess, "run", not_a_repository)
    REAL_CHECK_PRIVACY(tmp_path / "anywhere")


def test_the_project_default_directory_is_ignored():
    REAL_CHECK_PRIVACY(PROJECT_ROOT / ev.DEFAULT_DIR)


def test_loading_refuses_an_unignored_directory_before_reading_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "check_privacy", REAL_CHECK_PRIVACY)
    repo = tmp_path / "repo"
    (repo / "eval_real").mkdir(parents=True)
    git("init", "-q", cwd=repo)

    with pytest.raises(ev.PrivacyError):
        ev.load(repo / "eval_real")


# ---- manifest validation --------------------------------------------------------------------------

def test_a_document_without_a_recorded_permission_is_excluded_and_listed(tmp_path):
    root = build(tmp_path, [
        {"id": "OK", "pages": [invoice("SHJ-1")], "fields": []},
        {"id": "NOPERM", "pages": [invoice("SHJ-2")], "permission": {"basis": "customer_consent"}},
        {"id": "ODD", "pages": [invoice("SHJ-3")], "permission": {"basis": "found_online", "confirmed_by": "x", "confirmed_on": "y"}},
    ])
    dataset = ev.load(root)

    assert set(dataset.documents) == {"OK"}
    assert {item["id"]: item["reason"][:25] for item in dataset.excluded} == {"NOPERM": "source_permission needs a", "ODD": "source_permission needs a"}


@pytest.mark.parametrize("change, reason", [
    ({"document_type": "MEMO"}, "document_type must be one of"), ({"split": "test"}, "split must be one of"), ({"layout_family": ""}, "layout_family is required"),
    ({"redaction": {"status": "maybe"}}, "redaction.status"), ({"properties": ["telepathy"]}, "unknown properties"),
    ({"file": "../outside.pdf"}, "file must be a path under documents/"), ({"file": "documents/nothing.pdf"}, "file must be a path under documents/"),
    ({"sha256": "0" * 64}, "SHA-256 differs"),
])
def test_invalid_manifest_entries_are_excluded_with_the_reason(tmp_path, change, reason):
    root = build(tmp_path, [{"id": "D1", "pages": [invoice("SHJ-1")]}])
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["documents"][0].update(change)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    dataset = ev.load(root)

    assert dataset.documents == {} and reason in dataset.excluded[0]["reason"]


def test_duplicate_and_unsafe_ids_are_excluded(tmp_path):
    root = build(tmp_path, [{"id": "D1", "pages": [invoice("SHJ-1")]}])
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["documents"] += [dict(manifest["documents"][0]), {**manifest["documents"][0], "id": "../etc/passwd"}]
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    dataset = ev.load(root)

    assert list(dataset.documents) == ["D1"] and [e["reason"][:12] for e in dataset.excluded] == ["duplicate id", "the id must "]


# ---- label verification --------------------------------------------------------------------------------

def test_labels_count_only_when_independently_verified_and_attested(tmp_path):
    good = [field("invoice_total", "EXTRACTED", 110.0, 1)]
    root = build(tmp_path, [
        {"id": "OK", "pages": [invoice("SHJ-1")], "fields": good},
        {"id": "NOLABEL", "pages": [invoice("SHJ-2")], "verified": False},
        {"id": "NOVALUE", "pages": [invoice("SHJ-3")], "fields": [field("invoice_total", "EXTRACTED")]},
        {"id": "BADSTATE", "pages": [invoice("SHJ-4")], "fields": [field("invoice_total", "MAYBE")]},
    ])
    dataset = ev.load(root)
    scorable = [doc_id for doc_id, *_ in dataset.scorable("development")]

    assert scorable == ["OK"]
    assert {u["id"]: u["reason"][:20] for u in dataset.unscored} == {"NOLABEL": "no labels", "NOVALUE": "an EXTRACTED label h", "BADSTATE": "a label has an unkno"}


def test_the_same_person_cannot_label_and_verify_and_the_attestation_is_required(tmp_path):
    same = ev.load(build(tmp_path / "a", [{"id": "D", "pages": [invoice("SHJ-1")]}], labelled_by="x", verified_by="x"))
    same.scorable("development")
    unattested = ev.load(build(tmp_path / "b", [{"id": "D", "pages": [invoice("SHJ-1")]}], attested=False))
    unattested.scorable("development")

    assert "same person" in same.unscored[0]["reason"] and "did not attest" in unattested.unscored[0]["reason"]


# ---- the arithmetic of the metrics -----------------------------------------------------------------------------

def test_exact_match_state_and_false_extraction_counts_are_exact(tmp_path):
    root = build(tmp_path, [
        # right value, right page, right absence
        {"id": "A1", "pages": [invoice("SHJ-001", ("Tax", "$5.00"))],
         "fields": [field("invoice_total", "EXTRACTED", 110.0, 1), field("tax", "EXTRACTED", 5.0, 1), field("due_date", "MISSING"), field("currency", "MISSING")]},
        # wrong value label: the extractor is "wrong" against it
        {"id": "A2", "pages": [invoice("SHJ-002")], "fields": [field("invoice_total", "EXTRACTED", 999.0, 1), field("invoice_number", "EXTRACTED", "INV-002", 2)]},
        # labelled missing but present: counted as a false extraction
        {"id": "A3", "pages": [invoice("SHJ-003")], "fields": [field("invoice_total", "MISSING")]},
    ])
    result = run(root)
    m = result

    assert m["documents_scored"] == 3 and m["labelled_fields"] == 7
    # EXTRACTED labels: total(A1) ok, tax(A1) ok, total(A2) wrong, invoice_number(A2) right value wrong page -> value ok
    assert (m["exact_match"]["k"], m["exact_match"]["n"]) == (3, 4)
    assert (m["numeric_parsing"]["k"], m["numeric_parsing"]["n"]) == (2, 3)
    assert (m["wrong_value_shown_as_extracted"]["k"], m["wrong_value_shown_as_extracted"]["n"]) == (1, 4)
    # non-extracted labels: due_date MISSING ok, currency MISSING ok (bare $), total MISSING but extracted -> false extraction
    assert (m["false_extraction"]["k"], m["false_extraction"]["n"]) == (1, 3)
    assert (m["missing_detection"]["k"], m["missing_detection"]["n"]) == (2, 3)
    assert (m["provenance"]["page_correct"]["k"], m["provenance"]["page_correct"]["n"]) == (2, 4)       # invoice_number page 2 is wrong, total(A2) value wrong
    assert m["state_confusion"]["MISSING->EXTRACTED"] == 1 and m["state_confusion"]["MISSING->MISSING"] == 2
    assert (m["state_accuracy"]["k"], m["state_accuracy"]["n"]) == (6, 7)
    assert m["by_document_type"]["INVOICE"]["exact"]["n"] == 4 and m["by_field"]["INVOICE.invoice_total"]["n"] == 2


def test_currency_is_scored_for_code_and_for_assumption(tmp_path):
    root = build(tmp_path, [
        {"id": "C1", "pages": [invoice("SHJ-1", ("Currency", "CAD"))], "fields": [field("currency", "EXTRACTED", "CAD", 1)]},
        {"id": "C2", "pages": [invoice("SHJ-2")], "fields": [field("currency", "MISSING")]},                       # bare dollar: not assumed
        {"id": "C3", "pages": [invoice("SHJ-3", items=(("Base Freight", "USD 100.00"), ("Fuel Surcharge", "USD 10.00")), total="USD 110.00")],
         "fields": [field("currency", "EXTRACTED", "EUR", 1)]},                                                     # label says EUR, document says USD
        {"id": "C4", "pages": [invoice("SHJ-4", ("Currency", "ZZZ"))], "fields": [field("currency", "MISSING")]},  # unknown code: not "missing" but extracted? no: unparseable
    ])
    c = run(root)["currency"]

    assert (c["correct_code"]["k"], c["correct_code"]["n"]) == (1, 2)
    assert (c["assumed_when_not_stated"]["k"], c["assumed_when_not_stated"]["n"]) == (0, 2)


def test_conflicts_are_detected_and_false_conflicts_counted(tmp_path):
    root = build(tmp_path, [
        {"id": "F1", "type": "BOL", "pages": [text("BILL OF LADING", "SHJ-1", [("Number of Pieces", "10"), ("Number of Pieces", "12")])],
         "fields": [field("piece_count", "CONFLICTING")]},
        {"id": "F2", "type": "BOL", "pages": [text("BILL OF LADING", "SHJ-2", [("Number of Pieces", "10"), ("Number of Pieces", "12")])],
         "fields": [field("piece_count", "EXTRACTED", 10, 1)]},                                                      # the label missed the conflict
        {"id": "F3", "type": "BOL", "pages": [text("BILL OF LADING", "SHJ-3", [("Number of Pieces", "7")])],
         "fields": [field("piece_count", "CONFLICTING")]},                                                           # nothing to detect
    ])
    c = run(root)["conflict_detection"]

    assert (c["recall"]["k"], c["recall"]["n"]) == (1, 2) and (c["false_conflict"]["k"], c["false_conflict"]["n"]) == (1, 2)


def test_pages_given_where_the_label_says_unknown_are_counted(tmp_path):
    root = build(tmp_path, [{"id": "P1", "pages": [invoice("SHJ-1")], "fields": [field("invoice_total", "EXTRACTED", 110.0, page_unknown=True)]}])
    p = run(root)["provenance"]

    assert (p["invented_pages"]["k"], p["invented_pages"]["n"]) == (1, 1)


def test_review_state_and_false_completion(tmp_path):
    complete = invoice("SHJ-1")
    root = build(tmp_path, [
        {"id": "R1", "pages": [complete], "complete": True},
        {"id": "R2", "pages": [complete.replace("Total Invoice Amount:\n$110.00", "Total Invoice Amount:\nTBD")], "complete": False},
        {"id": "R3", "pages": [complete], "complete": False},                                                         # should need review but the extractor accepts it
    ])
    r = run(root)["review_state"]

    assert (r["agreement"]["k"], r["agreement"]["n"]) == (2, 3) and (r["false_completion"]["k"], r["false_completion"]["n"]) == (1, 2)


def test_invoice_arithmetic_agreement_recall_and_precision(tmp_path):
    root = build(tmp_path, [
        {"id": "M1", "pages": [invoice("SHJ-1")], "arithmetic": "match"},
        {"id": "M2", "pages": [invoice("SHJ-2", total="$150.00")], "arithmetic": "mismatch"},
        {"id": "M3", "pages": [invoice("SHJ-3", total="$150.00")], "arithmetic": "match"},                      # labelled match but it is a mismatch
        {"id": "M4", "pages": [text("FREIGHT INVOICE", "SHJ-4", [("Invoice Number", "I"), ("Total Invoice Amount", "$5.00")])], "arithmetic": "unchecked"},
    ])
    a = run(root)["invoice_arithmetic"]

    assert (a["agreement"]["k"], a["agreement"]["n"]) == (3, 4)
    assert (a["mismatch_recall"]["k"], a["mismatch_recall"]["n"]) == (1, 1) and (a["mismatch_precision"]["k"], a["mismatch_precision"]["n"]) == (1, 2)


def test_line_item_precision_and_recall(tmp_path):
    root = build(tmp_path, [{"id": "L1", "pages": [invoice("SHJ-1")], "line_items": [["Base Freight", 100.0], ["Fuel Surcharge", 10.0], ["Detention", 25.0]]}])
    li = run(root)["line_items"]

    assert (li["precision"]["k"], li["precision"]["n"]) == (2, 2) and (li["recall"]["k"], li["recall"]["n"]) == (2, 3)


def test_malformed_encrypted_and_scanned_files_are_scored_by_status_and_never_crash(tmp_path):
    root = build(tmp_path, [
        {"id": "S1", "image_only": True, "type": "OTHER", "properties": ["image_only"], "status": "needs_ocr"},
        {"id": "S2", "encrypted": True, "type": "OTHER", "properties": ["encrypted"], "status": "unreadable"},
        {"id": "S3", "bytes": b"not a pdf at all", "type": "OTHER", "properties": ["malformed"], "status": "unreadable"},
        {"id": "S4", "bytes": b"%PDF-1.4 truncated", "type": "OTHER", "properties": ["malformed"], "status": "extracted"},     # mislabelled on purpose
    ])
    r = run(root)

    assert r["crashes"] == []
    assert (r["failure_properties"]["image_only"]["status_correct"]["k"], r["failure_properties"]["image_only"]["documents"]) == (1, 1)
    assert (r["failure_properties"]["encrypted"]["status_correct"]["k"], r["failure_properties"]["encrypted"]["documents"]) == (1, 1)
    assert (r["failure_properties"]["malformed"]["status_correct"]["k"], r["failure_properties"]["malformed"]["documents"]) == (1, 2)


def test_an_extractor_that_raises_is_a_counted_finding_not_a_score(tmp_path):
    root = build(tmp_path, [{"id": "X1", "pages": [invoice("SHJ-1")], "fields": [field("invoice_total", "EXTRACTED", 110.0, 1)]}])
    dataset = ev.load(root)

    def boom(path):
        raise RuntimeError("extractor bug")

    result = ev.score(dataset, "development", extract=boom)

    assert result["crashes"] == [{"id": "X1", "error": "RuntimeError"}] and result["documents_scored"] == 0 and result["labelled_fields"] == 0


def test_a_field_the_extractor_has_no_entry_for_counts_as_unsupported(tmp_path):
    root = build(tmp_path, [{"id": "U1", "pages": [invoice("SHJ-1")], "fields": [field("signature_image", "UNSUPPORTED"), field("invoice_total", "EXTRACTED", 110.0, 1)]}])

    assert run(root)["state_accuracy"]["k"] == 2


def test_document_type_recognition_is_scored(tmp_path):
    root = build(tmp_path, [{"id": "T1", "type": "BOL", "pages": [invoice("SHJ-1")]}, {"id": "T2", "pages": [invoice("SHJ-2")]}])
    t = run(root)["document_type"]

    assert (t["k"], t["n"]) == (1, 2)


# ---- not measured ----------------------------------------------------------------------------------------

def test_with_no_verified_documents_the_report_says_not_measured_and_gives_no_rate(tmp_path):
    root = build(tmp_path, [{"id": "N1", "pages": [invoice("SHJ-1")], "verified": False}])
    report = ev.evaluate(root)
    out = ev.render(report)

    assert report["result"]["documents_scored"] == 0 and "RESULT: NOT MEASURED" in out and "No real-world accuracy is reported or implied" in out
    assert "%" not in out.split("Coverage:")[0]
    assert report["coverage"]["enough"] is False and any(not row["met"] for row in report["coverage"]["rows"])


def test_small_samples_print_counts_instead_of_rates(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "MIN_N", 10)
    root = build(tmp_path, [{"id": "Z1", "pages": [invoice("SHJ-1")], "fields": [field("invoice_total", "EXTRACTED", 110.0, 1)]}])
    out = ev.render(ev.evaluate(root))

    assert "not measured: 1 of 1 (fewer than 10 labelled cases)" in out and "100.0%" not in out
    assert "THIS sample" in out


def test_coverage_lists_what_is_missing(tmp_path):
    cover = ev.coverage(ev.load(build(tmp_path, [{"id": "V1", "pages": [invoice("SHJ-1")], "fields": [], "properties": ["credit"]}])))
    rows = {row["need"]: row for row in cover["rows"]}

    assert cover["verified_documents"] == 1 and rows["documents with credit"]["have"] == 1 and rows["documents with credit"]["met"] is False
    assert rows["INVOICE: verified documents"]["have"] == 1 and rows["BOL: layout families"]["have"] == 0 and cover["enough"] is False


# ---- independence (no tuning on the held-out set) --------------------------------------------------------------

HELDOUT = [{"id": f"H{i}", "split": "heldout", "pages": [invoice(f"SHJ-{i}")], "fields": [field("invoice_total", "EXTRACTED", 110.0, 1)]} for i in range(3)]


def test_the_heldout_set_cannot_be_scored_before_it_is_frozen(tmp_path):
    root = build(tmp_path, HELDOUT)

    with pytest.raises(ev.EvalError, match="freeze"):
        ev.evaluate(root, "heldout")

    assert not (root / "heldout_runs.jsonl").exists()


def test_freeze_needs_verified_heldout_documents_and_happens_once(tmp_path):
    with pytest.raises(ev.EvalError, match="no valid held-out"):
        ev.freeze(build(tmp_path / "a", [{"id": "D", "pages": [invoice("SHJ-1")]}]))

    with pytest.raises(ev.EvalError, match="without verified labels"):
        ev.freeze(build(tmp_path / "b", [*HELDOUT, {"id": "H9", "split": "heldout", "pages": [invoice("SHJ-9")], "verified": False}]))

    root = build(tmp_path / "c", HELDOUT)
    record = ev.freeze(root)

    assert record["heldout_ids"] == ["H0", "H1", "H2"] and record["extractor_version"] == de.EXTRACTOR_VERSION

    with pytest.raises(ev.EvalError, match="already frozen"):
        ev.freeze(root)


def test_a_first_scoring_of_an_unchanged_frozen_set_is_independent_and_is_recorded(tmp_path):
    root = build(tmp_path, HELDOUT)
    ev.freeze(root)
    report = ev.evaluate(root, "heldout")

    assert report["independent"] is True and report["reasons"] == [] and report["result"]["documents_scored"] == 3
    assert "Independent performance: YES" in ev.render(report)
    assert len((root / "heldout_runs.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_changed_labels_a_changed_extractor_or_earlier_use_remove_independence(tmp_path, monkeypatch):
    original_version = de.EXTRACTOR_VERSION
    root = build(tmp_path, HELDOUT)
    ev.freeze(root)

    labels = json.loads((root / "labels.json").read_text(encoding="utf-8"))
    labels["documents"]["H0"]["notes"] = "corrected after the freeze"
    (root / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    changed = ev.evaluate(root, "heldout")

    assert changed["independent"] is False and any("labels changed after the freeze" in r for r in changed["reasons"])

    other = build(tmp_path / "other", HELDOUT)
    ev.freeze(other)
    monkeypatch.setattr(de, "EXTRACTOR_VERSION", de.EXTRACTOR_SCHEMA + "+changedparser")
    version = ev.evaluate(other, "heldout")

    assert version["independent"] is False and any("extractor changed since the freeze" in r for r in version["reasons"])
    assert "Independent performance: NO" in ev.render(version)

    monkeypatch.setattr(de, "EXTRACTOR_VERSION", original_version)
    third = build(tmp_path / "third", HELDOUT)
    ev.freeze(third)
    (third / "heldout_runs.jsonl").write_text(json.dumps({"at": "then", "extractor_version": "older", "documents": 3}) + "\n", encoding="utf-8")
    used = ev.evaluate(third, "heldout")

    assert used["independent"] is False and any("already scored by other extractor versions" in r for r in used["reasons"])


def test_the_development_split_is_never_called_independent(tmp_path):
    report = ev.evaluate(build(tmp_path, [{"id": "D1", "pages": [invoice("SHJ-1")], "fields": [field("invoice_total", "EXTRACTED", 110.0, 1)]}]))

    assert report["independent"] is False and "not independent" in report["reasons"][0] and "Independent performance: NO" in ev.render(report)


# ---- nothing leaks, nothing is modified ----------------------------------------------------------------------------

def leak_corpus(tmp_path):
    doc = text("FREIGHT INVOICE", "SHJ-1", [("Invoice Number", SENTINEL), ("Base Freight", "$100.00"), ("Total Invoice Amount", "$100.00")])

    return build(tmp_path, [{"id": "L1", "pages": [doc], "fields": [field("invoice_number", "EXTRACTED", "WRONG-" + SENTINEL[::-1], 1), field("invoice_total", "EXTRACTED", 100.0, 1)]}])


def test_reports_show_ids_and_shapes_never_values_or_file_names(tmp_path):
    root = leak_corpus(tmp_path)
    report = ev.evaluate(root)
    out, data = ev.render(report), json.dumps(ev.to_json(report))

    for body in (out, data):
        assert SENTINEL not in body and SENTINEL[::-1] not in body and "L1.pdf" not in body and str(tmp_path) not in body

    assert "L1 INVOICE.invoice_number" in out and "AAA-AAAAAAAA-9999" in out
    assert "_records" not in data and "expected_value" not in data


def test_values_are_printed_only_when_asked_and_never_into_the_json(tmp_path):
    report = ev.evaluate(leak_corpus(tmp_path))

    assert SENTINEL in ev.render(report, show_values=True) and SENTINEL not in json.dumps(ev.to_json(report))


def test_evaluation_never_modifies_the_documents_and_never_uses_the_network(tmp_path, monkeypatch):
    root = leak_corpus(tmp_path)
    before = {p.name: p.read_bytes() for p in (root / "documents").iterdir()}

    def no_network(*args, **kwargs):
        raise AssertionError("the evaluation must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    ev.evaluate(root)

    assert {p.name: p.read_bytes() for p in (root / "documents").iterdir()} == before


# ---- command line --------------------------------------------------------------------------------------------------------

def test_cli_template_validate_coverage_and_a_not_measured_evaluation(tmp_path, capsys):
    root = tmp_path / "eval_dir"

    assert cli.main(["template", "--dir", str(root)]) == 0
    assert (root / "manifest.json").is_file() and (root / "labels.json").is_file() and (root / "documents").is_dir()
    assert cli.main(["validate", "--dir", str(root)]) == 0
    assert cli.main(["coverage", "--dir", str(root)]) == 0
    assert cli.main(["evaluate", "--dir", str(root)]) == 0

    out = capsys.readouterr().out
    reports = sorted((root / "reports").glob("real_eval_development_*"))

    assert "RESULT: NOT MEASURED" in out and len(reports) == 2 and "NOT MEASURED" in [p for p in reports if p.suffix == ".txt"][0].read_text(encoding="utf-8")
    assert json.loads([p for p in reports if p.suffix == ".json"][0].read_text(encoding="utf-8"))["result"]["documents_scored"] == 0


def test_cli_reports_usage_errors_with_exit_code_2(tmp_path, capsys):
    root = build(tmp_path, HELDOUT)

    assert cli.main(["evaluate", "--dir", str(root), "--split", "heldout"]) == 2
    assert cli.main(["validate", "--dir", str(tmp_path / "missing")]) == 2
    assert cli.main(["freeze", "--dir", str(root)]) == 0 and cli.main(["freeze", "--dir", str(root)]) == 2
    assert "Cannot continue" in capsys.readouterr().err


def test_cli_evaluate_files_are_redacted_and_show_values_stays_on_the_console(tmp_path, capsys):
    root = leak_corpus(tmp_path)

    assert cli.main(["evaluate", "--dir", str(root), "--show-values"]) == 0
    console = capsys.readouterr().out
    written = "".join(p.read_text(encoding="utf-8") for p in (root / "reports").iterdir())

    assert SENTINEL in console and SENTINEL not in written and "L1.pdf" not in written


def test_the_example_files_are_shapes_with_no_real_data():
    for name in ("manifest.example.json", "labels.example.json"):
        data = json.loads((PROJECT_ROOT / "docs" / "real_world_evaluation" / name).read_text(encoding="utf-8"))

        assert "EXAMPLE ONLY" in json.dumps(data)

    assert "NOT MEASURED" in (PROJECT_ROOT / "docs" / "real_world_evaluation" / "PROTOCOL.md").read_text(encoding="utf-8")
