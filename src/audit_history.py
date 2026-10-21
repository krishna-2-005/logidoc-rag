"""
LogiDoc-RAG audit history

Records what the freight audit found for each shipment over time, and
compares an audit with the previous one. It is separate from the audit
itself (reconciliation.reconcile_shipment decides every status and
amount) and from the exception review states (review_store): history
only remembers results.

A snapshot holds, per shipment: the final, quantity, delivery and
invoice statuses; the relevant amounts, counts and dates; the findings
(the exceptions of audit_analytics.build_exceptions: key, category,
severity, a one-line summary, the numeric difference and a signature of
the audited values); and the state of every audited value. It never
holds document text or evidence. It also holds an input fingerprint:
a hash of the shipment's documents (audit_analytics.shipment_fingerprint),
so a changed document is visible and every snapshot says which input
version it came from.

Value states (audit_states):

    verified        the value was read and calculated (a confirmed
                    mismatch is still verified: the difference is real)
    unavailable     the value was not found in the documents
    conflicting     the documents give different values
    review_required the value could not be read or is inconsistent
    not_assessed    the shipment could not be reconciled at all

Recording is an explicit action (record_run). A run stores a snapshot
only for a shipment whose result or input changed since its latest
snapshot; otherwise the run points at the existing snapshot, so
re-running an unchanged audit never writes duplicate snapshots. Rows
are never updated or deleted (database triggers refuse it). Every run
is one transaction.

Comparing two snapshots of a shipment (compare):

    new          a finding not in the previous snapshot or earlier ones
    reappeared   a finding not in the previous snapshot but seen earlier
    persistent   in both, with the same audited values
    changed      in both, with a different difference or values
    resolved     in the previous snapshot, no longer detected, and the
                 audit behind it could be assessed now
    unverified   in the previous snapshot, no longer shown, but the
                 audit behind it now needs review or could not be
                 reconciled: not treated as resolved
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1

VERIFIED = "verified"
UNAVAILABLE = "unavailable"
CONFLICTING = "conflicting"
REVIEW_REQUIRED = "review_required"
NOT_ASSESSED = "not_assessed"

AUDITS = ("quantity", "delivery", "invoice")

# The audit a finding depends on, by the start of its key
FINDING_AUDIT = (
    ("quantity", "quantity"),
    ("delivery", "delivery"),
    ("invoice", "invoice"),
    ("review-quantity", "quantity"),
    ("review-delivery", "delivery"),
    ("review-invoice", "invoice"),
)

# Numbers and values compared between two snapshots
COMPARED_FIELDS = (
    ("final_status", "final status"),
    ("quantity_status", "quantity status"),
    ("delivery_status", "delivery status"),
    ("invoice_status", "invoice status"),
    ("bol_pieces", "BOL pieces"),
    ("pod_pieces", "POD pieces delivered"),
    ("piece_difference", "piece difference"),
    ("expected_date", "expected delivery date"),
    ("actual_date", "actual delivery date"),
    ("days_late", "days late"),
    ("invoice_total", "invoice total"),
    ("line_item_sum", "line-item sum"),
    ("invoice_difference", "invoice difference"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT NOT NULL,
    label              TEXT,
    shipments_checked  INTEGER NOT NULL,
    snapshots_written  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS shipment_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES audit_runs (id),
    shipment_key       TEXT NOT NULL,
    shipment_id        TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    input_fingerprint  TEXT NOT NULL,
    digest             TEXT NOT NULL,
    reconciled         INTEGER NOT NULL CHECK (reconciled IN (0, 1)),
    error              TEXT,
    final_status       TEXT,
    quantity_status    TEXT,
    delivery_status    TEXT,
    invoice_status     TEXT,
    bol_pieces         INTEGER,
    pod_pieces         INTEGER,
    piece_difference   INTEGER,
    expected_date      TEXT,
    actual_date        TEXT,
    days_late          INTEGER,
    invoice_total      REAL,
    line_item_sum      REAL,
    invoice_difference REAL,
    discrepancy_count  INTEGER NOT NULL,
    review_count       INTEGER NOT NULL,
    audit_states       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS snapshots_by_shipment
    ON shipment_snapshots (shipment_key, id);

CREATE TABLE IF NOT EXISTS snapshot_findings (
    snapshot_id  INTEGER NOT NULL REFERENCES shipment_snapshots (id),
    finding_key  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    category     TEXT NOT NULL,
    severity     TEXT NOT NULL,
    summary      TEXT NOT NULL,
    difference   REAL,
    unit         TEXT,
    signature    TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, finding_key)
);

CREATE TABLE IF NOT EXISTS run_shipments (
    run_id       INTEGER NOT NULL REFERENCES audit_runs (id),
    shipment_key TEXT NOT NULL,
    snapshot_id  INTEGER NOT NULL REFERENCES shipment_snapshots (id),
    PRIMARY KEY (run_id, shipment_key)
);

CREATE TRIGGER IF NOT EXISTS audit_runs_no_update
    BEFORE UPDATE ON audit_runs
    BEGIN SELECT RAISE(ABORT, 'audit history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_runs_no_delete
    BEFORE DELETE ON audit_runs
    BEGIN SELECT RAISE(ABORT, 'audit history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_update
    BEFORE UPDATE ON shipment_snapshots
    BEGIN SELECT RAISE(ABORT, 'audit history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
    BEFORE DELETE ON shipment_snapshots
    BEGIN SELECT RAISE(ABORT, 'audit history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS findings_no_update
    BEFORE UPDATE ON snapshot_findings
    BEGIN SELECT RAISE(ABORT, 'audit history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS findings_no_delete
    BEFORE DELETE ON snapshot_findings
    BEGIN SELECT RAISE(ABORT, 'audit history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS run_shipments_no_update
    BEFORE UPDATE ON run_shipments
    BEGIN SELECT RAISE(ABORT, 'audit history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS run_shipments_no_delete
    BEFORE DELETE ON run_shipments
    BEGIN SELECT RAISE(ABORT, 'audit history is append-only'); END;
"""


