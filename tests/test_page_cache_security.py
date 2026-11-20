"""
Security and lifecycle of the document text cache (docs/DOCUMENT_TEXT_CACHE.md):
its text is never logged, never backed up and never left behind after a
document is removed or replaced; it is owner-only where the platform has
permission bits; and it can be inspected and cleared safely from the command
line (scripts/manage_page_cache.py).

Synthetic PDFs in temporary folders.
"""

import logging
import os
import stat
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests", PROJECT_ROOT / "scripts"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import backup  # noqa: E402
import config  # noqa: E402
import manage_page_cache  # noqa: E402
import page_text_cache  # noqa: E402
from audit_service import AuditService, Locations  # noqa: E402
from backup import server_lock_path  # noqa: E402
from file_lock import FileLock  # noqa: E402
from test_ingestion_manager import make_pdf  # noqa: E402

SECRET = "ZQX-CONSIGNEE-SECRET-4471"
OTHER = "ZQX-REPLACEMENT-TEXT-9028"


def write_document(raw, marker=SECRET, name="BOL_SHJ-2026-777.pdf"):

    make_pdf(raw / name, "BILL OF LADING", ["Shipment Number: SHJ-2026-777", "", f"Consignee: {marker}",
                                            "Number of Pieces: 12"])


@pytest.fixture
def installation(tmp_path):

    data = tmp_path / "data"
    (data / "raw").mkdir(parents=True)
    write_document(data / "raw")

    return Locations(data / "raw", tmp_path / "chroma_db", data / "audit_reviews.sqlite3", data / "audit_history.sqlite3",
                     data / "ingestion_register.sqlite3", data / "quarantine", data / "archive")


def audited(locations):
    """One audit, with the cache committed and closed (as after a server stop)."""

    service = AuditService(locations)
    service.snapshot()
    service.page_cache.close()

    return service


def cache_bytes(locations):

    return b"".join(path.read_bytes() for path in locations.page_cache_path().parent.glob("page_text.cache*"))


# ============================================================
# The text stays in the cache
# ============================================================

def test_the_cache_holds_the_text_and_no_log_line_ever_does(installation, caplog):

    with caplog.at_level(logging.DEBUG):
        audited(installation)                                                   # a miss, stored
        audited(installation)                                                   # a hit

        # A damaged cache, a cache that cannot be opened: still nothing of the text in a message
        installation.page_cache_path().write_bytes(b"not a database" * 40)
        audited(installation)
        page_text_cache.PageTextCache(installation.raw_dir / "BOL_SHJ-2026-777.pdf" / "below-a-file.cache").pages_of(
            (installation.raw_dir / "BOL_SHJ-2026-777.pdf").read_bytes())

    assert SECRET not in caplog.text
    assert "page text cache cannot be used" in caplog.text                      # the fallback is said, by error type only
    assert str(installation.page_cache_path()) not in caplog.text


def test_a_backup_never_includes_the_cache(installation, tmp_path):

    audited(installation)
    assert SECRET.encode() in cache_bytes(installation)

    sources = backup.Sources.from_locations(installation, installation.raw_dir.parent / "auth.sqlite3")
    manifest, folder = backup.create_backup(sources, tmp_path / "backups", include_index=False)

    listed = [item["path"] for item in manifest["files"]]
    assert listed and not [path for path in listed if "cache" in path.lower()]
    assert not [path for path in folder.rglob("*") if "page_text" in path.name or path.name == "cache"]

    # And a restore creates none: the restored installation rebuilds it
    report = backup.restore_backup(folder, tmp_path / "restored", protected=(installation.raw_dir.parent,))
    assert report["status"] == "restored"
    assert not (tmp_path / "restored" / "data" / "cache").exists()


# ============================================================
# Nothing stale is left behind
# ============================================================

def test_the_text_of_a_replaced_document_is_gone_from_the_file_after_the_next_audit(installation):

    audited(installation)
    assert SECRET.encode() in cache_bytes(installation)

    write_document(installation.raw_dir, OTHER)
    audited(installation)

    found = cache_bytes(installation)
    assert OTHER.encode() in found
    assert SECRET.encode() not in found            # deleted and overwritten (secure_delete), not only unlinked
    assert page_text_cache.describe(installation.page_cache_path())["entries"] == 1


