"""
Webhooks: signed notifications of committed changes to external systems
(docs/WEBHOOKS_AND_DELIVERY.md).

Where events come from (only what is committed)

    The application already keeps three append-only logs. The collector reads
    each one after its cursor, and only committed rows are visible to a
    reader, so a change that was rolled back can never become an event:

    audit_runs      recorded audit runs (src/audit_history.py feed): every
                    shipment whose run wrote a new snapshot with a freight
                    audit gives freight_audit.completed, and
                    freight_audit.discrepancy_detected when its outcome is
                    DISCREPANCY
    review_events   the exception review log (src/review_store.py):
                    detected -> exception.created; any change of status
                    (people's actions and the audit reopening one) ->
                    exception.status_changed
    carrier_events  the carrier registry's log (src/carrier_registry.py):
                    create -> carrier.created, update -> carrier.updated,
                    approved_link -> carrier_name_link.approved

    The event id is derived from (source, source row, type), and the outbox
    holds each (source, source row, type) once, so reading a log twice (a
    retry, a restart) can never create a second event. The cursor moves in
    the same transaction that stores the events.

    Collection starts when the first endpoint is registered: the cursors are
    set to the logs' current ends, so history from before is not replayed.

Delivery

    Every event is delivered to each active endpoint subscribed to its type
    (one delivery row per event and endpoint: never two). A delivery is
    POSTed as JSON:

        {"id": "evt_...", "type": "...", "api_version": "1",
         "occurred_at": "...", "created_at": "...", "data": {...}}

    with the headers

        LogiDoc-Event-Id, Idempotency-Key (the event id again), LogiDoc-Event-Type,
        LogiDoc-Delivery-Id, LogiDoc-Delivery-Attempt, LogiDoc-Timestamp (Unix seconds),
        LogiDoc-Signature: v1=<hex HMAC-SHA256 of "<timestamp>.<event id>.<body>">

    A retry sends the same event: the same event id and Idempotency-Key and the
    same body bytes (rebuilt from the stored, append-only event). Only the
    attempt number, the timestamp and so the signature change.

    During a secret rotation's grace period the header carries a second v1=
    signature made with the previous secret. verify_signature() is the
    receiver's check (tolerance 5 minutes); receivers must also remember the
    event ids they processed (ReplayGuard): delivery is at least once.

    2xx is success. 408, 425, 429 and 5xx, timeouts, connection, DNS and TLS
    failures are retried with exponential back-off and jitter
    (LOGIDOC_WEBHOOK_MAX_ATTEMPTS attempts, 30 s doubling up to 6 hours;
    Retry-After is honoured within that). Redirects are not followed and
    other 4xx answers are not retried: the delivery fails at once, and an
    administrator can retry it after fixing the receiver.

Secrets

    A signing secret is shown once, when the endpoint is created or its secret
    rotated. It is never stored: the store keeps a random salt and a version,
    and the secret is HMAC-SHA256(LOGIDOC_WEBHOOK_SECRET_KEY, endpoint id,
    version, salt). Without that key nothing can be signed, so no endpoint can
    be created and nothing is delivered (the screens say why).

Destinations are checked by src/url_safety.py when saved and again before
every attempt; the connection goes to the address that was checked.
"""

import base64
import hashlib
import hmac
import http.client
import json
import logging
import random
import secrets as secrets_module
import socket
import ssl
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import authz
import integration_store as istore
import url_safety
from integration_store import dumps, later, loads, parse_time, text_time, utc_now

log = logging.getLogger("logidoc.webhooks")

API_VERSION = "1"

EVENT_TYPES = {
    "freight_audit.completed": "A recorded audit run wrote a shipment's new freight audit result.",
    "freight_audit.discrepancy_detected": "A recorded freight audit found the invoice above or below its rate confirmation.",
    "exception.created": "The audit detected a new exception (recorded as open).",
    "exception.status_changed": "An exception's review status changed (a person's action, or the audit reopening it).",
    "carrier.created": "A carrier was registered in the carrier master data.",
    "carrier.updated": "A registered carrier's master data changed.",
    "carrier_name_link.approved": "A second administrator approved linking a document carrier name to a registered carrier.",
}

TEST_EVENT = "webhook.test"

SIGNATURE_HEADER = "LogiDoc-Signature"
TIMESTAMP_HEADER = "LogiDoc-Timestamp"
EVENT_HEADER = "LogiDoc-Event-Id"
# The event id again, under the conventional name receivers deduplicate on: the same on every attempt
IDEMPOTENCY_HEADER = "Idempotency-Key"

TOLERANCE_SECONDS = 300
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MIN_KEY_LENGTH = 32

# Retried: the receiver or the network may recover
RETRYABLE_STATUS = (408, 425, 429)
RETRYABLE_ERRORS = ("timeout", "connection_error", "dns_error", "tls_error", "http_5xx", "http_408", "http_425",
                    "http_429", "interrupted")

# Keys whose values are masked in a payload preview
SENSITIVE_KEYS = ("contact_email", "contact_phone", "secret", "token", "password", "authorization", "credential")


class WebhookError(ValueError):
    """A request that is refused (the message says why)."""


class DeliveryDisabled(WebhookError):
    """Nothing can be signed: LOGIDOC_WEBHOOK_SECRET_KEY is missing or too short."""


class SignatureError(ValueError):
    """A receiver's verification failed."""


# ============================================================
# SETTINGS
# ============================================================

def whole(environ, name, default, low, high):

    try:
        value = int(str(environ.get(name) or "").strip() or default)
    except ValueError:
        return default

    return min(max(value, low), high)