class HistoryError(ValueError):
    """
    The history database cannot be used (wrong schema version).
    """


def now_text():

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
# SNAPSHOT RECORDS (from an audit result; nothing is recalculated)
# ============================================================

def value_state(audit):
    """
    The state of one audited value from its reconciliation entry.
    The reason text of the reconciliation tells why it needs review:
    "was not found" is unavailable, "different values" is
    conflicting, anything else (unreadable, inconsistent) needs
    review.
    """

    if (audit or {}).get("status") != "NEEDS REVIEW":
        return {"state": VERIFIED, "reason": None}

    reason = str(audit.get("reason") or "")

    if "different values" in reason:
        state = CONFLICTING
    elif "was not found" in reason:
        state = UNAVAILABLE
    else:
        state = REVIEW_REQUIRED

    return {"state": state, "reason": reason[:300] or None}


def audit_states(result):

    if not result.get("success"):
        return {name: {"state": NOT_ASSESSED, "reason": None} for name in AUDITS}

    return {name: value_state(result.get(name)) for name in AUDITS}


def number(value):

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return value


def snapshot_record(shipment, exceptions):
    """
    The compact record of one shipment of an
    audit_analytics.build_snapshot() snapshot, with that shipment's
    exceptions (audit_analytics.build_exceptions). Values the audit
    does not have stay None; they are never turned into 0.
    """

    result = shipment["result"]
    success = bool(result.get("success"))

    quantity = (result.get("quantity") or {}) if success else {}
    delivery = (result.get("delivery") or {}) if success else {}
    invoice = (result.get("invoice") or {}) if success else {}

    record = {
        "shipment_key": shipment["shipment_key"],
        "shipment_id": shipment["shipment_id"],
        "input_fingerprint": shipment["input_fingerprint"],
        "reconciled": 1 if success else 0,
        "error": None if success else str(result.get("error") or "Reconciliation failed.")[:300],
        "final_status": result.get("final_status") if success else None,
        "quantity_status": quantity.get("status"),
        "delivery_status": delivery.get("status"),
        "invoice_status": invoice.get("status"),
        "bol_pieces": number(quantity.get("bol")),
        "pod_pieces": number(quantity.get("pod")),
        "piece_difference": number(quantity.get("difference")),
        "expected_date": delivery.get("expected"),
        "actual_date": delivery.get("actual"),
        "days_late": number(delivery.get("days_late")),
        "invoice_total": number(invoice.get("total")),
        "line_item_sum": number(invoice.get("line_item_sum")),
        "invoice_difference": number(invoice.get("difference")),
        "discrepancy_count": len(result.get("discrepancies") or []) if success else 0,
        "review_count": len(result.get("review_items") or []) if success else 0,
        "audit_states": audit_states(result),
        "findings": [
            {
                "finding_key": item["exception_key"],
                "kind": item["kind"],
                "category": item["category"],
                "severity": item["severity"],
                "summary": str(item["description"])[:400],
                "difference": number(item["difference"]),
                "unit": item.get("unit"),
                "signature": item["signature"],
            }
            for item in exceptions
            if item["shipment_key"] == shipment["shipment_key"]
        ],
    }

    record["digest"] = digest(record)

    return record


