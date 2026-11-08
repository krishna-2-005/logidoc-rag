"""
The performance changes made after measuring (scripts/benchmark_scale.py):
the freight audit's extraction cache, the audit history's version 5 indexes,
exceptions grouped once per shipment, and the benchmark itself. Each must
leave every result exactly as it was.

Synthetic PDFs in a temporary folder; no network, no model.

Run from the project root:
    python -m pytest tests/test_scale_safeguards.py
"""

import random
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests", PROJECT_ROOT / "scripts"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_analytics  # noqa: E402
import audit_history  # noqa: E402
import benchmark_scale  # noqa: E402
import carrier_intelligence as ci  # noqa: E402
import document_extraction  # noqa: E402
from audit_history import HistoryStore  # noqa: E402
from test_api import Rig  # noqa: E402


# ============================================================
# THE FREIGHT AUDIT'S EXTRACTION CACHE
# ============================================================

@pytest.fixture
def counted(tmp_path, monkeypatch):
    """A local rig with two synthetic shipments, counting every PDF extraction."""

    rig = Rig(tmp_path, "local")

    for path in list(rig.raw.glob("*.pdf")):
        path.unlink()

    for n in range(2):
        benchmark_scale.write_shipment_pdfs(rig.raw, f"BENCH-{n:06d}", "Swift Lines")

    calls = []
    real = document_extraction.extract_document

    def extract(path, *args, **kwargs):
        calls.append(Path(path).name)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(document_extraction, "extract_document", extract)
    rig.state.service.invalidate()

    return rig, calls


def test_unchanged_files_are_not_extracted_again(counted):

    rig, calls = counted
    service = rig.state.service

    first = service.snapshot()
    assert len(calls) == 8

    service.invalidate()
    second = service.snapshot()

    assert len(calls) == 8                                    # nothing extracted again
    assert {item["shipment_id"]: item["freight_audit"]["outcome"] for item in first["shipments"]} == \
        {item["shipment_id"]: item["freight_audit"]["outcome"] for item in second["shipments"]}


def test_a_changed_file_is_extracted_again_and_a_removed_one_forgotten(counted):

    rig, calls = counted
    service = rig.state.service
    service.snapshot()

    # The same name, new content (a different fuel surcharge on the rate confirmation)
    from test_freight_audit import pairs
    from test_ingestion_manager import make_pdf

    make_pdf(rig.raw / "RATE_CONFIRMATION_BENCH-000000.pdf", "RATE CONFIRMATION", ["Shipment Number: BENCH-000000", ""] + pairs(
        Carrier="Swift Lines", Origin="Chicago, Illinois", Destination="Atlanta, Georgia", Line_Haul="$1,400.00",
        Fuel_Surcharge="$200.00", Payment_Terms="Net 30"))
    (rig.raw / "POD_BENCH-000001.pdf").unlink()
    service.invalidate()

    snapshot = service.snapshot()

    assert calls[8:] == ["RATE_CONFIRMATION_BENCH-000000.pdf"]
    assert len(service.extractions) == 7                      # the removed POD is not kept
    shipment = next(item for item in snapshot["shipments"] if item["shipment_id"] == "BENCH-000000")
    fuel = next(c for c in shipment["freight_audit"]["charges"] if c["charge"] == "Fuel surcharge")
    assert fuel["expected"] == 200.0 and fuel["outcome"] == "MATCHED"


def test_the_cache_gives_the_same_audit_as_reading_every_file(counted):

    rig, _ = counted
    service = rig.state.service
    cached = service.snapshot()

    service.extractions.clear()
    service.invalidate()
    fresh = service.snapshot()

    strip = lambda audit: {k: v for k, v in audit.items() if k != "audited_at"}  # noqa: E731
    assert [strip(item["freight_audit"]) for item in cached["shipments"]] == \
        [strip(item["freight_audit"]) for item in fresh["shipments"]]


# ============================================================
# AUDIT HISTORY VERSION 5 (INDEXES)
# ============================================================

