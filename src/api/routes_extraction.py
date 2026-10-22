"""
HTTP API: structured extraction results, reviewer corrections and their approval.

Running an extraction needs the administrator role (it writes the extraction
store). Viewing results needs the reviewer role. A correction is a PROPOSAL
(reviewer or administrator): it has no effect on any audit until a different,
signed-in administrator approves it (POST /corrections/{id}/decision); the
store enforces who may decide and the state rules, these routes only map errors
to HTTP. An approval that an audit calculation consumes is followed by a
recorded, recalculated run for that shipment (see AuditService.decide_correction).

A document is named, never given as a path: it is looked up in the server's own
listing of the document folder. Corrections never replace the extracted values.
A proposal names the correction it was based on (based_on, null when there was
none): when the field changed since, it is refused with 409.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

import authz
import document_extraction as de
import extraction_store as store_module
from api.core import ApiState, get_state, require
from extraction_store import ExtractionStore
from shipment_identity import identifier_key


# Row ids are small positive integers; anything outside this range cannot exist and must not reach SQLite
MAX_ID = 2**31 - 1
RowId = Annotated[int, PathParam(ge=1, le=MAX_ID)]

router = APIRouter(prefix="/api/extractions")
history_router = APIRouter(prefix="/api/history")

View = Depends(require(authz.EXTRACTION_VIEW))
Correct = Depends(require(authz.EXTRACTION_CORRECT))
Run = Depends(require(authz.EXTRACTION_RUN))
Approve = Depends(require(authz.EXTRACTION_APPROVE))

LIMITATIONS = [
    "A correction is a proposal until a different administrator approves it; only an approved correction reaches an audit.",
    "Approved corrections are read by the quantity, delivery and invoice audits (and so the financial exposure) for these fields only: "
    "BOL piece_count and expected_delivery_date, POD delivered_pieces and delivery_date, INVOICE invoice_total. "
    "Charges by label, carrier, route and document completeness still use the original readers.",
    "No OCR: scanned pages are reported as needing OCR.",
    "No calibrated confidence: a field is trusted because its label matched, and validation says what was checked.",
]


class RunBody(BaseModel):

    document: str = Field(min_length=1, max_length=200)


class CorrectionBody(BaseModel):

    field: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)
    # Required, may be null: the latest correction of this field the reviewer saw
    based_on: int | None = Field(..., ge=1, le=MAX_ID)


class DecisionBody(BaseModel):

    decision: str = Field(pattern="^(approve|reject)$")
    note: str | None = Field(default=None, max_length=1000)


class RecomputeBody(BaseModel):

    shipment_id: str | None = Field(default=None, max_length=100)


def store_of(state, create=False):

    path = state.locations.extraction_path()

    if not path.is_file() and not create:
        return None

    return ExtractionStore(path)


def summary(record, store):
    item = {key: value for key, value in record.items() if key != "payload"}
    corrections = store.corrections(record["id"])
    item["correction_count"] = len(corrections)
    item["pending_count"] = sum(1 for c in corrections if c["status"] == store_module.PENDING)

    return item


def denied(error):

    return HTTPException(status_code=403, detail=str(error))


@router.get("")
def list_extractions(principal=View, state: ApiState = Depends(get_state), shipment_id: str | None = Query(None, max_length=100)):

    store = store_of(state)

    if store is None:
        return {"extractions": [], "extractor_version": de.EXTRACTOR_VERSION}

    key = identifier_key(shipment_id) if shipment_id else None
    rows = store.list(key) if (key or not shipment_id) else []

    return {"extractions": [summary(row, store) for row in rows], "extractor_version": de.EXTRACTOR_VERSION}


@router.get("/integrity")
def integrity(principal=Depends(require(authz.ADMIN_MAINTENANCE)), state: ApiState = Depends(get_state)):

    store = store_of(state)

    if store is None:
        return {"ok": True, "corrections_checked": 0, "decisions_checked": 0, "extractions_checked": 0,
                "unchained_legacy": 0, "problems": []}

    return store.verify_history()


@router.get("/pending")
def pending(principal=View, state: ApiState = Depends(get_state)):
    """The proposed corrections waiting for a decision."""

    store = store_of(state)

    return {"pending": store.pending() if store else []}


@router.get("/staleness")
def staleness(principal=Depends(require(authz.HISTORY_VIEW)), state: ApiState = Depends(get_state)):
    """Shipments whose latest recorded run did not consume the approved corrections the audit reads now."""

    return {"stale": state.service.staleness(), "corrections_available": state.service.corrections_available()}


@router.post("/recompute")
def recompute(body: RecomputeBody, principal=Depends(require(authz.HISTORY_RECORD)), state: ApiState = Depends(get_state)):
    """
    Records the recalculated run for the stale shipments (or the named one):
    the retry after an approval whose run could not be recorded.
    """

    keys = None

    if body.shipment_id:
        found = state.service.shipment(body.shipment_id)

        if found is None:
            raise HTTPException(status_code=404, detail="No such shipment.")

        keys = [found["shipment_key"]]

    try:
        return state.service.record_corrected_run(principal, shipment_keys=keys)
    except authz.PermissionDenied as error:
        raise denied(error) from None


@router.post("/run")
def run_extraction(body: RunBody, principal=Run, state: ApiState = Depends(get_state)):

    found = next((path for path in sorted(state.locations.raw_dir.glob("*.pdf")) if path.name == body.document), None)

    if found is None:
        raise HTTPException(status_code=404, detail="No such document.")

    extraction = de.extract_document(found)

    if not extraction.sha256:
        raise HTTPException(status_code=422, detail="The document could not be read.")

    store = store_of(state, create=True)
    extraction_id, created = store.save(extraction)

    return {"id": extraction_id, "created": created, "status": extraction.status, "complete": extraction.complete,
            "review_reasons": extraction.review_reasons}


@router.get("/{extraction_id}")
def extraction_detail(extraction_id: RowId, principal=View, state: ApiState = Depends(get_state)):

    store = store_of(state)
    record = store.row(extraction_id) if store else None

    if record is None:
        raise HTTPException(status_code=404, detail="No such extraction.")

    return {**record, "effective": store.effective(extraction_id), "corrections": store.corrections(extraction_id),
            "limitations": LIMITATIONS}


@router.post("/{extraction_id}/corrections")
def propose_correction(extraction_id: RowId, body: CorrectionBody, principal=Correct, state: ApiState = Depends(get_state)):
    """Records a proposed correction. It has no effect until it is approved."""

    store = store_of(state)

    if store is None or store.row(extraction_id) is None:
        raise HTTPException(status_code=404, detail="No such extraction.")

    try:
        return store.propose(principal, extraction_id, body.field, body.value, body.reason, based_on=body.based_on)
    except authz.PermissionDenied as error:
        raise denied(error) from None
    except store_module.StaleCorrection as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except store_module.StoreError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.post("/corrections/{correction_id}/decision")
def decide(correction_id: RowId, body: DecisionBody, principal=Approve, state: ApiState = Depends(get_state)):
    """
    Approves or rejects a proposed correction. A different, signed-in
    administrator is needed; an approval is followed by a recalculated, recorded
    run when a calculation consumes the field (see "recompute" in the reply).
    """

    try:
        return state.service.decide_correction(principal, correction_id, body.decision, body.note)
    except authz.PermissionDenied as error:
        raise denied(error) from None
    except store_module.InvalidTransition as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except store_module.StoreError as error:
        status = 404 if "Unknown correction" in str(error) else 422
        raise HTTPException(status_code=status, detail=str(error)) from None
    except ValueError:
        raise HTTPException(status_code=404, detail="No such correction.") from None


@history_router.get("/runs/{run_id}/inputs")
def run_inputs(run_id: RowId, principal=Depends(require(authz.HISTORY_VIEW)), state: ApiState = Depends(get_state),
               shipment_id: str | None = Query(None, max_length=100)):
    """
    The inputs a recorded run consumed (value, source, correction, document, page),
    and the run that superseded it, if any. Runs recorded before inputs were
    recorded return none: they are not reconstructed.
    """

    if not state.locations.history_db.is_file():
        raise HTTPException(status_code=404, detail="No recorded history.")

    key = identifier_key(shipment_id) if shipment_id else None

    if shipment_id and not key:
        raise HTTPException(status_code=422, detail="shipment_id is not a shipment identifier.")

    history = state.service.history_store()

    if not any(r["id"] == run_id for r in history.runs(100000)):
        raise HTTPException(status_code=404, detail="No such run.")

    inputs = history.run_inputs(run_id, key)
    keys = sorted({item["shipment_key"] for item in inputs})

    return {"run_id": run_id, "inputs": inputs, "recorded": bool(inputs),
            "superseded_by": {k: history.superseded_by(run_id, k) for k in keys}}
