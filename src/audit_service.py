"""
LogiDoc-RAG audit service

Loads what the dashboards need from the document store, without
Streamlit: the audit snapshot (every shipment reconciled by
reconciliation.reconcile_shipment), the document scan, the exception
register with its review state, and the index. The Streamlit app and the
HTTP API both use the same functions, so a shipment has one result
wherever it is shown.

Nothing here calculates an audit: the figures come from the existing
modules (reconciliation, audit_analytics, financial_exposure,
document_completeness, audit_explanations, trend_analytics).

Results are cached per corpus state: the raw folder (file names, sizes
and times) and the open ingestion errors. A document that is added,
replaced or removed, or an ingestion problem that appears or is
resolved, makes the next call recompute; an unchanged corpus is read
once. The cache lives in memory in one process.

Approved reviewer corrections (extraction_store) are part of what the audit
reads: an approved correction of a field a calculation consumes replaces the
legacy reader's value for that shipment (audit_inputs), and every shipment of
the snapshot carries "inputs", the record of what each calculation consumed and
from where. A pending, rejected or legacy correction never reaches the audit.
The cache key includes the approved-corrections token, so an approval makes the
next read recalculate; recording the result is a separate, explicit run
(record_corrected_run) that supersedes the shipment's earlier runs.

Paths are fixed when the service is built (config.py or the tests); no
method takes a path from a caller.
"""

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import audit_analytics as analytics
import audit_inputs
import authz
import document_extraction
import document_completeness as completeness
import financial_exposure as exposure
import ingestion_service
import metrics
import reconciliation
import tms_audit
from audit_analytics import build_exceptions, build_snapshot, source_fingerprint, with_reviews
from chunker import read_chunks
from ingestion_manager import get_destination_collection
from review_store import Policy, ReviewStore, ReviewView, now_text
from audit_history import HistoryStore
import sqlite3

from extraction_store import ExtractionStore, StoreUnavailable
from shipment_identity import identifier_key

log = logging.getLogger(__name__)

# Cache token while the extraction store is damaged or from a newer version
UNAVAILABLE_TOKEN = (-1, -1, -1)


class ReAuditRefused(ValueError):
    """A re-audit was not recorded: nothing changed, or the state changed since its preview (HTTP 409)."""


class ReAuditInvalid(ValueError):
    """A re-audit request is not valid (HTTP 422)."""


def controlled_ids(item):
    """
    (line-item correction ids, TMS resolution ids) the audit of one shipment
    reads now: what only a controlled re-audit records.
    """

    import line_item_corrections

    inputs = item.get("inputs") or {}
    line_items = {
        entry["correction_id"] for entry in (item.get("freight_audit") or {}).get("corrections_applied") or []
        if line_item_corrections.index_of(entry.get("field")) is not None
    }
    line_items |= {
        entry["correction_id"] for entry in inputs.get("fields") or []
        if entry["field"] in audit_inputs.LINE_ITEM_FIELDS and entry["source"] == audit_inputs.APPROVED and entry.get("correction_id")
    }

    return line_items, set(inputs.get("applied_tms_resolution_ids") or [])


def with_controlled(result, controlled):
    """The recompute answer, naming the shipments left to a controlled re-audit only when there are some."""

    return {**result, "re_audit_required": controlled} if controlled else result


def tms_readings(chunks):
    """{consumed TMS field: (state, value as tms_audit compares it)} as the shipment's documents read now."""

    from shipment_calculations import FIELDS

    readings = audit_inputs.document_readings(chunks, tms_audit.CONSUMED)

    return {name: (state, tms_audit.normalized(FIELDS[name][2], value) if state == "extracted" else None)
            for name, (state, value) in readings.items()}


def freight_skipped(reason):
    """A freight audit that did not run, saying why (it never blocks the reconciliation)."""

    return {"outcome": None, "explanation": [reason], "documents": {"contract": [], "execution": [], "invoice": []},
            "currency": None, "charges": [], "checks": [], "financial_impact": None, "tolerances": {}}


