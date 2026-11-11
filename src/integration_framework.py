"""
TMS/ERP integration framework (docs/TMS_ERP_INTEGRATION_FRAMEWORK.md).

A provider-neutral way to bring records from an external system into
LogiDoc-RAG, and to hand it LogiDoc's audit runs, without putting anything of
the external system into the freight audit. The layers are separate:

    contracts       ShipmentRecord, CarrierRecord, Page, ConnectionResult:
                    typed, provider-neutral (this module)
    providers       Provider subclasses: authentication, transport and the
                    partner's own format, nothing else. The only provider is
                    the sandbox TMS (src/sandbox_tms.py), on synthetic data
    errors          every provider failure is one IntegrationError category
                    (authentication_failed, rate_limited, timeout,
                    unavailable, invalid_response, not_supported,
                    configuration); normalize() maps anything else onto them
    sync state      the integration store (src/integration_store.py):
                    settings, cursor, status, runs, rejections
    records         ExternalRecordStore: what was imported, with provenance
                    (integration, provider, external id, the partner's change
                    time, the runs that brought it) and every version

How a sync runs (SyncEngine.run)

    1. The integration's lease is claimed: one sync of an integration at a
       time, whether scheduled or asked for.
    2. Per entity the provider supports, pages are fetched from the stored
       cursor. A rate limit or timeout is retried (3 times, back-off, the
       partner's Retry-After honoured up to a minute); an authentication
       failure stops the run.
    3. Each record is validated and mapped to its contract. A refused record
       is a rejection (entity, external id, reason), never a guess.
    4. A record seen twice in one run is a duplicate (the later change time
       wins). A record equal to what is stored is unchanged; a different one
       is a new version (updated). A field corrected by an administrator
       (an override) is never overwritten: the received value is kept in the
       version and counted as a conflict.
    5. The cursor is saved after each page, after the page's records: a
       crash re-reads one page, and re-reading changes nothing (idempotent).
    6. Providers that accept audit runs get LogiDoc's recorded runs after
       their own cursor (the audit-run feed).

Nothing imported is written into LogiDoc's documents, audits, reviews or
carrier registry: imported records are reference data in their own store.
Sandbox records live only in the sandbox folder (LOGIDOC_INTEGRATION_SANDBOX_DIR,
an explicit, separate folder); without it the sandbox provider is not offered.

Adding a real provider: see the document above (a Provider subclass, its
settings check, a credential environment variable, contract mapping, and
tests against the vendor's own sandbox before any claim of support).
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

import authz
import integration_store as istore
from integration_store import dumps, later, loads, parse_time, text_time, utc_now
from redaction import redact

log = logging.getLogger("logidoc.integrations")

ENTITIES = ("shipments", "carriers")
SHIPMENT_STATUSES = ("booked", "dispatched", "in_transit", "delivered", "cancelled", "exception")
CREDENTIAL_ENV = re.compile(r"^LOGIDOC_INTEGRATION_[A-Z0-9_]{1,60}$")
PAGE_RETRIES = 3
MAX_RETRY_AFTER = 60
MIN_SYNC_INTERVAL = 60
MAX_SYNC_INTERVAL = 7 * 86400


# ============================================================
# CONTRACTS
# ============================================================

@dataclass(frozen=True)
class ShipmentRecord:
    """A shipment as an external TMS states it (provider-neutral)."""

    external_id: str
    reference: str
    status: str
    carrier_external_id: str | None
    carrier_name: str | None
    origin: str | None
    destination: str | None
    pickup_date: str | None
    delivery_date: str | None
    pieces: int | None
    weight_kg: float | None
    currency: str | None
    freight_charge: float | None
    source_updated_at: str


@dataclass(frozen=True)
class CarrierRecord:
    """A carrier as an external system states it (never merged into the carrier registry by itself)."""

    external_id: str
    legal_name: str
    scac: str | None
    mc_number: str | None
    dot_number: str | None
    status: str
    source_updated_at: str


@dataclass(frozen=True)
class Page:
    """records: the partner's raw items; next_cursor: where the next page starts; has_more: call again."""

    records: list
    next_cursor: dict | None
    has_more: bool


@dataclass(frozen=True)
class ConnectionResult:

    ok: bool
    category: str | None
    detail: str
    latency_ms: int


# ============================================================
# ERRORS
# ============================================================

class IntegrationError(Exception):
    """A provider failure in one category. retryable: the same call may work later."""

    category = "integration_error"
    retryable = False

    def __init__(self, message="", retry_after=None):

        super().__init__(message or self.category.replace("_", " "))
        self.retry_after = retry_after


class AuthenticationFailed(IntegrationError):
    category = "authentication_failed"


class RateLimited(IntegrationError):
    category = "rate_limited"
    retryable = True


class ProviderTimeout(IntegrationError):
    category = "timeout"
    retryable = True


class ProviderUnavailable(IntegrationError):
    category = "unavailable"
    retryable = True


class InvalidResponse(IntegrationError):
    category = "invalid_response"


class NotSupported(IntegrationError):
    category = "not_supported"


class ConfigurationError(IntegrationError):
    category = "configuration"


class IntegrationRefused(ValueError):
    """A management request that is refused (the message says why)."""


class SyncBusy(RuntimeError):
    """A sync of this integration is running."""


def normalize(error):
    """Any failure of a provider call as an IntegrationError (the message never names a host or a credential)."""

    if isinstance(error, IntegrationError):
        return error
    if isinstance(error, (TimeoutError,)) or type(error).__name__ in ("timeout", "ReadTimeout", "ConnectTimeout"):
        return ProviderTimeout("The external system did not answer in time.")
    if isinstance(error, (ConnectionError, OSError)):
        return ProviderUnavailable("The external system could not be reached.")
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return InvalidResponse("The external system's answer could not be read.")

    return IntegrationError("The call to the external system failed.")


