"""
Carrier master data: the registered carriers, their identifiers, and which
carrier names printed on the documents belong to which registered carrier.

    registry = CarrierRegistry(path)
    carrier = registry.create(principal, legal_name="Swift Lines Inc", scac="SWFT", mc_number="MC-123456")
    request = registry.propose_link(principal, "Swift Lines", carrier["id"], reason)
    registry.decide_link(other_administrator, request["id"], "approve")

Identity rules

    * Identifiers are validated, never guessed: SCAC 2-4 letters (stored upper
      case); MC (FMCSA docket) and USDOT numbers 1-8 digits ("MC-", "MC",
      "USDOT", "DOT" prefixes accepted, stored as digits). A malformed value is
      refused, not cleaned into something else.
    * An identifier belongs to one carrier: a second carrier with the same SCAC,
      MC or USDOT number is refused, naming the carrier that has it.
    * A new carrier whose legal or trading name equals an existing one's after
      ignoring case, spaces and punctuation is refused as a possible duplicate,
      unless the request says it is a different company (confirm_distinct).
    * A name printed on the documents (compared like carrier_intelligence:
      case and spacing ignored) is attached to a registered carrier only by an
      approved link: one administrator proposes it with a reason, a different
      administrator approves or rejects it. That is the only way two document
      names become one carrier (a merge); similar names are never merged by
      themselves. A name already linked elsewhere is moved, or unlinked, by a
      new approved request, never silently.
    * Nothing about a shipment is stored here: shipments are associated with a
      registered carrier when the figures are read (through the names their
      documents print), so recorded audit history is never rewritten, and
      unlinking a name restores the earlier grouping.
    * Every change is an event (carrier_events, append-only: triggers refuse
      UPDATE and DELETE) with who made it, when, and the values before and after.
"""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import authz
from trend_analytics import carrier_key

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS carriers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    legal_name    TEXT NOT NULL,
    trading_name  TEXT,
    scac          TEXT UNIQUE,
    mc_number     TEXT UNIQUE,
    dot_number    TEXT UNIQUE,
    contact_name  TEXT,
    contact_email TEXT,
    contact_phone TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS carrier_names (
    name_key     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    carrier_id   INTEGER NOT NULL REFERENCES carriers (id),
    request_id   INTEGER NOT NULL,
    linked_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS link_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name_key      TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    carrier_id    INTEGER REFERENCES carriers (id),
    reason        TEXT NOT NULL,
    proposed_by   TEXT NOT NULL,
    proposed_at   TEXT NOT NULL,
    decision      TEXT CHECK (decision IN ('approved', 'rejected')),
    decided_by    TEXT,
    decided_at    TEXT,
    decision_note TEXT
);

CREATE TABLE IF NOT EXISTS carrier_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    carrier_id INTEGER,
    request_id INTEGER,
    before     TEXT,
    after      TEXT
);

