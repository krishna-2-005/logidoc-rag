"""
TMS shipments in the audit (docs/TMS_SHIPMENT_AUDIT.md).

A shipment record a TMS integration imported (integration_framework,
ExternalRecordStore) is matched to the audited shipment its reference names
(identifier_key(reference) == the shipment key). Only enabled integrations are
read, each record where it was imported (a sandbox's records stay in the sandbox
folder). Nothing of a record is written into the documents, the extraction store
or a recorded audit except as a recorded input beside the value it stands for.

What is compared (TMS field / the documents' value)

    expected_pieces  pieces         / BOL Number of Pieces        quantity audit
    actual_date      delivery_date  / POD Delivery Date           delivery audit
                     (only once the TMS says the shipment was delivered)
    pickup_date      pickup_date    / the BOL's pickup date       cross-check
    carrier          carrier_name   / the BOL's carrier           cross-check
    origin           origin         / the BOL's origin            cross-check
    destination      destination    / the BOL's destination       cross-check
    agreed_rate      freight_charge / the rate confirmation's agreed rate, with
                                      the currencies              cross-check

States of a comparison

    agrees             the same value
    not_compared       the TMS states nothing (or the shipment is not delivered)
    conflict           the values differ and nobody has decided: OPEN
    document_missing   only the TMS states it (the documents' value is missing,
                       unreadable or conflicting): OPEN
    resolved_document  a person kept the documents' value
    resolved_tms       a person chose the TMS value: the audit reads it (only for
                       expected_pieces and actual_date, the fields a calculation
                       consumes)
    protected          an approved reviewer correction is in force: the TMS value
                       is shown, never used, and cannot be chosen

Rules

    - An open conflict changes no result: the audit keeps reading the documents,
      and the field carries the warning tms_conflict until someone decides.
    - A resolution applies only while the TMS record, its value and the
      documents' value are the ones it was made on. A sync that brings another
      value, or a changed document, reopens the conflict (the old resolution
      stays in the history).
    - Correction protection: an approved correction always wins over a TMS
      value; a TMS value someone corrected by hand in LogiDoc's copy of the
      record (an override, one person) can be shown but never chosen, so the
      two-person rule of corrections cannot be bypassed through the TMS.
    - Two TMS records naming one shipment: nothing is compared or chosen until
      the duplicate is resolved in the TMS.
    - Deciding needs extraction.approve and a signed-in account; a reason is
      required; resolutions are append-only (the integration store's
      tms_resolutions table).
"""

import json
import logging
from contextlib import closing
from dataclasses import dataclass, field

import authz
from integration_store import dumps, loads, text_time, utc_now
from shipment_identity import identifier_key

log = logging.getLogger("logidoc.tms_audit")

MIN_REASON = 10
MAX_REASON = 1000

# name, label, TMS field, kind, consumed by a calculation (the audit field it stands for)
COMPARED = (
    ("expected_pieces", "Expected pieces", "pieces", "count", True),
    ("actual_date", "Actual delivery date", "delivery_date", "date", True),
    ("pickup_date", "Pickup date", "pickup_date", "date", False),
    ("carrier", "Carrier", "carrier_name", "text", False),
    ("origin", "Origin", "origin", "text", False),
    ("destination", "Destination", "destination", "text", False),
    ("agreed_rate", "Agreed freight charge", "freight_charge", "money", False),
)

CONSUMED = tuple(name for name, _, _, _, consumed in COMPARED if consumed)
FIELD_NAMES = tuple(name for name, *_ in COMPARED)

# A cross-check field: (document type, extracted field) in the freight audit's inputs
DOCUMENT_FIELD = {
    "pickup_date": ("BOL", "pickup_date"),
    "carrier": ("BOL", "carrier"),
    "origin": ("BOL", "origin"),
    "destination": ("BOL", "destination"),
    "agreed_rate": ("RATE_CONFIRMATION", "agreed_rate"),
}

OPEN_STATES = ("conflict", "document_missing")


class TmsRefused(ValueError):
    """The resolution cannot be made (HTTP 409): the state changed, it is protected, or nothing is open."""


class TmsInvalid(ValueError):
    """The request is not valid (HTTP 422)."""


# ============================================================
# VALUES
# ============================================================

def normalized(kind, value):
    """A value in the form two sources are compared in (None stays None)."""

    if value is None:
        return None

    if kind == "count":
        return int(value)

    if kind == "money":
        return round(float(value), 2)

    if kind == "date":
        return str(value)[:10]

    return " ".join(str(value).split()).casefold()


