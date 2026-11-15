"""
The migration rehearsal (scripts/rehearse_migration.py): it upgrades a copy of
a restored data folder, never the source or the installation's own data, and
its report proves every earlier row survived without holding any of them.

Synthetic databases in temporary folders: a review store at version 2 and an
audit history at version 3, as the real data was found in Sprint 4.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests", PROJECT_ROOT / "scripts"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_history  # noqa: E402
import rehearse_migration  # noqa: E402
from rehearse_migration import RehearsalError, rehearse  # noqa: E402
from test_freight_operations import version_2_database  # noqa: E402


def v3_history(path):
    """An audit history as version 3 wrote it, with one recorded run."""

    with sqlite3.connect(path) as connection:
        connection.executescript(audit_history.SCHEMA)
        connection.execute("INSERT INTO schema_info VALUES (1)")

        for statement in (*audit_history.MIGRATION_V2, *audit_history.MIGRATION_V3):
            connection.execute(statement)

        connection.execute("UPDATE schema_info SET version = 3")
        at = "2026-10-01T09:00:00+00:00"
        connection.execute("INSERT INTO audit_runs (created_at, label, shipments_checked, snapshots_written) "
                           "VALUES (?, 'old', 1, 1)", (at,))
        connection.execute("INSERT INTO shipment_snapshots (run_id, shipment_key, shipment_id, created_at, input_fingerprint, "
                           "digest, reconciled, discrepancy_count, review_count, audit_states) "
                           "VALUES (1, 'K1', 'K-1', ?, 'f', 'd', 1, 0, 0, '{}')", (at,))
        connection.execute("INSERT INTO run_shipments (run_id, shipment_key, snapshot_id) VALUES (1, 'K1', 1)")


@pytest.fixture
def restored(tmp_path):

    data = tmp_path / "restored" / "data"
    (data / "raw").mkdir(parents=True)
    version_2_database(data / "audit_reviews.sqlite3")
    v3_history(data / "audit_history.sqlite3")

    return data


def contents(folder):

    return {path.name: path.read_bytes() for path in Path(folder).iterdir() if path.is_file()}


def test_a_rehearsal_upgrades_the_copy_keeps_every_row_and_leaves_the_source_alone(restored, tmp_path):

    before = contents(restored)
    work = tmp_path / "rehearsal"

    report = rehearse(restored, work)

    assert report["ok"], [name for name, passed in report["checks"].items() if not passed]
    assert contents(restored) == before
    assert report["databases"]["reviews"]["changes"]["version"] == [2, 3]
    assert report["databases"]["history"]["changes"]["version"] == [3, 6]
    assert report["databases"]["history"]["changes"]["tables_added"] == ["freight_audit_records"]
    assert report["databases"]["history"]["changes"]["columns_added"] == {"run_inputs": ["tms"]}
    assert report["review_copy_before_upgrade"] == {"kept": True, "version": 2}
    assert (work / "data" / "audit_reviews.before-v3.sqlite3").is_file()

    with sqlite3.connect(work / "data" / "audit_reviews.sqlite3") as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 3


def test_the_report_holds_counts_and_digests_never_a_row(restored, tmp_path):

    rehearse(restored, tmp_path / "rehearsal")
    text = (tmp_path / "rehearsal" / "rehearsal-report.json").read_text(encoding="utf-8")

    assert "Duplicate of a corrected invoice" not in text and "K|invoice" not in text and "memo 1" not in text
    assert json.loads(text)["databases"]["reviews"]["after"]["rows"]["exception_reviews"] == 1


def test_a_changed_row_fails_the_rehearsal(restored, tmp_path, monkeypatch):

    original = rehearse_migration.facts_after

    def tampered(path, before):
        if path.name == "audit_reviews.sqlite3":
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE exception_reviews SET note = 'changed' WHERE exception_key = 'K|invoice'")
        return original(path, before)

    monkeypatch.setattr(rehearse_migration, "facts_after", tampered)
    report = rehearse(restored, tmp_path / "rehearsal")

    assert not report["ok"]
    assert report["databases"]["reviews"]["changes"]["tables_changed"] == ["exception_reviews: rows differ"]


def test_the_installation_data_and_existing_folders_are_refused(restored, tmp_path):

    with pytest.raises(RehearsalError):
        rehearse(PROJECT_ROOT / "data", tmp_path / "rehearsal")

    with pytest.raises(RehearsalError):
        rehearse(restored, PROJECT_ROOT / "data" / "rehearsal")

    with pytest.raises(RehearsalError):
        rehearse(restored, PROJECT_ROOT)              # holds the data folder

    existing = tmp_path / "existing"
    existing.mkdir()

    with pytest.raises(RehearsalError):
        rehearse(restored, existing)

    with pytest.raises(RehearsalError):
        rehearse(restored, restored / "inside")

    assert not (PROJECT_ROOT / "data" / "rehearsal").exists()
