"""
Tests for src/trend_analytics.py and the schema version 2 of
src/audit_history.py (carrier and currency on snapshots).

The trend tests use synthetic observation sequences with hand-computed
expectations: no PDFs, no index, no Ollama. The store tests use real
reconcile_shipment() results on synthetic chunks and a temporary SQLite
file.

Run from the project root:
    python -m pytest tests/test_trend_analytics.py
"""

import csv
import io
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_analytics as analytics  # noqa: E402
import audit_history as history  # noqa: E402
import trend_analytics as trends  # noqa: E402
from test_audit_analytics import snapshot  # noqa: E402
from test_reconciliation_consistency import A, documents  # noqa: E402


_ids = iter(range(1, 10_000))


def obs(run, day, shipment, carrier="Acme", final="NO DISCREPANCIES", delivery="ON TIME", quantity="MATCH",
        invoice="MATCH", days_late=0, pieces=0, difference=0.0, currency=None, categories=(), reconciled=True):
    """
    One observation, shaped like HistoryStore.observations().
    """

    return {
        "id": next(_ids), "run_id": run, "observed_at": f"{day}T10:00:00+00:00",
        "shipment_key": shipment, "shipment_id": shipment, "carrier": carrier, "currency": currency,
        "reconciled": 1 if reconciled else 0, "final_status": final if reconciled else None,
        "quantity_status": quantity, "delivery_status": delivery, "invoice_status": invoice,
        "piece_difference": pieces, "days_late": days_late, "invoice_total": 1000.0,
        "invoice_difference": difference,
        "findings": [{"category": name, "finding_key": f"{shipment}|{name}"} for name in categories],
    }


def delayed(run, day, shipment, **kwargs):

    return obs(run, day, shipment, final="DISCREPANCIES DETECTED", delivery="DELAYED", days_late=3,
               categories=("Delivery delay",), **kwargs)


def by_period(result):

    return {entry["period"]: entry for entry in result["series"]}


# ------------------------------------------------------------
# Rates, sample sizes and missing values
# ------------------------------------------------------------

def test_rates_come_with_their_sample_size_and_interval():

    rows = [obs(1, "2026-01-05", "A"), obs(1, "2026-01-05", "B"), delayed(1, "2026-01-05", "C"),
            delayed(1, "2026-01-05", "D")]

    entry = trends.analyze(rows)["series"][0]

    assert entry["period"] == "2026-01" and entry["shipments"] == 4
    assert (entry["delay"]["count"], entry["delay"]["n"], entry["delay"]["rate"]) == (2, 4, 0.5)
    assert entry["delay"]["low"] < 0.5 < entry["delay"]["high"]
    assert entry["average_days_late"] == {"value": 3.0, "n": 2}


def test_wilson_interval_matches_a_known_value():

    low, high = trends.wilson(5, 10)

    assert round(low, 3) == 0.237 and round(high, 3) == 0.763
    assert trends.wilson(0, 0) == (None, None)
    assert trends.wilson(0, 10)[0] == 0.0 and trends.wilson(10, 10)[1] == 1.0


def test_missing_values_are_not_counted_as_on_time_or_zero():

    rows = [
        obs(1, "2026-01-05", "A"),
        # Delivery date unreadable: the delivery cannot be called on time
        obs(1, "2026-01-05", "B", final="NEEDS REVIEW", delivery="NEEDS REVIEW", days_late=None,
            quantity="NEEDS REVIEW", pieces=None, invoice="NEEDS REVIEW", difference=None),
        obs(1, "2026-01-05", "C", reconciled=False, delivery=None, quantity=None, invoice=None, days_late=None,
            pieces=None, difference=None),
    ]

    entry = trends.analyze(rows)["series"][0]

    assert entry["shipments"] == 3
    # Only A could be assessed for every rate
    assert (entry["delay"]["count"], entry["delay"]["n"]) == (0, 1)
    assert (entry["shortage"]["count"], entry["shortage"]["n"]) == (0, 1)
    assert (entry["invoice_mismatch"]["count"], entry["invoice_mismatch"]["n"]) == (0, 1)
    assert (entry["discrepancy"]["count"], entry["discrepancy"]["n"]) == (0, 1)
    assert (entry["needs_review"]["count"], entry["needs_review"]["n"]) == (2, 3)