def shown(value):
    """A value as JSON can hold it."""

    return json.loads(json.dumps(value, default=str)) if value is not None else None


# ============================================================
# WHAT IS READ
# ============================================================

@dataclass
class TmsData:
    """Everything the audit needs from the TMS integrations, read once per audit."""

    matches: dict = field(default_factory=dict)        # shipment_key -> [match]
    resolutions: dict = field(default_factory=dict)    # shipment_key -> [resolution], oldest first
    unavailable: list = field(default_factory=list)    # integrations whose records could not be read

    def match_of(self, key):
        """The one TMS record of a shipment, or None (none, or ambiguous)."""

        found = self.matches.get(key) or []

        return found[0] if len(found) == 1 else None

    def applicable(self, key, name, record_id, tms_value, document_value):
        """The latest resolution of this field made on exactly these values, or None."""

        found = None

        for item in self.resolutions.get(key) or []:
            if (item["field"] == name and item["record_id"] == record_id and item["tms_value"] == shown(tms_value)
                    and item["document_value"] == shown(document_value)):
                found = item

        return found


def match_view(integration, record):
    """A matched TMS record with its provenance (no secret, no credential)."""

    data = record["data"] or {}

    return {
        "integration_id": integration["id"], "integration_name": integration["name"], "provider": integration["provider"],
        "record_id": record["id"], "external_id": record["external_id"], "reference": data.get("reference"),
        "status": data.get("status"), "source_updated_at": record["source_updated_at"], "imported_at": record["updated_at"],
        "first_run_id": record["first_run_id"], "last_run_id": record["last_run_id"], "content_hash": record["content_hash"],
        "corrected_fields": list(record.get("corrected_fields") or []), "data": data,
    }