def add_freight_audits(snapshot, by_key, raw_dir, corrections_of, extraction_cache=None):
    """
    Adds item["freight_audit"] to every shipment of the snapshot: its carrier
    invoice matched against its rate confirmation and BOL/POD (freight_audit),
    with the shipment's approved extraction corrections applied.
    corrections_of(shipment_key) gives them, or None when the extraction
    store cannot be read (the audit then reads the documents as printed and
    says so). Each PDF is extracted once, read-only. Shipments whose
    documents print colliding IDs are not audited (their documents are never
    used together).

    extraction_cache: {(file name, SHA-256 of its content, extractor version):
    extraction} kept between audits, so a recalculation after one upload
    extracts only the new or changed files (measured: extraction was half of
    a recalculation). A file whose content changed has another hash, so it is
    never served from the cache; entries of files no longer in the folder are
    dropped. The cached extractions are only read: corrections are applied to
    copies (freight_audit.apply_corrections).
    """

    from datetime import datetime, timezone

    import freight_audit

    audited_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        policy = freight_audit.load_policy()
    except freight_audit.PolicyError as error:
        for item in snapshot["shipments"]:
            item["freight_audit"] = freight_skipped(f"The freight audit tolerances cannot be used: {error}")
        return

    extracted = {}
    cache = extraction_cache if extraction_cache is not None else {}
    kept = set()

    def extraction_of(name):
        if name not in extracted:
            path = Path(raw_dir) / str(name)
            try:
                if not path.is_file():
                    extracted[name] = None
                else:
                    key = (str(name), hashlib.sha256(path.read_bytes()).hexdigest(), document_extraction.EXTRACTOR_VERSION)
                    kept.add(key)
                    if key not in cache:
                        cache[key] = document_extraction.extract_document(path)
                    extracted[name] = cache[key]
            except Exception as error:  # one unreadable file must not hide the other shipments' audits
                log.warning("freight audit: %s could not be extracted: %s", name, error)
                extracted[name] = None
        return extracted[name]

    sources = {
        key: sorted({str((chunk.get("metadata") or {}).get("source")) for chunk in chunks if (chunk.get("metadata") or {}).get("source")})
        for key, chunks in by_key.items() if key
    }

    for item in snapshot["shipments"]:
        key = item["shipment_key"]

        if (item.get("result") or {}).get("identity_collision"):
            item["freight_audit"] = freight_skipped(item["result"]["error"])
            continue

        try:
            own = [e for e in (extraction_of(name) for name in sources.get(key, [])) if e is not None]
            others = [
                e for other, names in sources.items() if other != key
                for e in (extraction_of(name) for name in names)
                if e is not None and e.document_type == document_extraction.INVOICE
            ]
            texts = [
                {**{k: (chunk.get("metadata") or {}).get(k) for k in ("source", "page", "document_type")},
                 "text": chunk.get("text")}
                for chunk in by_key.get(key, [])
            ]

            corrections = corrections_of(key)

            result = freight_audit.audit_freight(own, texts=texts, policy=policy, other_invoices=others,
                                                 corrections=corrections or ())
            result["audited_at"] = audited_at

            if corrections is None:
                result["explanation"].append(
                    "The extraction store could not be read, so approved corrections are not applied: "
                    "the documents are read as printed."
                )

            item["freight_audit"] = result

        except Exception as error:  # reported on the shipment, never breaks the reconciliation
            log.exception("freight audit of %s failed", key)
            item["freight_audit"] = freight_skipped(f"The freight audit could not be completed: {error}")

    # Only the files of this audit stay cached
    for key in [key for key in cache if key not in kept]:
        del cache[key]


