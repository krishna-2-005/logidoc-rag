"""
The persistent page text cache (src/page_text_cache.py): unchanged PDFs are not
parsed again, a replaced one always is, a removed one is forgotten, a damaged
cache or document never breaks the audit, and the audit gives the same results
with and without it.

Synthetic PDFs in temporary folders.
"""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import document_extraction  # noqa: E402
import document_loader  # noqa: E402
import page_text_cache  # noqa: E402
from audit_service import AuditService, Locations  # noqa: E402
from page_text_cache import PageTextCache  # noqa: E402
from test_freight_audit import SHIPMENT, write_contracted_shipment  # noqa: E402

INVOICE = "INVOICE_SHJ-2026-101.pdf"


@pytest.fixture
def raw(tmp_path):

    folder = tmp_path / "data" / "raw"
    folder.mkdir(parents=True)
    write_contracted_shipment(folder)

    return folder


def service_on(raw, cache=True):

    data = raw.parent
    service = AuditService(Locations(raw, data.parent / "chroma", data / "reviews.sqlite3", data / "history.sqlite3",
                                     data / "register.sqlite3", data / "quarantine", data / "archive"))

    if not cache:
        service.page_cache = PageTextCache(None)

    return service


def stable(value):
    """A snapshot without the time it was audited at."""

    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items() if key != "audited_at"}

    if isinstance(value, (list, tuple)):
        return [stable(item) for item in value]

    return value


def parses(monkeypatch):
    """Counts the PDFs actually parsed from here on."""

    counted = []
    real = page_text_cache.parse_pages
    monkeypatch.setattr(page_text_cache, "parse_pages", lambda data: counted.append(1) or real(data))

    return counted


def stored_entries(path):

    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT count(*) FROM page_text").fetchone()[0]


# ============================================================
# Hits, misses and identity
# ============================================================

def test_a_document_is_parsed_once_and_then_served_from_the_cache(raw, tmp_path, monkeypatch):

    counted = parses(monkeypatch)
    cache = PageTextCache(tmp_path / "cache" / "pages.sqlite3")
    data = (raw / INVOICE).read_bytes()

    first = cache.pages_of(data)
    again = cache.pages_of(data)

    assert first == again and first[0][0] == 1 and "Fuel Surcharge" in first[0][1]
    assert len(counted) == 1 and cache.stats() == {"hits": 1, "misses": 1, "stored": 1, "enabled": True}

    # After a restart: another cache object on the same file
    cache.flush()
    cache.close()
    assert PageTextCache(tmp_path / "cache" / "pages.sqlite3").pages_of(data) == first
    assert len(counted) == 1


def test_identity_is_the_content_not_the_name(raw, tmp_path, monkeypatch):

    counted = parses(monkeypatch)
    cache = PageTextCache(tmp_path / "pages.sqlite3")
    cache.pages_of((raw / INVOICE).read_bytes())

    # The same bytes under another name: a hit
    copy = raw / "renamed.pdf"
    copy.write_bytes((raw / INVOICE).read_bytes())
    document_loader.load_document(copy, cache=cache)
    assert len(counted) == 1

    # Other bytes under the same name: a miss, and the new text
    write_contracted_shipment(raw, fuel_billed="$185.00")
    pages = document_loader.extract_pdf(raw / INVOICE, cache)
    assert len(counted) == 2 and "$185.00" in pages[0]["text"] and "$200.00" not in pages[0]["text"]


def test_the_loader_and_the_extractor_give_the_same_with_and_without_the_cache(raw, tmp_path):

    cache = PageTextCache(tmp_path / "pages.sqlite3")

    for name in sorted(path.name for path in raw.glob("*.pdf")):
        for _ in range(2):          # a miss, then a hit
            assert document_loader.load_document(raw / name, cache=cache) == document_loader.load_document(raw / name)
            cached = document_extraction.extract_document(raw / name, cache=cache)
            plain = document_extraction.extract_document(raw / name)
            assert cached == plain


def test_another_parser_version_is_a_miss(raw, tmp_path, monkeypatch):

    cache = PageTextCache(tmp_path / "pages.sqlite3")
    data = (raw / INVOICE).read_bytes()
    cache.pages_of(data)

    monkeypatch.setattr(page_text_cache, "PARSER", "pymupdf-next|text-sorted-stripped-1")
    counted = parses(monkeypatch)

    cache.pages_of(data)
    assert len(counted) == 1

    # The entry of the earlier parser goes at the next prune
    assert cache.prune() == 1
    assert stored_entries(tmp_path / "pages.sqlite3") == 1


