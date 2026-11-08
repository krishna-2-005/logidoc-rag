"""
Carrier master data (src/carrier_registry.py): registered carriers, their
validated identifiers, and the approved links of document names to them.

Reading needs exceptions.view (like Carrier Intelligence). Registering,
changing and proposing or deciding links need carriers.manage
(administrators); a link is decided by a different administrator than the one
who proposed it. Nothing here changes an audit or a recorded history.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import authz
from api import schemas
from api.core import ApiState, get_state, require
from carrier_registry import CarrierRegistry, RegistryConflict, RegistryError

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

    return CarrierRegistry(path)


def resolver_of(state):
    """{document name key: registered carrier} of the approved links ({} without a registry)."""

    registry = registry_of(state)

    return registry.resolver() if registry is not None else {}


def refusal(error):

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