@dataclass(frozen=True)
class Locations:
    """
    Where everything lives. Built by the server, never from a request.
    """

    raw_dir: Path
    chroma_dir: Path
    review_db: Path
    history_db: Path
    register_db: Path
    quarantine_dir: Path
    archive_dir: Path
    extraction_db: Path = None
    carrier_db: Path = None
    integration_db: Path = None

    def integration_path(self):
        """The integration store (webhooks, jobs, TMS/ERP syncs): its own setting, or next to the raw folder."""

        return Path(self.integration_db) if self.integration_db else Path(self.raw_dir).parent / "integrations.sqlite3"

    def extraction_path(self):
        """The extraction store: its own setting, or next to the raw folder."""

        return Path(self.extraction_db) if self.extraction_db else Path(self.raw_dir).parent / "extractions.sqlite3"

    def carrier_registry_path(self):
        """The carrier master data: its own setting, or next to the raw folder."""

        return Path(self.carrier_db) if self.carrier_db else Path(self.raw_dir).parent / "carrier_registry.sqlite3"

    @staticmethod
    def from_config(config):

        places = config.ingestion_locations(config.RAW_DATA_DIR)

        return Locations(
            Path(config.RAW_DATA_DIR), Path(config.CHROMA_DIR), Path(config.REVIEW_DB_PATH),
            Path(config.HISTORY_DB_PATH), places["register"], places["quarantine"], places["archive"], places["extractions"],
            Path(config.CARRIER_DB_PATH) if getattr(config, "CARRIER_DB_PATH", None) else None,
            Path(config.INTEGRATION_DB_PATH) if getattr(config, "INTEGRATION_DB_PATH", None) else None,
        )


