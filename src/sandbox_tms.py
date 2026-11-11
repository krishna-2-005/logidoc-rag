"""
The sandbox TMS: a simulated external transport management system on
synthetic data, for building and testing integrations without a vendor
(docs/TMS_ERP_INTEGRATION_FRAMEWORK.md). It is not a real vendor's API and
claims no compatibility with one.

Where it lives

    Only in the sandbox folder (LOGIDOC_INTEGRATION_SANDBOX_DIR, an explicit
    folder apart from the data, index and backup folders; never offered in
    production): sandbox_tms.sqlite3 is the simulated system, and the records
    a sandbox integration imports go to sandbox_imports.sqlite3 beside it.
    Every identifier starts with SBX-, every name with "Sandbox".

What it simulates

    data            6 carriers and 40 shipments, generated the same way every
                    time, with a few malformed ones (an unknown status, a
                    delivery before its pickup, a charge without currency, an
                    invalid SCAC) to exercise rejections
    paging          items ordered by (updated_at, id); the cursor is the last
                    item's pair, so a page is never skipped or repeated
    incremental     simulate_changes() moves shipments forward (status,
                    delivery date) with a new updated_at: the next sync finds
                    exactly those
    authentication  the credential must be the sandbox token "sandbox-token"
                    (a fixed, public value: this is not a secret), read from
                    the environment variable the integration names
                    (LOGIDOC_INTEGRATION_SANDBOX_TOKEN by default);
                    simulate_auth_failure refuses it anyway
    failures        rate_limit_every=N: every Nth request answers "rate
                    limited, retry after 1 s"; timeout_every=N: every Nth
                    request times out; unavailable: every request fails
    duplicates      duplicate_records repeats a page's first item at its end
    audit runs      receives LogiDoc's recorded audit runs (idempotent by run
                    id)
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from integration_framework import (AuthenticationFailed, IntegrationRefused, Page, Provider, ProviderTimeout,
                                   ProviderUnavailable, RateLimited)

SANDBOX_TOKEN = "sandbox-token"
DEFAULT_CREDENTIAL_ENV = "LOGIDOC_INTEGRATION_SANDBOX_TOKEN"
BASE_TIME = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)

CARRIERS = (
    ("SBX-CAR-001", "Sandbox Carrier Alpha LLC", "SBXA", "MC-900001", "USDOT 3900001"),
    ("SBX-CAR-002", "Sandbox Carrier Bravo Inc", "SBXB", "MC-900002", "USDOT 3900002"),
    ("SBX-CAR-003", "Sandbox Carrier Charlie Ltd", "SBXC", "MC-900003", None),
    ("SBX-CAR-004", "Sandbox Carrier Delta Freight", "SBXD", None, "USDOT 3900004"),
    ("SBX-CAR-005", "Sandbox Carrier Echo Logistics", None, "MC-900005", "USDOT 3900005"),
    ("SBX-CAR-006", "Sandbox Carrier Foxtrot Transport", "SBX1", None, None),   # invalid SCAC: rejected
)

LANES = (("Chicago, IL", "Atlanta, GA"), ("Dallas, TX", "Denver, CO"), ("Columbus, OH", "Nashville, TN"),
         ("Phoenix, AZ", "Los Angeles, CA"), ("Memphis, TN", "St. Louis, MO"))

STATUSES = ("booked", "dispatched", "in_transit", "delivered")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    entity     TEXT NOT NULL,
    id         TEXT NOT NULL,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (entity, id)
);
CREATE INDEX IF NOT EXISTS items_by_change ON items (entity, updated_at, id);
CREATE TABLE IF NOT EXISTS received_runs (run_id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, shipments INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def stamp(moment):

    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def seed_items():
    """The synthetic data: the same every time."""

    items = []

    for index, (identifier, name, scac, mc, dot) in enumerate(CARRIERS):
        items.append(("carriers", identifier, {
            "id": identifier, "legal_name": name, "scac": scac, "mc_number": mc, "dot_number": dot, "status": "active",
            "updated_at": stamp(BASE_TIME + timedelta(minutes=index)),
        }))

    for number in range(1, 41):
        identifier = f"SBX-SHP-{number:04d}"
        carrier = CARRIERS[(number - 1) % 5]
        origin, destination = LANES[(number - 1) % len(LANES)]
        status = STATUSES[(number - 1) % len(STATUSES)]
        pickup = (BASE_TIME + timedelta(days=number % 20)).date()
        delivery = pickup + timedelta(days=2 + number % 3) if status == "delivered" else None
        item = {
            "id": identifier, "reference": f"SBX-PRO-{10000 + number}", "status": status, "carrier_id": carrier[0],
            "carrier_name": carrier[1], "origin": origin, "destination": destination, "pickup_date": pickup.isoformat(),
            "delivery_date": delivery.isoformat() if delivery else None, "pieces": 4 + number % 9,
            "weight_kg": round(350.0 + number * 17.5, 1), "currency": "USD", "freight_charge": round(900 + number * 37.25, 2),
            "updated_at": stamp(BASE_TIME + timedelta(hours=1, minutes=number)),
        }
        if number == 13:
            item["status"] = "lost"                                   # not a known status: rejected
        if number == 27:
            item["status"], item["pickup_date"], item["delivery_date"] = "delivered", "2026-10-20", "2026-10-18"   # before pickup
        if number == 39:
            item["currency"] = None                                   # a charge without currency: rejected
        items.append(("shipments", identifier, item))

    return items


class SandboxTMS:
    """The simulated system (its own database in the sandbox folder)."""

    def __init__(self, folder, clock=lambda: datetime.now(timezone.utc)):

        self.path = Path(folder) / "sandbox_tms.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock

        with closing(self.connect()) as connection, connection:
            connection.executescript(SCHEMA)
            if connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
                connection.executemany("INSERT INTO items (entity, id, payload, updated_at) VALUES (?, ?, ?, ?)",
                                       [(entity, identifier, json.dumps(payload), payload["updated_at"])
                                        for entity, identifier, payload in seed_items()])

    def connect(self):

        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row

        return connection

    def page(self, entity, cursor, size, duplicates=False):

        cursor = cursor or {}
        after_time, after_id = cursor.get("updated_at") or "", cursor.get("id") or ""

        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM items WHERE entity = ? AND (updated_at > ? OR (updated_at = ? AND id > ?)) "
                "ORDER BY updated_at, id LIMIT ?", (entity, after_time, after_time, after_id, size + 1)).fetchall()

        more = len(rows) > size
        rows = rows[:size]
        records = [json.loads(row["payload"]) for row in rows]

        if duplicates and records:
            records.append(dict(records[0]))

        next_cursor = {"updated_at": rows[-1]["updated_at"], "id": rows[-1]["id"]} if rows else (cursor or None)

        return Page(records=records, next_cursor=next_cursor, has_more=more)

    def count_request(self):

        with closing(self.connect()) as connection, connection:
            connection.execute("INSERT INTO counters (name, value) VALUES ('requests', 1) "
                               "ON CONFLICT (name) DO UPDATE SET value = value + 1")
            return connection.execute("SELECT value FROM counters WHERE name = 'requests'").fetchone()[0]

    def change(self, count=3):
        """Moves `count` shipments one step forward, with a new change time. Returns their ids."""

        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute("SELECT max(updated_at) FROM items").fetchone()[0]
            moment = max(datetime.fromisoformat(latest.replace("Z", "+00:00")), self.clock()) + timedelta(seconds=1)
            rows = connection.execute(
                "SELECT * FROM items WHERE entity = 'shipments' AND json_extract(payload, '$.status') IN "
                "('booked', 'dispatched', 'in_transit') ORDER BY updated_at, id LIMIT ?", (int(count),)).fetchall()
            changed = []
            for offset, row in enumerate(rows):
                payload = json.loads(row["payload"])
                position = STATUSES.index(payload["status"])
                payload["status"] = STATUSES[position + 1]
                if payload["status"] == "delivered":
                    pickup = datetime.fromisoformat(payload["pickup_date"]).date()
                    payload["delivery_date"] = (pickup + timedelta(days=3)).isoformat()
                payload["updated_at"] = stamp(moment + timedelta(seconds=offset))
                connection.execute("UPDATE items SET payload = ?, updated_at = ? WHERE entity = 'shipments' AND id = ?",
                                   (json.dumps(payload), payload["updated_at"], row["id"]))
                changed.append(row["id"])

        return changed

    def receive_runs(self, runs):

        accepted = 0

        with closing(self.connect()) as connection, connection:
            for run in runs:
                accepted += connection.execute(
                    "INSERT OR IGNORE INTO received_runs (run_id, received_at, shipments) VALUES (?, ?, ?)",
                    (int(run["run_id"]), stamp(self.clock()), len(run.get("shipments") or []))).rowcount

        return accepted

    def received_runs(self):

        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM received_runs ORDER BY run_id")]


class SandboxProvider(Provider):

    key = "sandbox_tms"
    label = "Sandbox TMS (synthetic data)"
    description = ("A simulated transport management system on synthetic data, kept in its own sandbox folder. It is "
                   "not a real vendor's API.")
    entities = ("carriers", "shipments")
    accepts_audit_runs = True
    sandbox = True
    default_credential_env = DEFAULT_CREDENTIAL_ENV

    NUMBERS = {"page_size": (1, 200, 10), "rate_limit_every": (0, 100, 0), "timeout_every": (0, 100, 0)}
    SWITCHES = ("simulate_auth_failure", "duplicate_records", "unavailable")

    @classmethod
    def check_settings(cls, settings):

        unknown = set(settings) - set(cls.NUMBERS) - set(cls.SWITCHES)

        if unknown:
            raise IntegrationRefused(f"Unknown setting: {', '.join(sorted(unknown))}.")

        checked = {}

        for name, (low, high, default) in cls.NUMBERS.items():
            value = settings.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise IntegrationRefused(f"{name} must be a whole number from {low} to {high}.")
            checked[name] = value

        for name in cls.SWITCHES:
            value = settings.get(name, False)
            if not isinstance(value, bool):
                raise IntegrationRefused(f"{name} must be true or false.")
            checked[name] = value

        return checked

    @classmethod
    def availability(cls, context):

        if context.production:
            return "The sandbox provider is not offered in production."
        if context.sandbox_dir is None:
            return "Set LOGIDOC_INTEGRATION_SANDBOX_DIR to a separate folder to use the sandbox TMS."

        return None

    def system(self):

        return SandboxTMS(self.context.sandbox_dir, self.context.clock)

    def authenticate(self):

        if self.settings.get("unavailable"):
            raise ProviderUnavailable("The sandbox TMS is set to be unavailable.")
        if not self.credential:
            raise AuthenticationFailed("No credential: the environment variable the integration names is not set.")
        if self.settings.get("simulate_auth_failure") or self.credential != SANDBOX_TOKEN:
            raise AuthenticationFailed("The sandbox TMS refused the credential.")

    def simulate_failures(self, system):

        number = system.count_request()
        every = self.settings.get("rate_limit_every") or 0

        if every and number % every == 0:
            raise RateLimited("The sandbox TMS is rate limiting requests.", retry_after=1)

        every = self.settings.get("timeout_every") or 0

        if every and number % every == 0:
            raise ProviderTimeout("The sandbox TMS did not answer in time.")

    def test_connection(self):

        self.authenticate()
        self.system()

    def fetch(self, entity, cursor, page_size):

        self.authenticate()
        system = self.system()
        self.simulate_failures(system)

        return system.page(entity, cursor, page_size, duplicates=bool(self.settings.get("duplicate_records")))

    def send_audit_runs(self, runs):

        self.authenticate()

        return self.system().receive_runs(runs)

    def simulate_changes(self, count=3):

        if not 1 <= int(count) <= 20:
            raise IntegrationRefused("Change 1 to 20 shipments at a time.")

        return {"changed": self.system().change(int(count))}