@dataclass
class WebhookSettings:

    secret_key: bytes | None = None
    policy: url_safety.Policy = field(default_factory=url_safety.Policy)
    timeout_seconds: int = 10
    max_attempts: int = 8
    base_delay_seconds: int = 30
    max_delay_seconds: int = 6 * 3600
    rotation_grace_hours: int = 24
    production: bool = False

    @staticmethod
    def from_env(environ, production=False):

        key = str(environ.get("LOGIDOC_WEBHOOK_SECRET_KEY") or "").strip()

        return WebhookSettings(
            secret_key=key.encode("utf-8") if len(key) >= MIN_KEY_LENGTH else None,
            policy=url_safety.Policy.from_env(environ, production),
            timeout_seconds=whole(environ, "LOGIDOC_WEBHOOK_TIMEOUT_SECONDS", 10, 1, 60),
            max_attempts=whole(environ, "LOGIDOC_WEBHOOK_MAX_ATTEMPTS", 8, 1, 30),
            rotation_grace_hours=whole(environ, "LOGIDOC_WEBHOOK_ROTATION_GRACE_HOURS", 24, 0, 720),
            production=production,
        )

    @property
    def delivery_enabled(self):

        return self.secret_key is not None

    def problems(self):
        """Why delivery is off, in words for the screen (never a value)."""

        if self.secret_key is None:
            return [f"LOGIDOC_WEBHOOK_SECRET_KEY is not set (at least {MIN_KEY_LENGTH} characters): nothing can be "
                    "signed, so no endpoint can be registered and nothing is delivered."]

        return []

    def require_key(self):

        if self.secret_key is None:
            raise DeliveryDisabled(self.problems()[0])

        return self.secret_key


# ============================================================
# SECRETS AND SIGNATURES
# ============================================================

def derive_secret(key, endpoint_id, version, salt):
    """An endpoint's signing secret, recomputed from the server key (never stored)."""

    digest = hmac.new(key, f"logidoc-webhook|{endpoint_id}|{version}|{salt}".encode("utf-8"), hashlib.sha256).digest()

    return "whsec_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def sign(secret, timestamp, event_id, body):
    """The v1 signature: hex HMAC-SHA256 of "<timestamp>.<event id>.<body>" with the secret."""

    message = f"{int(timestamp)}.{event_id}.".encode("utf-8") + body

    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signature_header(secrets, timestamp, event_id, body):

    return ", ".join(f"v1={sign(secret, timestamp, event_id, body)}" for secret in secrets)


def header(headers, name):

    wanted = name.lower()

    return next((value for key, value in headers.items() if key.lower() == wanted), None)


def verify_signature(secret, headers, body, tolerance=TOLERANCE_SECONDS, now=None):
    """
    The receiver's check. secret: the endpoint's secret (or a list, during a
    rotation). Raises SignatureError when a header is missing or malformed, the
    timestamp is outside the tolerance (a replay of an old request), or no
    signature matches. Returns the event id. A receiver must still refuse an
    event id it has already processed (ReplayGuard).
    """

    timestamp_text = header(headers, TIMESTAMP_HEADER)
    signature_text = header(headers, SIGNATURE_HEADER)
    event_id = header(headers, EVENT_HEADER)

    if not timestamp_text or not signature_text or not event_id:
        raise SignatureError("A signature header is missing.")

    try:
        timestamp = int(timestamp_text)
    except ValueError:
        raise SignatureError("The timestamp is not a number.") from None

    now = time.time() if now is None else now

    if abs(now - timestamp) > tolerance:
        raise SignatureError("The timestamp is outside the tolerance: a delayed or replayed request.")

    offered = [part.strip()[3:] for part in signature_text.split(",") if part.strip().startswith("v1=")]

    if not offered:
        raise SignatureError("No v1 signature.")

    for candidate in ([secret] if isinstance(secret, str) else list(secret)):
        expected = sign(candidate, timestamp, event_id, body)
        if any(hmac.compare_digest(expected, value) for value in offered):
            return event_id

    raise SignatureError("No signature matches.")


class ReplayGuard:
    """
    A receiver's memory of processed event ids (within the signature
    tolerance and some margin): seen() is True for a repeat. A production
    receiver keeps this in its own database, keyed by the event id.
    """

    def __init__(self, ttl_seconds=2 * TOLERANCE_SECONDS, clock=time.time):

        self.ttl = ttl_seconds
        self.clock = clock
        self.ids = {}

    def seen(self, event_id):

        now = self.clock()
        self.ids = {key: at for key, at in self.ids.items() if now - at <= self.ttl}

        if event_id in self.ids:
            return True

        self.ids[event_id] = now

        return False


def event_id_of(source, source_ref, event_type):
    """Deterministic: the same committed change always gets the same id."""

    return "evt_" + hashlib.sha256(f"{source}|{source_ref}|{event_type}".encode("utf-8")).hexdigest()[:32]


def envelope(event):

    return {"id": event["id"], "type": event["event_type"], "api_version": API_VERSION,
            "occurred_at": event["occurred_at"], "created_at": event["created_at"], "data": loads(event["payload"], {})}


def body_of(event):

    return json.dumps(envelope(event), separators=(",", ":"), sort_keys=True).encode("utf-8")


def masked(value, key=""):
    """A payload preview: values under sensitive keys replaced, recursively."""

    if isinstance(value, dict):
        return {k: masked(v, k) for k, v in value.items()}

    if isinstance(value, list):
        return [masked(item, key) for item in value]

    if value is not None and any(word in key.lower() for word in SENSITIVE_KEYS):
        return "[redacted]"

    return value


# ============================================================
# EVENT SOURCES (committed logs only)
# ============================================================