# ============================================================
# PROVIDERS
# ============================================================

@dataclass
class ProviderContext:
    """What a provider may use: its sandbox folder (sandbox providers only), the clock and the sleep."""

    sandbox_dir: Path | None = None
    production: bool = False
    clock: object = utc_now
    sleep: object = time.sleep


class Provider(ABC):
    """
    One external system. A subclass holds authentication, transport and the
    partner's format; it returns raw items and raises IntegrationError
    categories. It never reads or writes LogiDoc's stores.
    """

    key = "provider"
    label = "Provider"
    description = ""
    entities = ()
    accepts_audit_runs = False
    sandbox = False
    default_credential_env = None

    def __init__(self, settings, credential, context):

        self.settings = settings
        self.credential = credential
        self.context = context

    @staticmethod
    def check_settings(settings):
        """The provider's settings, checked (raise IntegrationRefused). No secret may be a setting."""

        return {}

    @classmethod
    def availability(cls, context):
        """None when the provider can be used here, else why not."""

        return None

    @abstractmethod
    def test_connection(self):
        """Raises IntegrationError when the system cannot be reached or refuses the credential."""

    @abstractmethod
    def fetch(self, entity, cursor, page_size):
        """One page of entity items after cursor (None: from the start). Returns Page."""

    def send_audit_runs(self, runs):
        """Hands LogiDoc's recorded audit runs to the system; returns how many it accepted."""

        raise NotSupported("This system does not take audit runs.")

    def map(self, entity, raw):
        """The raw item as its contract; raise ValueError with the reason to reject it."""

        return map_shipment(raw) if entity == "shipments" else map_carrier(raw)


def text_field(raw, name, required=False, limit=200):

    value = raw.get(name)

    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{name} is missing.")
        return None

    if not isinstance(value, (str, int)):
        raise ValueError(f"{name} is not text.")

    text = " ".join(str(value).split())

    if len(text) > limit:
        raise ValueError(f"{name} is longer than {limit} characters.")

    return text


def date_field(raw, name):

    value = text_field(raw, name, limit=10)

    if value is None:
        return None

    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"{name} is not a date (YYYY-MM-DD).") from None


def number_field(raw, name, whole=False):

    value = raw.get(name)

    if value is None or value == "":
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} is not a number.")

    if value < 0:
        raise ValueError(f"{name} is negative.")

    if whole:
        if int(value) != value:
            raise ValueError(f"{name} is not a whole number.")
        return int(value)

    return float(value)


def timestamp_field(raw, name):

    value = text_field(raw, name, required=True, limit=40)

    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} is not a timestamp.") from None

    if moment.tzinfo is None:
        raise ValueError(f"{name} has no time zone.")

    return text_time(moment)


def map_shipment(raw):
    """A partner's shipment item as ShipmentRecord. ValueError names the first refused field."""

    if not isinstance(raw, dict):
        raise ValueError("The item is not an object.")

    status = text_field(raw, "status", required=True, limit=20).lower()

    if status not in SHIPMENT_STATUSES:
        raise ValueError(f"status {status!r} is not one of {', '.join(SHIPMENT_STATUSES)}.")

    pickup, delivery = date_field(raw, "pickup_date"), date_field(raw, "delivery_date")

    if pickup and delivery and delivery < pickup:
        raise ValueError("delivery_date is before pickup_date.")

    currency = text_field(raw, "currency", limit=3)

    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency is not a three-letter code.")

    charge = number_field(raw, "freight_charge")

    if charge is not None and not currency:
        raise ValueError("freight_charge has no currency.")

    return ShipmentRecord(
        external_id=text_field(raw, "id", required=True, limit=100),
        reference=text_field(raw, "reference", required=True, limit=100),
        status=status,
        carrier_external_id=text_field(raw, "carrier_id", limit=100),
        carrier_name=text_field(raw, "carrier_name"),
        origin=text_field(raw, "origin"),
        destination=text_field(raw, "destination"),
        pickup_date=pickup,
        delivery_date=delivery,
        pieces=number_field(raw, "pieces", whole=True),
        weight_kg=number_field(raw, "weight_kg"),
        currency=currency,
        freight_charge=charge,
        source_updated_at=timestamp_field(raw, "updated_at"),
    )


def map_carrier(raw):

    if not isinstance(raw, dict):
        raise ValueError("The item is not an object.")

    from carrier_registry import RegistryError, validate

    try:
        identifiers = validate({"legal_name": raw.get("legal_name"), "scac": raw.get("scac"),
                                "mc_number": raw.get("mc_number"), "dot_number": raw.get("dot_number")})
    except RegistryError as error:
        # The registry's own rules: a malformed identifier is refused, never cleaned into something else
        raise ValueError(str(error)) from None

    status = (text_field(raw, "status", limit=20) or "active").lower()

    if status not in ("active", "inactive"):
        raise ValueError(f"status {status!r} is not active or inactive.")

    return CarrierRecord(
        external_id=text_field(raw, "id", required=True, limit=100),
        legal_name=identifiers["legal_name"], scac=identifiers.get("scac"), mc_number=identifiers.get("mc_number"),
        dot_number=identifiers.get("dot_number"), status=status, source_updated_at=timestamp_field(raw, "updated_at"),
    )


def registered_providers():
    """{key: Provider subclass}: the providers this version knows."""

    import sandbox_tms

    return {sandbox_tms.SandboxProvider.key: sandbox_tms.SandboxProvider}


# ============================================================
# IMPORTED RECORDS (with provenance)
# ============================================================

RECORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS external_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    integration_id    INTEGER NOT NULL,
    provider          TEXT NOT NULL,
    entity            TEXT NOT NULL CHECK (entity IN ('shipments', 'carriers')),
    external_id       TEXT NOT NULL,
    source_updated_at TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    received          TEXT NOT NULL,
    data              TEXT NOT NULL,
    first_run_id      INTEGER NOT NULL,
    last_run_id       INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (integration_id, entity, external_id)
);

