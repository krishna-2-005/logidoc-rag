"""
Regression tests for the structured-extraction audit findings (H1, H2, M1-M5,
L1, L3). Each test starts from the audit's reproduction.

Synthetic PDFs only; no network, no model.

Run: python -m pytest tests/test_extraction_audit_fixes.py
"""

import logging
import sqlite3
import sys
import threading
from pathlib import Path

import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_extraction as de  # noqa: E402
from migration_support import upgrade  # noqa: E402
import extraction_store as store_module  # noqa: E402
from authz import Principal  # noqa: E402
from document_extraction import CONFLICTING, EXTRACTED, extract_document  # noqa: E402
from extraction_store import ExtractionStore, StaleCorrection, StoreError  # noqa: E402
from test_api import Rig  # noqa: E402

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
BOB = Principal("bob", "reviewer", True, 2)
CAROL = Principal("carol", "reviewer", True, 3)
ALICE = Principal("alice", "admin", True, 1)
REASON = "Checked against the carrier's copy."
SENTINEL = "ZQX-SENTINEL-4417"

INV = "FREIGHT INVOICE\nInvoice Number: INV-9\nShipment Number: SHJ-2026-950\n"


def make_pdf(folder, name, pages):
    document = pymupdf.open()

    for text in pages:
        page = document.new_page()

        if text:
            for index, line in enumerate(text.split("\n")):
                page.insert_text((40, 50 + index * 14), line, fontsize=9)

    path = Path(folder) / name
    document.save(str(path))
    document.close()

    return path


def ex(tmp_path, pages, name="d.pdf"):
    return extract_document(make_pdf(tmp_path, name, pages if isinstance(pages, list) else [pages]))


def issues(extraction):
    return {x["code"] for x in de.validate_shipment([extraction]) if x["severity"] != "info"}


def codes(extraction):
    return {x["code"] for x in de.validate_shipment([extraction])}


# ---- H1: invoice line-item classification ----------------------------------------------------

def test_h1_counts_weights_and_identifiers_are_not_charges(tmp_path):
    e = ex(tmp_path, INV + "Number of Pieces:\n48\nTotal Weight:\n1,200\nAccount Number:\n55012\nPRO Number:\n$77\n"
                           "Base Freight:\n$100.00\nTotal Invoice Amount:\n$100.00")

    assert [(i.description, i.amount) for i in e.line_items] == [("Base Freight", 100.0)]
    assert {x["label"] for x in e.excluded_amounts} == {"Number of Pieces", "Total Weight", "Account Number", "PRO Number"}
    assert all(x["reason"] == "not_a_charge" for x in e.excluded_amounts)
    assert issues(e) == set() and e.complete


def test_h1_payments_and_balances_are_not_charges_even_with_a_currency_mark(tmp_path):
    e = ex(tmp_path, INV + "Base Freight:\n$100.00\nAmount Paid:\n$100.00\nPrevious Balance:\n$40.00\nDeposit Received:\nUSD 10.00\n"
                           "Total Invoice Amount:\n$100.00")

    assert [i.description for i in e.line_items] == ["Base Freight"]
    assert {x["label"] for x in e.excluded_amounts} == {"Amount Paid", "Previous Balance", "Deposit Received"}
    assert "invoice_arithmetic_mismatch" not in codes(e)


def test_h1_a_number_without_a_currency_mark_is_unclassified_and_blocks_completeness_and_arithmetic(tmp_path):
    e = ex(tmp_path, INV + "Base Freight:\n$100.00\nHandling:\n25\nTotal Invoice Amount:\n$125.00")

    assert [i.description for i in e.line_items] == ["Base Freight"]
    assert e.unclassified_amounts == [{"label": "Handling", "page": 1, "reason": "no_currency_mark"}]
    assert not e.complete and any(r.startswith("unclassified_amounts") for r in e.review_reasons)
    assert "invoice_arithmetic_unchecked" in codes(e) and "invoice_arithmetic_mismatch" not in codes(e)


@pytest.mark.parametrize("label", ["Discount", "Volume Rebate", "Adjustment", "Customer Allowance"])
def test_h1_discounts_and_adjustments_need_review(tmp_path, label):
    e = ex(tmp_path, INV + f"Base Freight:\n$100.00\n{label}:\n-$10.00\nTotal Invoice Amount:\n$90.00")

    assert e.unclassified_amounts[0]["reason"] == "adjustment_needs_review" and not e.complete
    assert "invoice_arithmetic_unchecked" in codes(e)