def indexes(path):

    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def test_a_version_4_history_gets_its_indexes_and_keeps_every_row(tmp_path):

    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    rng = random.Random(4)
    shipments = [benchmark_scale.synthetic_shipment(n, rng) for n in range(5)]
    snapshot = {"shipments": shipments, "unidentified_sources": [], "unreadable_files": []}
    store.record_run(snapshot, audit_analytics.build_exceptions(snapshot), label="before")

    # Back to version 4 as it was shipped: no indexes, version 4
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX run_input_sets_by_shipment")
        connection.execute("DROP INDEX run_shipments_by_shipment")
        connection.execute("UPDATE schema_info SET version = 4")
        before = {table: connection.execute(f"SELECT * FROM {table}").fetchall()
                  for table in ("audit_runs", "shipment_snapshots", "snapshot_findings", "run_shipments", "run_inputs",
                                "freight_audit_records")}

    HistoryStore(path)
    HistoryStore(path)                                       # idempotent

    assert {"run_input_sets_by_shipment", "run_shipments_by_shipment"} <= indexes(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == audit_history.SCHEMA_VERSION == 5
        assert {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in before} == before

        plan = " ".join(row[3] for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT run_id FROM run_shipments WHERE shipment_key = ? AND run_id < ? "
            "ORDER BY run_id DESC LIMIT 1", ("BENCH000001", 99)))
        assert "run_shipments_by_shipment" in plan


# ============================================================
# EXCEPTIONS GROUPED ONCE
# ============================================================

def test_each_shipment_gets_exactly_its_own_exceptions():

    rng = random.Random(7)
    shipments = [benchmark_scale.synthetic_shipment(n, rng) for n in range(60)]
    snapshot = {"shipments": shipments, "unidentified_sources": [], "unreadable_files": []}
    exceptions = audit_analytics.build_exceptions(snapshot)

    for item in exceptions:
        item["review_status"] = "open"

    rows = ci.shipment_rows(shipments, exceptions, [])

    for row in rows:
        assert sorted(e["exception_key"] for e in row["exceptions"]) == \
            sorted(e["exception_key"] for e in exceptions if e["shipment_key"] == row["shipment_key"])

    detail = ci.carrier_detail(shipments, exceptions, [], "swift lines", limit=500)
    by_key = {row["shipment_key"]: row for row in rows}

    for row in detail["shipment_rows"]:
        assert row["exceptions"] == by_key[row["shipment_key"]]["exceptions"]

    assert detail["exceptions"]["total"] == sum(len(by_key[r["shipment_key"]]["exceptions"]) for r in detail["shipment_rows"])


def test_the_recorded_findings_are_each_shipments_own(tmp_path):

    rng = random.Random(9)
    shipments = [benchmark_scale.synthetic_shipment(n, rng) for n in range(30)]
    snapshot = {"shipments": shipments, "unidentified_sources": [], "unreadable_files": []}
    exceptions = audit_analytics.build_exceptions(snapshot)

    store = HistoryStore(tmp_path / "history.sqlite3")
    store.record_run(snapshot, exceptions, label="grouped")

    for item in shipments:
        (recorded,) = store.snapshots(item["shipment_key"])
        assert sorted(f["finding_key"] for f in recorded["findings"]) == \
            sorted(e["exception_key"] for e in exceptions if e["shipment_key"] == item["shipment_key"])


# ============================================================
# THE BENCHMARK ITSELF
# ============================================================

def test_the_benchmark_runs_on_synthetic_data_only(tmp_path):

    figures = benchmark_scale.run_size(100, tmp_path)

    assert figures["exceptions"] > 0
    for name in ("carrier_aggregation", "carrier_detail_page", "exception_aggregation", "history_record_run"):
        assert figures[name]["seconds"] >= 0 and figures[name]["peak_mib"] >= 0
    assert all(item["shipment_id"].startswith("BENCH-")
               for item in [benchmark_scale.synthetic_shipment(n, random.Random(1)) for n in range(5)])
