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

Schema version 2 adds the registration status (active, on_hold, inactive) and
a structured address. A version 1 store is upgraded when it is opened, in one
transaction: the new columns get their defaults (status active, no address)
and no row or event is changed. Indexes for the searches and the per-carrier
history come with it.
"""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import authz
from trend_analytics import carrier_key

SCHEMA_VERSION = 2

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

# Registration status: active (in use), on_hold (do not use until checked), inactive (no longer used)
STATUSES = ("active", "on_hold", "inactive")

# Version 1 -> 2: one transaction, every row kept (new columns take their defaults)
MIGRATION_V2 = (
    "ALTER TABLE carriers ADD COLUMN status TEXT NOT NULL DEFAULT 'active' "
    "CHECK (status IN ('active', 'on_hold', 'inactive'))",
    "ALTER TABLE carriers ADD COLUMN address_line1 TEXT",
    "ALTER TABLE carriers ADD COLUMN address_line2 TEXT",
    "ALTER TABLE carriers ADD COLUMN city TEXT",
    "ALTER TABLE carriers ADD COLUMN region TEXT",
    "ALTER TABLE carriers ADD COLUMN postal_code TEXT",
    "ALTER TABLE carriers ADD COLUMN country TEXT",
    "CREATE INDEX IF NOT EXISTS carriers_by_status ON carriers (status)",
    "CREATE INDEX IF NOT EXISTS carrier_events_by_carrier ON carrier_events (carrier_id, id)",
    "CREATE INDEX IF NOT EXISTS carrier_events_by_request ON carrier_events (request_id, id)",
    "CREATE INDEX IF NOT EXISTS link_requests_by_name ON link_requests (name_key, decision)",
    "CREATE INDEX IF NOT EXISTS link_requests_by_carrier ON link_requests (carrier_id, decision)",
)

ADDRESS = ("address_line1", "address_line2", "city", "region", "postal_code", "country")

FIELDS = ("legal_name", "trading_name", "scac", "mc_number", "dot_number", "contact_name", "contact_email", "contact_phone",
          "status", *ADDRESS)
IDENTIFIERS = ("scac", "mc_number", "dot_number")

# Sorting of the carrier list: the columns a client may name
SORTS = ("legal_name", "trading_name", "scac", "mc_number", "dot_number", "status", "created_at", "updated_at")

SCAC = re.compile(r"^[A-Z]{2,4}$")
MC = re.compile(r"^(?:MC)?[\s#:-]*(\d{1,8})$")
DOT = re.compile(r"^(?:US\s*DOT|USDOT|DOT)?[\s#:-]*(\d{1,8})$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
PHONE = re.compile(r"^\+?[0-9()\-\s.]{7,25}$")
POSTAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{1,11}$")
COUNTRY = re.compile(r"^[A-Za-z]{2}$")


class RegistryError(ValueError):
    """A value or a change that is refused (the message says why)."""


class RegistryConflict(RegistryError):
    """The change would give one identifier or name to two carriers."""


class RegistryVersionError(RegistryError):
    """The store was written by a newer version of the application."""


def now_text():

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def loose_key(name):
    """Letters and digits only: to find possible duplicates (never to merge)."""

    return re.sub(r"[^0-9a-z]", "", str(name or "").casefold())


def like_pattern(text):
    """text for a LIKE ... ESCAPE '\\' pattern: its own %, _ and \\ match themselves."""

    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        elif field in ("trading_name", "contact_name", "address_line1", "address_line2"):
            label = {"address_line1": "Address line 1", "address_line2": "Address line 2"}.get(
                field, field.replace("_", " ").capitalize())
            cleaned[field] = clean_text(raw, label)
        elif field in ("city", "region"):
            cleaned[field] = clean_text(raw, "City" if field == "city" else "Region (state or province)", limit=100)
        elif field == "status":
            text = str(raw or "").strip().lower()
            if text not in STATUSES:
                raise RegistryError(f"{raw!r} is not a registration status: {', '.join(STATUSES)}.")
            cleaned[field] = text
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
        elif field == "postal_code":
            text = " ".join(str(raw).split())
            if not POSTAL.match(text):
                raise RegistryError(f"{raw!r} is not a postal code: 2 to 12 letters, digits, spaces or hyphens.")
            cleaned[field] = text.upper()
        elif field == "country":
            text = str(raw).strip()
            if not COUNTRY.match(text):
                raise RegistryError(f"{raw!r} is not a country code: two letters (ISO 3166-1), such as US or CA.")
            cleaned[field] = text.upper()

    if not partial and "legal_name" not in cleaned:
        raise RegistryError("The legal name is required.")

    return cleaned


class CarrierRegistry:

    def __init__(self, path):

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as connection, connection:
            connection.executescript(SCHEMA)
            row = connection.execute("SELECT version FROM schema_info").fetchone()
            if row is None:
                # A new store starts at version 1 and takes the same upgrade path as an existing one
                connection.execute("INSERT INTO schema_info (version) VALUES (1)")
                version = 1
            else:
                version = row["version"]

        if version > SCHEMA_VERSION:
            raise RegistryVersionError(
                f"The carrier registry has schema version {version}; this version of the application reads "
                f"{SCHEMA_VERSION}. Update the application.")

        if version < SCHEMA_VERSION:
            self.migrate()

    def migrate(self):
        """
        Brings the schema to SCHEMA_VERSION one step at a time. Each step is one
        transaction (its statements and the version, or nothing); the version is
        read again inside it, so two processes opening the store at once do not
        apply a step twice.
        """

        steps = {2: MIGRATION_V2}
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)

        try:
            for target in range(2, SCHEMA_VERSION + 1):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if connection.execute("SELECT version FROM schema_info").fetchone()[0] == target - 1:
                        for statement in steps[target]:
                            connection.execute(statement)
                        connection.execute("UPDATE schema_info SET version = ?", (target,))
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
        finally:
            connection.close()

    def version(self):

        with closing(self.connect()) as connection:
            return connection.execute("SELECT version FROM schema_info").fetchone()[0]

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
    # Reading for the management screens (paged)
    # --------------------------------------------------------

    def search(self, query="", statuses=(), sort="legal_name", order="asc", offset=0, limit=25):
        """
        A page of carriers: {"carriers": [...], "total"}. query matches any part
        of the legal or trading name (letter case ignored), the SCAC, and the MC
        and USDOT numbers with or without their prefixes. statuses: keep only
        these registration statuses. Each carrier carries its linked document
        names and the number of link requests waiting for it.
        """

        if sort not in SORTS:
            raise RegistryError(f"Sort by one of: {', '.join(SORTS)}.")
        if order not in ("asc", "desc"):
            raise RegistryError("The order is asc or desc.")
        unknown = set(statuses) - set(STATUSES)
        if unknown:
            raise RegistryError(f"Unknown registration status: {', '.join(sorted(unknown))}.")

        where, params = [], []
        text = " ".join(str(query or "").split())

        if text:
            like = f"%{like_pattern(text)}%"
            clauses = ["legal_name LIKE ? ESCAPE '\\'", "coalesce(trading_name, '') LIKE ? ESCAPE '\\'",
                       "coalesce(scac, '') LIKE ? ESCAPE '\\'"]
            params += [like, like, f"%{like_pattern(text.upper())}%"]

            for pattern, column in ((MC, "mc_number"), (DOT, "dot_number")):
                match = pattern.match(text.upper())
                if match:
                    clauses.append(f"coalesce({column}, '') LIKE ?")
                    params.append(f"%{int(match.group(1))}%")

            where.append("(" + " OR ".join(clauses) + ")")

        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params += list(statuses)

        condition = f" WHERE {' AND '.join(where)}" if where else ""
        direction = "DESC" if order == "desc" else "ASC"

        with closing(self.connect()) as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM carriers{condition}", params).fetchone()[0]
            rows = [dict(row) for row in connection.execute(
                f"SELECT * FROM carriers{condition} ORDER BY ({sort} IS NULL), {sort} COLLATE NOCASE {direction}, id "
                f"LIMIT ? OFFSET ?", (*params, int(limit), int(offset)))]
            self.attach(connection, rows)

        return {"carriers": rows, "total": total}

    def record(self, carrier_id):
        """One carrier with its linked document names and waiting link requests, or None."""

        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM carriers WHERE id = ?", (carrier_id,)).fetchone()
            if row is None:
                return None
            carrier = dict(row)
            self.attach(connection, [carrier])

        return carrier

    @staticmethod
    def attach(connection, carriers):
        """Adds each carrier's linked document names and the count of link requests waiting for it."""

        ids = [carrier["id"] for carrier in carriers]

        if not ids:
            return

        marks = ", ".join("?" for _ in ids)
        names, pending = {}, {}

        for row in connection.execute(f"SELECT carrier_id, display_name FROM carrier_names WHERE carrier_id IN ({marks}) "
                                      "ORDER BY display_name", ids):
            names.setdefault(row["carrier_id"], []).append(row["display_name"])

        for row in connection.execute(f"SELECT carrier_id, COUNT(*) AS waiting FROM link_requests WHERE decision IS NULL "
                                      f"AND carrier_id IN ({marks}) GROUP BY carrier_id", ids):
            pending[row["carrier_id"]] = row["waiting"]

        for carrier in carriers:
            carrier["document_names"] = names.get(carrier["id"], [])
            carrier["pending_requests"] = pending.get(carrier["id"], 0)

    def name_links(self, carrier_id=None):
        """The approved links in force: document name, carrier, the request that made it, who proposed and approved it."""

        query = ("SELECT n.name_key, n.display_name, n.carrier_id, n.request_id, n.linked_at, r.proposed_by, r.decided_by, "
                 "c.legal_name FROM carrier_names n JOIN carriers c ON c.id = n.carrier_id "
                 "LEFT JOIN link_requests r ON r.id = n.request_id")
        params = ()

        if carrier_id is not None:
            query += " WHERE n.carrier_id = ?"
            params = (carrier_id,)

        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(query + " ORDER BY n.display_name COLLATE NOCASE", params)]

    def link_queue(self, state="pending", carrier_id=None, offset=0, limit=50):
        """
        A page of link requests, newest first: {"requests", "total"}. state:
        pending, decided or all. carrier_id: the requests that concern that
        carrier (proposed for it, or unlinking one of its names). Each request
        names the proposed carrier and the carrier the name is linked to now.
        """

        if state not in ("pending", "decided", "all"):
            raise RegistryError("The state is pending, decided or all.")

        where, params = [], []

        if state == "pending":
            where.append("r.decision IS NULL")
        elif state == "decided":
            where.append("r.decision IS NOT NULL")

        if carrier_id is not None:
            where.append("(r.carrier_id = ? OR n.carrier_id = ?)")
            params += [carrier_id, carrier_id]

        condition = f" WHERE {' AND '.join(where)}" if where else ""
        base = (" FROM link_requests r LEFT JOIN carrier_names n ON n.name_key = r.name_key "
                "LEFT JOIN carriers p ON p.id = r.carrier_id LEFT JOIN carriers c ON c.id = n.carrier_id")

        with closing(self.connect()) as connection:
            total = connection.execute(f"SELECT COUNT(*){base}{condition}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT r.*, p.legal_name AS carrier_legal_name, n.carrier_id AS current_carrier_id, "
                f"c.legal_name AS current_carrier_legal_name{base}{condition} ORDER BY r.id DESC LIMIT ? OFFSET ?",
                (*params, int(limit), int(offset))).fetchall()

        return {"requests": [dict(row) for row in rows], "total": total}

    def pending_by_name(self):
        """{document name key: the request waiting for a decision}."""

        with closing(self.connect()) as connection:
            return {row["name_key"]: dict(row) for row in connection.execute(
                "SELECT r.*, p.legal_name AS carrier_legal_name FROM link_requests r "
                "LEFT JOIN carriers p ON p.id = r.carrier_id WHERE r.decision IS NULL")}

    def carrier_events(self, carrier_id, offset=0, limit=50):
        """
        A carrier's history, newest first: {"events", "total"}: its own changes,
        the link requests proposed for it, and the requests that moved or unlinked
        one of its names.
        """

        condition = (" WHERE carrier_id = ? OR request_id IN (SELECT id FROM link_requests WHERE carrier_id = ?) "
                     "OR json_extract(before, '$.carrier_id') = ?")
        params = (carrier_id, carrier_id, carrier_id)

        with closing(self.connect()) as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM carrier_events{condition}", params).fetchone()[0]
            rows = connection.execute(f"SELECT * FROM carrier_events{condition} ORDER BY id DESC LIMIT ? OFFSET ?",
                                      (*params, int(limit), int(offset))).fetchall()

        return {"events": [{**dict(row), "before": json.loads(row["before"]) if row["before"] else None,
                            "after": json.loads(row["after"]) if row["after"] else None} for row in rows],
                "total": total}

    def events_after(self, after_id, limit=200):
        """The events after id after_id, oldest first (the webhook collector reads committed events this way)."""

        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT * FROM carrier_events WHERE id > ? ORDER BY id LIMIT ?",
                                      (int(after_id), int(limit))).fetchall()

        return [{**dict(row), "before": json.loads(row["before"]) if row["before"] else None,
                 "after": json.loads(row["after"]) if row["after"] else None} for row in rows]

    def last_event_id(self):

        with closing(self.connect()) as connection:
            return connection.execute("SELECT coalesce(max(id), 0) FROM carrier_events").fetchone()[0]

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
            row["status"] = row["status"] or "active"
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