# ============================================================
# The audit: restart, replaced, removed
# ============================================================

def test_a_restart_parses_nothing_and_audits_the_same(raw, monkeypatch):

    cold = service_on(raw)
    first = stable(cold.snapshot())
    assert cold.page_cache.stats()["misses"] == 4
    cold.page_cache.close()

    counted = parses(monkeypatch)
    restarted = service_on(raw)

    assert stable(restarted.snapshot()) == first
    assert counted == [] and restarted.page_cache.stats()["misses"] == 0

    # And exactly what the audit gives with no cache at all
    assert stable(service_on(raw, cache=False).snapshot()) == first


def test_a_replaced_document_is_read_again_and_the_audit_changes(raw, tmp_path, monkeypatch):

    service = service_on(raw)
    assert service.shipment(SHIPMENT)["freight_audit"]["outcome"] == "DISCREPANCY"

    counted = parses(monkeypatch)
    # Only the invoice is replaced: it now bills the contracted fuel surcharge
    other = tmp_path / "replacement"
    other.mkdir()
    write_contracted_shipment(other, fuel_billed="$180.00")
    (raw / INVOICE).write_bytes((other / INVOICE).read_bytes())

    after = service.shipment(SHIPMENT)["freight_audit"]
    fuel = next(item for item in after["charges"] if item["charge"] == "Fuel surcharge")

    assert fuel["outcome"] != "DISCREPANCY"
    assert len(counted) == 1                                    # only the changed invoice was parsed


def test_a_removed_document_is_forgotten_by_the_cache(raw):

    service = service_on(raw)
    service.snapshot()
    assert stored_entries(service.locations.page_cache_path()) == 4

    (raw / "POD_SHJ-2026-101.pdf").unlink()
    service.snapshot()

    assert stored_entries(service.locations.page_cache_path()) == 3


# ============================================================
# Damage never breaks the audit
# ============================================================

def test_a_damaged_cache_file_is_ignored(raw):

    reference = stable(service_on(raw, cache=False).snapshot())
    path = service_on(raw).locations.page_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a database" * 50)

    service = service_on(raw)

    assert stable(service.snapshot()) == reference
    assert service.page_cache.stats()["enabled"] is False


def test_a_cache_that_cannot_be_created_is_ignored(raw):

    blocker = raw.parent / "cache"
    blocker.write_text("a file where the cache folder would be")

    service = service_on(raw)

    assert service.shipment(SHIPMENT)["freight_audit"]["outcome"] == "DISCREPANCY"
    assert service.page_cache.stats()["enabled"] is False


def test_an_entry_that_does_not_decode_is_parsed_again(raw, tmp_path, monkeypatch):

    cache = PageTextCache(tmp_path / "pages.sqlite3")
    data = (raw / INVOICE).read_bytes()
    good = cache.pages_of(data)
    cache.flush()

    with closing(sqlite3.connect(tmp_path / "pages.sqlite3")) as connection:
        connection.execute("UPDATE page_text SET pages = '{not json'")
        connection.commit()

    counted = parses(monkeypatch)
    assert cache.pages_of(data) == good and len(counted) == 1
    cache.flush()

    with closing(sqlite3.connect(tmp_path / "pages.sqlite3")) as connection:
        assert json.loads(connection.execute("SELECT pages FROM page_text").fetchone()[0])[0][0] == 1


def test_a_corrupted_document_is_reported_and_never_cached(raw):

    (raw / INVOICE).write_bytes(b"%PDF-1.4 damaged beyond reading")
    service = service_on(raw)

    snapshot = service.snapshot()

    assert INVOICE in {item["filename"] for item in snapshot["unreadable_files"]}
    assert stored_entries(service.locations.page_cache_path()) == 3

    # Repaired: read again, and the audit is back
    write_contracted_shipment(raw)
    assert service.shipment(SHIPMENT)["freight_audit"]["outcome"] == "DISCREPANCY"


def test_without_a_path_nothing_is_kept(raw, monkeypatch):

    counted = parses(monkeypatch)
    cache = PageTextCache(None)
    data = (raw / INVOICE).read_bytes()

    assert cache.pages_of(data) == cache.pages_of(data)
    assert len(counted) == 2 and cache.stats()["enabled"] is False