def digest(record):
    """
    A hash of everything a snapshot would store (and its input), to
    tell whether anything changed since the latest snapshot.
    """

    payload = {key: value for key, value in record.items() if key != "digest"}

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


# ============================================================
# COMPARISON
# ============================================================

def finding_audit(finding_key):
    """
    Which audit (quantity, delivery, invoice) a finding depends on;
    None for carrier and route findings, which have no audit state.
    """

    code = finding_key.split("|", 1)[-1]

    for prefix, audit in FINDING_AUDIT:
        if code == prefix:
            return audit

    return None


def compare(previous, current, earlier_keys=()):
    """
    What changed between two snapshots of one shipment (see the
    module docstring). previous may be None (a first snapshot).
    earlier_keys: finding keys seen in snapshots before previous, to
    tell a new finding from one that reappeared.
    """

    changes = {
        "first": previous is None,
        "input_changed": False,
        "status_change": None,
        "new": [], "reappeared": [], "persistent": [], "changed": [],
        "resolved": [], "unverified": [],
        "value_changes": [],
        "state_changes": [],
    }

    now = {item["finding_key"]: item for item in current["findings"]}

    if previous is None:
        changes["new"] = list(now.values())
        return changes

    before = {item["finding_key"]: item for item in previous["findings"]}

    changes["input_changed"] = previous["input_fingerprint"] != current["input_fingerprint"]

    if previous["final_status"] != current["final_status"]:
        changes["status_change"] = (previous["final_status"], current["final_status"])

    for key, item in now.items():

        if key not in before:
            changes["reappeared" if key in set(earlier_keys) else "new"].append(item)
        elif before[key]["signature"] != item["signature"]:
            changes["changed"].append(dict(item, previous_difference=before[key]["difference"]))
        else:
            changes["persistent"].append(item)

    for key, item in before.items():

        if key in now:
            continue

        audit = finding_audit(key)
        state = None if audit is None else current["audit_states"][audit]["state"]

        if not current["reconciled"] or state not in (None, VERIFIED):
            changes["unverified"].append(item)
        else:
            changes["resolved"].append(item)

    for field, label in COMPARED_FIELDS:

        if previous[field] != current[field]:
            changes["value_changes"].append({
                "field": field, "label": label,
                "previous": previous[field], "current": current[field],
            })

    for audit in AUDITS:

        old, new = previous["audit_states"][audit]["state"], current["audit_states"][audit]["state"]

        if old != new:
            changes["state_changes"].append({"audit": audit, "previous": old, "current": new})

    return changes


# ============================================================
# STORE
# ============================================================