def test_a_rate_with_nothing_to_count_is_none_not_zero():

    rows = [obs(1, "2026-01-05", "A", final="NEEDS REVIEW", delivery="NEEDS REVIEW", days_late=None)]

    entry = trends.analyze(rows)["series"][0]

    assert entry["delay"]["n"] == 0 and entry["delay"]["rate"] is None
    assert entry["average_days_late"] == {"value": None, "n": 0}


def test_shortage_counts_fewer_pieces_delivered_not_an_overage():

    rows = [
        obs(1, "2026-01-05", "A", quantity="MISMATCH", pieces=2, final="DISCREPANCIES DETECTED"),
        obs(1, "2026-01-05", "B", quantity="MISMATCH", pieces=-3, final="DISCREPANCIES DETECTED"),
        obs(1, "2026-01-05", "C"),
    ]

    shortage = trends.analyze(rows)["series"][0]["shortage"]

    assert (shortage["count"], shortage["n"]) == (1, 3)


def test_the_average_delay_ignores_early_and_unknown_days():

    rows = [delayed(1, "2026-01-05", "A"), delayed(1, "2026-01-05", "B"),
            obs(1, "2026-01-05", "C", delivery="EARLY", days_late=-4),
            obs(1, "2026-01-05", "D", final="DISCREPANCIES DETECTED", delivery="DELAYED", days_late=None)]

    assert trends.analyze(rows)["series"][0]["average_days_late"] == {"value": 3.0, "n": 2}


# ------------------------------------------------------------
# Periods
# ------------------------------------------------------------

def test_periods_group_by_the_time_of_the_audit_run():

    rows = [obs(1, "2026-01-30", "A"), obs(2, "2026-02-02", "A"), delayed(2, "2026-02-03", "B"),
            obs(3, "2026-03-15", "A")]

    result = trends.analyze(rows, period="month")

    assert [entry["period"] for entry in result["series"]] == ["2026-01", "2026-02", "2026-03"]
    assert [entry["shipments"] for entry in result["series"]] == [1, 2, 1]


def test_week_and_quarter_periods():

    rows = [obs(1, "2026-01-05", "A"), obs(2, "2026-01-12", "A"), obs(3, "2026-05-01", "A")]

    assert [e["period"] for e in trends.analyze(rows, period="week")["series"]] == [
        "2026-W02", "2026-W03", "2026-W18"]
    assert [e["period"] for e in trends.analyze(rows, period="quarter")["series"]] == ["2026-Q1", "2026-Q2"]

    with pytest.raises(ValueError):
        trends.analyze(rows, period="decade")


def test_a_shipment_counts_once_per_period_with_its_latest_observation():

    # Delayed in the first run of the month, fixed by the second
    rows = [delayed(1, "2026-02-01", "A"), obs(2, "2026-02-20", "A")]

    entry = trends.analyze(rows)["series"][0]

    assert entry["shipments"] == 1
    assert entry["delay"]["count"] == 0
    assert entry["discrepancy"]["count"] == 0


def test_a_status_change_between_periods_shows_in_the_series():

    rows = [delayed(1, "2026-01-10", "A"), delayed(1, "2026-01-10", "B"),
            obs(2, "2026-02-10", "A"), delayed(2, "2026-02-10", "B")]

    periods = by_period(trends.analyze(rows))

    assert periods["2026-01"]["discrepancy"]["rate"] == 1.0
    assert periods["2026-02"]["discrepancy"]["rate"] == 0.5


def test_an_unchanged_shipment_still_counts_in_a_later_period():
    # The store points a later run at the earlier snapshot: same snapshot id, new run
    first = obs(1, "2026-01-10", "A")
    again = dict(first, run_id=2, observed_at="2026-02-10T10:00:00+00:00")

    periods = by_period(trends.analyze([first, again]))

    assert periods["2026-01"]["shipments"] == 1 and periods["2026-02"]["shipments"] == 1