def test_h1_marked_charges_credits_and_quantity_lines_are_still_charges(tmp_path):
    e = ex(tmp_path, INV + "Base Freight:\nCAD 500.00\nLift Gate:\n$40.00\nStorage:\n2 x $15.00 = $30.00\nCredit:\n-$20.00\n"
                           "Total Invoice Amount:\n$550.00")

    assert [(i.description, i.amount) for i in e.line_items] == [
        ("Base Freight", 500.0), ("Lift Gate", 40.0), ("Storage", 30.0), ("Credit", -20.0)]
    assert e.unclassified_amounts == [] and "invoice_arithmetic_mismatch" not in codes(e)


def test_h1_the_fixture_invoice_is_unchanged():
    e = extract_document(FIXTURES / "shipment_SHJ-2026-001" / "INVOICE_SHJ-2026-001.pdf")

    assert [(i.description, i.amount) for i in e.line_items] == [
        ("Base Freight", 1200.0), ("Fuel Surcharge", 180.0), ("Detention Charge", 0.0), ("Other Accessorial Charges", 50.0)]
    assert e.unclassified_amounts == [] and e.complete
    assert "invoice_arithmetic_mismatch" in codes(e)          # 1430 vs 1550: the known fixture discrepancy


# ---- H2: repeated fields with an unreadable occurrence -------------------------------------------------

def test_h2_a_value_next_to_an_unparseable_occurrence_is_not_chosen(tmp_path):
    e = ex(tmp_path, [INV + "Base Freight:\n$100.00\nTotal Invoice Amount:\n$100.00", "Total Invoice Amount:\nillegible"])
    item = e.fields["invoice_total"]

    assert item.state == CONFLICTING and item.value is None and not e.complete
    assert {(a["value"], a["page"], a.get("problem")) for a in item.alternatives} == {(100.0, 1, None), (None, 2, "unparseable")}
    assert "invoice_total: conflicting values" in e.review_reasons
    assert "invoice_arithmetic_unchecked" not in codes(e) and "invoice_arithmetic_mismatch" not in codes(e)


def test_h2_an_empty_repeated_label_keeps_the_value_but_needs_review(tmp_path):
    e = ex(tmp_path, [INV + "Base Freight:\n$100.00\nTotal Invoice Amount:\n$100.00", "Total Invoice Amount:\nPayment Terms:\nNet 30"])
    item = e.fields["invoice_total"]

    assert item.state == EXTRACTED and item.value == 100.0 and item.page == 1 and item.needs_review
    assert any(w.startswith("empty_occurrence") for w in item.warnings) and not e.complete


def test_h2_two_readable_equal_values_still_need_no_review(tmp_path):
    e = ex(tmp_path, [INV + "Base Freight:\n$100.00\nTotal Invoice Amount:\n$100.00", "Total Invoice Amount:\n$100.00"])

    assert e.fields["invoice_total"].state == EXTRACTED and not e.fields["invoice_total"].needs_review and e.complete


# ---- M1: field boundaries ----------------------------------------------------------------------------------

def test_m1_footer_text_is_not_absorbed_into_a_single_line_field(tmp_path):
    e = ex(tmp_path, INV + "Base Freight:\n$100.00\nTotal Invoice Amount:\n$100.00\nPayment Terms:\nNet 30\n"
                           "Thank you for your business\nRemit to PO Box 12, Dallas")
    item = e.fields["payment_terms"]

    assert item.value == "Net 30" and item.needs_review and "Thank you" not in (item.raw_text or "")
    assert any(w.startswith("extra_lines_ignored") for w in item.warnings) and not e.complete


def test_m1_a_currency_label_followed_by_other_text_reads_one_line(tmp_path):
    e = ex(tmp_path, INV + "Currency:\nUSD\nAll amounts payable within 30 days\nBase Freight:\n$1.00\nTotal Invoice Amount:\n$1.00")

    assert e.value("currency") == "USD" and e.fields["currency"].needs_review and not e.complete


def test_m1_multi_line_fields_keep_their_lines_up_to_the_cap(tmp_path):
    ok = ex(tmp_path, "BILL OF LADING\nShipment Number: SHJ-2026-950\nShipper:\nAcme\n1 Road\nDallas, Texas\n"
                      "Number of Pieces:\n3\nExpected Delivery Date:\nOctober 3, 2026", "a.pdf")

    assert ok.value("shipper") == {"name": "Acme", "address_lines": ["1 Road", "Dallas, Texas"]}
    assert not ok.fields["shipper"].needs_review and ok.complete

    long = ex(tmp_path, "BILL OF LADING\nShipment Number: SHJ-2026-950\nShipper:\nAcme\n1\n2\n3\n4\n5", "b.pdf")

    assert long.value("shipper")["address_lines"] == ["1", "2", "3"] and long.fields["shipper"].needs_review


