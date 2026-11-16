"""
The migration guard (src/migration_guard.py): the installation's review and
history databases are upgraded only at startup, and only when a fresh, verified
backup holds their current contents. Missing, stale, corrupted and valid
backups; the stores' own check; the API and Streamlit starts.

Isolated copies only: a review database at schema version 2 and an audit
history at version 3, as the real data was found, in temporary folders.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_history  # noqa: E402
import backup  # noqa: E402
import config  # noqa: E402
import migration_guard  # noqa: E402
import review_store  # noqa: E402
from audit_history import HistoryStore  # noqa: E402
from audit_service import Locations  # noqa: E402
from migration_guard import MigrationBlocked  # noqa: E402
from review_store import ReviewStore, ReviewView  # noqa: E402
from test_freight_operations import version_2_database  # noqa: E402
from test_rehearse_migration import v3_history  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_registrations():

    migration_guard.reset()
    yield
    migration_guard.reset()


class Installation:
    """An installation in a temporary folder whose two databases wait for their upgrade."""

    def __init__(self, root):

        self.data = root / "data"
        (self.data / "raw").mkdir(parents=True)
        self.backups = root / "backups"
        self.locations = Locations(
            self.data / "raw", root / "chroma_db", self.data / "audit_reviews.sqlite3",
            self.data / "audit_history.sqlite3", self.data / "ingestion_register.sqlite3",
            self.data / "quarantine", self.data / "archive",
        )
        version_2_database(self.locations.review_db)
        v3_history(self.locations.history_db)

    def backup(self):

        sources = backup.Sources.from_locations(self.locations, self.data / "auth.sqlite3")
        manifest, folder = backup.create_backup(sources, self.backups, include_index=False)

        return manifest["backup_id"], folder

    def files(self):

        return {path.name: path.read_bytes() for path in self.data.iterdir() if path.is_file()}

    def versions(self):

        found = {}
        for name, path in (("reviews", self.locations.review_db), ("history", self.locations.history_db)):
            with sqlite3.connect(path) as connection:
                found[name] = connection.execute("SELECT version FROM schema_info").fetchone()[0]
        return found


@pytest.fixture
def installation(tmp_path):

    return Installation(tmp_path)


# ============================================================
# Backups: missing, stale, corrupted, valid
# ============================================================

def test_without_a_backup_nothing_is_upgraded_and_the_message_says_what_to_do(installation):

    before = installation.files()

    with pytest.raises(MigrationBlocked) as refused:
        migration_guard.prepare(installation.locations, installation.backups)

    message = str(refused.value)
    assert "exception review database (schema version 2 to 3)" in message
    assert "audit history database (schema version 3 to 6)" in message
    assert "no complete backup" in message and "python scripts/manage_backups.py create" in message
    assert str(installation.data) not in message           # settings and commands, never a path
    assert installation.files() == before                  # not opened for writing, no .before-v3 copy


def test_a_backup_made_before_the_last_change_is_stale(installation):

    installation.backup()

    with sqlite3.connect(installation.locations.review_db) as connection:
        connection.execute("UPDATE exception_reviews SET note = 'Changed after the backup.'")

    before = installation.files()

    with pytest.raises(MigrationBlocked) as refused:
        migration_guard.prepare(installation.locations, installation.backups)

    assert "does not hold the current exception review database" in str(refused.value)
    assert installation.files() == before
    assert installation.versions() == {"reviews": 2, "history": 3}


def test_a_corrupted_backup_is_not_trusted(installation):

    backup_id, folder = installation.backup()
    copy = next(path for path in (folder / "databases").iterdir() if "history" in path.name)
    data = bytearray(copy.read_bytes())
    data[-100] ^= 0xFF
    copy.write_bytes(bytes(data))

    with pytest.raises(MigrationBlocked) as refused:
        migration_guard.prepare(installation.locations, installation.backups)

    assert f"backup {backup_id} fails verification (checksum" in str(refused.value)
    assert installation.versions() == {"reviews": 2, "history": 3}


def test_an_incomplete_backup_is_not_trusted(installation):

    _, folder = installation.backup()
    folder.rename(folder.with_name(folder.name + ".partial"))

    with pytest.raises(MigrationBlocked, match="no complete backup"):
        migration_guard.prepare(installation.locations, installation.backups)


def test_a_fresh_verified_backup_lets_the_upgrade_run_once_at_startup(installation):

    backup_id, folder = installation.backup()
    kept = {path.name: path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    rows = ReviewView(installation.locations.review_db).states()

    report = migration_guard.prepare(installation.locations, installation.backups)

    assert report["backup_id"] == backup_id
    assert sorted(report["upgraded"]) == ["audit_history", "audit_reviews"]
    assert installation.versions() == {"reviews": review_store.SCHEMA_VERSION, "history": audit_history.SCHEMA_VERSION}
    assert ReviewStore(installation.locations.review_db).states()["K|invoice"]["note"] == rows["K|invoice"]["note"]
    # The backup is only read
    assert {path.name: path.read_bytes() for path in folder.rglob("*") if path.is_file()} == kept

    # Nothing pending any more: the next start needs no backup
    assert migration_guard.prepare(installation.locations, installation.backups / "none")["pending"] == []


def test_current_or_missing_databases_need_no_backup(tmp_path):

    locations = Locations(tmp_path / "raw", tmp_path / "chroma", tmp_path / "reviews.sqlite3",
                          tmp_path / "history.sqlite3", tmp_path / "register.sqlite3", tmp_path / "q", tmp_path / "a")

    assert migration_guard.prepare(locations, tmp_path / "backups")["pending"] == []

    # A guarded path with no file yet: created at the current version, which is not an upgrade
    ReviewStore(locations.review_db)
    HistoryStore(locations.history_db)

    assert migration_guard.prepare(locations, tmp_path / "backups")["pending"] == []


def test_an_unreadable_database_stops_the_start_with_a_clear_message(installation):

    installation.locations.history_db.write_bytes(b"this is not a database" * 100)

    with pytest.raises(MigrationBlocked, match="audit history database cannot be read"):
        migration_guard.prepare(installation.locations, installation.backups)


# ============================================================
# The stores never upgrade a guarded database on their own
# ============================================================

def test_after_a_refused_start_no_store_upgrades_a_guarded_database(installation):

    with pytest.raises(MigrationBlocked):
        migration_guard.prepare(installation.locations, installation.backups)

    before = installation.files()

    with pytest.raises(MigrationBlocked):
        ReviewStore(installation.locations.review_db)

    with pytest.raises(MigrationBlocked):
        HistoryStore(installation.locations.history_db)

    # A read through the read-only view still works, and writes nothing
    assert ReviewView(installation.locations.review_db).states()["K|invoice"]["status"] == "dismissed"
    assert installation.files() == before


def test_paths_never_registered_upgrade_as_before(installation):
    """Tests, rehearsal copies and restore drills are not the installation's databases."""

    ReviewStore(installation.locations.review_db)
    HistoryStore(installation.locations.history_db)

    assert installation.versions() == {"reviews": review_store.SCHEMA_VERSION, "history": audit_history.SCHEMA_VERSION}


