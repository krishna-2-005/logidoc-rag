"""
HTTP API: document ingestion, the ingestion register, settings and chat.

Uploads go through ingestion_service (validation, duplicate detection,
retries, the register) and changes to stored documents through
document_lifecycle (archive first, restore on failure); both need the
administrator role. The routes only enforce size limits and shape
responses.
"""

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

import authz
import config
import document_lifecycle as lifecycle
import ingestion_register as register_module
import ingestion_service as service
from api.core import ApiState, get_principal, get_state, require
from ingestion_register import IngestionRegister, STATUSES


router = APIRouter(prefix="/api")

ViewIngestion = Depends(require(authz.INGESTION_VIEW))


class ResolveBody(BaseModel):

    note: str = Field(min_length=1, max_length=1000)


class ChatBody(BaseModel):

    question: str = Field(min_length=1, max_length=1000)
    shipment_id: str | None = Field(default=None, max_length=100)


def register_of(state, create=False):

    path = state.locations.register_db

    if not path.is_file() and not create:
        return None

    return IngestionRegister(path)


def denied(error):

    return HTTPException(status_code=403, detail=str(error))


async def read_limited(upload, limit):
    """
    The bytes of an upload, never more than limit (the file is refused
    as too large instead of being read in full).
    """

    data = await upload.read(limit + 1)

    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"{upload.filename}: larger than the {limit // (1024 * 1024)} MB limit.")

    return data


# ------------------------------------------------------------
# Register
# ------------------------------------------------------------

@router.get("/ingestion/register")
def register_list(
    principal=ViewIngestion, state: ApiState = Depends(get_state), status: list[str] = Query(default=[]),
    search: str = Query("", max_length=100),
):

    unknown = set(status) - set(STATUSES)

    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown status: {sorted(unknown)}.")

    register = register_of(state)

    if register is None:
        return {"files": [], "summary": {**{name: 0 for name in STATUSES}, "open_errors": 0}, "actions": []}

    files = register.files(statuses=tuple(status), search=search)

    for record in files:
        record.pop("quarantine_name", None)

    return {"files": files, "summary": register.summary(), "actions": register.actions(50)}


@router.get("/ingestion/errors")
def errors(principal=ViewIngestion, state: ApiState = Depends(get_state)):

    register = register_of(state)

    return [{key: value for key, value in record.items() if key != "quarantine_name"} | {"can_retry": bool(record["quarantine_name"])}
            for record in (register.errors() if register else [])]


@router.post("/ingestion/upload")
async def upload(
    files: list[UploadFile] = File(), principal=Depends(get_principal), state: ApiState = Depends(get_state),
):
    """
    Ingests PDFs (administrators). Every file gets a status in the
    register; one bad file does not stop the others.
    """

    try:
        authz.require(principal, authz.INGESTION_WRITE)
    except authz.PermissionDenied as error:
        raise denied(error) from None

    if not files or len(files) > 20:
        raise HTTPException(status_code=422, detail="Upload between 1 and 20 files at a time.")

    limit = state.settings.max_upload_mb * 1024 * 1024

    uploads = [service.Upload(item.filename or "", await read_limited(item, limit)) for item in files]

    summary = run_ingestion(state, principal, uploads)

    return shape_summary(summary)


def run_ingestion(state, principal, uploads):

    locations = state.locations

    summary = service.process_files(
        principal, uploads, locations.raw_dir, locations.chroma_dir, register_of(state, create=True),
        locations.quarantine_dir,
    )

    if summary["corpus_changed"]:
        state.service.invalidate()

    return summary


def shape_summary(summary):

    return {
        **{key: summary[key] for key in ("indexed", "partial", "duplicates", "failed", "unsupported", "corpus_changed", "errors")},
        "files": [{key: value for key, value in item.items() if key != "quarantine_name"} for item in summary["files"]],
    }


@router.post("/ingestion/{file_id}/retry")
def retry(file_id: int, principal=Depends(get_principal), state: ApiState = Depends(get_state)):

    register = register_of(state)

    if register is None:
        raise HTTPException(status_code=404, detail="No such ingestion record.")

    try:
        summary = service.retry(
            principal, register, file_id, state.locations.raw_dir, state.locations.chroma_dir,
            state.locations.quarantine_dir,
        )
    except authz.PermissionDenied as error:
        raise denied(error) from None
    except register_module.RegisterError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None

    if summary["corpus_changed"]:
        state.service.invalidate()

    return shape_summary(summary)