class HistoryStore:

    def __init__(self, path):

        self.path = Path(path)
        self.initialize()

    def connect(self):

        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    def initialize(self):

        self.path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.connect()) as connection:

            with connection:

                connection.executescript(SCHEMA)

                version = connection.execute("SELECT version FROM schema_info").fetchone()

                if version is None:
                    connection.execute(
                        "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
                    )
                elif version["version"] != SCHEMA_VERSION:
                    raise HistoryError(
                        f"Audit history database {self.path} has schema version "
                        f"{version['version']}, expected {SCHEMA_VERSION}."
                    )

    # --------------------------------------------------------
    # Writing
    # --------------------------------------------------------

    def record_run(self, snapshot, exceptions, label=None, at=None):
        """
        Records an audit run for every shipment of snapshot (an
        audit_analytics.build_snapshot() result) with its exceptions
        (audit_analytics.build_exceptions). One transaction: either the
        whole run is stored or nothing is. Returns
        {"run_id", "shipments_checked", "snapshots_written",
        "written": [shipment ids], "unchanged": [shipment ids]}.
        """

        at = at or now_text()
        records = [snapshot_record(item, exceptions) for item in snapshot["shipments"]]

        written, unchanged = [], []

        with closing(self.connect()) as connection:

            with connection:

                # Which shipments changed since their latest snapshot
                latest_ids = {}

                for record in records:

                    latest = connection.execute(
                        "SELECT id, digest FROM shipment_snapshots "
                        "WHERE shipment_key = ? ORDER BY id DESC LIMIT 1",
                        (record["shipment_key"],),
                    ).fetchone()

                    if latest is not None and latest["digest"] == record["digest"]:
                        latest_ids[record["shipment_key"]] = latest["id"]
                        unchanged.append(record["shipment_id"])
                    else:
                        written.append(record["shipment_id"])

                cursor = connection.execute(
                    "INSERT INTO audit_runs (created_at, label, shipments_checked, snapshots_written) "
                    "VALUES (?, ?, ?, ?)",
                    (at, (label or "").strip() or None, len(records), len(written)),
                )

                run_id = cursor.lastrowid

                for record in records:

                    snapshot_id = latest_ids.get(record["shipment_key"])

                    if snapshot_id is None:
                        snapshot_id = self.insert_snapshot(connection, run_id, record, at)

                    connection.execute(
                        "INSERT INTO run_shipments (run_id, shipment_key, snapshot_id) VALUES (?, ?, ?)",
                        (run_id, record["shipment_key"], snapshot_id),
                    )

        return {
            "run_id": run_id,
            "shipments_checked": len(records),
            "snapshots_written": len(written),
            "written": written,
            "unchanged": unchanged,
        }

    def insert_snapshot(self, connection, run_id, record, at):

        cursor = connection.execute(
            "INSERT INTO shipment_snapshots (run_id, shipment_key, shipment_id, created_at, "
            "input_fingerprint, digest, reconciled, error, final_status, quantity_status, "
            "delivery_status, invoice_status, bol_pieces, pod_pieces, piece_difference, "
            "expected_date, actual_date, days_late, invoice_total, line_item_sum, "
            "invoice_difference, discrepancy_count, review_count, audit_states) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, record["shipment_key"], record["shipment_id"], at,
                record["input_fingerprint"], record["digest"], record["reconciled"],
                record["error"], record["final_status"], record["quantity_status"],
                record["delivery_status"], record["invoice_status"], record["bol_pieces"],
                record["pod_pieces"], record["piece_difference"], record["expected_date"],
                record["actual_date"], record["days_late"], record["invoice_total"],
                record["line_item_sum"], record["invoice_difference"],
                record["discrepancy_count"], record["review_count"],
                json.dumps(record["audit_states"], sort_keys=True),
            ),
        )

        snapshot_id = cursor.lastrowid

        for finding in record["findings"]:

            connection.execute(
                "INSERT INTO snapshot_findings (snapshot_id, finding_key, kind, category, "
                "severity, summary, difference, unit, signature) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id, finding["finding_key"], finding["kind"], finding["category"],
                    finding["severity"], finding["summary"], finding["difference"],
                    finding["unit"], finding["signature"],
                ),
            )

        return snapshot_id

    # --------------------------------------------------------
    # Reading
    # --------------------------------------------------------

    def snapshots(self, shipment_key):
        """
        Every snapshot of a shipment, oldest first, each with its
        findings and the runs that confirmed it (found it unchanged).
        """

        with closing(self.connect()) as connection:

            rows = connection.execute(
                "SELECT * FROM shipment_snapshots WHERE shipment_key = ? ORDER BY id",
                (shipment_key,),
            ).fetchall()

            result = []

            for row in rows:

                snapshot = dict(row)
                snapshot["audit_states"] = json.loads(snapshot["audit_states"])

                snapshot["findings"] = [
                    dict(item)
                    for item in connection.execute(
                        "SELECT * FROM snapshot_findings WHERE snapshot_id = ? ORDER BY finding_key",
                        (row["id"],),
                    )
                ]

                snapshot["runs"] = [
                    dict(item)
                    for item in connection.execute(
                        "SELECT audit_runs.id AS run_id, audit_runs.created_at, audit_runs.label "
                        "FROM run_shipments JOIN audit_runs ON audit_runs.id = run_shipments.run_id "
                        "WHERE run_shipments.snapshot_id = ? ORDER BY audit_runs.id",
                        (row["id"],),
                    )
                ]

                result.append(snapshot)

            return result

    def timeline(self, shipment_key):
        """
        A shipment's snapshots, newest first, each with what changed
        since the snapshot before it (compare).
        """

        snapshots = self.snapshots(shipment_key)
        entries = []
        seen = set()

        for index, snapshot in enumerate(snapshots):

            previous = snapshots[index - 1] if index else None

            comparison = compare(previous, snapshot, earlier_keys=seen)

            seen |= {item["finding_key"] for item in snapshot["findings"]}

            entries.append({"snapshot": snapshot, "changes": comparison})

        return list(reversed(entries))

    def shipments(self):
        """
        {shipment_key: shipment_id} of every recorded shipment.
        """

        with closing(self.connect()) as connection:

            return {
                row["shipment_key"]: row["shipment_id"]
                for row in connection.execute(
                    "SELECT shipment_key, shipment_id FROM shipment_snapshots GROUP BY shipment_key"
                )
            }

    def runs(self, limit=50):

        with closing(self.connect()) as connection:

            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM audit_runs ORDER BY id DESC LIMIT ?", (int(limit),)
                )
            ]