# ---- M2: no document text in what is stored --------------------------------------------------------------------

def sentinel_document(folder, name="INVOICE_SHJ-2026-950.pdf"):
    return make_pdf(folder, name, [
        INV + f"Base Freight:\n$100.00\nTotal Invoice Amount:\nSEE {SENTINEL}\nCurrency:\n{SENTINEL[:3]}\nInvoice Date:\n{SENTINEL}",
        f"Total Invoice Amount:\n$100.00\nDue Date:\n{SENTINEL}",
    ])


def test_m2_a_sentinel_in_unreadable_fields_never_reaches_reasons_warnings_or_the_database(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    e = extract_document(sentinel_document(tmp_path))

    assert e.fields["invoice_date"].state == "UNPARSEABLE" and e.fields["invoice_total"].state == CONFLICTING
    assert e.fields["currency"].state == "UNPARSEABLE"

    messages = e.review_reasons + e.warnings + [w for f in e.fields.values() for w in f.warnings]

    assert not any(SENTINEL in m or SENTINEL[:3] in m for m in messages)

    store = ExtractionStore(tmp_path / "s.sqlite3")
    store.save(e)

    stored = b"".join(p.read_bytes() for p in tmp_path.glob("s.sqlite3*"))

    assert SENTINEL.encode() not in stored and SENTINEL[:3].encode() not in stored
    assert SENTINEL not in caplog.text


def test_m2_the_api_returns_no_document_text(tmp_path):
    rig = Rig(tmp_path, "required")
    sentinel_document(rig.raw)
    alice = rig.client("alice")

    run = alice.post("/api/extractions/run", json={"document": "INVOICE_SHJ-2026-950.pdf"})
    detail = alice.get(f"/api/extractions/{run.json()['id']}")

    assert run.status_code == 200 and detail.status_code == 200
    assert SENTINEL not in run.text and SENTINEL not in detail.text


# ---- M3: shipment ID corrections ------------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return ExtractionStore(tmp_path / "x.sqlite3")


def saved(store, name="INVOICE"):
    return store.save(extract_document(FIXTURES / "shipment_SHJ-2026-001" / f"{name}_SHJ-2026-001.pdf"))[0]


@pytest.mark.parametrize("value", ["not a shipment at all", "SHJ-2026-002", "SHJ-2026-001"])
def test_m3_shipment_ids_cannot_be_corrected(store, value):
    extraction_id = saved(store)

    with pytest.raises(StoreError, match="shipment ID corrections are not supported"):
        store.propose(BOB, extraction_id, "shipment_id", value, REASON, based_on=None)

    assert store.corrections(extraction_id) == [] and store.list()[0]["shipment_key"] == "SHJ2026001"
    assert store.effective(extraction_id)["shipment_id"]["value"] == "SHJ-2026-001"


def test_m3_the_api_refuses_a_shipment_id_correction(tmp_path):
    rig = Rig(tmp_path, "required")
    name = sorted(rig.raw.glob("INVOICE_*.pdf"))[0].name
    extraction_id = rig.client("alice").post("/api/extractions/run", json={"document": name}).json()["id"]
    reply = rig.client("bob").post(f"/api/extractions/{extraction_id}/corrections",
                                   json={"field": "shipment_id", "value": "SHJ-2026-002", "reason": REASON, "based_on": None})

    assert reply.status_code == 422 and "not supported" in reply.json()["detail"]


# ---- M5: stored history -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("foreign_keys", [True, False])
def test_m5_stored_extractions_cannot_be_updated_or_deleted_by_ordinary_sql(store, foreign_keys):
    extraction_id = saved(store)
    store.propose(BOB, extraction_id, "tax", "5", REASON, based_on=None)
    connection = sqlite3.connect(store.path)
    connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")

    for statement in ("UPDATE extractions SET payload = '{}'", "DELETE FROM extractions",
                      "UPDATE field_corrections SET reason = 'x'", "DELETE FROM field_corrections"):
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)

    connection.close()

    assert store.verify_history()["ok"] and len(store.corrections(extraction_id)) == 1
    assert store.row(extraction_id)["payload"]["fields"]["invoice_total"]["value"] == 1550.0


def chain(store, n=3):
    extraction_id = saved(store)
    last = None

    for value in range(1, n + 1):
        last = store.propose(BOB, extraction_id, "tax", str(value), REASON, based_on=last["id"] if last else None)
        store.decide(ALICE, last["id"], "approve")

    return extraction_id