CREATE TABLE IF NOT EXISTS external_record_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id    INTEGER NOT NULL REFERENCES external_records (id),
    run_id       INTEGER,
    change       TEXT NOT NULL CHECK (change IN ('created', 'updated', 'override', 'override_cleared')),
    content_hash TEXT NOT NULL,
    received     TEXT,
    kept_fields  TEXT,
    actor        TEXT,
    at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS record_overrides (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES external_records (id),
    field     TEXT NOT NULL,
    value     TEXT,
    cleared   INTEGER NOT NULL DEFAULT 0 CHECK (cleared IN (0, 1)),
    reason    TEXT NOT NULL,
    actor     TEXT NOT NULL,
    at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS records_by_integration ON external_records (integration_id, entity, id);
CREATE INDEX IF NOT EXISTS versions_by_record ON external_record_versions (record_id, id);
CREATE INDEX IF NOT EXISTS overrides_by_record ON record_overrides (record_id, field, id);

CREATE TRIGGER IF NOT EXISTS external_record_versions_no_update BEFORE UPDATE ON external_record_versions
    BEGIN SELECT RAISE(ABORT, 'record history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS external_record_versions_no_delete BEFORE DELETE ON external_record_versions
    BEGIN SELECT RAISE(ABORT, 'record history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_overrides_no_update BEFORE UPDATE ON record_overrides
    BEGIN SELECT RAISE(ABORT, 'record history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_overrides_no_delete BEFORE DELETE ON record_overrides
    BEGIN SELECT RAISE(ABORT, 'record history is append-only'); END;
"""

RECORD_SCHEMA_VERSION = 1

# Fields of each contract an administrator may correct by hand
CORRECTABLE = {
    "shipments": ("reference", "status", "carrier_name", "origin", "destination", "pickup_date", "delivery_date", "pieces",
                  "weight_kg", "currency", "freight_charge"),
    "carriers": ("legal_name", "scac", "mc_number", "dot_number", "status"),
}


def content_hash(values):
    """The record's content without the partner's change time: a touch without a change is not a change."""

    content = {name: value for name, value in values.items() if name != "source_updated_at"}

    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class ExternalRecordStore:
    """Imported records with provenance, every version, and the manual corrections that win over imports."""

    def __init__(self, path):

        import sqlite3

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sqlite3 = sqlite3

        with closing(self.connect()) as connection, connection:
            connection.executescript(RECORD_SCHEMA)
            if connection.execute("SELECT version FROM schema_info").fetchone() is None:
                connection.execute("INSERT INTO schema_info (version) VALUES (?)", (RECORD_SCHEMA_VERSION,))

    def connect(self):

        connection = self.sqlite3.connect(self.path, timeout=15)
        connection.row_factory = self.sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    @staticmethod
    def overrides_of(connection, record_id):
        """{field: value} of the corrections in force (the latest row per field, unless cleared)."""

        found = {}

        for row in connection.execute("SELECT * FROM record_overrides WHERE record_id = ? ORDER BY id", (record_id,)):
            if row["cleared"]:
                found.pop(row["field"], None)
            else:
                found[row["field"]] = loads(row["value"])

        return found

    def apply(self, integration_id, provider, run_id, entity, values, at):
        """
        Stores one mapped record. Returns (change, kept_fields): change is
        created, updated or unchanged; kept_fields the corrected fields whose
        received value differed (the correction was kept).
        """

        digest = content_hash(values)

        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM external_records WHERE integration_id = ? AND entity = ? AND external_id = ?",
                                     (integration_id, entity, values["external_id"])).fetchone()

            if row is None:
                record_id = connection.execute(
                    "INSERT INTO external_records (integration_id, provider, entity, external_id, source_updated_at, content_hash, "
                    "received, data, first_run_id, last_run_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (integration_id, provider, entity, values["external_id"], values["source_updated_at"], digest, dumps(values),
                     dumps(values), run_id, run_id, at, at)).lastrowid
                connection.execute("INSERT INTO external_record_versions (record_id, run_id, change, content_hash, received, at) "
                                   "VALUES (?, ?, 'created', ?, ?, ?)", (record_id, run_id, digest, dumps(values), at))
                return "created", []

            if row["content_hash"] == digest:
                connection.execute("UPDATE external_records SET last_run_id = ?, source_updated_at = max(source_updated_at, ?) "
                                   "WHERE id = ?", (run_id, values["source_updated_at"], row["id"]))
                return "unchanged", []

            overrides = self.overrides_of(connection, row["id"])
            kept = sorted(name for name, value in overrides.items() if values.get(name) != value)
            data = {**values, **overrides}
            connection.execute(
                "UPDATE external_records SET source_updated_at = ?, content_hash = ?, received = ?, data = ?, last_run_id = ?, "
                "updated_at = ? WHERE id = ?", (values["source_updated_at"], digest, dumps(values), dumps(data), run_id, at, row["id"]))
            connection.execute(
                "INSERT INTO external_record_versions (record_id, run_id, change, content_hash, received, kept_fields, at) "
                "VALUES (?, ?, 'updated', ?, ?, ?, ?)", (row["id"], run_id, digest, dumps(values), dumps(kept) if kept else None, at))

            return "updated", kept

    def override(self, actor, record_id, field_name, value, reason, at, clear=False):
        """An administrator's correction of one field (or its removal). Imports never overwrite it."""

        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM external_records WHERE id = ?", (record_id,)).fetchone()

            if row is None:
                raise LookupError("No such record.")

            if field_name not in CORRECTABLE[row["entity"]]:
                raise IntegrationRefused(f"{field_name} cannot be corrected (one of: {', '.join(CORRECTABLE[row['entity']])}).")

            connection.execute("INSERT INTO record_overrides (record_id, field, value, cleared, reason, actor, at) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (record_id, field_name, None if clear else dumps(value), 1 if clear else 0, reason, actor, at))
            received = loads(row["received"], {})
            data = {**received, **self.overrides_of(connection, record_id)}
            connection.execute("UPDATE external_records SET data = ?, updated_at = ? WHERE id = ?", (dumps(data), at, record_id))
            connection.execute("INSERT INTO external_record_versions (record_id, change, content_hash, kept_fields, actor, at) "
                               "VALUES (?, ?, ?, ?, ?, ?)",
                               (record_id, "override_cleared" if clear else "override", row["content_hash"],
                                dumps([field_name]), actor, at))

        return self.record(record_id)

    def records(self, integration_id, entity=None, search="", offset=0, limit=50):

        where, params = ["integration_id = ?"], [integration_id]

        if entity:
            where.append("entity = ?")
            params.append(entity)

        if search:
            where.append("(external_id LIKE ? OR data LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]

        condition = " WHERE " + " AND ".join(where)

        with closing(self.connect()) as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM external_records{condition}", params).fetchone()[0]
            rows = connection.execute(f"SELECT * FROM external_records{condition} ORDER BY entity, external_id LIMIT ? OFFSET ?",
                                      (*params, int(limit), int(offset))).fetchall()
            records = []
            for row in rows:
                record = self.view(row)
                record["corrected_fields"] = sorted(self.overrides_of(connection, row["id"]))
                records.append(record)

        return {"records": records, "total": total}

    @staticmethod
    def view(row):

        record = dict(row)
        record["data"] = loads(record["data"], {})
        record.pop("received", None)

        return record

    def record(self, record_id):

        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM external_records WHERE id = ?", (record_id,)).fetchone()

            if row is None:
                raise LookupError("No such record.")

            record = self.view(row)
            record["received"] = loads(row["received"], {})
            record["corrections"] = self.overrides_of(connection, record_id)
            record["corrected_fields"] = sorted(record["corrections"])
            record["versions"] = [{**dict(item), "received": loads(item["received"]), "kept_fields": loads(item["kept_fields"], [])}
                                  for item in connection.execute(
                                      "SELECT * FROM external_record_versions WHERE record_id = ? ORDER BY id DESC LIMIT 50",
                                      (record_id,))]
            record["override_history"] = [{**dict(item), "value": loads(item["value"]), "cleared": bool(item["cleared"])}
                                          for item in connection.execute(
                                              "SELECT * FROM record_overrides WHERE record_id = ? ORDER BY id DESC", (record_id,))]

        return record

    def counts(self, integration_id):

        with closing(self.connect()) as connection:
            return {row["entity"]: row["records"] for row in connection.execute(
                "SELECT entity, COUNT(*) AS records FROM external_records WHERE integration_id = ? GROUP BY entity",
                (integration_id,))}


# ============================================================
# THE SERVICE
# ============================================================

def sandbox_folder(environ, protected=(), production=False):
    """
    The sandbox folder, or (None, why not). It must be set explicitly
    (LOGIDOC_INTEGRATION_SANDBOX_DIR), absolute, and apart from the real data,
    index and backup folders; never in production.
    """

    raw = str(environ.get("LOGIDOC_INTEGRATION_SANDBOX_DIR") or "").strip()

    if production:
        return None, "The sandbox provider is not offered in production."
    if not raw:
        return None, "Set LOGIDOC_INTEGRATION_SANDBOX_DIR to a separate folder to use the sandbox TMS."

    folder = Path(raw)

    if not folder.is_absolute():
        return None, "LOGIDOC_INTEGRATION_SANDBOX_DIR must be an absolute path."

    resolved = folder.resolve()

    for place in protected:
        if place is None:
            continue
        other = Path(place).resolve()
        if resolved == other or other in resolved.parents or resolved in other.parents:
            return None, "LOGIDOC_INTEGRATION_SANDBOX_DIR must be apart from the data, index and backup folders."

    return resolved, None


class IntegrationService:
    """
    The configured integrations (integration store at path), their syncs and
    the imported records. Every change takes a Principal with
    integrations.manage.
    """

    def __init__(self, path, context, environ=None, feed_reader=None, providers=None, metrics=None, clock=utc_now,
                 max_pages=20, unavailable_reason=None):

        self.path = Path(path)
        self.context = context
        self.environ = os.environ if environ is None else environ
        self.feed_reader = feed_reader
        self.providers = providers if providers is not None else registered_providers()
        self.metrics = metrics
        self.clock = clock
        self.max_pages = max_pages
        self.unavailable_reason = unavailable_reason
        self.workers = []

    def store(self, create=False):

        return istore.open_store(self.path, create=create)

    # --------------------------------------------------------
    # Providers and configuration
    # --------------------------------------------------------

    def reason_of(self, provider):
        """Why a provider cannot be used here, or None."""

        if provider.sandbox and self.context.sandbox_dir is None:
            return self.unavailable_reason or "The sandbox folder is not configured."

        return provider.availability(self.context)

    def provider_list(self):

        result = []

        for key, provider in self.providers.items():
            reason = self.reason_of(provider)
            result.append({"key": key, "label": provider.label, "description": provider.description,
                           "entities": list(provider.entities), "accepts_audit_runs": provider.accepts_audit_runs,
                           "sandbox": provider.sandbox, "available": reason is None, "unavailable_reason": reason,
                           "default_credential_env": provider.default_credential_env})

        return result

    def provider_class(self, key):

        provider = self.providers.get(key)

        if provider is None:
            raise IntegrationRefused(f"Unknown provider {key!r}.")

        reason = self.reason_of(provider)

        if reason:
            raise IntegrationRefused(reason)

        return provider

    @staticmethod
    def check_interval(seconds):

        if not MIN_SYNC_INTERVAL <= int(seconds) <= MAX_SYNC_INTERVAL:
            raise IntegrationRefused(f"The sync interval must be {MIN_SYNC_INTERVAL} to {MAX_SYNC_INTERVAL} seconds.")

        return int(seconds)

    @staticmethod
    def check_credential_env(name):

        if name is None:
            return None

        if not CREDENTIAL_ENV.match(str(name)):
            raise IntegrationRefused("The credential must be named by an environment variable LOGIDOC_INTEGRATION_<NAME> "
                                     "(upper-case letters, digits and _): its value is read when the system is called, "
                                     "never stored.")

        return str(name)

    def view(self, row):

        integration = dict(row)
        integration["settings"] = loads(integration["settings"], {})
        integration["cursor"] = loads(integration["cursor"], {})
        integration["enabled"] = bool(integration["enabled"])
        integration["credential_configured"] = bool(integration["credential_env"]
                                                    and self.environ.get(integration["credential_env"]))
        integration["running"] = bool(integration["lease_until"] and parse_time(integration["lease_until"]) > self.clock())
        integration.pop("lease_until", None)

        return integration

    def change(self, connection, integration_id, actor, action, before=None, after=None):

        connection.execute("INSERT INTO integration_changes (integration_id, at, actor, action, before, after) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (integration_id, text_time(self.clock()), actor, action,
                            dumps(before) if before is not None else None, dumps(after) if after is not None else None))

    def create(self, principal, name, provider, settings=None, credential_env=None, sync_interval_seconds=3600, enabled=True):

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        provider_class = self.provider_class(provider)
        name = " ".join(str(name or "").split())

        if not 1 <= len(name) <= 100:
            raise IntegrationRefused("The name must be 1 to 100 characters.")

        checked = provider_class.check_settings(settings or {})
        credential_env = self.check_credential_env(credential_env or provider_class.default_credential_env)
        interval = self.check_interval(sync_interval_seconds)
        now = text_time(self.clock())
        store = self.store(create=True)

        with store.write() as connection:
            if connection.execute("SELECT 1 FROM integrations WHERE name = ?", (name,)).fetchone():
                raise IntegrationRefused(f"An integration named {name!r} exists already.")
            integration_id = connection.execute(
                "INSERT INTO integrations (name, provider, enabled, settings, credential_env, sync_interval_seconds, next_sync_at, "
                "cursor, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, provider, 1 if enabled else 0, dumps(checked), credential_env, interval, now, dumps({}),
                 principal.username, now, now)).lastrowid
            self.change(connection, integration_id, principal.username, "create",
                        after={"name": name, "provider": provider, "settings": checked, "credential_env": credential_env,
                               "sync_interval_seconds": interval, "enabled": bool(enabled)})

        return self.integration(integration_id)

    def row(self, connection, integration_id):

        row = connection.execute("SELECT * FROM integrations WHERE id = ?", (integration_id,)).fetchone()

        if row is None:
            raise LookupError("No such integration.")

        return row

    def update(self, principal, integration_id, name=None, enabled=None, settings=None, credential_env=None,
               sync_interval_seconds=None):

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        store = self.store()

        if store is None:
            raise LookupError("No such integration.")

        with store.write() as connection:
            row = self.row(connection, integration_id)
            changes = {}

            if name is not None:
                text = " ".join(str(name).split())
                if not 1 <= len(text) <= 100:
                    raise IntegrationRefused("The name must be 1 to 100 characters.")
                changes["name"] = text
            if enabled is not None:
                changes["enabled"] = 1 if enabled else 0
            if settings is not None:
                provider_class = self.providers.get(row["provider"])
                changes["settings"] = dumps(provider_class.check_settings(settings) if provider_class else settings)
            if credential_env is not None:
                changes["credential_env"] = self.check_credential_env(credential_env)
            if sync_interval_seconds is not None:
                changes["sync_interval_seconds"] = self.check_interval(sync_interval_seconds)

            if not changes:
                raise IntegrationRefused("Nothing to change.")

            before = {key: row[key] for key in changes}
            connection.execute(f"UPDATE integrations SET {', '.join(f'{key} = ?' for key in changes)}, updated_at = ? WHERE id = ?",
                               (*changes.values(), text_time(self.clock()), integration_id))
            action = ("enable" if enabled else "disable") if list(changes) == ["enabled"] else "update"
            self.change(connection, integration_id, principal.username, action, before=before, after=changes)

        return self.integration(integration_id)

    def integrations(self):

        store = self.store()

        if store is None:
            return []

        with store.read() as connection:
            rows = connection.execute("SELECT * FROM integrations ORDER BY name").fetchall()

        return [self.decorate(self.view(row)) for row in rows]

    def integration(self, integration_id):

        store = self.store()

        if store is None:
            raise LookupError("No such integration.")

        with store.read() as connection:
            row = self.row(connection, integration_id)

        return self.decorate(self.view(row))

    def decorate(self, integration):
        """Adds the record counts and the last run's figures."""

        store = self.store()

        with store.read() as connection:
            last = connection.execute("SELECT * FROM sync_runs WHERE integration_id = ? ORDER BY id DESC LIMIT 1",
                                      (integration["id"],)).fetchone()
            totals = connection.execute(
                "SELECT COALESCE(SUM(imported), 0) AS imported, COALESCE(SUM(updated), 0) AS updated, "
                "COALESCE(SUM(rejected), 0) AS rejected, COUNT(*) AS runs FROM sync_runs WHERE integration_id = ?",
                (integration["id"],)).fetchone()

        integration["last_run"] = self.run_view(last) if last else None
        integration["totals"] = dict(totals)

        try:
            records = self.records_store(integration, create=False)
            integration["records"] = records.counts(integration["id"]) if records else {}
        except IntegrationRefused:
            integration["records"] = {}

        return integration

    def changes(self, integration_id, limit=50):

        store = self.store()

        if store is None:
            return []

        with store.read() as connection:
            return [{**dict(row), "before": loads(row["before"]), "after": loads(row["after"])} for row in connection.execute(
                "SELECT * FROM integration_changes WHERE integration_id = ? ORDER BY id DESC LIMIT ?", (integration_id, limit))]

    # --------------------------------------------------------
    # Providers at work
    # --------------------------------------------------------

    def records_store(self, integration, create=True):
        """
        Where this integration's imported records live: the sandbox folder for
        a sandbox provider (never the real data folder), the integration store
        for any other.
        """

        provider_class = self.providers.get(integration["provider"])

        if provider_class is not None and provider_class.sandbox:
            if self.context.sandbox_dir is None:
                raise IntegrationRefused(self.unavailable_reason or "The sandbox folder is not configured.")
            path = Path(self.context.sandbox_dir) / "sandbox_imports.sqlite3"
        else:
            path = self.path

        if not create and not path.is_file():
            return None

        return ExternalRecordStore(path)

    def provider_for(self, integration):

        provider_class = self.provider_class(integration["provider"])
        name = integration["credential_env"]
        credential = self.environ.get(name) if name else None

        return provider_class(integration["settings"], credential, self.context)

    def test_connection(self, principal, integration_id):

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        integration = self.integration(integration_id)
        result = self.check(integration)

        log.info("Integration %s connection test by %s: %s.", integration_id, principal.username,
                 "ok" if result.ok else result.category, extra={"operation": "integration_test", "outcome": result.category or "ok"})

        return result

    def check(self, integration):
        """Calls the provider's connection test and stores the status."""

        started = time.monotonic()

        try:
            self.provider_for(integration).test_connection()
            result = ConnectionResult(True, None, "Connected.", int((time.monotonic() - started) * 1000))
        except IntegrationRefused as error:
            result = ConnectionResult(False, "configuration", str(error), 0)
        except Exception as error:  # noqa: BLE001 - normalized and recorded
            failure = normalize(error)
            result = ConnectionResult(False, failure.category, redact(str(failure), limit=300),
                                      int((time.monotonic() - started) * 1000))

        status = "connected" if result.ok else ("auth_failed" if result.category == "authentication_failed" else "error")
        now = text_time(self.clock())

        with self.store().write() as connection:
            connection.execute("UPDATE integrations SET connection_status = ?, connection_checked_at = ?, connection_error = ?, "
                               "updated_at = ? WHERE id = ?",
                               (status, now, None if result.ok else f"{result.category}: {result.detail}", now, integration["id"]))

        return result

    # --------------------------------------------------------
    # Syncs
    # --------------------------------------------------------

    def claim(self, integration_id, trigger, requested_by):
        """The integration's lease and a new run row. Raises SyncBusy while another sync runs."""

        now = self.clock()

        with self.store().write() as connection:
            row = self.row(connection, integration_id)
            lease = parse_time(row["lease_until"])

            if lease is not None and lease > now:
                raise SyncBusy("A sync of this integration is running.")

            connection.execute("UPDATE integrations SET lease_until = ? WHERE id = ?", (later(now, 900), integration_id))
            run_id = connection.execute(
                "INSERT INTO sync_runs (integration_id, trigger, requested_by, started_at, status, cursor_before) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (integration_id, trigger, requested_by, text_time(now), row["cursor"])).lastrowid

        return run_id

    def recover(self):
        """Runs left 'running' by a stopped process: interrupted (the next sync resumes from the saved cursor)."""

        store = self.store()

        if store is None:
            return 0

        now = text_time(self.clock())

        with store.write() as connection:
            stale = [row["id"] for row in connection.execute(
                "SELECT id FROM integrations WHERE lease_until IS NOT NULL AND lease_until < ?", (now,))]
            for integration_id in stale:
                connection.execute("UPDATE sync_runs SET status = 'interrupted', finished_at = ?, error_category = 'interrupted', "
                                   "error_message = 'The process stopped during the sync.' WHERE integration_id = ? AND status = 'running'",
                                   (now, integration_id))
                connection.execute("UPDATE integrations SET lease_until = NULL, next_sync_at = ? WHERE id = ?", (now, integration_id))

        return len(stale)

    def sync_now(self, principal, integration_id):
        """An administrator's sync, in its own thread; the lease is claimed here (SyncBusy at once). Returns the run id."""

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        integration = self.integration(integration_id)
        self.provider_class(integration["provider"])
        run_id = self.claim(integration_id, "manual", principal.username)
        worker = threading.Thread(target=self.execute, args=(integration_id, run_id), name=f"logidoc-sync-{integration_id}",
                                  daemon=True)
        self.workers = [item for item in self.workers if item.is_alive()] + [worker]
        worker.start()

        return run_id

    def run_due(self, should_stop=lambda: False):
        """The scheduled job: every enabled integration whose sync is due, one after another."""

        store = self.store()

        if store is None:
            return {"synced": 0}

        self.recover()
        now = text_time(self.clock())

        with store.read() as connection:
            due = [row["id"] for row in connection.execute(
                "SELECT id FROM integrations WHERE enabled = 1 AND next_sync_at <= ? AND (lease_until IS NULL OR lease_until < ?) "
                "ORDER BY next_sync_at", (now, now))]

        results = {}

        for integration_id in due:
            if should_stop():
                break
            try:
                run_id = self.claim(integration_id, "schedule", None)
            except SyncBusy:
                continue
            results[integration_id] = self.execute(integration_id, run_id, should_stop)["status"]

        return {"synced": len(results), "outcomes": results}

    def execute(self, integration_id, run_id, should_stop=lambda: False):
        """Runs a claimed sync (SyncEngine) and records its outcome. Returns the run."""

        integration = self.integration(integration_id)
        engine = SyncEngine(self, integration, run_id, should_stop)
        engine.run()

        return self.run(run_id)

    def runs(self, integration_id, offset=0, limit=50):

        store = self.store()

        if store is None:
            return {"runs": [], "total": 0}

        with store.read() as connection:
            total = connection.execute("SELECT COUNT(*) FROM sync_runs WHERE integration_id = ?", (integration_id,)).fetchone()[0]
            rows = connection.execute("SELECT * FROM sync_runs WHERE integration_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                                      (integration_id, int(limit), int(offset))).fetchall()

        return {"runs": [self.run_view(row) for row in rows], "total": total}

    @staticmethod
    def run_view(row):

        run = dict(row)
        run["cursor_before"] = loads(run["cursor_before"], {})
        run["cursor_after"] = loads(run["cursor_after"], {})

        return run

    def run(self, run_id):

        store = self.store()

        if store is None:
            raise LookupError("No such sync run.")

        with store.read() as connection:
            row = connection.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()

            if row is None:
                raise LookupError("No such sync run.")

            rejections = [dict(item) for item in connection.execute(
                "SELECT * FROM sync_rejections WHERE run_id = ? ORDER BY id LIMIT 500", (run_id,))]

        return {**self.run_view(row), "rejections": rejections}

    # --------------------------------------------------------
    # Imported records
    # --------------------------------------------------------

    def records(self, integration_id, entity=None, search="", offset=0, limit=50):

        integration = self.integration(integration_id)
        store = self.records_store(integration, create=False)

        if store is None:
            return {"records": [], "total": 0}

        return store.records(integration_id, entity, search, offset, limit)

    def record(self, integration_id, record_id):

        integration = self.integration(integration_id)
        store = self.records_store(integration, create=False)

        if store is None:
            raise LookupError("No such record.")

        record = store.record(record_id)

        if record["integration_id"] != integration_id:
            raise LookupError("No such record.")

        return record

    def correct(self, principal, integration_id, record_id, field_name, value, reason, clear=False):
        """An administrator's correction of an imported field: later imports never overwrite it."""

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        reason = " ".join(str(reason or "").split())

        if len(reason) < 10:
            raise IntegrationRefused("Say why the value is corrected (at least 10 characters).")

        self.record(integration_id, record_id)
        store = self.records_store(self.integration(integration_id))

        return store.override(principal.username, record_id, field_name, value, reason, text_time(self.clock()), clear)

    def simulate_changes(self, principal, integration_id, count=3):
        """Sandbox only: the simulated TMS changes some shipments, as a partner's system would."""

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        integration = self.integration(integration_id)
        provider_class = self.provider_class(integration["provider"])

        if not provider_class.sandbox:
            raise IntegrationRefused("Only a sandbox integration can simulate changes.")

        return self.provider_for(integration).simulate_changes(count)

    def health(self):
        """The scheduled health check: tests every enabled integration's connection."""

        result = []

        for integration in self.integrations():
            if not integration["enabled"]:
                continue
            outcome = self.check(integration)
            result.append({"integration_id": integration["id"], "ok": outcome.ok, "category": outcome.category,
                           "consecutive_failures": integration["consecutive_failures"]})

        return result

    def overdue(self):
        """Enabled integrations whose sync is late by more than its interval (at least 5 minutes)."""

        now = self.clock()
        late = []

        for integration in self.integrations():
            due = parse_time(integration["next_sync_at"])
            if integration["enabled"] and due and (now - due).total_seconds() > max(300, integration["sync_interval_seconds"]):
                late.append(integration)

        return late


class SyncEngine:
    """One sync run of one integration (see the module docstring)."""

    def __init__(self, service, integration, run_id, should_stop):

        self.service = service
        self.integration = integration
        self.run_id = run_id
        self.should_stop = should_stop
        self.counts = {name: 0 for name in ("fetched", "imported", "updated", "unchanged", "duplicates", "rejected",
                                             "conflicts", "pages", "retries")}
        self.rejections = []
        self.cursor = dict(integration["cursor"] or {})
        self.more = False

    def now(self):

        return text_time(self.service.clock())

    def fetch(self, provider, entity, cursor, page_size):
        """One page, retried on a retryable failure (back-off; Retry-After honoured up to a minute)."""

        for attempt in range(PAGE_RETRIES + 1):
            try:
                return provider.fetch(entity, cursor, page_size)
            except Exception as error:  # noqa: BLE001 - normalized below
                failure = normalize(error)
                if not failure.retryable or attempt == PAGE_RETRIES:
                    raise failure from None
                self.counts["retries"] += 1
                wait = min(failure.retry_after if failure.retry_after is not None else 2 ** attempt, MAX_RETRY_AFTER)
                self.service.context.sleep(wait)

    def reject(self, entity, external_id, reason):

        self.counts["rejected"] += 1
        self.rejections.append((entity, external_id, reason[:300]))

    def save_progress(self):

        with self.service.store().write() as connection:
            connection.execute("UPDATE integrations SET cursor = ? WHERE id = ?", (dumps(self.cursor), self.integration["id"]))
            for entity, external_id, reason in self.rejections:
                connection.execute("INSERT INTO sync_rejections (run_id, entity, external_id, reason, at) VALUES (?, ?, ?, ?, ?)",
                                   (self.run_id, entity, external_id, reason, self.now()))
            connection.execute(
                "UPDATE sync_runs SET cursor_after = ?, fetched = ?, imported = ?, updated = ?, unchanged = ?, duplicates = ?, "
                "rejected = ?, conflicts = ?, pages = ?, retries = ? WHERE id = ?",
                (dumps(self.cursor), *(self.counts[name] for name in ("fetched", "imported", "updated", "unchanged", "duplicates",
                                                                      "rejected", "conflicts", "pages", "retries")), self.run_id))

        self.rejections = []

    def sync_entity(self, provider, records, entity, page_size):

        seen = {}
        pages = 0

        while not self.should_stop():

            page = self.fetch(provider, entity, self.cursor.get(entity), page_size)
            self.counts["pages"] += 1
            pages += 1
            mapped = {}

            for raw in page.records:
                self.counts["fetched"] += 1
                external = raw.get("id") if isinstance(raw, dict) else None
                try:
                    record = provider.map(entity, raw)
                except ValueError as error:
                    self.reject(entity, str(external)[:100] if external is not None else None, str(error))
                    continue
                values = asdict(record)
                earlier = mapped.get(values["external_id"])
                if earlier is not None:
                    # The same record twice in one page: kept once, the later change winning
                    self.counts["duplicates"] += 1
                    if values["source_updated_at"] > earlier["source_updated_at"]:
                        mapped[values["external_id"]] = values
                    continue
                if seen.get(values["external_id"]) == content_hash(values):
                    # An earlier page of this run brought the same content: a duplicate, not a change
                    self.counts["duplicates"] += 1
                    continue
                mapped[values["external_id"]] = values

            for values in mapped.values():
                seen[values["external_id"]] = content_hash(values)
                change, kept = records.apply(self.integration["id"], self.integration["provider"], self.run_id, entity, values,
                                             self.now())
                self.counts[{"created": "imported", "updated": "updated", "unchanged": "unchanged"}[change]] += 1
                self.counts["conflicts"] += len(kept)

            if page.next_cursor is not None:
                self.cursor[entity] = page.next_cursor

            # After the page's records: a crash re-reads this page, which changes nothing
            self.save_progress()

            if not page.has_more:
                return

            if pages >= self.service.max_pages:
                self.more = True
                return

        self.more = True

    def send_audit_runs(self, provider):

        if not provider.accepts_audit_runs or self.service.feed_reader is None:
            return

        after = int((self.cursor.get("audit_runs") or {}).get("after") or 0)

        while not self.should_stop():
            feed = self.service.feed_reader(after, 50)
            if not feed["runs"]:
                return
            accepted = provider.send_audit_runs(feed["runs"])
            self.counts.setdefault("audit_runs_sent", 0)
            self.counts["audit_runs_sent"] += accepted
            after = feed["next_after"]
            self.cursor["audit_runs"] = {"after": after}
            self.save_progress()
            if not feed["has_more"]:
                return

    def run(self):

        integration = self.integration
        error = None

        try:
            provider = self.service.provider_for(integration)
            records = self.service.records_store(integration)
            page_size = int(integration["settings"].get("page_size") or 50)

            for entity in provider.entities:
                self.sync_entity(provider, records, entity, page_size)

            self.send_audit_runs(provider)
        except IntegrationRefused as caught:
            error = ConfigurationError(str(caught))
        except Exception as caught:  # noqa: BLE001 - normalized and recorded
            error = normalize(caught)
            if not isinstance(caught, IntegrationError):
                log.exception("Sync %s of integration %s failed unexpectedly.", self.run_id, integration["id"])

        self.finish(error)

    def finish(self, error):

        now = self.service.clock()
        integration = self.integration
        progressed = self.counts["pages"] > 0

        if error is None:
            status = "partial" if self.counts["rejected"] else "succeeded"
        else:
            status = "partial" if progressed else "failed"

        failures = 0 if error is None else integration["consecutive_failures"] + 1
        interval = integration["sync_interval_seconds"]
        # More pages waiting: continue soon; a failure: back off within the interval
        wait = 60 if (error is None and self.more) else interval if error is None else min(interval, 60 * 2 ** min(failures - 1, 6))
        connection_status = ("connected" if error is None else
                             "auth_failed" if isinstance(error, AuthenticationFailed) else "error")

        with self.service.store().write() as connection:
            for entity, external_id, reason in self.rejections:
                connection.execute("INSERT INTO sync_rejections (run_id, entity, external_id, reason, at) VALUES (?, ?, ?, ?, ?)",
                                   (self.run_id, entity, external_id, reason, text_time(now)))
            self.rejections = []
            connection.execute(
                "UPDATE sync_runs SET status = ?, finished_at = ?, cursor_after = ?, fetched = ?, imported = ?, updated = ?, "
                "unchanged = ?, duplicates = ?, rejected = ?, conflicts = ?, pages = ?, retries = ?, error_category = ?, "
                "error_message = ? WHERE id = ?",
                (status, text_time(now), dumps(self.cursor),
                 *(self.counts[name] for name in ("fetched", "imported", "updated", "unchanged", "duplicates", "rejected",
                                                  "conflicts", "pages", "retries")),
                 error.category if error else None, redact(str(error), limit=300) if error else None, self.run_id))
            connection.execute(
                "UPDATE integrations SET lease_until = NULL, cursor = ?, next_sync_at = ?, consecutive_failures = ?, "
                "last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END, "
                "last_failure_at = CASE WHEN ? THEN ? ELSE last_failure_at END, last_error = ?, connection_status = ?, "
                "connection_checked_at = ?, connection_error = ?, updated_at = ? WHERE id = ?",
                (dumps(self.cursor), later(now, wait), failures, error is None, text_time(now), error is not None, text_time(now),
                 f"{error.category}: {redact(str(error), limit=200)}" if error else None, connection_status, text_time(now),
                 f"{error.category}: {redact(str(error), limit=200)}" if error else None, text_time(now), integration["id"]))

        if self.service.metrics is not None:
            self.service.metrics.inc("logidoc_sync_runs_total", {"provider": integration["provider"], "outcome": status})
            for name in ("imported", "updated", "rejected", "duplicates", "conflicts"):
                if self.counts[name]:
                    self.service.metrics.inc("logidoc_sync_records_total", {"provider": integration["provider"], "change": name},
                                             self.counts[name])

        log.info("Sync %s of integration %s: %s (%s fetched, %s imported, %s updated, %s rejected).", self.run_id,
                 integration["id"], status, self.counts["fetched"], self.counts["imported"], self.counts["updated"],
                 self.counts["rejected"], extra={"operation": "integration_sync", "outcome": status})