class TmsSource:
    """
    Reads the enabled TMS integrations for the audit. service_of() gives the
    integration service, or None when no integration store exists (then there
    is nothing to read and nothing is created).
    """

    def __init__(self, service_of):

        self.service_of = service_of

    def service(self):

        try:
            return self.service_of()
        except Exception:  # noqa: BLE001 - the audit must not fail because the integration runtime cannot start
            log.exception("The integration service could not be opened for the audit")
            return None

    def integrations(self, service):

        store = service.store()

        if store is None:
            return store, []

        with store.read() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT id, name, provider, enabled, updated_at FROM integrations WHERE enabled = 1 ORDER BY id")]

        return store, rows

    def token(self):
        """Changes whenever a TMS shipment record, an enabled integration or a resolution does (audit cache key)."""

        service = self.service()

        if service is None:
            return ()

        try:
            store, rows = self.integrations(service)

            if store is None:
                return ()

            parts = []

            for row in rows:
                records = self.records_store(service, row)
                if records is None:
                    parts.append((row["id"], row["updated_at"], None))
                    continue
                # Every import that changes a record and every correction by hand appends a version: its id
                # changes with each of them (a change time has whole seconds, and two can fall in one second)
                with closing(records.connect()) as connection:
                    count, newest = connection.execute(
                        "SELECT COUNT(DISTINCT r.id), MAX(v.id) FROM external_records r "
                        "LEFT JOIN external_record_versions v ON v.record_id = r.id "
                        "WHERE r.integration_id = ? AND r.entity = 'shipments'", (row["id"],)).fetchone()
                parts.append((row["id"], row["updated_at"], count, newest))

            with store.read() as connection:
                newest_resolution = connection.execute("SELECT COALESCE(MAX(id), 0) FROM tms_resolutions").fetchone()[0]

            return tuple(parts) + (("resolutions", newest_resolution),)
        except Exception:  # noqa: BLE001 - a broken store is reported by load(); the key must still change
            log.exception("The TMS records could not be checked for changes")
            return (("unavailable", text_time(utc_now())),)

    @staticmethod
    def records_store(service, integration):

        try:
            return service.records_store({"provider": integration["provider"]}, create=False)
        except Exception:  # noqa: BLE001 - e.g. the sandbox folder is no longer configured
            return None

    def load(self):
        """TmsData: every enabled integration's shipment records by the shipment their reference names."""

        data = TmsData()
        service = self.service()

        if service is None:
            return data

        try:
            store, rows = self.integrations(service)
        except Exception:  # noqa: BLE001
            log.exception("The integration store could not be read for the audit")
            data.unavailable.append({"integration_id": None, "reason": "The integration store could not be read."})
            return data

        if store is None:
            return data

        for row in rows:
            records = self.records_store(service, row)

            if records is None:
                data.unavailable.append({"integration_id": row["id"], "reason": "Its imported records cannot be opened."})
                continue

            try:
                found = records.records(row["id"], entity="shipments", limit=1_000_000)["records"]
            except Exception:  # noqa: BLE001
                log.exception("The records of integration %s could not be read", row["id"])
                data.unavailable.append({"integration_id": row["id"], "reason": "Its imported records could not be read."})
                continue

            for record in found:
                key = identifier_key((record["data"] or {}).get("reference"))
                if key:
                    data.matches.setdefault(key, []).append(match_view(row, record))

        with store.read() as connection:
            for item in connection.execute("SELECT * FROM tms_resolutions ORDER BY id"):
                resolution = dict(item)
                resolution["tms_value"] = loads(resolution["tms_value"])
                resolution["document_value"] = loads(resolution["document_value"])
                data.resolutions.setdefault(resolution["shipment_key"], []).append(resolution)

        return data

    # --------------------------------------------------------
    # Deciding
    # --------------------------------------------------------

    def resolve(self, principal, shipment, name, choice, reason, record_id, tms_value, document_value):
        """
        Records a person's decision on one open comparison of a shipment (the
        snapshot's item, as the audit computed it now). tms_value and
        document_value are the values the person was shown: when either changed
        since, TmsRefused (nothing is recorded). Returns the stored resolution.
        """

        authz.require(principal, authz.EXTRACTION_APPROVE)

        if not principal.authenticated:
            raise authz.PermissionDenied("A TMS conflict can only be resolved by a signed-in account.")

        if name not in FIELD_NAMES:
            raise TmsInvalid(f"{name} is not a compared field (one of: {', '.join(FIELD_NAMES)}).")

        if choice not in ("document", "tms"):
            raise TmsInvalid("The choice must be document or tms.")

        reason = " ".join((reason or "").split())

        if len(reason) < MIN_REASON:
            raise TmsInvalid(f"Write why (at least {MIN_REASON} characters).")

        if len(reason) > MAX_REASON:
            raise TmsInvalid(f"The reason is longer than {MAX_REASON} characters.")

        view = shipment.get("tms") or {}

        if view.get("ambiguous"):
            raise TmsRefused("Several TMS records name this shipment; resolve the duplicate in the TMS first.")

        row = next((item for item in view.get("comparisons") or [] if item["field"] == name), None)

        if row is None or view.get("match") is None:
            raise TmsRefused("No TMS record is matched to this shipment.")

        if row["state"] == "protected":
            raise TmsRefused("An approved correction is in force for this field: a TMS value never replaces it. "
                             "Propose a correction to change it.")

        if row["state"] not in OPEN_STATES:
            raise TmsRefused(f"Nothing to resolve: the comparison is {row['state'].replace('_', ' ')}.")

        match = view["match"]

        if record_id != match["record_id"] or shown(tms_value) != row["tms_value"] or shown(document_value) != row["document_value"]:
            raise TmsRefused("The TMS record or the documents changed since this comparison was shown. Reload it and decide again.")

        if choice == "tms" and not row["consumed"]:
            raise TmsInvalid("No calculation reads this field: the TMS value can only be noted, not chosen. Keep the documents' value.")

        if choice == "tms" and row["tms_corrected_by_hand"]:
            raise TmsRefused("This TMS value was corrected by hand in LogiDoc, not received from the TMS: it cannot replace "
                             "the documents' value. Propose a correction of the document instead (it needs a second person).")

        service = self.service()
        store = service.store() if service is not None else None

        if store is None:
            raise TmsRefused("The integration store is not available.")

        at = text_time(utc_now())

        with store.write() as connection:
            # Someone else decided the same comparison meanwhile: not twice
            for item in connection.execute("SELECT * FROM tms_resolutions WHERE shipment_key = ? AND field = ? ORDER BY id",
                                           (shipment["shipment_key"], name)):
                if (item["record_id"] == match["record_id"] and loads(item["tms_value"]) == row["tms_value"]
                        and loads(item["document_value"]) == row["document_value"]):
                    raise TmsRefused(f"This comparison was already resolved by {item['actor']} (resolution {item['id']}).")

            cursor = connection.execute(
                "INSERT INTO tms_resolutions (shipment_key, field, choice, integration_id, record_id, external_id, "
                "source_updated_at, tms_value, document_value, reason, actor, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (shipment["shipment_key"], name, choice, match["integration_id"], match["record_id"], match["external_id"],
                 match["source_updated_at"], dumps(row["tms_value"]), dumps(row["document_value"]), reason,
                 principal.username, at))

        log.info("TMS resolution %s: %s of %s kept from the %s", cursor.lastrowid, name, shipment["shipment_key"],
                 "TMS" if choice == "tms" else "documents", extra={"operation": "tms_resolution", "outcome": choice})

        return {"id": cursor.lastrowid, "shipment_key": shipment["shipment_key"], "shipment_id": shipment["shipment_id"],
                "field": name, "choice": choice, "integration_id": match["integration_id"], "record_id": match["record_id"],
                "external_id": match["external_id"], "source_updated_at": match["source_updated_at"],
                "tms_value": row["tms_value"], "document_value": row["document_value"], "reason": reason,
                "actor": principal.username, "at": at}


