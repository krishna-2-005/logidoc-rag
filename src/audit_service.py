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

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import audit_analytics as analytics
import audit_inputs
import authz
import document_completeness as completeness
import financial_exposure as exposure
import ingestion_service
import reconciliation
from audit_analytics import build_exceptions, build_snapshot, source_fingerprint, with_reviews
from chunker import read_chunks
from ingestion_manager import get_destination_collection
from review_store import Policy, ReviewStore
from audit_history import HistoryStore
import sqlite3

from extraction_store import ExtractionStore, StoreUnavailable
from shipment_identity import identifier_key

log = logging.getLogger(__name__)

# Cache token while the extraction store is damaged or from a newer version
UNAVAILABLE_TOKEN = (-1, -1, -1)


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

    def extraction_path(self):
        """The extraction store: its own setting, or next to the raw folder."""

        return Path(self.extraction_db) if self.extraction_db else Path(self.raw_dir).parent / "extractions.sqlite3"

    @staticmethod
    def from_config(config):

        places = config.ingestion_locations(config.RAW_DATA_DIR)

        return Locations(
            Path(config.RAW_DATA_DIR), Path(config.CHROMA_DIR), Path(config.REVIEW_DB_PATH),
            Path(config.HISTORY_DB_PATH), places["register"], places["quarantine"], places["archive"], places["extractions"],
        )


class AuditService:

    def __init__(self, locations, policy=None, reconcile=None):

        self.locations = locations
        self.policy = policy or Policy()
        self.reconcile = reconcile or reconciliation.reconcile_shipment
        self.lock = threading.RLock()
        self.cache = {}

    # --------------------------------------------------------
    # Stores
    # --------------------------------------------------------

    def review_store(self):

        return ReviewStore(self.locations.review_db, self.policy)

    def history_store(self):

        return HistoryStore(self.locations.history_db)

    def extraction_store(self):
        """
        The extraction store when it exists and is at the current schema
        version, else None. Opening it for the audit never creates or migrates
        anything.
        """

        return ExtractionStore.open_if_current(self.locations.extraction_path())

    def corrections_token(self):
        """Changes whenever a correction is approved or an extraction stored (part of the audit cache key)."""

        try:
            store = self.extraction_store()

            return store.audit_token() if store is not None else (0, 0, 0)
        except (StoreUnavailable, sqlite3.DatabaseError):
            return UNAVAILABLE_TOKEN

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

            def reconcile(shipment_id, chunks=None, **kwargs):

                # Only approved corrections of this shipment's own documents; with
                # none, the calculation is called exactly as it always was
                key = identifier_key(shipment_id)
                overrides, plan = {}, {}

                if store is not None:
                    try:
                        overrides, plan = audit_inputs.build_overrides(store.approved_corrections(key), shipment_id, hashes)
                    except sqlite3.DatabaseError:
                        flags["unavailable"] = True

                plans[key] = plan

                if overrides:
                    kwargs["overrides"] = overrides

                return self.reconcile(shipment_id, chunks=chunks, **kwargs)

            snapshot = build_snapshot(chunks, reconcile, unreadable_files=problems)

            by_key = {}

            for chunk in chunks:
                by_key.setdefault(identifier_key((chunk.get("metadata") or {}).get("shipment_id")), []).append(chunk)

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

            return snapshot

        return self.cached("snapshot", compute, extra=self.corrections_token())

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

    def exceptions(self, sync=True):
        """
        The exceptions of every shipment with their review state. sync
        records newly detected exceptions in the review store (the audit
        decides what exists; the store remembers what people did).
        """

        found = build_exceptions(self.snapshot())

        if not sync:
            return with_reviews(found, {})

        reviews = self.review_store().sync(found)

        return with_reviews(found, reviews)

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
        approved corrections the audit reads now (history.staleness).
        """

        applied = self.current_applied()

        if not applied or not self.locations.history_db.is_file():
            return []

        return self.history_store().staleness(applied)

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

        stale = self.staleness()
        keys = [item["shipment_key"] for item in stale if shipment_keys is None or item["shipment_key"] in set(shipment_keys)]

        if not keys:
            return {"recorded": False, "run_id": None, "shipments": []}

        snapshot = self.snapshot()
        wanted = {item["shipment_key"]: item["missing_correction_ids"] for item in stale if item["shipment_key"] in keys}
        applied = self.current_applied()
        outcome = self.history_store().record_run(
            snapshot, build_exceptions(snapshot), label=label or "Approved correction", shipment_keys=keys,
            only_stale={k: applied.get(k, wanted[k]) for k in wanted},
        )

        # Another caller recorded the same recalculation between the check above and the write
        if outcome["run_id"] is None:
            return {"recorded": False, "run_id": None, "shipments": []}

        return {"recorded": True, "run_id": outcome["run_id"], "shipments": keys}

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

        outcome = ExtractionStore(path).decide(principal, correction_id, decision, note)
        self.invalidate()

        if outcome["decision"] != "approved":
            return {**outcome, "recompute": {"status": "not_needed"}}

        if (outcome["document_type"], outcome["field_name"]) not in audit_inputs.TARGETS:
            return {**outcome, "recompute": {"status": "no_calculation_consumes_this_field"}}

        if outcome["shipment_key"] is None:
            return {**outcome, "recompute": {"status": "no_shipment"}}

        if not principal.can(authz.HISTORY_RECORD):
            return {**outcome, "recompute": {"status": "not_authorized"}}

        try:
            recorded = self.record_corrected_run(
                principal, label=f"Approved correction {correction_id}", shipment_keys=[outcome["shipment_key"]]
            )
        except Exception:
            log.exception("Recording the run after approving correction %s failed", correction_id)

            return {**outcome, "recompute": {"status": "failed", "message": "The approval is recorded; the run was not. Record it again."}}

        status = "recorded" if recorded["recorded"] else "no_recorded_history"

        return {**outcome, "recompute": {"status": status, "run_id": recorded["run_id"], "shipments": recorded["shipments"]}}