class EventSources:
    """
    Reads the application's append-only logs. Nothing here writes to them, and
    a store that does not exist is not created (its position is 0).
    """

    NAMES = ("audit_runs", "review_events", "carrier_events")

    def __init__(self, locations, policy=None):

        self.locations = locations
        self.policy = policy

    def history(self):

        import audit_history

        path = Path(self.locations.history_db)

        return audit_history.HistoryStore(path) if path.is_file() else None

    def reviews(self):

        import review_store

        path = Path(self.locations.review_db)

        return review_store.ReviewStore(path, self.policy) if path.is_file() else None

    def carriers(self):

        import carrier_registry

        path = Path(self.locations.carrier_registry_path())

        return carrier_registry.CarrierRegistry(path) if path.is_file() else None

    def position(self, source):
        """The log's current end: a new reader starts here."""

        store = {"audit_runs": self.history, "review_events": self.reviews, "carrier_events": self.carriers}[source]()

        if store is None:
            return 0

        return {"audit_runs": lambda: store.last_run_id(), "review_events": lambda: store.last_event_id(),
                "carrier_events": lambda: store.last_event_id()}[source]()

    def read(self, source, after, limit):
        """[(position, [(event_type, source_ref, occurred_at, data)])], oldest first."""

        return {"audit_runs": self.audit_runs, "review_events": self.review_events,
                "carrier_events": self.carrier_events}[source](after, limit)

    def audit_runs(self, after, limit):

        store = self.history()

        if store is None:
            return []

        result = []

        for run in store.feed(after=after, limit=max(1, min(limit, 50)))["runs"]:
            events = []
            for item in run["shipments"]:
                if not item["changed"] or not item["freight_outcome"]:
                    continue
                data = {
                    "run_id": run["run_id"], "run_label": run["label"], "recorded_at": run["created_at"],
                    "shipment_id": item["shipment_id"], "shipment_key": item["shipment_key"],
                    "snapshot_id": item["snapshot_id"], "outcome": item["freight_outcome"], "carrier": item["carrier"],
                    "currency": item["currency"], "invoice_total": item["invoice_total"],
                    "discrepancy_count": item["discrepancy_count"], "review_count": item["review_count"],
                    "final_status": item["final_status"],
                }
                reference = f"snapshot:{item['snapshot_id']}"
                events.append(("freight_audit.completed", reference, run["created_at"], data))
                if item["freight_outcome"] == "DISCREPANCY":
                    events.append(("freight_audit.discrepancy_detected", reference, run["created_at"], data))
            result.append((run["run_id"], events))

        return result

    def review_events(self, after, limit):

        store = self.reviews()

        if store is None:
            return []

        result = []

        for row in store.events_after(after, limit):
            base = {"exception_key": row["exception_key"], "shipment_id": row["shipment_id"],
                    "shipment_key": row["shipment_key"], "category": row["category"]}
            events = []
            if row.get("event_type") == "detected":
                events.append(("exception.created", f"review_event:{row['id']}", row["created_at"],
                               {**base, "status": row["to_status"], "detected_at": row["created_at"]}))
            elif row["from_status"] is not None and row["from_status"] != row["to_status"]:
                events.append(("exception.status_changed", f"review_event:{row['id']}", row["created_at"],
                               {**base, "from_status": row["from_status"], "to_status": row["to_status"],
                                "action": row.get("event_type"), "actor": row.get("actor") or row.get("reviewer"),
                                "actor_role": row.get("actor_role"), "changed_at": row["created_at"]}))
            result.append((row["id"], events))

        return result

    def carrier_events(self, after, limit):

        store = self.carriers()

        if store is None:
            return []

        result = []

        for row in store.events_after(after, limit):
            events = []
            reference = f"carrier_event:{row['id']}"
            if row["action"] == "create":
                events.append(("carrier.created", reference, row["at"],
                               {"carrier_id": row["carrier_id"], "carrier": row["after"], "actor": row["actor"]}))
            elif row["action"] == "update":
                changes = {name: {"before": (row["before"] or {}).get(name), "after": value}
                           for name, value in (row["after"] or {}).items()}
                events.append(("carrier.updated", reference, row["at"],
                               {"carrier_id": row["carrier_id"], "changes": changes, "actor": row["actor"]}))
            elif row["action"] == "approved_link":
                after_values = row["after"] or {}
                events.append(("carrier_name_link.approved", reference, row["at"],
                               {"request_id": row["request_id"], "document_name": after_values.get("name"),
                                "carrier_id": after_values.get("carrier_id"),
                                "previous_carrier_id": (row["before"] or {}).get("carrier_id"),
                                "approved_by": row["actor"], "approved_at": row["at"]}))
            result.append((row["id"], events))

        return result


# ============================================================
# TRANSPORT (pinned to the checked address)
# ============================================================

