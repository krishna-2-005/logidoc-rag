"""
Carrier master data (src/carrier_registry.py): registered carriers, their
validated identifiers, and the approved links of document names to them.

Reading needs exceptions.view (like Carrier Intelligence). Registering,
changing and proposing or deciding links need carriers.manage
(administrators); a link is decided by a different administrator than the one
who proposed it. Nothing here changes an audit or a recorded history.

The management screens read through the paged routes:

    GET /api/carrier-registry/carriers              search, status filter, sort, page
    GET /api/carrier-registry/carriers/{id}         master data, links, requests, history
    GET /api/carrier-registry/carriers/{id}/events  the carrier's history (administrators)
    GET /api/carrier-registry/links                 link requests (pending, decided, all)
    GET /api/carrier-registry/document-names        every carrier name printed on the
                                                    documents, its shipments and link state
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import authz
import carrier_intelligence
from api import schemas
from api.core import ApiState, get_state, require
from carrier_registry import CarrierRegistry, RegistryConflict, RegistryError, RegistryVersionError, loose_key

router = APIRouter(prefix="/api")

View = Depends(require(authz.EXCEPTIONS_VIEW))
Manage = Depends(require(authz.CARRIERS_MANAGE))


class CarrierBody(BaseModel):

    legal_name: str = Field(min_length=1, max_length=200)
    trading_name: str | None = Field(default=None, max_length=200)
    scac: str | None = Field(default=None, max_length=10)
    mc_number: str | None = Field(default=None, max_length=20)
    dot_number: str | None = Field(default=None, max_length=20)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=254)
    contact_phone: str | None = Field(default=None, max_length=30)
    status: Literal["active", "on_hold", "inactive"] = "active"
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    region: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, max_length=10)
    # A carrier whose name equals an existing one's is refused unless this says they are different companies
    confirm_distinct: bool = False


class CarrierChanges(BaseModel):

    legal_name: str | None = Field(default=None, min_length=1, max_length=200)
    trading_name: str | None = Field(default=None, max_length=200)
    scac: str | None = Field(default=None, max_length=10)
    mc_number: str | None = Field(default=None, max_length=20)
    dot_number: str | None = Field(default=None, max_length=20)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=254)
    contact_phone: str | None = Field(default=None, max_length=30)
    status: Literal["active", "on_hold", "inactive"] | None = None
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    region: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, max_length=10)


class LinkBody(BaseModel):

    name: str = Field(min_length=1, max_length=200)
    # None: the name is to be unlinked from the carrier it belongs to
    carrier_id: int | None = Field(..., ge=1)
    reason: str = Field(min_length=10, max_length=1000)


class DecisionBody(BaseModel):

    decision: str = Field(pattern="^(approve|reject)$")
    note: str | None = Field(default=None, max_length=1000)


def registry_of(state, create=False):
    """The registry, or None when it was never created (reading never creates it)."""

    path = state.locations.carrier_registry_path()

    if not create and not path.is_file():
        return None

    try:
        return CarrierRegistry(path)
    except RegistryVersionError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None


def resolver_of(state):
    """{document name key: registered carrier} of the approved links ({} without a registry)."""

    registry = registry_of(state)

    return registry.resolver() if registry is not None else {}


def refusal(error):

    if isinstance(error, RegistryVersionError):
        return HTTPException(status_code=503, detail=str(error))
    if isinstance(error, authz.PermissionDenied):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, RegistryConflict):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error).strip("'\""))
    return HTTPException(status_code=422, detail=str(error))


def changed(state):
    """A new link changes how shipments group: forget the cached figures."""

    state.service.invalidate()


@router.get("/carrier-registry", response_model=schemas.CarrierRegistryResponse, response_model_exclude_unset=True,
            responses=schemas.ERRORS)
def registry(principal=View, state: ApiState = Depends(get_state)):

    store = registry_of(state)

    if store is None:
        return {"carriers": [], "requests": [], "pending": 0}

    requests = store.requests()

    return {"carriers": store.carriers(), "requests": requests, "pending": sum(r["decision"] is None for r in requests)}


@router.post("/carrier-registry", status_code=201, response_model=schemas.RegisteredCarrierRecord,
             response_model_exclude_unset=True, responses={**schemas.ERRORS, 409: {"model": schemas.ErrorResponse}})
def register_carrier(body: CarrierBody, principal=Manage, state: ApiState = Depends(get_state)):

    try:
        values = body.model_dump(exclude={"confirm_distinct"})
        return registry_of(state, create=True).create(principal, confirm_distinct=body.confirm_distinct, **values)
    except (RegistryError, LookupError, authz.PermissionDenied) as error:
        raise refusal(error) from None


@router.patch("/carrier-registry/{carrier_id}", response_model=schemas.RegisteredCarrierRecord,
              response_model_exclude_unset=True, responses={**schemas.ERRORS, 409: {"model": schemas.ErrorResponse}})
def change_carrier(carrier_id: int, body: CarrierChanges, principal=Manage, state: ApiState = Depends(get_state)):

    store = registry_of(state)

    if store is None:
        raise HTTPException(status_code=404, detail="No such carrier.")

    try:
        carrier = store.update(principal, carrier_id, **body.model_dump(exclude_unset=True))
    except (RegistryError, LookupError, authz.PermissionDenied) as error:
        raise refusal(error) from None

    changed(state)

    return carrier


@router.post("/carrier-registry/links", status_code=201, response_model=schemas.LinkRequest,
             response_model_exclude_unset=True, responses={**schemas.ERRORS, 409: {"model": schemas.ErrorResponse}})
def propose_link(body: LinkBody, principal=Manage, state: ApiState = Depends(get_state)):
    """Proposes that a document name belongs to a registered carrier (or is unlinked). No effect until approved."""

    try:
        return registry_of(state, create=True).propose_link(principal, body.name, body.carrier_id, body.reason)
    except (RegistryError, LookupError, authz.PermissionDenied) as error:
        raise refusal(error) from None


@router.post("/carrier-registry/links/{request_id}/decision", response_model=schemas.LinkRequest,
             response_model_exclude_unset=True, responses={**schemas.ERRORS, 409: {"model": schemas.ErrorResponse}})
def decide_link(request_id: int, body: DecisionBody, principal=Manage, state: ApiState = Depends(get_state)):
    """Approves or rejects a link request (a different administrator than the proposer)."""

    store = registry_of(state)

    if store is None:
        raise HTTPException(status_code=404, detail="No such request.")

    try:
        request = store.decide_link(principal, request_id, body.decision, body.note)
    except (RegistryError, LookupError, authz.PermissionDenied) as error:
        raise refusal(error) from None

    changed(state)

    return request


@router.get("/carrier-registry/events", response_model=list[schemas.RegistryEvent], response_model_exclude_unset=True,
            responses=schemas.ERRORS)
def registry_events(principal=Manage, state: ApiState = Depends(get_state)):

    store = registry_of(state)

    return store.events() if store is not None else []


# ------------------------------------------------------------
# Management screens (paged reads)
# ------------------------------------------------------------

Status = Literal["active", "on_hold", "inactive"]
SortField = Literal["legal_name", "trading_name", "scac", "mc_number", "dot_number", "status", "created_at", "updated_at"]


@router.get("/carrier-registry/carriers", response_model=schemas.CarrierPage, response_model_exclude_unset=True,
            responses=schemas.ERRORS)
def carrier_page(
    principal=View, state: ApiState = Depends(get_state),
    search: str = Query("", max_length=100), status: list[Status] = Query(default=[]),
    sort: SortField = Query("legal_name"), order: Literal["asc", "desc"] = Query("asc"),
    offset: int = Query(0, ge=0, le=1000000), limit: int = Query(25, ge=1, le=200),
):
    """
    A page of registered carriers. search matches any part of the legal or
    trading name, the SCAC, and the MC or USDOT number (with or without its
    prefix); status keeps the registration statuses given.
    """

    store = registry_of(state)

    if store is None:
        return {"carriers": [], "total": 0, "offset": offset, "limit": limit}

    try:
        page = store.search(search, tuple(status), sort, order, offset, limit)
    except RegistryError as error:
        raise refusal(error) from None

    return {**page, "offset": offset, "limit": limit}


@router.get("/carrier-registry/carriers/{carrier_id}", response_model=schemas.CarrierRegistryDetail,
            response_model_exclude_unset=True, responses=schemas.ERRORS)
def carrier_record(carrier_id: int, principal=View, state: ApiState = Depends(get_state)):
    """
    A registered carrier: master data, the document names linked to it, the link
    requests that concern it, and (administrators) its history. Its delivery,
    audit and exception figures are Carrier Intelligence's, at
    GET /api/carriers/{intelligence_key} (null: no document name is linked yet).
    """

    store = registry_of(state)
    carrier = store.record(carrier_id) if store else None

    if carrier is None:
        raise HTTPException(status_code=404, detail="No such carrier.")

    links = store.name_links(carrier_id)

    return {
        "carrier": carrier,
        "name_links": links,
        "pending_requests": store.link_queue("pending", carrier_id, 0, 200)["requests"],
        "recent_decisions": store.link_queue("decided", carrier_id, 0, 20)["requests"],
        "history": ({**store.carrier_events(carrier_id, 0, 50), "offset": 0, "limit": 50}
                    if principal.can(authz.CARRIERS_MANAGE) else None),
        "intelligence_key": f"registered:{carrier_id}" if links else None,
    }


@router.get("/carrier-registry/carriers/{carrier_id}/events", response_model=schemas.RegistryEventPage,
            response_model_exclude_unset=True, responses=schemas.ERRORS)
def carrier_history(carrier_id: int, principal=Manage, state: ApiState = Depends(get_state),
                    offset: int = Query(0, ge=0, le=1000000), limit: int = Query(50, ge=1, le=200)):
    """A carrier's history, newest first: its changes and the link decisions about it (administrators)."""

    store = registry_of(state)

    if store is None:
        return {"events": [], "total": 0, "offset": offset, "limit": limit}

    return {**store.carrier_events(carrier_id, offset, limit), "offset": offset, "limit": limit}


