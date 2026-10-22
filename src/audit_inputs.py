"""
LogiDoc-RAG audit inputs

Which values an audit actually consumed, and where each came from. Two jobs:

    build_overrides   turns the APPROVED reviewer corrections of a shipment into
                      the FieldValues the calculations read in place of the
                      document's own value (shipment_calculations.Records)
    build_inputs      records, for one shipment, every audit field with its
                      effective value, its source (legacy reader or approved
                      correction), the correction and extraction behind it, the
                      document and page (None when unknown), validation and
                      warnings. History stores it per run.

Source-selection policy (see docs/CORRECTION_AWARE_AUDIT.md)

    1. an approved correction that applies is the value
    2. otherwise the legacy reader's value, exactly as before; structured
       extraction without a correction is shown beside it as a cross-check
       ("structured", with "agrees") and is never preferred or mixed in
    3. missing, conflicting and unparseable values stay unresolved (value None)

A correction applies only while its document is unchanged (the file's SHA-256 is
still the extraction's) and belongs to the shipment audited. Among approvals for
the same document and field the latest decision wins (the others are listed as
superseded); approvals on two different documents that disagree make the field
unresolved. Pending, rejected and legacy corrections never reach this module.

Nothing here stores document text: values are typed, and reasons are codes.
"""

import hashlib
from datetime import date

from shipment_calculations import FIELDS, FieldValue, Records, read_document_field


CALCULATION_VERSION = "2"

# (document type, extraction field) -> the audit field a calculation reads
TARGETS = {
    ("BOL", "piece_count"): "expected_pieces",
    ("POD", "delivered_pieces"): "delivered_pieces",
    ("BOL", "expected_delivery_date"): "expected_date",
    ("POD", "delivery_date"): "actual_date",
    ("INVOICE", "invoice_total"): "invoice_total",
}

AUDIT_FIELDS = tuple(TARGETS.values())

# The extraction field behind an audit field, and the calculations that consume it
SOURCE_FIELD = {audit: (doc_type, field) for (doc_type, field), audit in TARGETS.items()}

CONSUMERS = {
    "expected_pieces": ("quantity",),
    "delivered_pieces": ("quantity",),
    "expected_date": ("delivery",),
    "actual_date": ("delivery",),
    "invoice_total": ("invoice", "financial_exposure"),
}

LEGACY = "legacy_reader"
APPROVED = "approved_correction"


def file_hashes(folder):
    """{file name: SHA-256} of the PDFs of a folder."""

    from pathlib import Path

    hashes = {}

    for path in sorted(Path(folder).glob("*.pdf")):
        try:
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue

    return hashes


def shown(kind, value):
    """How a typed value is written the way the documents write it."""

    if kind == "date":
        return f"{value:%B} {value.day}, {value.year}"

    if kind == "money":
        return f"{value:,.2f}"

    return str(value)


def typed(kind, stored):
    """The value a calculation reads, from the stored JSON value."""

    if kind == "date":
        return date.fromisoformat(stored)

    if kind == "money":
        return round(float(stored), 2)

    return int(stored)


def json_value(value):
    return value.isoformat() if isinstance(value, date) else value


def override_value(audit_field, correction, shipment_id):
    kind = FIELDS[audit_field][2]
    value = typed(kind, correction["value"])
    raw = shown(kind, value)
    doc_type = correction["document_type"]

    record = {
        "document": "", "normalized": "", "raw": "", "type": doc_type.lower(),
        "metadata": {"source": correction["filename"], "document_type": doc_type, "shipment_id": shipment_id,
                     "value_origin": APPROVED, "correction_id": correction["correction_id"]},
    }

    return FieldValue(value=value, text=f"{raw} (approved correction {correction['correction_id']})", raw=raw,
                      record=record, start=0)


def conflict_value(audit_field, corrections, shipment_id):
    """An unresolved value: approved corrections on different documents disagree."""

    first = corrections[0]
    record = {"document": "", "normalized": "", "raw": "", "type": first["document_type"].lower(),
              "metadata": {"source": first["filename"], "document_type": first["document_type"], "shipment_id": shipment_id,
                           "value_origin": APPROVED, "correction_id": None}}

    return FieldValue(value=None, text="approved corrections disagree", raw="approved corrections disagree", record=record, start=0)


def build_overrides(corrections, shipment_id, hashes):
    """
    (overrides, plan): overrides is {audit field: FieldValue} for
    shipment_calculations.Records; plan is {audit_field: {"winner": correction or
    None, "superseded": [ids], "conflict": [ids]}} plus "ignored": [{correction_id,
    reason}]. corrections: ExtractionStore.approved_corrections(shipment_key);
    hashes: file_hashes(raw folder).
    """

    ignored, candidates = [], {}

    for correction in corrections:
        audit_field = TARGETS.get((correction["document_type"], correction["field_name"]))

        if audit_field is None:
            continue

        if hashes.get(correction["filename"]) != correction["sha256"]:
            ignored.append({"correction_id": correction["correction_id"], "reason": "document_changed_or_removed"})
            continue

        candidates.setdefault(audit_field, []).append(correction)

    overrides, plan = {}, {"ignored": ignored}

    for audit_field, items in candidates.items():
        latest = {}

        for correction in items:                      # ordered by decision: the last of a document wins
            latest[correction["sha256"]] = correction

        superseded = [c["correction_id"] for c in items if latest[c["sha256"]] is not c]
        winners = list(latest.values())

        if len({repr(c["value"]) for c in winners}) > 1:
            overrides[audit_field] = conflict_value(audit_field, winners, shipment_id)
            plan[audit_field] = {"winner": None, "superseded": superseded, "conflict": sorted(c["correction_id"] for c in winners)}
        else:
            winner = max(winners, key=lambda c: c["decision_id"])
            overrides[audit_field] = override_value(audit_field, winner, shipment_id)
            plan[audit_field] = {"winner": winner, "superseded": superseded, "conflict": []}

    return overrides, plan