# ------------------------------------------------------------
# Empty and insufficient history
# ------------------------------------------------------------

def test_no_history_is_an_empty_state_not_zero_metrics():

    result = trends.analyze([])

    assert result["empty"] and result["series"] == [] and result["carriers"]["carriers"] == []
    assert "No audit history" in result["insufficient_reason"]


def test_one_period_is_not_a_trend():

    result = trends.analyze([obs(1, "2026-01-05", "A"), obs(1, "2026-01-05", "B")])

    assert not result["empty"] and len(result["series"]) == 1
    assert "no trend" in result["insufficient_reason"]


def test_filters_that_match_nothing_say_so():

    rows = [obs(1, "2026-01-05", "A"), obs(2, "2026-02-05", "A")]

    assert "No shipment matches" in trends.analyze(rows, carriers=["Nobody"])["insufficient_reason"]
    assert "date range" in trends.analyze(rows, start=date(2027, 1, 1))["insufficient_reason"]


# ------------------------------------------------------------
# Filters
# ------------------------------------------------------------

def test_filters_by_carrier_status_category_and_date_range():

    rows = [
        delayed(1, "2026-01-10", "A", carrier="Acme"),
        obs(1, "2026-01-10", "B", carrier="Bolt"),
        delayed(2, "2026-03-10", "C", carrier=None),
    ]

    def shipments(**filters):
        return sum(entry["shipments"] for entry in trends.analyze(rows, **filters)["series"])

    assert shipments() == 3
    assert shipments(carriers=["Acme"]) == 1
    assert shipments(carriers=[trends.NOT_STATED]) == 1
    assert shipments(statuses=["NO DISCREPANCIES"]) == 1
    assert shipments(categories=["Delivery delay"]) == 2
    assert shipments(start=date(2026, 2, 1)) == 1
    assert shipments(end=date(2026, 1, 31)) == 2
    assert shipments(carriers=["Acme", "Bolt"], statuses=["NO DISCREPANCIES"]) == 1


def test_filter_options_come_from_the_recorded_history():

    options = trends.filter_options([delayed(1, "2026-01-10", "A", carrier="Acme"),
                                     obs(2, "2026-02-10", "B", carrier=None)])

    assert options["carriers"] == ["Acme", "Not stated"]
    assert options["statuses"] == ["DISCREPANCIES DETECTED", "NO DISCREPANCIES"]
    assert options["categories"] == ["Delivery delay"]
    assert (options["first"], options["last"]) == (date(2026, 1, 10), date(2026, 2, 10))


# ------------------------------------------------------------
# Money
# ------------------------------------------------------------

def mismatch(run, day, shipment, difference, **kwargs):

    return obs(run, day, shipment, final="DISCREPANCIES DETECTED", invoice="MISMATCH", difference=difference,
               categories=("Invoice mismatch",), **kwargs)


def test_variance_is_an_arithmetic_mismatch_summed_within_a_currency():

    rows = [mismatch(1, "2026-01-05", "A", 30.0), mismatch(1, "2026-01-05", "B", -10.5),
            obs(1, "2026-01-05", "C")]

    group = trends.analyze(rows)["series"][0]["variance"]

    assert group == [{"currency": "Not stated", "invoices_checked": 3, "mismatches": 2, "net": 19.5,
                      "absolute": 40.5, "unknown_amount": 0}]


def test_currencies_are_never_mixed_and_unknown_is_its_own_group():

    rows = [mismatch(1, "2026-01-05", "A", 30.0, currency="USD"),
            mismatch(1, "2026-01-05", "B", 20.0, currency="CAD"),
            mismatch(1, "2026-01-05", "C", 5.0, currency=None)]

    groups = {g["currency"]: g for g in trends.analyze(rows)["series"][0]["variance"]}

    assert set(groups) == {"USD", "CAD", "Not stated"}
    assert groups["USD"]["absolute"] == 30.0 and groups["CAD"]["absolute"] == 20.0
    assert groups["Not stated"]["absolute"] == 5.0