@router.get("/carrier-registry/links", response_model=schemas.LinkQueue, response_model_exclude_unset=True,
            responses=schemas.ERRORS)
def link_queue(principal=View, state: ApiState = Depends(get_state),
               state_filter: Literal["pending", "decided", "all"] = Query("pending", alias="state"),
               carrier_id: int | None = Query(None, ge=1), offset: int = Query(0, ge=0, le=1000000),
               limit: int = Query(50, ge=1, le=200)):
    """Link requests, newest first, with the proposed carrier and the carrier the name is linked to now."""

    store = registry_of(state)

    if store is None:
        return {"requests": [], "total": 0, "offset": offset, "limit": limit}

    return {**store.link_queue(state_filter, carrier_id, offset, limit), "offset": offset, "limit": limit}


@router.get("/carrier-registry/document-names", response_model=schemas.DocumentNamePage,
            response_model_exclude_unset=True, responses=schemas.ERRORS)
def document_names(principal=View, state: ApiState = Depends(get_state),
                   search: str = Query("", max_length=100),
                   link_state: Literal["all", "unlinked", "pending", "linked"] = Query("all", alias="state"),
                   offset: int = Query(0, ge=0, le=1000000), limit: int = Query(50, ge=1, le=200)):
    """
    Every carrier name printed on the documents, with the shipments and
    documents it was read from and its link state: linked (to which carrier),
    pending (a request waits for a second administrator) or unlinked.
    suggestions lists registered carriers whose legal or trading name is the
    same name apart from case, spaces and punctuation: a hint for a person,
    never applied by itself.
    """

    from api.routes_audit import carrier_inputs

    names = carrier_intelligence.document_names(carrier_inputs(state)[0])
    store = registry_of(state)
    links = {row["name_key"]: row for row in store.name_links()} if store else {}
    pending = store.pending_by_name() if store else {}
    carriers = store.carriers() if store else []

    for key, link in links.items():
        # A linked name no document prints any more stays listed, so it can still be unlinked
        names.setdefault(key, {"name": link["display_name"], "spellings": [link["display_name"]], "shipment_ids": [],
                               "documents": [], "shipments": 0})

    by_loose = {}
    for carrier in carriers:
        for value in (carrier["legal_name"], carrier["trading_name"]):
            if value and loose_key(value):
                by_loose.setdefault(loose_key(value), {})[carrier["id"]] = {"id": carrier["id"], "legal_name": carrier["legal_name"]}

    rows = []

    for key, entry in names.items():
        link = links.get(key)
        request = pending.get(key)
        rows.append({
            "name_key": key, "name": entry["name"], "spellings": entry["spellings"], "shipments": entry["shipments"],
            "shipment_ids": entry["shipment_ids"][:50], "documents": entry["documents"],
            "state": "pending" if request else "linked" if link else "unlinked",
            "linked_carrier": {"id": link["carrier_id"], "legal_name": link["legal_name"]} if link else None,
            "pending_request": ({field: request[field] for field in
                                 ("id", "carrier_id", "carrier_legal_name", "proposed_by", "proposed_at", "reason")}
                                if request else None),
            "suggestions": [item for item in by_loose.get(loose_key(entry["name"]), {}).values()
                            if not link or item["id"] != link["carrier_id"]],
        })

    counts = {name: sum(row["state"] == name for row in rows) for name in ("unlinked", "pending", "linked")}
    wanted = " ".join(search.split()).casefold()

    if wanted:
        rows = [row for row in rows if any(wanted in text.casefold() for text in [row["name"], *row["spellings"]])]

    if link_state != "all":
        rows = [row for row in rows if row["state"] == link_state]

    rows.sort(key=lambda row: (-row["shipments"], row["name"].casefold()))

    return {"names": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit, "counts": counts}