CREATE TRIGGER IF NOT EXISTS carrier_events_no_update BEFORE UPDATE ON carrier_events
    BEGIN SELECT RAISE(ABORT, 'carrier history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS carrier_events_no_delete BEFORE DELETE ON carrier_events
    BEGIN SELECT RAISE(ABORT, 'carrier history is append-only'); END;
"""

FIELDS = ("legal_name", "trading_name", "scac", "mc_number", "dot_number", "contact_name", "contact_email", "contact_phone")
IDENTIFIERS = ("scac", "mc_number", "dot_number")

SCAC = re.compile(r"^[A-Z]{2,4}$")
MC = re.compile(r"^(?:MC)?[\s#:-]*(\d{1,8})$")
DOT = re.compile(r"^(?:US\s*DOT|USDOT|DOT)?[\s#:-]*(\d{1,8})$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
PHONE = re.compile(r"^\+?[0-9()\-\s.]{7,25}$")


class RegistryError(ValueError):
    """A value or a change that is refused (the message says why)."""


class RegistryConflict(RegistryError):
    """The change would give one identifier or name to two carriers."""


def now_text():

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def loose_key(name):
    """Letters and digits only: to find possible duplicates (never to merge)."""

    return re.sub(r"[^0-9a-z]", "", str(name or "").casefold())


def clean_text(value, field, required=False, limit=200):

    text = " ".join(str(value).split()) if value is not None else ""

    if not text:
        if required:
            raise RegistryError(f"{field} is required.")
        return None

    if len(text) > limit:
        raise RegistryError(f"{field} is longer than {limit} characters.")

    return text


def validate(values, partial=False):
    """The cleaned values of the fields given; RegistryError for a malformed one."""

    cleaned = {}

    for field in FIELDS:

        if field not in values:
            continue

        raw = values[field]

        if field == "legal_name":
            cleaned[field] = clean_text(raw, "The legal name", required=True)
        elif field in ("trading_name", "contact_name"):
            cleaned[field] = clean_text(raw, field.replace("_", " ").capitalize())
        elif raw is None or not str(raw).strip():
            cleaned[field] = None
        elif field == "scac":
            text = str(raw).strip().upper()
            if not SCAC.match(text):
                raise RegistryError(f"{raw!r} is not a SCAC: 2 to 4 letters.")
            cleaned[field] = text
        elif field == "mc_number":
            match = MC.match(str(raw).strip().upper())
            if not match:
                raise RegistryError(f"{raw!r} is not an MC number: up to 8 digits, optionally after 'MC'.")
            cleaned[field] = str(int(match.group(1)))
        elif field == "dot_number":
            match = DOT.match(str(raw).strip().upper())
            if not match:
                raise RegistryError(f"{raw!r} is not a USDOT number: up to 8 digits, optionally after 'USDOT'.")
            cleaned[field] = str(int(match.group(1)))
        elif field == "contact_email":
            text = str(raw).strip()
            if not EMAIL.match(text) or len(text) > 254:
                raise RegistryError(f"{raw!r} is not an email address.")
            cleaned[field] = text
        elif field == "contact_phone":
            text = " ".join(str(raw).split())
            if not PHONE.match(text) or sum(c.isdigit() for c in text) < 7:
                raise RegistryError(f"{raw!r} is not a phone number.")
            cleaned[field] = text

    if not partial and "legal_name" not in cleaned:
        raise RegistryError("The legal name is required.")

    return cleaned


class CarrierRegistry:

    def __init__(self, path):

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as connection, connection:
            connection.executescript(SCHEMA)
            if connection.execute("SELECT version FROM schema_info").fetchone() is None:
                connection.execute("INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,))

    def connect(self):

        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    @staticmethod
    def event(connection, actor, action, carrier_id=None, request_id=None, before=None, after=None):

        connection.execute(
            "INSERT INTO carrier_events (at, actor, action, carrier_id, request_id, before, after) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_text(), actor, action, carrier_id, request_id,
             json.dumps(before, sort_keys=True) if before is not None else None,
             json.dumps(after, sort_keys=True) if after is not None else None),
        )

    # --------------------------------------------------------
    # Reading
    # --------------------------------------------------------

    def carriers(self):

        with closing(self.connect()) as connection:
            carriers = [dict(row) for row in connection.execute("SELECT * FROM carriers ORDER BY legal_name COLLATE NOCASE")]
            names = {}
            for row in connection.execute("SELECT * FROM carrier_names ORDER BY display_name"):
                names.setdefault(row["carrier_id"], []).append(row["display_name"])

        for carrier in carriers:
            carrier["document_names"] = names.get(carrier["id"], [])

        return carriers

    def carrier(self, carrier_id):

        return next((c for c in self.carriers() if c["id"] == carrier_id), None)

    def requests(self, pending_only=False):

        query = "SELECT * FROM link_requests" + (" WHERE decision IS NULL" if pending_only else "") + " ORDER BY id"

        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(query)]

    def events(self, limit=500):

        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT * FROM carrier_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

        return [{**dict(row), "before": json.loads(row["before"]) if row["before"] else None,
                 "after": json.loads(row["after"]) if row["after"] else None} for row in rows]

    def resolver(self):
        """{document name key: registered carrier} of the approved links (carrier_intelligence groups by it)."""

        carriers = {c["id"]: c for c in self.carriers()}

        with closing(self.connect()) as connection:
            return {row["name_key"]: carriers[row["carrier_id"]] for row in connection.execute("SELECT * FROM carrier_names")}

    # --------------------------------------------------------
    # Carriers
    # --------------------------------------------------------

    def check_unique(self, connection, values, carrier_id=None):

        for field in IDENTIFIERS:
            if values.get(field):
                other = connection.execute(f"SELECT id, legal_name FROM carriers WHERE {field} = ? AND id IS NOT ?",
                                           (values[field], carrier_id)).fetchone()
                if other:
                    label = {"scac": "SCAC", "mc_number": "MC number", "dot_number": "USDOT number"}[field]
                    raise RegistryConflict(f"{label} {values[field]} already belongs to carrier {other['id']} ({other['legal_name']}).")

    def create(self, principal, confirm_distinct=False, **values):

        authz.require(principal, authz.CARRIERS_MANAGE)
        cleaned = validate(values)

        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self.check_unique(connection, cleaned)

            if not confirm_distinct:
                wanted = {loose_key(cleaned["legal_name"]), loose_key(cleaned.get("trading_name"))} - {""}
                for row in connection.execute("SELECT id, legal_name, trading_name FROM carriers"):
                    if wanted & ({loose_key(row["legal_name"]), loose_key(row["trading_name"])} - {""}):
                        raise RegistryConflict(
                            f"Possibly the same company as carrier {row['id']} ({row['legal_name']}). Link its document "
                            "names to that carrier, or say that it is a different company (confirm_distinct)."
                        )

            at = now_text()
            row = {field: cleaned.get(field) for field in FIELDS}
            cursor = connection.execute(
                f"INSERT INTO carriers ({', '.join(FIELDS)}, created_by, created_at, updated_at) "
                f"VALUES ({', '.join('?' for _ in FIELDS)}, ?, ?, ?)",
                (*row.values(), principal.username, at, at),
            )
            self.event(connection, principal.username, "create", carrier_id=cursor.lastrowid, after=row)

        return self.carrier(cursor.lastrowid)

    def update(self, principal, carrier_id, **values):

        authz.require(principal, authz.CARRIERS_MANAGE)
        cleaned = validate(values, partial=True)

        if not cleaned:
            raise RegistryError("Nothing to change.")

        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM carriers WHERE id = ?", (carrier_id,)).fetchone()

            if current is None:
                raise LookupError("No such carrier.")

            self.check_unique(connection, cleaned, carrier_id)
            before = {field: current[field] for field in cleaned}
            connection.execute(
                f"UPDATE carriers SET {', '.join(f'{f} = ?' for f in cleaned)}, updated_at = ? WHERE id = ?",
                (*cleaned.values(), now_text(), carrier_id),
            )
            self.event(connection, principal.username, "update", carrier_id=carrier_id, before=before, after=cleaned)

        return self.carrier(carrier_id)

    # --------------------------------------------------------
    # Document names: two-person links
    # --------------------------------------------------------

    def propose_link(self, principal, name, carrier_id, reason):
        """
        Proposes that the document name belongs to carrier_id (None: unlink it).
        It has no effect until a different administrator approves it.
        """

        authz.require(principal, authz.CARRIERS_MANAGE)

        display = clean_text(name, "The document name", required=True)
        key = carrier_key(display)
        reason = clean_text(reason, "The reason", required=True, limit=1000)

        if len(reason) < 10:
            raise RegistryError("The reason must say why (at least 10 characters).")

        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")

            if carrier_id is not None and connection.execute("SELECT 1 FROM carriers WHERE id = ?", (carrier_id,)).fetchone() is None:
                raise LookupError("No such carrier.")

            if connection.execute("SELECT 1 FROM link_requests WHERE name_key = ? AND decision IS NULL", (key,)).fetchone():
                raise RegistryConflict(f"A request for '{display}' is already waiting for a decision.")

            linked = connection.execute("SELECT carrier_id FROM carrier_names WHERE name_key = ?", (key,)).fetchone()

            if carrier_id is None and linked is None:
                raise RegistryError(f"'{display}' is not linked to any carrier.")

            if linked is not None and linked["carrier_id"] == carrier_id:
                raise RegistryError(f"'{display}' is already linked to carrier {carrier_id}.")

            cursor = connection.execute(
                "INSERT INTO link_requests (name_key, display_name, carrier_id, reason, proposed_by, proposed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key, display, carrier_id, reason, principal.username, now_text()),
            )
            self.event(connection, principal.username, "propose_link" if carrier_id else "propose_unlink",
                       carrier_id=carrier_id, request_id=cursor.lastrowid,
                       before={"carrier_id": linked["carrier_id"] if linked else None},
                       after={"name": display, "carrier_id": carrier_id, "reason": reason})

        return self.request(cursor.lastrowid)

    def request(self, request_id):

        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM link_requests WHERE id = ?", (request_id,)).fetchone()

        return dict(row) if row else None

    def decide_link(self, principal, request_id, decision, note=None):
        """Approves or rejects a link request; never by the person who proposed it."""

        authz.require(principal, authz.CARRIERS_MANAGE)

        if decision not in ("approve", "reject"):
            raise RegistryError("The decision is approve or reject.")

        note = clean_text(note, "The note", limit=1000)

        if decision == "reject" and not note:
            raise RegistryError("A rejection needs a note.")

        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM link_requests WHERE id = ?", (request_id,)).fetchone()

            if row is None:
                raise LookupError("No such request.")

            if row["decision"] is not None:
                raise RegistryConflict(f"This request was already {row['decision']}.")

            if row["proposed_by"] == principal.username:
                raise authz.PermissionDenied("The person who proposed a link cannot decide it: another administrator must.")

            at = now_text()
            outcome = "approved" if decision == "approve" else "rejected"
            connection.execute("UPDATE link_requests SET decision = ?, decided_by = ?, decided_at = ?, decision_note = ? WHERE id = ?",
                               (outcome, principal.username, at, note, request_id))

            before = connection.execute("SELECT carrier_id FROM carrier_names WHERE name_key = ?", (row["name_key"],)).fetchone()

            if outcome == "approved":
                connection.execute("DELETE FROM carrier_names WHERE name_key = ?", (row["name_key"],))
                if row["carrier_id"] is not None:
                    connection.execute(
                        "INSERT INTO carrier_names (name_key, display_name, carrier_id, request_id, linked_at) VALUES (?, ?, ?, ?, ?)",
                        (row["name_key"], row["display_name"], row["carrier_id"], request_id, at),
                    )

            self.event(connection, principal.username, f"{outcome}_link", carrier_id=row["carrier_id"], request_id=request_id,
                       before={"carrier_id": before["carrier_id"] if before else None},
                       after={"name": row["display_name"], "carrier_id": row["carrier_id"] if outcome == "approved" else
                              (before["carrier_id"] if before else None), "note": note})

        return self.request(request_id)