class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a fixed address, with the host name for SNI and certificate checks."""

    def __init__(self, host, address, port, timeout, context):

        super().__init__(host, port, timeout=timeout, context=context)
        self.pinned = address

    def connect(self):

        sock = socket.create_connection((self.pinned, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PinnedHTTPConnection(http.client.HTTPConnection):

    def __init__(self, host, address, port, timeout):

        super().__init__(host, port, timeout=timeout)
        self.pinned = address

    def connect(self):

        self.sock = socket.create_connection((self.pinned, self.port), self.timeout)


def http_transport(destination, headers, body, timeout):
    """POSTs body; returns (status, {retry-after}). Redirects are not followed; at most 64 KB of the answer is read."""

    if destination.scheme == "https":
        connection = PinnedHTTPSConnection(destination.host, destination.address, destination.port, timeout,
                                           ssl.create_default_context())
    else:
        connection = PinnedHTTPConnection(destination.host, destination.address, destination.port, timeout)

    try:
        connection.request("POST", destination.path, body=body, headers=headers)
        response = connection.getresponse()
        response.read(MAX_RESPONSE_BYTES)
        return response.status, {"retry-after": response.getheader("Retry-After")}
    finally:
        connection.close()


def classify_error(error):
    """(category, retryable, detail) of a failed attempt: words only, never the URL or a secret."""

    if isinstance(error, url_safety.UnsafeDestination):
        return "destination_refused", False, str(error)
    if isinstance(error, socket.gaierror):
        return "dns_error", True, "The host name could not be resolved."
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "timeout", True, "The receiver did not answer in time."
    if isinstance(error, ssl.SSLCertVerificationError):
        return "tls_error", True, "The receiver's certificate could not be verified."
    if isinstance(error, ssl.SSLError):
        return "tls_error", True, "The TLS handshake failed."
    if isinstance(error, (ConnectionError, OSError, http.client.HTTPException)):
        return "connection_error", True, "The connection failed or was closed."

    return "unexpected_error", True, "The attempt failed unexpectedly."


def classify_status(status):

    if 200 <= status < 300:
        return None, None, "Accepted."
    if 300 <= status < 400:
        return "redirect_not_followed", False, f"The receiver answered {status} (a redirect): redirects are not followed."
    if status in RETRYABLE_STATUS:
        return f"http_{status}", True, f"The receiver answered {status}."
    if status >= 500:
        return "http_5xx", True, f"The receiver answered {status}."

    return "http_4xx", False, f"The receiver answered {status}: not retried (the request itself was refused)."


# ============================================================
# THE SERVICE
# ============================================================

class WebhookService:
    """
    Endpoints, the event outbox and deliveries in the integration store at
    path. Every change takes a Principal with integrations.manage.
    """

    def __init__(self, path, settings, sources=None, transport=http_transport, resolver=socket.getaddrinfo,
                 clock=utc_now, metrics=None, jitter=random.random):

        self.path = Path(path)
        self.settings = settings
        self.sources = sources
        self.transport = transport
        self.resolver = resolver
        self.clock = clock
        self.metrics = metrics
        self.jitter = jitter

    def store(self, create=False):

        return istore.open_store(self.path, create=create)

    # --------------------------------------------------------
    # Endpoints
    # --------------------------------------------------------

    @staticmethod
    def check_types(event_types):

        types = sorted(set(event_types or ()))

        if not types:
            raise WebhookError("Choose at least one event type.")

        unknown = [name for name in types if name not in EVENT_TYPES]

        if unknown:
            raise WebhookError(f"Unknown event type: {', '.join(unknown)}.")

        return types

    @staticmethod
    def check_name(name):

        text = " ".join(str(name or "").split())

        if not 1 <= len(text) <= 100:
            raise WebhookError("The name must be 1 to 100 characters.")

        return text

    def check_destination(self, url):

        try:
            url_safety.check_url(url, self.settings.policy)
        except url_safety.UnsafeDestination as error:
            raise WebhookError(f"The destination is refused: {error}") from None

        return str(url).strip()

    @staticmethod
    def public(row):
        """An endpoint as the API shows it: never the salt, never a secret."""

        endpoint = dict(row)
        endpoint["url"] = display_url(endpoint["url"])
        endpoint["event_types"] = loads(endpoint["event_types"], [])
        endpoint["enabled"] = bool(endpoint["enabled"])
        endpoint["archived"] = endpoint["archived_at"] is not None
        endpoint["rotation_in_progress"] = endpoint["previous_secret_salt"] is not None

        for name in ("secret_salt", "previous_secret_salt", "previous_secret_version"):
            endpoint.pop(name, None)

        return endpoint

    def change(self, connection, endpoint_id, actor, action, before=None, after=None):

        connection.execute(
            "INSERT INTO endpoint_changes (endpoint_id, at, actor, action, before, after) VALUES (?, ?, ?, ?, ?, ?)",
            (endpoint_id, text_time(self.clock()), actor, action,
             dumps(before) if before is not None else None, dumps(after) if after is not None else None))

    def create_endpoint(self, principal, name, url, event_types, enabled=True):
        """Registers an endpoint. Returns (endpoint, secret): the secret is shown this once."""

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        key = self.settings.require_key()
        name, url, types = self.check_name(name), self.check_destination(url), self.check_types(event_types)
        now = text_time(self.clock())
        salt = secrets_module.token_hex(16)
        store = self.store(create=True)

        with store.write() as connection:
            cursor = connection.execute(
                "INSERT INTO webhook_endpoints (name, url, event_types, enabled, secret_version, secret_salt, "
                "secret_rotated_at, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (name, url, dumps(types), 1 if enabled else 0, salt, now, principal.username, now, now))
            endpoint_id = cursor.lastrowid
            self.change(connection, endpoint_id, principal.username, "create",
                        after={"name": name, "url": display_url(url), "event_types": types, "enabled": bool(enabled)})

        self.start_cursors(store)

        return self.endpoint(endpoint_id), derive_secret(key, endpoint_id, 1, salt)

    def start_cursors(self, store):
        """The first endpoint starts collection at the logs' current ends (history is not replayed)."""

        if self.sources is None:
            return

        with store.read() as connection:
            known = {row["source"] for row in connection.execute("SELECT source FROM source_cursors")}

        missing = [name for name in EventSources.NAMES if name not in known]

        if not missing:
            return

        positions = {name: self.sources.position(name) for name in missing}
        now = text_time(self.clock())

        with store.write() as connection:
            for name, position in positions.items():
                connection.execute("INSERT OR IGNORE INTO source_cursors (source, position, updated_at) VALUES (?, ?, ?)",
                                   (name, position, now))

    def endpoint_row(self, connection, endpoint_id):

        row = connection.execute("SELECT * FROM webhook_endpoints WHERE id = ?", (endpoint_id,)).fetchone()

        if row is None:
            raise LookupError("No such webhook endpoint.")

        return row

    def endpoint(self, endpoint_id):

        store = self.store()

        if store is None:
            raise LookupError("No such webhook endpoint.")

        with store.read() as connection:
            endpoint = self.public(self.endpoint_row(connection, endpoint_id))

        endpoint["stats"] = self.stats(endpoint_id)

        return endpoint

    def update_endpoint(self, principal, endpoint_id, name=None, url=None, event_types=None, enabled=None):

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        store = self.store()

        if store is None:
            raise LookupError("No such webhook endpoint.")

        changes = {}

        if name is not None:
            changes["name"] = self.check_name(name)
        if url is not None:
            changes["url"] = self.check_destination(url)
        if event_types is not None:
            changes["event_types"] = self.check_types(event_types)
        if enabled is not None:
            changes["enabled"] = bool(enabled)

        if not changes:
            raise WebhookError("Nothing to change.")

        with store.write() as connection:
            current = self.public(self.endpoint_row(connection, endpoint_id))

            if current["archived"]:
                raise WebhookError("This endpoint is archived; register a new one.")

            before = {name: current[name] for name in changes}
            stored = {name: dumps(value) if name == "event_types" else (1 if value else 0) if name == "enabled" else value
                      for name, value in changes.items()}
            connection.execute(
                f"UPDATE webhook_endpoints SET {', '.join(f'{name} = ?' for name in stored)}, updated_at = ? WHERE id = ?",
                (*stored.values(), text_time(self.clock()), endpoint_id))
            action = ("enable" if changes["enabled"] else "disable") if list(changes) == ["enabled"] else "update"
            # The history shows URLs as the screens do (query values masked); `before` is already masked (public)
            logged = {**changes, **({"url": display_url(changes["url"])} if "url" in changes else {})}
            self.change(connection, endpoint_id, principal.username, action, before=before, after=logged)

        return self.endpoint(endpoint_id)

    def rotate_secret(self, principal, endpoint_id):
        """A new secret (returned once). The previous one keeps signing beside it for the grace period."""

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        key = self.settings.require_key()
        store = self.store()

        if store is None:
            raise LookupError("No such webhook endpoint.")

        now = self.clock()
        salt = secrets_module.token_hex(16)

        with store.write() as connection:
            row = self.endpoint_row(connection, endpoint_id)

            if row["archived_at"]:
                raise WebhookError("This endpoint is archived.")

            version = row["secret_version"] + 1
            grace = self.settings.rotation_grace_hours
            connection.execute(
                "UPDATE webhook_endpoints SET secret_version = ?, secret_salt = ?, previous_secret_version = ?, "
                "previous_secret_salt = ?, previous_secret_expires_at = ?, secret_rotated_at = ?, updated_at = ? "
                "WHERE id = ?",
                (version, salt, row["secret_version"] if grace else None, row["secret_salt"] if grace else None,
                 later(now, grace * 3600) if grace else None, text_time(now), text_time(now), endpoint_id))
            self.change(connection, endpoint_id, principal.username, "rotate_secret",
                        before={"secret_version": row["secret_version"]},
                        after={"secret_version": version, "previous_valid_hours": grace})

        return self.endpoint(endpoint_id), derive_secret(key, endpoint_id, version, salt)

    def archive_endpoint(self, principal, endpoint_id):
        """Deactivates the endpoint for good: nothing more is delivered; its history stays."""

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        store = self.store()

        if store is None:
            raise LookupError("No such webhook endpoint.")

        now = text_time(self.clock())

        with store.write() as connection:
            row = self.endpoint_row(connection, endpoint_id)

            if row["archived_at"]:
                raise WebhookError("This endpoint is already archived.")

            connection.execute("UPDATE webhook_endpoints SET enabled = 0, archived_at = ?, archived_by = ?, updated_at = ? "
                               "WHERE id = ?", (now, principal.username, now, endpoint_id))
            cancelled = connection.execute(
                "UPDATE webhook_deliveries SET status = 'cancelled', completed_at = ?, updated_at = ?, lease_until = NULL "
                "WHERE endpoint_id = ? AND status IN ('pending', 'retrying')", (now, now, endpoint_id)).rowcount
            self.change(connection, endpoint_id, principal.username, "archive", after={"cancelled_deliveries": cancelled})

        return self.endpoint(endpoint_id)

    def endpoints(self, include_archived=False):

        store = self.store()

        if store is None:
            return []

        with store.read() as connection:
            rows = connection.execute("SELECT * FROM webhook_endpoints" +
                                      ("" if include_archived else " WHERE archived_at IS NULL") + " ORDER BY id").fetchall()

        stats = self.all_stats()

        return [{**self.public(row), "stats": stats.get(row["id"], self.empty_stats())} for row in rows]

    def changes(self, endpoint_id, limit=50):

        store = self.store()

        if store is None:
            return []

        with store.read() as connection:
            return [{**dict(row), "before": loads(row["before"]), "after": loads(row["after"])} for row in connection.execute(
                "SELECT * FROM endpoint_changes WHERE endpoint_id = ? ORDER BY id DESC LIMIT ?", (endpoint_id, limit))]

    # --------------------------------------------------------
    # Statistics
    # --------------------------------------------------------

    @staticmethod
    def empty_stats():

        return {"deliveries": 0, "succeeded": 0, "failed": 0, "waiting": 0, "success_rate": None,
                "last_attempt_at": None, "last_success_at": None, "last_failure_at": None, "window_days": 7}

    def all_stats(self, endpoint_id=None, days=7):
        """Per endpoint, over deliveries created in the last `days` days (tests excluded)."""

        store = self.store()

        if store is None:
            return {}

        since = later(self.clock(), -days * 86400)
        query = (
            "SELECT endpoint_id, COUNT(*) AS deliveries, "
            "SUM(status = 'succeeded') AS succeeded, SUM(status = 'failed') AS failed, "
            "SUM(status IN ('pending', 'retrying', 'sending')) AS waiting, MAX(last_attempt_at) AS last_attempt_at, "
            "MAX(CASE WHEN status = 'succeeded' THEN completed_at END) AS last_success_at, "
            "MAX(CASE WHEN status = 'failed' THEN completed_at END) AS last_failure_at "
            "FROM webhook_deliveries WHERE test = 0 AND created_at >= ?"
        )
        params = [since]

        if endpoint_id is not None:
            query += " AND endpoint_id = ?"
            params.append(endpoint_id)

        result = {}

        with store.read() as connection:
            for row in connection.execute(query + " GROUP BY endpoint_id", params):
                done = (row["succeeded"] or 0) + (row["failed"] or 0)
                result[row["endpoint_id"]] = {
                    "deliveries": row["deliveries"], "succeeded": row["succeeded"] or 0, "failed": row["failed"] or 0,
                    "waiting": row["waiting"] or 0, "success_rate": round((row["succeeded"] or 0) / done, 4) if done else None,
                    "last_attempt_at": row["last_attempt_at"], "last_success_at": row["last_success_at"],
                    "last_failure_at": row["last_failure_at"], "window_days": days,
                }

        return result

    def stats(self, endpoint_id):

        return self.all_stats(endpoint_id).get(endpoint_id, self.empty_stats())

    # --------------------------------------------------------
    # Deliveries (reading)
    # --------------------------------------------------------

    def deliveries(self, endpoint_id=None, statuses=(), offset=0, limit=50):

        store = self.store()

        if store is None:
            return {"deliveries": [], "total": 0}

        where, params = [], []

        if endpoint_id is not None:
            where.append("d.endpoint_id = ?")
            params.append(endpoint_id)

        if statuses:
            where.append(f"d.status IN ({', '.join('?' for _ in statuses)})")
            params += list(statuses)

        condition = f" WHERE {' AND '.join(where)}" if where else ""

        with store.read() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM webhook_deliveries d{condition}", params).fetchone()[0]
            rows = connection.execute(
                "SELECT d.*, e.event_type, e.occurred_at, w.name AS endpoint_name FROM webhook_deliveries d "
                "JOIN webhook_events e ON e.id = d.event_id JOIN webhook_endpoints w ON w.id = d.endpoint_id"
                f"{condition} ORDER BY d.id DESC LIMIT ? OFFSET ?", (*params, int(limit), int(offset))).fetchall()

        return {"deliveries": [self.delivery_view(row) for row in rows], "total": total}

    @staticmethod
    def delivery_view(row):

        view = dict(row)
        view["test"] = bool(view["test"])
        view.pop("lease_until", None)

        return view

    def delivery(self, delivery_id):
        """A delivery with its event (payload with sensitive values masked) and every attempt."""

        store = self.store()

        if store is None:
            raise LookupError("No such delivery.")

        with store.read() as connection:
            row = connection.execute(
                "SELECT d.*, e.event_type, e.occurred_at, w.name AS endpoint_name FROM webhook_deliveries d "
                "JOIN webhook_events e ON e.id = d.event_id JOIN webhook_endpoints w ON w.id = d.endpoint_id "
                "WHERE d.id = ?", (delivery_id,)).fetchone()

            if row is None:
                raise LookupError("No such delivery.")

            event = connection.execute("SELECT * FROM webhook_events WHERE id = ?", (row["event_id"],)).fetchone()
            attempts = [dict(item) for item in connection.execute(
                "SELECT * FROM delivery_attempts WHERE delivery_id = ? ORDER BY id", (delivery_id,))]

        preview = masked(envelope(event))

        return {**self.delivery_view(row), "payload_preview": preview, "attempt_log": attempts,
                "retryable": row["status"] in ("failed", "retrying")}

    # --------------------------------------------------------
    # The outbox
    # --------------------------------------------------------

    def record_event(self, connection, event_type, source, source_ref, occurred_at, data, endpoint_ids=None, test=False):
        """
        Stores the event once and a delivery for each active endpoint subscribed
        to its type (or the endpoints given). Returns the event id, or None when
        this (source, source_ref, type) was already recorded.
        """

        event_id = event_id_of(source, source_ref, event_type)
        now = text_time(self.clock())
        inserted = connection.execute(
            "INSERT OR IGNORE INTO webhook_events (id, event_type, source, source_ref, occurred_at, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (event_id, event_type, source, source_ref, occurred_at, now, dumps(data))).rowcount

        if not inserted:
            return None

        if endpoint_ids is None:
            endpoint_ids = [row["id"] for row in connection.execute(
                "SELECT id, event_types FROM webhook_endpoints WHERE archived_at IS NULL AND enabled = 1")
                if event_type in loads(row["event_types"], [])]

        for endpoint_id in endpoint_ids:
            connection.execute(
                "INSERT OR IGNORE INTO webhook_deliveries (event_id, endpoint_id, status, next_attempt_at, test, created_at, "
                "updated_at) VALUES (?, ?, 'pending', ?, ?, ?, ?)", (event_id, endpoint_id, now, 1 if test else 0, now, now))

        if self.metrics is not None:
            self.metrics.inc("logidoc_webhook_events_total", {"type": event_type})

        return event_id

    def collect(self, sources=None, budget=500, only=None):
        """
        Reads each log after its cursor and stores its events, the cursor moving
        in the same transaction. Returns {source: events created}. Does nothing
        before the first endpoint exists (no store, or no cursor yet).
        """

        sources = sources or self.sources
        store = self.store()
        created = {}

        if store is None or sources is None:
            return created

        with store.read() as connection:
            cursors = {row["source"]: row["position"] for row in connection.execute("SELECT * FROM source_cursors")}

        for name in only or EventSources.NAMES:

            if name not in cursors:
                continue

            batch = sources.read(name, cursors[name], budget)
            count = 0

            if not batch:
                created[name] = 0
                continue

            with store.write() as connection:
                # The cursor is read again under the write lock: two collectors cannot both apply this batch
                position = connection.execute("SELECT position FROM source_cursors WHERE source = ?", (name,)).fetchone()[0]

                for row_position, events in batch:
                    if row_position <= position:
                        continue
                    for event_type, reference, occurred_at, data in events:
                        if self.record_event(connection, event_type, name, reference, occurred_at, data):
                            count += 1
                    position = row_position

                connection.execute("UPDATE source_cursors SET position = ?, updated_at = ? WHERE source = ?",
                                   (position, text_time(self.clock()), name))

            created[name] = count

        return created

    def cursors(self):

        store = self.store()

        if store is None:
            return []

        with store.read() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM source_cursors ORDER BY source")]

    # --------------------------------------------------------
    # Delivering
    # --------------------------------------------------------

    def secrets_of(self, row, now):
        """The current secret, and the previous one while its grace period lasts."""

        key = self.settings.require_key()
        found = [derive_secret(key, row["id"], row["secret_version"], row["secret_salt"])]
        expires = parse_time(row["previous_secret_expires_at"])

        if row["previous_secret_salt"] and expires and expires > now:
            found.append(derive_secret(key, row["id"], row["previous_secret_version"], row["previous_secret_salt"]))

        return found

    def retry_delay(self, cycle_attempt, retry_after=None):
        """Exponential back-off with jitter: base * 2^(n-1), at most the maximum, times 0.5 to 1."""

        delay = min(self.settings.max_delay_seconds, self.settings.base_delay_seconds * 2 ** max(cycle_attempt - 1, 0))
        delay = delay * (0.5 + 0.5 * self.jitter())

        if retry_after is not None:
            delay = max(delay, min(retry_after, self.settings.max_delay_seconds))

        return int(delay)

    def lease(self, delivery_id, trigger, allowed=("pending", "retrying")):
        """Claims a delivery for one attempt (atomic). Returns the delivery and endpoint rows, or None."""

        store = self.store()
        now = self.clock()

        with store.write() as connection:
            row = connection.execute("SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)).fetchone()

            if row is None or row["status"] not in allowed:
                return None

            connection.execute(
                "UPDATE webhook_deliveries SET status = 'sending', lease_until = ?, attempts = attempts + 1, "
                "cycle_attempts = cycle_attempts + 1, last_attempt_at = ?, updated_at = ? WHERE id = ?",
                (later(now, self.settings.timeout_seconds + 60), text_time(now), text_time(now), delivery_id))
            row = connection.execute("SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)).fetchone()
            endpoint = connection.execute("SELECT * FROM webhook_endpoints WHERE id = ?", (row["endpoint_id"],)).fetchone()
            event = connection.execute("SELECT * FROM webhook_events WHERE id = ?", (row["event_id"],)).fetchone()

        return dict(row), dict(endpoint), dict(event)

    def attempt(self, delivery_id, trigger="schedule", retry=True):
        """One attempt of one delivery. Returns the delivery after it, or None when it was not available."""

        claimed = self.lease(delivery_id, trigger)

        if claimed is None:
            return None

        row, endpoint, event = claimed
        started = self.clock()
        clock_start = time.monotonic()
        status_code, retry_after = None, None

        try:
            body = body_of(event)

            if len(body) > MAX_PAYLOAD_BYTES:
                category, retryable, detail = "payload_too_large", False, f"The payload is over {MAX_PAYLOAD_BYTES} bytes."
            else:
                destination = url_safety.resolve_destination(endpoint["url"], self.settings.policy, self.resolver)
                timestamp = int(started.timestamp())
                headers = {
                    "Content-Type": "application/json", "User-Agent": "LogiDoc-Webhooks/1",
                    "LogiDoc-Event-Id": event["id"], IDEMPOTENCY_HEADER: event["id"], "LogiDoc-Event-Type": event["event_type"],
                    "LogiDoc-Delivery-Id": str(row["id"]), "LogiDoc-Delivery-Attempt": str(row["attempts"]),
                    TIMESTAMP_HEADER: str(timestamp),
                    SIGNATURE_HEADER: signature_header(self.secrets_of(endpoint, started), timestamp, event["id"], body),
                }
                status_code, extra = self.transport(destination, headers, body, self.settings.timeout_seconds)
                category, retryable, detail = classify_status(status_code)
                retry_after = seconds_of((extra or {}).get("retry-after"))
        except DeliveryDisabled as error:
            category, retryable, detail = "delivery_disabled", True, str(error)
        except Exception as error:  # noqa: BLE001 - every failure is classified and recorded, never raised to the job
            category, retryable, detail = classify_error(error)

        duration_ms = int((time.monotonic() - clock_start) * 1000)
        now = self.clock()
        # A test delivery is attempted only when an administrator asks: never queued for retries
        retry = retry and not row["test"]
        retryable = retryable and retry and row["cycle_attempts"] < self.settings.max_attempts

        if category is None:
            outcome, status = "succeeded", "succeeded"
        elif retryable:
            outcome, status = "retry", "retrying"
        else:
            outcome, status = "failed", "failed"
            if category in RETRYABLE_ERRORS and retry:
                detail = f"{detail} Gave up after {row['cycle_attempts']} attempts."

        next_at = later(now, self.retry_delay(row["cycle_attempts"], retry_after)) if status == "retrying" else None

        with self.store().write() as connection:
            connection.execute(
                "INSERT INTO delivery_attempts (delivery_id, attempt, trigger, started_at, duration_ms, status_code, outcome, "
                "error, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (delivery_id, row["attempts"], trigger, text_time(started), duration_ms, status_code, outcome, category,
                 detail[:300]))
            connection.execute(
                "UPDATE webhook_deliveries SET status = ?, lease_until = NULL, next_attempt_at = ?, last_status_code = ?, "
                "last_error = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (status, next_at, status_code, category, text_time(now),
                 text_time(now) if status in ("succeeded", "failed") else None, delivery_id))

        if self.metrics is not None:
            self.metrics.inc("logidoc_webhook_deliveries_total", {"outcome": outcome})
            self.metrics.observe("logidoc_webhook_delivery_seconds", duration_ms / 1000)

        if outcome != "succeeded":
            log.warning("Webhook delivery %s to endpoint %s: %s (%s).", delivery_id, endpoint["id"], outcome, category,
                        extra={"operation": "webhook_delivery", "outcome": outcome, "error_category": category})

        return self.delivery(delivery_id)

    def due(self, statuses, limit):

        store = self.store()

        if store is None:
            return []

        marks = ", ".join("?" for _ in statuses)

        with store.read() as connection:
            return [row["id"] for row in connection.execute(
                f"SELECT d.id FROM webhook_deliveries d JOIN webhook_endpoints w ON w.id = d.endpoint_id "
                f"WHERE d.status IN ({marks}) AND d.next_attempt_at <= ? AND d.test = 0 AND w.enabled = 1 "
                f"AND w.archived_at IS NULL "
                f"ORDER BY d.next_attempt_at, d.id LIMIT ?", (*statuses, text_time(self.clock()), int(limit)))]

    def deliver_due(self, statuses=("pending",), limit=50, should_stop=lambda: False):
        """Attempts the due deliveries of the statuses given (oldest first). Returns {outcome: count}."""

        if not self.settings.delivery_enabled:
            return {"skipped": "delivery disabled: no signing key"}

        counts = {"succeeded": 0, "retrying": 0, "failed": 0}

        for delivery_id in self.due(statuses, limit):
            if should_stop():
                break
            result = self.attempt(delivery_id, "schedule")
            if result is not None:
                counts[result["status"]] = counts.get(result["status"], 0) + 1

        return counts

    def recover(self):
        """
        A delivery left 'sending' by a stopped process (its lease expired) goes
        back to the retry queue: the receiver may or may not have it, so it is
        sent again (receivers deduplicate by event id). Returns how many.
        """

        store = self.store()

        if store is None:
            return 0

        now = text_time(self.clock())

        with store.write() as connection:
            rows = connection.execute("SELECT id, attempts FROM webhook_deliveries WHERE status = 'sending' AND lease_until < ?",
                                      (now,)).fetchall()
            for row in rows:
                connection.execute(
                    "INSERT INTO delivery_attempts (delivery_id, attempt, trigger, started_at, outcome, error, detail) "
                    "VALUES (?, ?, 'recovery', ?, 'retry', 'interrupted', ?)",
                    (row["id"], row["attempts"], now, "The attempt was interrupted (the server stopped); it is sent again."))
                connection.execute("UPDATE webhook_deliveries SET status = 'retrying', lease_until = NULL, next_attempt_at = ?, "
                                   "last_error = 'interrupted', updated_at = ? WHERE id = ?", (now, now, row["id"]))

        return len(rows)

    def retry_delivery(self, principal, delivery_id):
        """An administrator's retry of a failed (or waiting) delivery: a new cycle of attempts, now."""

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        store = self.store()

        if store is None:
            raise LookupError("No such delivery.")

        now = text_time(self.clock())

        with store.write() as connection:
            row = connection.execute("SELECT d.*, w.archived_at FROM webhook_deliveries d JOIN webhook_endpoints w "
                                     "ON w.id = d.endpoint_id WHERE d.id = ?", (delivery_id,)).fetchone()

            if row is None:
                raise LookupError("No such delivery.")
            if row["archived_at"]:
                raise WebhookError("The endpoint is archived.")
            if row["status"] not in ("failed", "retrying"):
                raise WebhookError(f"Only a failed or retrying delivery can be retried (this one is {row['status']}).")

            connection.execute(
                "UPDATE webhook_deliveries SET status = 'retrying', cycle_attempts = 0, next_attempt_at = ?, completed_at = NULL, "
                "manual_retries = manual_retries + 1, last_retry_by = ?, last_retry_at = ?, updated_at = ? WHERE id = ?",
                (now, principal.username, now, now, delivery_id))
            self.change(connection, row["endpoint_id"], principal.username, "retry_delivery", after={"delivery_id": delivery_id})

        return self.attempt(delivery_id, "manual") or self.delivery(delivery_id)

    def send_test(self, principal, endpoint_id):
        """A webhook.test event to this endpoint only, attempted now, never retried by itself."""

        authz.require(principal, authz.INTEGRATIONS_MANAGE)
        self.settings.require_key()
        store = self.store()

        if store is None:
            raise LookupError("No such webhook endpoint.")

        with store.write() as connection:
            row = self.endpoint_row(connection, endpoint_id)

            if row["archived_at"]:
                raise WebhookError("This endpoint is archived.")

            now = text_time(self.clock())
            event_id = self.record_event(connection, TEST_EVENT, "test", uuid.uuid4().hex, now,
                                         {"message": "A test event from LogiDoc-RAG.", "endpoint_id": endpoint_id,
                                          "requested_by": principal.username}, endpoint_ids=[endpoint_id], test=True)
            delivery_id = connection.execute("SELECT id FROM webhook_deliveries WHERE event_id = ? AND endpoint_id = ?",
                                             (event_id, endpoint_id)).fetchone()[0]
            self.change(connection, endpoint_id, principal.username, "test", after={"delivery_id": delivery_id})

        return self.attempt(delivery_id, "test", retry=False)

    def health(self):
        """Per active endpoint: consecutive failed attempts and whether it needs attention."""

        store = self.store()

        if store is None:
            return []

        result = []

        with store.read() as connection:
            for endpoint in connection.execute("SELECT id, name, enabled FROM webhook_endpoints WHERE archived_at IS NULL"):
                outcomes = [row["outcome"] for row in connection.execute(
                    "SELECT a.outcome FROM delivery_attempts a JOIN webhook_deliveries d ON d.id = a.delivery_id "
                    "WHERE d.endpoint_id = ? AND d.test = 0 ORDER BY a.id DESC LIMIT 20", (endpoint["id"],))]
                failing = 0
                for outcome in outcomes:
                    if outcome == "succeeded":
                        break
                    failing += 1
                failed = connection.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id = ? AND status = 'failed' "
                                            "AND test = 0", (endpoint["id"],)).fetchone()[0]
                result.append({"endpoint_id": endpoint["id"], "name": endpoint["name"], "enabled": bool(endpoint["enabled"]),
                               "consecutive_failures": failing, "failed_deliveries": failed})

        return result


def display_url(url):
    """The URL as screens show it: query values masked (receivers sometimes put a token there)."""

    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)

    if not parts.query:
        return url

    query = urlencode([(name, "[redacted]") for name, _ in parse_qsl(parts.query, keep_blank_values=True)], safe="[]")

    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def seconds_of(text):

    try:
        return max(int(str(text).strip()), 0) if text not in (None, "") else None
    except ValueError:
        return None