def test_m5_an_intact_history_verifies(store):
    chain(store)
    result = store.verify_history()

    assert result == {"ok": True, "corrections_checked": 3, "decisions_checked": 3, "extractions_checked": 1,
                      "aliases_checked": 1, "unchained_legacy": 0, "problems": []}


def tamper(store, *statements):
    connection = sqlite3.connect(store.path)

    for statement in statements:
        connection.execute(statement)

    connection.commit()
    connection.close()


def test_m5_a_changed_correction_is_detected(store):
    chain(store)
    tamper(store, "DROP TRIGGER field_corrections_no_update", "UPDATE field_corrections SET corrected = '99' WHERE id = 2")
    problems = store.verify_history()["problems"]

    assert problems == ["correction 2: its content does not match its hash"]


def test_m5_a_removed_correction_is_detected(store):
    chain(store)
    tamper(store, "DROP TRIGGER field_corrections_no_delete", "DELETE FROM field_corrections WHERE id = 2")

    assert any("chain is broken" in p for p in store.verify_history()["problems"])


def test_m5_a_rewritten_extraction_and_an_orphaned_correction_are_detected(store):
    extraction_id = chain(store, 1)
    tamper(store, "DROP TRIGGER extractions_no_update", f"UPDATE extractions SET payload = '{{}}' WHERE id = {extraction_id}")

    assert any("does not match its hash" in p and "extraction" in p for p in store.verify_history()["problems"])

    tamper(store, "DROP TRIGGER extractions_no_delete", f"DELETE FROM extractions WHERE id = {extraction_id}")

    assert any("is missing" in p for p in store.verify_history()["problems"])


def test_m5_rows_from_before_the_chain_are_reported_as_legacy_not_hidden(tmp_path):
    path = tmp_path / "v2.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")

    for version in (1, 2):
        for statement in store_module.statements(store_module.MIGRATIONS[version]):
            connection.execute(statement)

    connection.execute("INSERT INTO schema_info VALUES (2)")
    connection.execute("INSERT INTO extractions (filename, sha256, extractor_version, document_type, status, payload, created_at) "
                       "VALUES ('a.pdf', 'h', '1.0', 'INVOICE', 'extracted', '{\"fields\": {}}', 'then')")
    connection.execute("INSERT INTO field_corrections (extraction_id, field_name, previous, corrected, reason, actor, authenticated, at) "
                       "VALUES (1, 'tax', '{}', '5.0', 'an old correction', 'bob', 1, 'then')")
    connection.commit()
    connection.close()

    upgrade(path)
    upgraded = ExtractionStore(path)
    upgraded.propose(BOB, 1, "tax", "6", REASON, based_on=1)
    result = upgraded.verify_history()

    assert upgraded.version() == store_module.SCHEMA_VERSION and result["ok"] and result["unchained_legacy"] == 2 and result["corrections_checked"] == 2


def test_m5_the_integrity_check_is_for_administrators(tmp_path):
    rig = Rig(tmp_path, "required")

    assert rig.client("bob").get("/api/extractions/integrity").status_code == 403
    assert rig.client().get("/api/extractions/integrity").status_code == 401
    assert rig.client("alice").get("/api/extractions/integrity").json()["ok"] is True


# ---- M4: stale and concurrent corrections --------------------------------------------------------------------------------

def test_m4_two_reviewers_from_the_same_version_the_second_is_refused(store):
    extraction_id = saved(store)
    store.propose(BOB, extraction_id, "tax", "10", REASON, based_on=None)

    with pytest.raises(StaleCorrection):
        store.propose(CAROL, extraction_id, "tax", "12", REASON, based_on=None)

    assert [c["actor"] for c in store.corrections(extraction_id)] == ["bob"]
    store.decide(ALICE, store.corrections(extraction_id)[0]["id"], "approve")
    assert store.effective(extraction_id)["tax"]["value"] == 10.0


def test_m4_a_wrong_or_future_base_is_refused_and_the_right_base_is_accepted(store):
    extraction_id = saved(store)
    first = store.propose(BOB, extraction_id, "tax", "10", REASON, based_on=None)

    for base in (first["id"] + 5, None):
        with pytest.raises(StaleCorrection):
            store.propose(CAROL, extraction_id, "tax", "12", REASON, based_on=base)

    with pytest.raises(StoreError):
        store.propose(CAROL, extraction_id, "tax", "12", REASON, based_on="1")

    # The base is right but the first proposal is still awaiting its decision
    with pytest.raises(StoreError, match="awaiting a decision"):
        store.propose(CAROL, extraction_id, "tax", "12", REASON, based_on=first["id"])

    store.decide(ALICE, first["id"], "approve")

    assert store.propose(CAROL, extraction_id, "tax", "12", REASON, based_on=first["id"])["based_on"] == first["id"]
    # Another field has its own version
    assert store.propose(CAROL, extraction_id, "subtotal", "1", REASON, based_on=None)