def resolutions_of(source, shipment_key):
    """Every resolution recorded for a shipment, newest first (the history, including those that no longer apply)."""

    data = source.load() if source is not None else TmsData()

    return list(reversed(data.resolutions.get(shipment_key) or []))


# ============================================================
# IN THE AUDIT
# ============================================================

def tms_value_of(match, name, tms_field, kind):

    data = match["data"]

    # A delivery date is an actual delivery only once the TMS says the shipment was delivered
    if name == "actual_date" and data.get("status") != "delivered":
        return None

    return normalized(kind, data.get(tms_field))


def document_side(item, name, kind):
    """
    (state, value, source, text) of the documents' value of a compared field,
    as the audit read it: value in the compared form, text as written.
    """

    if name in CONSUMED:
        entry = next((f for f in (item.get("inputs") or {}).get("fields") or [] if f["field"] == name), None)

        if entry is None:
            return "missing", None, None, None

        reading = entry.get("document_reading") or {}
        state = reading.get("state")
        value = reading.get("value") if state == "extracted" else None

        return state, normalized(kind, value), entry.get("source"), shown(value)

    document_type, extracted = DOCUMENT_FIELD[name]
    values = {}

    for document in (item.get("freight_audit") or {}).get("inputs") or []:
        if document.get("document_type") != document_type:
            continue
        found = (document.get("fields") or {}).get(extracted) or {}
        if found.get("state") == "EXTRACTED" and found.get("value") is not None:
            values.setdefault(normalized(kind, found.get("value")), shown(found.get("value")))

    if not values:
        return "missing", None, None, None

    if len(values) > 1:
        return "conflicting", None, None, None

    value, text = next(iter(values.items()))

    return "extracted", value, "document", text


def plan_overrides(data, key, readings):
    """
    {audit field: resolution} of the consumed fields whose TMS value a person
    chose and whose resolution still applies. readings: {audit field: (state,
    normalized document value)} as the documents read now (no correction).
    """

    match = data.match_of(key)

    if match is None:
        return {}

    chosen = {}

    for name, _, tms_field, kind, consumed in COMPARED:
        if not consumed:
            continue

        tms_value = tms_value_of(match, name, tms_field, kind)
        state, document_value = readings.get(name, ("missing", None))

        # A value corrected by hand in LogiDoc's copy of the record never reaches a calculation
        if tms_value is None or tms_field in match["corrected_fields"]:
            continue

        resolution = data.applicable(key, name, match["record_id"], tms_value, document_value if state == "extracted" else None)

        if resolution is not None and resolution["choice"] == "tms":
            chosen[name] = {**resolution, "tms_value": tms_value}

    return chosen