def test_a_mismatch_without_an_amount_is_counted_but_not_added_as_zero():

    rows = [mismatch(1, "2026-01-05", "A", 12.0), mismatch(1, "2026-01-05", "B", None)]

    group = trends.analyze(rows)["series"][0]["variance"][0]

    assert group["mismatches"] == 1 and group["unknown_amount"] == 1 and group["absolute"] == 12.0


def test_no_mismatch_means_no_amount_rather_than_a_zero_amount():

    group = trends.analyze([obs(1, "2026-01-05", "A")])["series"][0]["variance"][0]

    assert group["mismatches"] == 0 and group["net"] is None and group["absolute"] is None


def test_the_overbilling_caveat_is_always_present():

    result = trends.analyze([obs(1, "2026-01-05", "A")])

    assert any("not" in note and "overbilling" in note for note in result["notes"])
    assert "overbilling" in trends.analytics_csv(result).decode("utf-8-sig")


# ------------------------------------------------------------
# Carrier scorecards
# ------------------------------------------------------------

def carrier_rows(name, clean, bad):

    shipments = [obs(1, "2026-01-05", f"{name}-ok-{n}", carrier=name) for n in range(clean)]
    shipments += [delayed(1, "2026-01-05", f"{name}-bad-{n}", carrier=name) for n in range(bad)]

    return shipments


def test_carriers_with_a_small_sample_are_shown_but_not_ranked():

    rows = carrier_rows("Big", 18, 2) + carrier_rows("Tiny", 0, 2)

    cards = {c["carrier"]: c for c in trends.analyze(rows)["carriers"]["carriers"]}

    assert cards["Big"]["rank"] == 1 and cards["Big"]["sufficient"]
    assert cards["Tiny"]["rank"] is None and not cards["Tiny"]["sufficient"]
    # The tiny carrier's 100% is not hidden, but it is not ranked above anyone
    assert cards["Tiny"]["discrepancy"]["rate"] == 1.0 and cards["Tiny"]["discrepancy"]["n"] == 2


def test_ranked_carriers_are_ordered_by_discrepancy_rate():

    rows = carrier_rows("Good", 28, 2) + carrier_rows("Poor", 14, 16)

    result = trends.analyze(rows)["carriers"]

    assert [c["carrier"] for c in result["carriers"]] == ["Good", "Poor"]
    assert [c["rank"] for c in result["carriers"]] == [1, 2]
    assert result["ranked"] == 2
    assert not any("chance" in note for note in result["notes"])


def test_overlapping_intervals_are_called_out():

    rows = carrier_rows("One", 8, 2) + carrier_rows("Two", 7, 3)

    notes = trends.analyze(rows)["carriers"]["notes"]

    assert any("chance" in note for note in notes)


def test_carrier_names_group_regardless_of_case_and_spacing_and_unknown_is_never_ranked():

    rows = carrier_rows("Acme Freight", 6, 0) + [
        obs(1, "2026-01-05", "X1", carrier="ACME   freight"),
    ] + [obs(1, "2026-01-05", f"N{n}", carrier=None) for n in range(9)]

    cards = {c["carrier"]: c for c in trends.analyze(rows)["carriers"]["carriers"]}

    assert cards["Acme Freight"]["shipments"] == 7
    assert cards["Not stated"]["rank"] is None and cards["Not stated"]["shipments"] == 9


def test_scorecards_use_each_shipments_latest_state():

    rows = [delayed(1, "2026-01-05", f"S{n}") for n in range(6)] + [obs(2, "2026-02-05", f"S{n}") for n in range(6)]

    card = trends.analyze(rows)["carriers"]["carriers"][0]

    assert card["shipments"] == 6 and card["discrepancy"]["count"] == 0


# ------------------------------------------------------------
# Repeat categories
# ------------------------------------------------------------