def test_m4_concurrent_corrections_from_one_version_record_exactly_one(store):
    extraction_id = saved(store)
    results, errors = [], []

    def attempt(value):
        try:
            results.append(ExtractionStore(store.path).propose(BOB, extraction_id, "tax", str(value), REASON, based_on=None))
        except StaleCorrection:
            errors.append(value)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(1, 9)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert len(results) == 1 and len(errors) == 7 and len(store.corrections(extraction_id)) == 1
    assert store.verify_history()["ok"]


def test_m4_the_api_returns_409_and_requires_the_base(tmp_path):
    rig = Rig(tmp_path, "required")
    name = sorted(rig.raw.glob("INVOICE_*.pdf"))[0].name
    extraction_id = rig.client("alice").post("/api/extractions/run", json={"document": name}).json()["id"]
    url = f"/api/extractions/{extraction_id}/corrections"
    body = {"field": "tax", "value": "9.50", "reason": REASON, "based_on": None}

    assert rig.client("bob").post(url, json=body).status_code == 200

    stale = rig.client("carol").post(url, json={**body, "value": "8.00"})

    assert stale.status_code == 409 and "Reload" in stale.json()["detail"]
    assert rig.client("carol").post(url, json={k: v for k, v in body.items() if k != "based_on"}).status_code == 422

    proposal = rig.client("carol").get(f"/api/extractions/{extraction_id}").json()["effective"]["tax"]["proposed"]

    assert proposal["corrected"] == 9.5
    assert rig.client("alice").post(f"/api/extractions/corrections/{proposal['id']}/decision", json={"decision": "approve"}).status_code == 200

    current = rig.client("carol").get(f"/api/extractions/{extraction_id}").json()["effective"]["tax"]

    assert current["value"] == 9.5
    assert rig.client("carol").post(url, json={**body, "value": "8.00", "based_on": current["correction_id"]}).status_code == 200


# ---- L1: extractor versioning --------------------------------------------------------------------------------------------

def test_l1_the_version_is_a_fingerprint_of_the_extraction_sources():
    sources = [PROJECT_ROOT / "src" / name for name in de.FINGERPRINT_SOURCES]

    assert all(path.is_file() for path in sources)
    assert de.EXTRACTOR_VERSION == f"{de.EXTRACTOR_SCHEMA}+{de.source_fingerprint(sources, extra=de.FINGERPRINT_EXTRA)}"


def test_l1_any_source_change_changes_the_fingerprint_but_line_endings_do_not(tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_bytes(b"x = 1\ny = 2\n")
    b.write_bytes(b"z = 3\n")
    base = de.source_fingerprint([a, b])

    a.write_bytes(b"x = 1\r\ny = 2\r\n")
    assert de.source_fingerprint([a, b]) == base

    a.write_bytes(b"x = 1\ny = 3\n")
    assert de.source_fingerprint([a, b]) != base


def test_l1_a_changed_version_stores_a_new_extraction_and_keeps_the_old(store, monkeypatch):
    first = saved(store)
    monkeypatch.setattr(de, "EXTRACTOR_VERSION", de.EXTRACTOR_SCHEMA + "+000000000000")
    changed = extract_document(FIXTURES / "shipment_SHJ-2026-001" / "INVOICE_SHJ-2026-001.pdf")

    assert changed.extractor_version.endswith("000000000000")

    second, created = store.save(changed)

    assert created and second != first and len(store.list()) == 2


# ---- L3: concurrent first start ------------------------------------------------------------------------------------------

def test_l3_concurrent_first_starts_migrate_once(tmp_path):
    path, errors = tmp_path / "race.sqlite3", []

    def start():
        try:
            ExtractionStore(path)
        except Exception as error:  # noqa: BLE001 - any failure is the finding
            errors.append(repr(error))

    threads = [threading.Thread(target=start) for _ in range(6)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert errors == [] and ExtractionStore(path).version() == store_module.SCHEMA_VERSION


# ---- found while fixing H1: amounts with a leading ISO code ------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("CAD 500.00", (500.0, "CAD")), ("500.00 CAD", (500.0, "CAD")), ("USD -20.00", (-20.0, "USD")),
    ("USD 10 EUR", (None, None)), ("TBD 5", (None, None)), ("12", (12.0, None)),
])
def test_money_with_a_leading_code_is_read_and_two_codes_are_not(raw, expected):
    amount, code, _ = de.parse_money(raw)

    assert (amount, code) == expected