def compare(item, data):
    """
    item["tms"]: the TMS record matched to an audited shipment and how each
    compared field stands, with the resolution that applies; the consumed
    fields' inputs carry it too (and the warning tms_conflict while open).
    """

    key = item["shipment_key"]
    found = data.matches.get(key) or []

    if not found:
        item["tms"] = None
        return

    if len(found) > 1:
        item["tms"] = {"match": None, "matches": found, "ambiguous": True, "comparisons": [], "open_conflicts": 0,
                       "applied_resolution_ids": []}
        return

    match = found[0]
    rows = []
    inputs = {entry["field"]: entry for entry in (item.get("inputs") or {}).get("fields") or []}

    for name, label, tms_field, kind, consumed in COMPARED:
        tms_value = tms_value_of(match, name, tms_field, kind)
        state, document_value, document_source, document_text = document_side(item, name, kind)
        by_hand = tms_field in match["corrected_fields"]
        entry = inputs.get(name)
        protected = consumed and entry is not None and entry["source"] == "approved_correction"
        resolution = None
        currency = None

        if name == "agreed_rate":
            currency = {"tms": match["data"].get("currency"), "documents": rate_currency(item)}

        if tms_value is None:
            status = "not_compared"
        elif protected:
            status = "protected"
        else:
            resolution = data.applicable(key, name, match["record_id"], tms_value, document_value if state == "extracted" else None)
            differs = state != "extracted" or document_value != tms_value or (
                currency is not None and currency["documents"] and currency["tms"] and currency["documents"] != currency["tms"])

            if not differs:
                status, resolution = "agrees", None
            elif resolution is not None:
                status = "resolved_tms" if resolution["choice"] == "tms" else "resolved_document"
            else:
                status = "conflict" if state == "extracted" else "document_missing"

        row = {
            "field": name, "label": label, "consumed": consumed, "tms_field": tms_field,
            "tms_value": shown(tms_value), "document_value": shown(document_value), "document_state": state,
            # As written (the values above are in the compared form, which a decision must name)
            "tms_text": shown(match["data"].get(tms_field)) if tms_value is not None else None, "document_text": document_text,
            "document_source": document_source, "state": status, "open": status in OPEN_STATES,
            "tms_corrected_by_hand": by_hand,
            "choices": ([] if status not in OPEN_STATES else
                        ["document"] + (["tms"] if consumed and not by_hand else [])),
            "resolution": public_resolution(resolution), "currency": currency,
            "protected_by": entry["correction_id"] if protected else None,
        }
        rows.append(row)

        if entry is not None:
            entry["tms"] = {
                "integration_id": match["integration_id"], "integration_name": match["integration_name"],
                "record_id": match["record_id"], "external_id": match["external_id"],
                "source_updated_at": match["source_updated_at"], "last_run_id": match["last_run_id"],
                "value": row["tms_value"], "state": status, "corrected_by_hand": by_hand,
                "resolution_id": resolution["id"] if resolution is not None else None,
            }

            if row["open"] and "tms_conflict" not in entry["warnings"]:
                entry["warnings"].append("tms_conflict")
                if entry["validation"] == "valid":
                    entry["validation"] = "warning"

    applied = sorted((item.get("inputs") or {}).get("applied_tms_resolution_ids") or [])

    item["tms"] = {"match": {k: v for k, v in match.items() if k != "content_hash"}, "matches": [], "ambiguous": False,
                   "comparisons": rows, "open_conflicts": sum(1 for row in rows if row["open"]),
                   "applied_resolution_ids": applied}


def rate_currency(item):

    for document in (item.get("freight_audit") or {}).get("inputs") or []:
        if document.get("document_type") == "RATE_CONFIRMATION":
            found = (document.get("fields") or {}).get("currency") or {}
            if found.get("state") == "EXTRACTED":
                return found.get("value")

    return None


def public_resolution(resolution):

    if resolution is None:
        return None

    return {name: resolution.get(name) for name in ("id", "choice", "reason", "actor", "at", "record_id", "external_id",
                                                   "source_updated_at", "tms_value", "document_value")}


def conflicts(snapshot):
    """Every open comparison of every shipment: the queue of TMS conflicts to decide."""

    result = []

    for item in snapshot["shipments"]:
        view = item.get("tms")

        if not view:
            continue

        if view.get("ambiguous"):
            result.append({"shipment_id": item["shipment_id"], "shipment_key": item["shipment_key"], "field": None,
                           "label": "Several TMS records", "state": "ambiguous", "tms_value": None, "document_value": None,
                           "integration_name": None, "external_id": ", ".join(m["external_id"] for m in view["matches"])})
            continue

        for row in view["comparisons"]:
            if row["open"]:
                result.append({"shipment_id": item["shipment_id"], "shipment_key": item["shipment_key"],
                               "field": row["field"], "label": row["label"], "state": row["state"],
                               "tms_value": row["tms_value"], "document_value": row["document_value"],
                               "integration_name": view["match"]["integration_name"],
                               "external_id": view["match"]["external_id"]})

    return result