class AuditService:

    def __init__(self, locations, policy=None, reconcile=None):

        self.locations = locations
        self.policy = policy or Policy()
        self.reconcile = reconcile or reconciliation.reconcile_shipment
        self.lock = threading.RLock()
        self.cache = {}
        # Freight audit extractions by (file, content hash, extractor version): survives invalidate()
        self.extractions = {}
        # The TMS integrations' shipment records (tms_audit.TmsSource), set by the API state; None: no TMS
        self.tms_source = None
        # {exception_key: when this process first saw the audit detect it}: the creation time a read shows
        # for an exception not recorded yet, and the time it is recorded with (survives invalidate())
        self.first_seen = {}
        # What record_detections last found already recorded (the audit and the review file): nothing to redo
        self.recorded_marker = None

    # --------------------------------------------------------
    # Stores
    # --------------------------------------------------------

    def review_store(self):
        """The review store for a change: opening it creates or upgrades the database when needed."""

        return ReviewStore(self.locations.review_db, self.policy)

    def review_view(self, detect=True):
        """
        The review states as a read request sees them (review_store.ReviewView): the database is only read,
        never created, upgraded or written. detect: with the exceptions the audit detects now recorded on the
        in-memory copy, as the next sync will record them (this computes the audit when it is not cached).
        """

        if not detect:
            return ReviewView(self.locations.review_db, self.policy)

        found = build_exceptions(self.snapshot())

        return ReviewView(self.locations.review_db, self.policy, found, detected_at=self.first_seen_of(found))

    def first_seen_of(self, found):

        now = now_text()

        with self.lock:

            for item in found:
                self.first_seen.setdefault(item["exception_key"], now)

            return {item["exception_key"]: self.first_seen[item["exception_key"]] for item in found}

    def history_store(self):

        return HistoryStore(self.locations.history_db)

    def extraction_store(self):
        """
        A read-only extraction store for the audit, else None (no file, or a file from before approvals
        existed, which can hold none). A current store and a valid older one still awaiting its upgrade are
        both read: approvals must not disappear because an upgrade is pending. An unreadable, too-new or
        structurally invalid file raises StoreUnavailable. Opening it never creates, migrates or writes.
        """

        return ExtractionStore.open_for_reading(self.locations.extraction_path())

    def store_state(self):
        """{state, version, reason} of the extraction store file (extraction_store.inspect): nothing is opened for writing."""

        found = ExtractionStore.inspect(self.locations.extraction_path())

        return {"state": found.state, "version": found.version, "reason": found.reason, "migration": found.migration}

    def corrections_token(self):
        """Changes whenever a correction is approved or an extraction stored (part of the audit cache key)."""

        try:
            store = self.extraction_store()

            return store.audit_token() if store is not None else (0, 0, 0)
        except (StoreUnavailable, sqlite3.DatabaseError):
            return UNAVAILABLE_TOKEN

    def tms_token(self):
        """Changes whenever a TMS shipment record or a TMS resolution does (part of the audit cache key)."""

        return self.tms_source.token() if self.tms_source is not None else ()

    def offered_problems(self):
        """
        The unresolved ingestion errors, as dicts (see
        ingestion_service.audit_problems).
        """

        return ingestion_service.open_problems(self.locations.register_db)

    # --------------------------------------------------------
    # Cache
    # --------------------------------------------------------

    def state_key(self):
        """
        Changes whenever a document or an open ingestion error does.
        """

        problems = ingestion_service.problems_token(self.offered_problems())

        return (source_fingerprint(self.locations.raw_dir), problems)

    def cached(self, name, compute, extra=None):

        key = (self.state_key(), extra)

        with self.lock:

            entry = self.cache.get(name)

            if entry is None or entry[0] != key:
                entry = (key, compute())
                self.cache[name] = entry

            return entry[1]

    def invalidate(self):
        """
        Forget everything cached (call after a document changed through
        this process; the state key would notice anyway).
        """

        with self.lock:
            self.cache.clear()

    # --------------------------------------------------------
    # The audit
    # --------------------------------------------------------

    def snapshot(self):
        """
        Every identified shipment reconciled from one reading of the raw
        PDFs, with the unreadable files and the open ingestion errors
        held against every shipment.
        """

        def compute():

            chunks, problems = read_chunks(self.locations.raw_dir)
            problems = ingestion_service.merge_problems(problems, self.offered_problems())

            unavailable = False

            try:
                store = self.extraction_store()
            except StoreUnavailable:
                store, unavailable = None, True

            hashes = audit_inputs.file_hashes(self.locations.raw_dir) if store is not None else {}
            plans = {}
            flags = {"unavailable": unavailable}

            # Each shipment's approved corrections are read once, and that one list is
            # used by both the reconciliation and the freight audit: a decision committed
            # in between must never reach one of them and not the other (a torn audit).
            # None: the store could not be read.
            approved = {}

            def approved_of(key):
                if key not in approved:
                    try:
                        approved[key] = store.approved_corrections(key) if store is not None else None
                    except sqlite3.DatabaseError:
                        approved[key] = None
                return approved[key]

            by_key = {}

            for chunk in chunks:
                by_key.setdefault(identifier_key((chunk.get("metadata") or {}).get("shipment_id")), []).append(chunk)

            # The TMS shipment records and the decisions made on them, read once for the whole audit
            tms = self.tms_source.load() if self.tms_source is not None else None

            def reconcile(shipment_id, chunks=None, **kwargs):

                # Only approved corrections of this shipment's own documents; with
                # none, the calculation is called exactly as it always was
                key = identifier_key(shipment_id)
                overrides, plan = {}, {}

                if store is not None:
                    corrections = approved_of(key)

                    if corrections is None:
                        flags["unavailable"] = True
                    else:
                        overrides, plan = audit_inputs.build_overrides(corrections, shipment_id, hashes)

                # A TMS value a person chose, never over an approved correction (tms_audit)
                if tms is not None and tms.match_of(key) is not None:
                    chosen = tms_audit.plan_overrides(tms, key, tms_readings(by_key.get(key, [])))

                    for name, resolution in chosen.items():
                        if name not in overrides and name not in plan:
                            overrides[name] = audit_inputs.tms_override(name, resolution, shipment_id)
                            plan.setdefault("tms", {})[name] = resolution

                plans[key] = plan

                if overrides:
                    kwargs["overrides"] = overrides

                return self.reconcile(shipment_id, chunks=chunks, **kwargs)

            snapshot = build_snapshot(chunks, reconcile, unreadable_files=problems)

            for item in snapshot["shipments"]:
                key = item["shipment_key"]
                try:
                    extractions = store.current_extractions(key, hashes) if store is not None else {}
                except sqlite3.DatabaseError:
                    extractions, flags["unavailable"] = {}, True

                item["inputs"] = audit_inputs.build_inputs(key, item["shipment_id"], by_key.get(key, []), plans.get(key), extractions)

            # A damaged store must not look like "no corrections were ever approved"
            if flags["unavailable"]:
                for item in snapshot["shipments"]:
                    audit_inputs.mark_corrections_unavailable(item["inputs"])

            with metrics.timed("logidoc_freight_audit_duration_seconds"):
                add_freight_audits(snapshot, by_key, self.locations.raw_dir, approved_of, self.extractions)

            # A correction the freight audit used is consumed by the audit like the
            # reconciliation's: history records it, staleness and supersession see it
            for item in snapshot["shipments"]:
                used = [c["correction_id"] for c in (item.get("freight_audit") or {}).get("corrections_applied") or []]

                if used and item.get("inputs") is not None:
                    item["inputs"]["applied_correction_ids"] = sorted(
                        set(item["inputs"].get("applied_correction_ids") or []) | set(used)
                    )

            # How each shipment's TMS record compares with what the audit read (after the freight audit: its
            # inputs hold the documents' carrier, route, pickup date and agreed rate)
            if tms is not None:
                for item in snapshot["shipments"]:
                    tms_audit.compare(item, tms)

                snapshot["tms_unavailable"] = tms.unavailable

            return snapshot

        # The extractor version is part of the key: a changed version recomputes the cross-check without a restart
        return self.cached("snapshot", compute,
                           extra=(self.corrections_token(), document_extraction.EXTRACTOR_VERSION, self.tms_token()))

    def scan(self):
        """
        Every PDF of the raw folder scanned for the completeness register.
        """

        return self.cached("scan", lambda: completeness.scan_folder(self.locations.raw_dir))

    def indexed_sources(self):
        """
        The file names in the search index, or None when it cannot be
        read.
        """

        try:

            metadatas = get_destination_collection(self.locations.chroma_dir).get(include=["metadatas"])["metadatas"]

        except Exception:
            return None

        return {
            str(metadata.get("source")) for metadata in metadatas or [] if metadata and metadata.get("source")
        } or None

    def has_documents(self):

        return bool(source_fingerprint(self.locations.raw_dir))

    def rows(self):

        return analytics.register_rows(self.snapshot())

    def exceptions(self, record=False, view=None):
        """
        The exceptions of every shipment with their review state (the audit
        decides what exists; the store remembers what people did). A read:
        the states come from a ReviewView (view, or a new one), and nothing is
        written. record: first records the detected exceptions in the review
        store (record_detections), for a change about to be made to one.
        """

        if record:
            self.record_detections()

        found = build_exceptions(self.snapshot())
        states = (view or ReviewView(self.locations.review_db, self.policy, found,
                                     detected_at=self.first_seen_of(found))).states()

        return with_reviews(found, {item["exception_key"]: states.get(item["exception_key"]) for item in found})

    def record_detections(self):
        """
        Records the exceptions the audit detects now in the review store
        (ReviewStore.sync), when the store does not hold them yet. Called before
        a change to an exception and by the server's background loop
        (operations.Operations.tick), never by a read request. Returns how many
        rows were written (0: nothing to record, or no documents).
        """

        if not self.has_documents():
            return 0

        found = build_exceptions(self.snapshot())
        path = Path(self.locations.review_db)
        stat = path.stat() if path.is_file() else None

        with self.lock:
            entry = self.cache.get("snapshot")
            marker = (entry[0] if entry else None, stat and (stat.st_mtime_ns, stat.st_size))

            if marker == self.recorded_marker:
                return 0

        detected_at = self.first_seen_of(found)
        changes = ReviewView(path, self.policy, found, detected_at=detected_at).changes

        if changes:
            # The store re-reads every row inside its write transaction: two callers at once record once
            self.review_store().sync(found, detected_at=detected_at)
        else:
            self.recorded_marker = marker

        return changes

    def shipment(self, shipment_id):
        """
        One shipment of the snapshot by its identifier (any spelling), or
        None.
        """

        from shipment_identity import identifier_key

        wanted = identifier_key(shipment_id)

        return next(
            (item for item in self.snapshot()["shipments"] if item["shipment_key"] == wanted), None
        )

    def exposure_rows(self):

        return exposure.exposure_rows(self.snapshot())

    def completeness(self):

        return completeness.register(self.scan(), self.indexed_sources())

    # --------------------------------------------------------
    # Corrections: approval, staleness and the recorded run
    # --------------------------------------------------------

    def current_applied(self):
        """{shipment_key: [approved correction ids the audit reads now]}."""

        return {
            item["shipment_key"]: item["inputs"]["applied_correction_ids"]
            for item in self.snapshot()["shipments"]
            if (item.get("inputs") or {}).get("applied_correction_ids")
        }

    def staleness(self):
        """
        The shipments whose latest recorded audit run did not consume the
        approved corrections the audit reads now (history.staleness). Each says
        whether recording it needs a controlled re-audit ("controlled": a
        missing correction is a line-item correction, which changes money
        amounts: re_audit_preview, then re_audit).
        """

        applied = self.current_applied()

        if not applied or not self.locations.history_db.is_file():
            return []

        stale = self.history_store().staleness(applied)
        items = {item["shipment_key"]: item for item in self.snapshot()["shipments"]}

        for entry in stale:
            line_items, _ = controlled_ids(items.get(entry["shipment_key"]) or {})
            entry["controlled_correction_ids"] = sorted(set(entry["missing_correction_ids"]) & line_items)
            entry["controlled"] = bool(entry["controlled_correction_ids"])

        return stale

    def outdated_cross_checks(self):
        """
        The shipments whose stored structured extractions were made by another
        extractor version than the current one: [{shipment_key, shipment_id,
        fields, extractor_versions, current_version}]. Recorded runs keep their
        own recorded inputs; this only says the cross-check beside them is stale.
        """

        current = document_extraction.EXTRACTOR_VERSION
        result = []

        for item in self.snapshot()["shipments"]:
            fields = [f for f in (item.get("inputs") or {}).get("fields", []) if "structured_extraction_outdated" in f["warnings"]]

            if fields:
                result.append({"shipment_key": item["shipment_key"], "shipment_id": item["shipment_id"],
                               "fields": [f["field"] for f in fields],
                               "extractor_versions": sorted({f["extractor_version"] for f in fields}), "current_version": current})

        return result

    def corrections_available(self):
        """False while the extraction store is damaged or from a newer version: approved corrections cannot be read."""

        return self.corrections_token() != UNAVAILABLE_TOKEN

    def record_corrected_run(self, principal, label=None, shipment_keys=None):
        """
        Records an audit run for the shipments that are stale (or those of
        shipment_keys that are), and nobody else: the new run supersedes each
        shipment's previous run, which keeps its snapshot and recorded inputs.
        Needs history.record. Returns {"recorded", "run_id", "shipments"}.
        """

        authz.require(principal, authz.HISTORY_RECORD)

        stale = [item for item in self.staleness() if shipment_keys is None or item["shipment_key"] in set(shipment_keys)]
        # A line-item correction changes money amounts: it is recorded by a controlled re-audit, never here
        controlled = [item["shipment_key"] for item in stale if item.get("controlled")]
        keys = [item["shipment_key"] for item in stale if not item.get("controlled")]

        if not keys:
            return with_controlled({"recorded": False, "run_id": None, "shipments": []}, controlled)

        snapshot = self.snapshot()
        wanted = {item["shipment_key"]: item["missing_correction_ids"] for item in stale if item["shipment_key"] in keys}
        applied = self.current_applied()
        outcome = self.history_store().record_run(
            snapshot, build_exceptions(snapshot), label=label or "Approved correction", shipment_keys=keys,
            only_stale={k: applied.get(k, wanted[k]) for k in wanted},
        )

        # Another caller recorded the same recalculation between the check above and the write
        if outcome["run_id"] is None:
            return with_controlled({"recorded": False, "run_id": None, "shipments": []}, controlled)

        return with_controlled({"recorded": True, "run_id": outcome["run_id"], "shipments": keys}, controlled)

    def decide_correction(self, principal, correction_id, decision, note=None):
        """
        Approves or rejects a proposed correction (extraction_store.decide, which
        enforces who may and the state rules), then, for an approval that a
        calculation consumes, records the recalculated run for that shipment.
        The decision and the run are two transactions in two databases: when the
        run cannot be recorded the approval stands, the audit already reads it,
        staleness() reports the shipment, and record_corrected_run can be
        called again. Returns the decision with "recompute": {status, ...}.
        """

        path = self.locations.extraction_path()

        if not path.is_file():
            raise ValueError("No such correction.")

        outcome = ExtractionStore.open_for_write(path).decide(principal, correction_id, decision, note)
        self.invalidate()

        if outcome["decision"] != "approved":
            return {**outcome, "recompute": {"status": "not_needed"}}

        import freight_audit
        import line_item_corrections

        consumed = (outcome["document_type"], outcome["field_name"])

        if consumed not in audit_inputs.TARGETS and not freight_audit.consumes(*consumed):
            return {**outcome, "recompute": {"status": "no_calculation_consumes_this_field"}}

        if outcome["shipment_key"] is None:
            return {**outcome, "recompute": {"status": "no_shipment"}}

        # A charge's amount changes money figures: its effect is recorded by a controlled re-audit (previewed and
        # confirmed by an administrator), never by the approval itself
        if line_item_corrections.is_line_item(*consumed):
            return {**outcome, "recompute": {"status": "re_audit_required", "shipments": [outcome["shipment_key"]]}}

        if not principal.can(authz.HISTORY_RECORD):
            return {**outcome, "recompute": {"status": "not_authorized"}}

        try:
            recorded = self.record_corrected_run(
                principal, label=f"Approved correction {correction_id}", shipment_keys=[outcome["shipment_key"]]
            )
        except Exception:
            log.exception("Recording the run after approving correction %s failed", correction_id)

            return {**outcome, "recompute": {"status": "failed", "message": "The approval is recorded; the run was not. Record it again."}}

        if outcome["shipment_key"] in recorded.get("re_audit_required", []):
            return {**outcome, "recompute": {"status": "re_audit_required", "shipments": [outcome["shipment_key"]]}}

        status = "recorded" if recorded["recorded"] else "no_recorded_history"

        return {**outcome, "recompute": {"status": status, "run_id": recorded["run_id"], "shipments": recorded["shipments"]}}

    # --------------------------------------------------------
    # Controlled re-audit
    # --------------------------------------------------------

    def preview_of(self, snapshot, item):
        """The re-audit preview of one shipment of this snapshot (see re_audit_preview)."""

        from audit_history import compare, snapshot_record

        key = item["shipment_key"]
        exceptions = [entry for entry in build_exceptions(snapshot) if entry["shipment_key"] == key]
        current = snapshot_record(item, exceptions)
        recorded = self.history_store().latest_recorded(key) if self.locations.history_db.is_file() else None
        previous = recorded["snapshot"] if recorded else None
        changes = compare(previous, current)
        inputs = item.get("inputs") or {}
        applied = sorted(inputs.get("applied_correction_ids") or [])
        tms_applied = sorted(inputs.get("applied_tms_resolution_ids") or [])
        line_items, _ = controlled_ids(item)
        before_ids = set((recorded or {}).get("applied_correction_ids") or [])
        added = sorted(set(applied) - before_ids)
        needed = recorded is None or previous["digest"] != current["digest"] or bool(added)
        freight_now = item.get("freight_audit") or {}
        freight_then = (previous or {}).get("freight_audit") or {}

        token = hashlib.sha256(json.dumps(
            [key, current["digest"], applied, tms_applied, recorded["run_id"] if recorded else None], default=str
        ).encode("utf-8")).hexdigest()

        def finding(entry):
            return {"finding_key": entry["finding_key"], "category": entry["category"], "severity": entry["severity"],
                    "summary": entry["summary"], "difference": entry["difference"]}

        return {
            "shipment_id": item["shipment_id"], "shipment_key": key, "needed": needed, "token": token,
            "recorded": None if recorded is None else {
                "run_id": recorded["run_id"], "recorded_at": recorded["recorded_at"], "label": recorded["label"],
                "final_status": previous["final_status"], "freight_outcome": freight_then.get("outcome"),
                "financial_impact": freight_then.get("financial_impact"),
            },
            "current": {"final_status": current["final_status"], "freight_outcome": freight_now.get("outcome"),
                        "financial_impact": freight_now.get("financial_impact")},
            "status_change": list(changes["status_change"]) if changes["status_change"] else None,
            "value_changes": changes["value_changes"],
            "state_changes": changes["state_changes"],
            "findings": {name: [finding(entry) for entry in changes[name]] for name in ("new", "resolved", "changed", "unverified")},
            "corrections": {"applied": applied, "added": added, "line_items": sorted(set(added) & line_items)},
            "tms_resolutions": tms_applied,
            "open_tms_conflicts": ((item.get("tms") or {}).get("open_conflicts") or 0),
        }

    def re_audit_preview(self, shipment_id):
        """
        What recording a re-audit of one shipment would change against its
        latest recorded run: final status, values, audit states, findings, the
        freight audit outcome and financial impact, and the approved corrections
        and TMS resolutions it would record. "needed": something differs.
        "token" names exactly this state: re_audit records it only while it is
        still the state the author saw. LookupError for an unknown shipment.
        """

        snapshot = self.snapshot()
        wanted = identifier_key(shipment_id)
        item = next((entry for entry in snapshot["shipments"] if entry["shipment_key"] == wanted), None)

        if item is None:
            raise LookupError("No such shipment.")

        return self.preview_of(snapshot, item)

    def re_audit(self, principal, shipment_id, token, reason):
        """
        The controlled re-audit: records a run for this one shipment, exactly as
        previewed (the token), with the reason and who in its label. Needs
        history.record. The new run supersedes the earlier ones it adds
        corrections to; they keep their snapshots and inputs. ReAuditRefused when
        nothing changed, when the state changed since the preview, or when
        another run was recorded for the shipment meanwhile: nothing is written.
        """

        authz.require(principal, authz.HISTORY_RECORD)

        reason = " ".join((reason or "").split())

        if len(reason) < 10:
            raise ReAuditInvalid("Write why the shipment is re-audited (at least 10 characters).")

        if len(reason) > 500:
            raise ReAuditInvalid("The reason is longer than 500 characters.")

        snapshot = self.snapshot()
        wanted = identifier_key(shipment_id)
        item = next((entry for entry in snapshot["shipments"] if entry["shipment_key"] == wanted), None)

        if item is None:
            raise LookupError("No such shipment.")

        preview = self.preview_of(snapshot, item)

        if preview["token"] != token:
            raise ReAuditRefused("The audit of this shipment changed since the preview. Review the new preview and confirm again.")

        if not preview["needed"]:
            raise ReAuditRefused("Nothing to re-audit: the latest recorded run already holds this result.")

        key = item["shipment_key"]
        expected = preview["recorded"]["run_id"] if preview["recorded"] else None
        outcome = self.history_store().record_run(
            snapshot, build_exceptions(snapshot), label=f"Re-audit by {principal.username}: {reason}", shipment_keys=[key],
            expect_latest={key: expected},
        )

        if outcome["run_id"] is None:
            raise ReAuditRefused("Another audit run was recorded for this shipment meanwhile. Review the new preview.")

        metrics.REGISTRY.inc("logidoc_re_audits_total", {"outcome": "recorded"})
        log.info("Re-audit run %s recorded for %s by %s", outcome["run_id"], key, principal.username,
                 extra={"operation": "re_audit", "outcome": "recorded"})

        return {"recorded": True, "run_id": outcome["run_id"], "shipment_id": item["shipment_id"], "shipment_key": key,
                "previous_run_id": expected, "snapshot_written": bool(outcome["snapshots_written"]),
                "corrections_added": preview["corrections"]["added"], "tms_resolutions": preview["tms_resolutions"]}

    def re_audits(self):
        """
        The shipments a controlled re-audit is waiting for: an approved
        line-item correction or a chosen TMS value the audit reads now, and a
        latest recorded run whose result differs. A shipment never recorded is
        not listed (its first recorded run will include them).
        """

        if not self.locations.history_db.is_file():
            return []

        snapshot = self.snapshot()
        history = self.history_store()
        exceptions = build_exceptions(snapshot)
        result = []

        from audit_history import snapshot_record

        for item in snapshot["shipments"]:
            line_items, tms_ids = controlled_ids(item)

            if not line_items and not tms_ids:
                continue

            recorded = history.latest_recorded(item["shipment_key"])

            if recorded is None:
                continue

            current = snapshot_record(item, [entry for entry in exceptions if entry["shipment_key"] == item["shipment_key"]])
            missing = sorted(line_items - set(recorded["applied_correction_ids"] or []))

            if missing or recorded["snapshot"]["digest"] != current["digest"]:
                result.append({"shipment_id": item["shipment_id"], "shipment_key": item["shipment_key"],
                               "recorded_run_id": recorded["run_id"], "line_item_correction_ids": missing,
                               "tms_resolution_ids": sorted(tms_ids)})

        return result