# ============================================================
# The API and Streamlit starts
# ============================================================

@pytest.fixture
def configured(installation, monkeypatch):
    """config.py pointing at the temporary installation, as the API and Streamlit read it."""

    monkeypatch.setattr(config, "RAW_DATA_DIR", installation.locations.raw_dir)
    monkeypatch.setattr(config, "CHROMA_DIR", installation.locations.chroma_dir)
    monkeypatch.setattr(config, "REVIEW_DB_PATH", installation.locations.review_db)
    monkeypatch.setattr(config, "HISTORY_DB_PATH", installation.locations.history_db)
    monkeypatch.setattr(config, "AUTH_DB_PATH", installation.data / "auth.sqlite3")
    monkeypatch.setattr(config, "BACKUP_DIR", installation.backups)

    return installation


def test_the_api_does_not_start_on_databases_awaiting_an_upgrade_without_a_backup(configured):

    from api.app import create_app
    from api.core import ApiSettings

    before = configured.files()

    with pytest.raises(MigrationBlocked):
        create_app(settings=ApiSettings(environ={"LOGIDOC_AUTH_MODE": "local"}))

    assert configured.files() == before


def test_the_api_upgrades_at_startup_with_a_fresh_backup_and_serves_reads(configured):

    from fastapi.testclient import TestClient

    from api.app import create_app
    from api.core import CLIENT_HEADER, ApiSettings

    configured.backup()
    app = create_app(settings=ApiSettings(environ={"LOGIDOC_AUTH_MODE": "local"}))

    assert configured.versions() == {"reviews": review_store.SCHEMA_VERSION, "history": audit_history.SCHEMA_VERSION}
    client = TestClient(app, headers={CLIENT_HEADER: "web"})
    assert client.get("/api/history/runs").status_code == 200


def test_the_server_command_explains_the_refusal_and_exits_4(configured, monkeypatch, capsys):

    import observability
    from api import serve

    # The server's own logging set-up would replace the test session's handlers
    monkeypatch.setattr(observability, "configure_logging", lambda **settings: [])
    monkeypatch.setattr(config, "DATA_DIR", configured.data)
    monkeypatch.setenv("LOGIDOC_AUTH_MODE", "local")
    monkeypatch.setenv("LOGIDOC_ENV", "test")

    assert serve.main([]) == 4
    assert "python scripts/manage_backups.py create" in capsys.readouterr().err
    assert configured.versions() == {"reviews": 2, "history": 3}


def test_the_streamlit_app_shows_the_refusal_and_upgrades_nothing(configured):

    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_resource.clear()
    before = configured.files()

    at = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=120).run()

    try:
        assert any("LogiDoc-RAG did not start" in item.value for item in at.error)
        assert configured.files() == before
    finally:
        st.cache_resource.clear()