def test_the_text_of_a_removed_document_is_gone_from_the_file_after_the_next_audit(installation):

    write_document(installation.raw_dir, OTHER, name="POD_SHJ-2026-777.pdf")
    audited(installation)
    assert page_text_cache.describe(installation.page_cache_path())["entries"] == 2

    (installation.raw_dir / "BOL_SHJ-2026-777.pdf").unlink()
    audited(installation)

    found = cache_bytes(installation)
    assert SECRET.encode() not in found and OTHER.encode() in found
    assert page_text_cache.describe(installation.page_cache_path())["entries"] == 1


def test_entries_never_accumulate_over_many_replacements(installation):

    for number in range(6):
        write_document(installation.raw_dir, f"ZQX-VERSION-{number}")
        audited(installation)

    assert page_text_cache.describe(installation.page_cache_path())["entries"] == 1
    found = cache_bytes(installation)
    assert b"ZQX-VERSION-5" in found and not [n for n in range(5) if f"ZQX-VERSION-{n}".encode() in found]


# ============================================================
# Permissions
# ============================================================

@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits; on Windows the folder's ACL applies (documented)")
def test_the_cache_is_created_for_its_owner_only(installation):

    audited(installation)
    path = installation.page_cache_path()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert page_text_cache.describe(path)["mode"] == "0o600"


def test_describing_the_cache_gives_counts_never_text(installation):

    assert page_text_cache.describe(installation.page_cache_path()) == {
        "exists": False, "bytes": 0, "entries": 0, "parsers": [], "mode": None}

    audited(installation)
    found = page_text_cache.describe(installation.page_cache_path())

    assert found["exists"] and found["entries"] == 1 and found["parsers"] == [page_text_cache.PARSER]
    assert SECRET not in repr(found)

    installation.page_cache_path().write_bytes(b"not a database" * 40)
    assert page_text_cache.describe(installation.page_cache_path())["entries"] is None


# ============================================================
# The command line: status, clear and rebuild
# ============================================================

@pytest.fixture
def configured(installation, monkeypatch):

    monkeypatch.setattr(config, "DATA_DIR", installation.raw_dir.parent)
    monkeypatch.setattr(config, "RAW_DATA_DIR", installation.raw_dir)
    monkeypatch.setattr(config, "CHROMA_DIR", installation.chroma_dir)

    return installation


def test_status_reports_the_cache_without_its_text_or_its_path(configured, capsys):

    assert manage_page_cache.main(["status"]) == 0
    assert "No document text cache yet" in capsys.readouterr().out

    audited(configured)
    assert manage_page_cache.main(["status"]) == 0
    out = capsys.readouterr().out

    assert "Documents cached: 1" in out and page_text_cache.PARSER in out
    assert SECRET not in out and str(configured.raw_dir.parent) not in out


def test_clear_needs_confirmation_and_a_stopped_server(configured, capsys):

    audited(configured)
    path = configured.page_cache_path()

    assert manage_page_cache.main(["clear"]) == 1 and path.is_file()
    assert "--confirm" in capsys.readouterr().err

    # The server holds the data folder's lock while it runs
    lock = FileLock(server_lock_path(config.DATA_DIR))
    lock.acquire()
    try:
        assert manage_page_cache.main(["clear", "--confirm"]) == 1 and path.is_file()
        assert "stop it first" in capsys.readouterr().err
    finally:
        lock.release()


def test_clear_removes_only_the_cache_and_the_next_audit_rebuilds_it(configured, capsys):

    service = audited(configured)
    before = service.snapshot()
    path = configured.page_cache_path()
    documents = {item.name: item.read_bytes() for item in configured.raw_dir.iterdir()}

    assert manage_page_cache.main(["clear", "--confirm"]) == 0
    assert "removed" in capsys.readouterr().out
    assert not path.exists()
    assert {item.name: item.read_bytes() for item in configured.raw_dir.iterdir()} == documents

    # Clearing again is harmless
    assert manage_page_cache.main(["clear", "--confirm"]) == 0

    rebuilt = AuditService(configured)
    after = rebuilt.snapshot()

    assert rebuilt.page_cache.stats()["misses"] == 1 and path.is_file()
    assert [item["shipment_key"] for item in after["shipments"]] == [item["shipment_key"] for item in before["shipments"]]
    assert after["shipments"][0]["result"] == before["shipments"][0]["result"]