def reading_state(values):
    """(state, typed value) of the document's own readings of a field; no text."""

    if not values:
        return "missing", None

    if any(item.value is None for item in values):
        return "unparseable", None

    if len({item.value for item in values}) > 1:
        return "conflicting", None

    return "extracted", values[0].value


def metadata_of(item):
    return (item.record.get("metadata") or {}) if item is not None else {}


def mark_corrections_unavailable(inputs):
    """
    Records that the extraction store could not be read, so approved corrections
    (if any) are not reflected in this audit. Visible in the inputs, in the
    recorded run and in the assistant's answer; never silent.
    """

    inputs["corrections_unavailable"] = True
    inputs["ignored"] = [*inputs.get("ignored", []), {"correction_id": None, "reason": "correction_store_unavailable"}]

    for entry in inputs["fields"]:
        if "correction_store_unavailable" not in entry["warnings"]:
            entry["warnings"].append("correction_store_unavailable")

        if entry["validation"] == "valid":
            entry["validation"] = "warning"


def build_inputs(shipment_key, shipment_id, chunks, plan, extractions=None):
    """
    The inputs of one shipment's audit: every audit field with where its value
    came from. chunks: the shipment's chunks (what the calculations read);
    plan: build_overrides' plan; extractions: {file name: stored extraction row
    with payload} of the documents as they are now, for the cross-check.
    """

    records = Records(
        {"document": c["text"], "metadata": c["metadata"], "type": str(c["metadata"].get("document_type") or "").lower(),
         "normalized": " ".join(str(c["text"]).split()), "raw": c["text"]}
        for c in chunks
    )
    plan = plan or {}
    fields, applied = [], []

    for audit_field in AUDIT_FIELDS:
        kind = FIELDS[audit_field][2]
        doc_type, extraction_field = SOURCE_FIELD[audit_field]
        values = read_document_field(audit_field, records)
        state, document_value = reading_state(values)
        first = values[0] if values else None
        source_name = metadata_of(first).get("source")

        entry = {
            "field": audit_field, "consumed_by": list(CONSUMERS[audit_field]), "source": LEGACY, "resolved": state == "extracted",
            "state": state, "value": json_value(document_value), "document": source_name, "page": metadata_of(first).get("page"),
            "validation": "valid" if state == "extracted" else "invalid", "warnings": [],
            "correction_id": None, "extraction_id": None, "extractor_version": None,
            "document_reading": {"state": state, "value": json_value(document_value)},
            "structured": None, "superseded_correction_ids": [],
        }

        row = (extractions or {}).get(source_name) if source_name else None

        if row is None and extractions:
            row = next((r for r in extractions.values() if r["document_type"] == doc_type), None)

        if row is not None:
            item = row["payload"]["fields"].get(extraction_field) or {}
            structured_state, structured_value = item.get("state"), item.get("value")
            agrees = None

            if structured_state == "EXTRACTED" and state == "extracted":
                agrees = structured_value == json_value(document_value)

            entry["structured"] = {"extraction_id": row["id"], "extractor_version": row["extractor_version"],
                                   "state": structured_state, "value": structured_value, "agrees": agrees}
            entry["extraction_id"], entry["extractor_version"] = row["id"], row["extractor_version"]

            if agrees is False:
                entry["warnings"].append("structured_extraction_differs")
                entry["validation"] = "warning"

        choice = plan.get(audit_field)

        if choice is not None and choice["winner"] is not None:
            winner = choice["winner"]
            entry.update(source=APPROVED, resolved=True, state="extracted", value=json_value(typed(kind, winner["value"])),
                         document=winner["filename"], page=None, validation="approved", correction_id=winner["correction_id"],
                         extraction_id=winner["extraction_id"], extractor_version=winner["extractor_version"],
                         superseded_correction_ids=choice["superseded"])
            applied.append(winner["correction_id"])
        elif choice is not None:
            entry.update(source=APPROVED, resolved=False, state="conflicting", value=None, validation="invalid",
                         warnings=entry["warnings"] + ["approved_corrections_disagree"], superseded_correction_ids=choice["superseded"])
            applied.extend(choice["conflict"])

        fields.append(entry)

    return {"calculation_version": CALCULATION_VERSION, "shipment_key": shipment_key, "shipment_id": shipment_id,
            "fields": fields, "applied_correction_ids": sorted(set(applied)), "ignored": list(plan.get("ignored", []))}