def test_repeat_categories_count_shipments_seen_by_more_than_one_audit():

    rows = [
        delayed(1, "2026-01-05", "A"), delayed(2, "2026-02-05", "A"),
        delayed(2, "2026-02-05", "B", carrier="Bolt"),
        mismatch(1, "2026-01-05", "C", 9.0),
    ]

    items = {item["category"]: item for item in trends.analyze(rows)["categories"]}

    assert items["Delivery delay"]["shipments_affected"] == 2
    assert items["Delivery delay"]["audit_observations"] == 3
    assert items["Delivery delay"]["shipments_repeating"] == 1
    assert items["Delivery delay"]["carriers"] == ["Acme", "Bolt"]
    assert items["Invoice mismatch"]["shipments_repeating"] == 0


# ------------------------------------------------------------
# Shipment history and comparison
# ------------------------------------------------------------

def test_two_snapshots_of_a_shipment_are_compared_whatever_the_order(tmp_path):

    store = history.HistoryStore(tmp_path / "h.sqlite3")
    key = record_documents(store, delivered=45)
    record_documents(store, delivered=48)

    snapshots = store.snapshots(key)
    assert len(snapshots) == 2

    forward = trends.compare_snapshots(snapshots, snapshots[0]["id"], snapshots[1]["id"])
    backward = trends.compare_snapshots(snapshots, snapshots[1]["id"], snapshots[0]["id"])

    assert forward["earlier"]["id"] == snapshots[0]["id"] == backward["earlier"]["id"]
    assert forward["changes"]["value_changes"] == backward["changes"]["value_changes"]
    assert any(change["field"] == "pod_pieces" for change in forward["changes"]["value_changes"])
    assert trends.compare_snapshots(snapshots, 9999, snapshots[0]["id"]) is None


# ------------------------------------------------------------
# CSV
# ------------------------------------------------------------

def parse(content):

    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))


def test_the_csv_is_flat_complete_and_records_the_filters():

    rows = [delayed(1, "2026-01-05", "A", carrier="Acme"), mismatch(2, "2026-02-05", "B", 7.0, carrier="Acme")]

    result = trends.analyze(rows, carriers=["Acme"])
    table = parse(trends.analytics_csv(result))

    assert tuple(table[0]) == trends.LONG_COLUMNS
    assert "carriers=Acme" in table[1][-1]
    sections = {row[0] for row in table[1:]}
    assert {"filters", "period", "carrier", "category", "note"} <= sections

    delay = [row for row in table if row[:2] == ["period", "2026-01"] and row[3] == "delay_rate_pct"][0]
    assert delay[4:7] == ["1", "1", "100.0"]


def test_a_blank_cell_is_not_available_never_zero_in_the_csv():

    result = trends.analyze([obs(1, "2026-01-05", "A", final="NEEDS REVIEW", delivery="NEEDS REVIEW",
                                 days_late=None)])
    table = parse(trends.analytics_csv(result))

    delay = [row for row in table if row[:2] == ["period", "2026-01"] and row[3] == "delay_rate_pct"][0]

    assert delay[5] == "0" and delay[6] == trends.cell(None) != "0"


def test_spreadsheet_formulas_are_neutralised_in_every_export():

    evil = "=HYPERLINK(\"http://evil\",\"x\")"
    rows = [delayed(1, "2026-01-05", f"S{n}", carrier=evil) for n in range(6)]
    rows[0]["findings"][0]["category"] = "+SUM(1+1)"

    result = trends.analyze(rows)

    for content in (trends.analytics_csv(result), trends.series_csv(result["series"]),
                    trends.carriers_csv(result["carriers"]), trends.categories_csv(result["categories"])):
        for row in parse(content):
            for value in row:
                assert not value.startswith(("=", "+", "-", "@", "\t", "\r")), value

    assert any(evil in cell for row in parse(trends.carriers_csv(result["carriers"])) for cell in row)


def test_the_wide_exports_have_their_documented_columns():

    result = trends.analyze([delayed(1, "2026-01-05", "A"), obs(2, "2026-02-05", "A")])

    assert tuple(parse(trends.series_csv(result["series"]))[0]) == trends.SERIES_COLUMNS
    assert tuple(parse(trends.carriers_csv(result["carriers"]))[0]) == trends.CARRIER_COLUMNS
    assert tuple(parse(trends.categories_csv(result["categories"]))[0]) == trends.CATEGORY_COLUMNS
    assert len(parse(trends.series_csv(result["series"]))) == 3