@router.post("/ingestion/{file_id}/resolve")
def resolve(file_id: int, body: ResolveBody, principal=Depends(get_principal), state: ApiState = Depends(get_state)):

    register = register_of(state)

    if register is None:
        raise HTTPException(status_code=404, detail="No such ingestion record.")

    try:
        record = service.resolve_error(principal, register, file_id, body.note)
    except authz.PermissionDenied as error:
        raise denied(error) from None
    except register_module.RegisterError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None

    state.service.invalidate()

    return {key: value for key, value in record.items() if key != "quarantine_name"}


# ------------------------------------------------------------
# Stored documents (administrators)
# ------------------------------------------------------------

def lifecycle_call(state, action):

    try:
        result = action()
    except authz.PermissionDenied as error:
        raise denied(error) from None
    except lifecycle.LifecycleError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None

    state.service.invalidate()

    return result


@router.post("/documents/reprocess")
def reprocess(name: str = Query(max_length=200), principal=Depends(get_principal), state: ApiState = Depends(get_state)):

    locations = state.locations

    return lifecycle_call(state, lambda: lifecycle.reprocess(
        principal, register_of(state, create=True), name, locations.raw_dir, locations.chroma_dir,
    ))


@router.post("/documents/replace")
async def replace(
    name: str = Query(max_length=200), file: UploadFile = File(), principal=Depends(get_principal),
    state: ApiState = Depends(get_state),
):

    try:
        authz.require(principal, authz.INGESTION_WRITE)
    except authz.PermissionDenied as error:
        raise denied(error) from None

    data = await read_limited(file, state.settings.max_upload_mb * 1024 * 1024)
    locations = state.locations

    return lifecycle_call(state, lambda: lifecycle.replace(
        principal, register_of(state, create=True), name, data, locations.raw_dir, locations.chroma_dir,
        locations.archive_dir,
    ))


@router.post("/documents/remove")
def remove(
    name: str = Query(max_length=200), confirm: bool = Query(False), principal=Depends(get_principal),
    state: ApiState = Depends(get_state),
):
    """
    Removes a document from the store and the index; its PDF is archived,
    never deleted. Needs confirm=true.
    """

    if not confirm:
        raise HTTPException(status_code=422, detail="Removing a document needs confirm=true.")

    locations = state.locations

    return lifecycle_call(state, lambda: lifecycle.remove(
        principal, register_of(state, create=True), name, locations.raw_dir, locations.chroma_dir,
        locations.archive_dir,
    ))


# ------------------------------------------------------------
# Settings (read-only, harmless values only)
# ------------------------------------------------------------

@router.get("/settings")
def settings(principal=Depends(get_principal), state: ApiState = Depends(get_state)):
    """
    A fixed list of non-secret options. Never a path, a database file, an
    origin list or an environment value.
    """

    return {
        "auth_mode": state.mode,
        "resolution_needs_approval": state.policy.require_approval,
        "minimum_note_length": state.policy.min_note,
        "resolution_needs_evidence": state.policy.resolve_needs_evidence,
        "upload_limit_mb": state.settings.max_upload_mb,
        "max_files_per_upload": 20,
        "language_model": config.LLM_MODEL,
        "embedding_model": config.EMBEDDING_MODEL,
        "ocr": "not available: image-only PDFs are reported as needing OCR",
        "currency_handling": "amounts are never converted or added across currencies",
        "editable_here": False,
    }


# ------------------------------------------------------------
# Chat (the existing RAG)
# ------------------------------------------------------------

@router.post("/chat")
def chat(body: ChatBody, principal=Depends(require(authz.EXCEPTIONS_VIEW))):
    """
    A question answered from the indexed documents (rag.ask_question),
    scoped to a shipment when one is given. The answer is generated text
    grounded in the returned evidence; it is not an audit result.
    """

    from rag import ask_question

    scope = None if not body.shipment_id or body.shipment_id == "All Shipments" else body.shipment_id

    try:
        answer, results = ask_question(body.question, scope)
    except Exception:
        raise HTTPException(status_code=503, detail="The question could not be answered: the language model or the index is unavailable.") from None

    return {
        "answer": answer,
        "status": results.get("status"),
        "answer_method": results.get("answer_method"),
        "scope": results.get("scope"),
        "evidence": results.get("evidence") or [],
        "generated": True,
        "notice": "Generated from the indexed documents. It does not change any audit result.",
    }