# ------------------------------------------------------------
# The history store: schema version 2
# ------------------------------------------------------------

def record_documents(store, **changes):
    """
    Audit synthetic documents of one shipment (changes: the arguments of
    test_reconciliation_consistency.documents) and record a run; returns
    the shipment key.
    """

    shots = snapshot(documents(shipment=A, **changes))
    store.record_run(shots, analytics.build_exceptions(shots))

    return "SHJ2026001"


def test_new_snapshots_keep_the_carrier_and_old_ones_stay_not_stated(tmp_path):

    store = history.HistoryStore(tmp_path / "h.sqlite3")
    key = record_documents(store)

    assert store.snapshots(key)[0]["carrier"] == "Swift Lines"
    assert store.observations()[0]["carrier"] == "Swift Lines"


def test_an_unchanged_audit_adds_an_observation_but_no_snapshot(tmp_path):

    store = history.HistoryStore(tmp_path / "h.sqlite3")
    key = record_documents(store)
    record_documents(store)

    assert len(store.snapshots(key)) == 1
    assert len(store.observations()) == 2


V1_SNAPSHOT_COLUMNS = (
    "run_id, shipment_key, shipment_id, created_at, input_fingerprint, digest, reconciled, "
    "final_status, quantity_status, delivery_status, invoice_status, discrepancy_count, "
    "review_count, audit_states"
)


def make_v1_history(path):

    connection = sqlite3.connect(path)
    connection.executescript(history.SCHEMA)
    connection.execute("INSERT INTO schema_info (version) VALUES (1)")
    connection.execute("INSERT INTO audit_runs (created_at, label, shipments_checked, snapshots_written) "
                       "VALUES ('2025-12-01T00:00:00+00:00', NULL, 1, 1)")
    connection.execute(
        f"INSERT INTO shipment_snapshots ({V1_SNAPSHOT_COLUMNS}) VALUES "
        "(1, 'K1', 'SHJ-K1', '2025-12-01T00:00:00+00:00', 'fp', 'dg', 1, 'NO DISCREPANCIES', 'MATCH', "
        "'ON TIME', 'MATCH', 0, 0, '{}')")
    connection.execute("INSERT INTO run_shipments (run_id, shipment_key, snapshot_id) VALUES (1, 'K1', 1)")
    connection.commit()
    connection.close()


def test_a_version_1_history_is_migrated_without_changing_old_rows(tmp_path):

    path = tmp_path / "old.sqlite3"
    make_v1_history(path)

    store = history.HistoryStore(path)
    observation = store.observations()[0]

    assert observation["carrier"] is None and observation["currency"] is None
    assert observation["final_status"] == "NO DISCREPANCIES"

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == history.SCHEMA_VERSION

        # Still append-only
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE shipment_snapshots SET carrier = 'Edited'")

    # Old history shows as "Not stated", never guessed
    row = trends.rows_from(store.observations())[0]
    assert (row["carrier"] or trends.NOT_STATED) == "Not stated"


def test_a_failed_history_migration_changes_nothing(tmp_path, monkeypatch):

    path = tmp_path / "old.sqlite3"
    make_v1_history(path)

    monkeypatch.setattr(history, "MIGRATION_V2", history.MIGRATION_V2 + ("ALTER TABLE nope ADD COLUMN x TEXT",))

    with pytest.raises(sqlite3.OperationalError):
        history.HistoryStore(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(shipment_snapshots)")}
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 1

    assert "carrier" not in columns


def test_history_recorded_before_the_carrier_existed_is_not_re_recorded(tmp_path):
    # With no carrier or currency the digest is what version 1 produced
    record = {"a": 1, "carrier": None, "currency": None}
    legacy = {"a": 1}

    assert history.digest(record) == history.digest(legacy)
    assert history.digest(dict(record, carrier="Acme")) != history.digest(legacy)
