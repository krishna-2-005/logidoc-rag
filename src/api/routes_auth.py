"""
HTTP API: session, sign-in and sign-out, and account administration.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

import auth
import authz
from api.core import SESSION_COOKIE, ApiState, get_principal, get_state, require
from auth import AuthError


router = APIRouter(prefix="/api")


class LoginBody(BaseModel):

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class NewUserBody(BaseModel):

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: Literal["admin", "reviewer"]


class ChangeUserBody(BaseModel):

    role: Literal["admin", "reviewer"] | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=1, max_length=256)


def describe(principal, mode):

    if principal is None:
        return {"mode": mode, "authenticated": False, "user": None, "permissions": []}

    return {
        "mode": mode,
        "authenticated": bool(principal.authenticated),
        "user": {
            "username": principal.username, "role": principal.role,
            "label": authz.ROLE_LABELS.get(principal.role, principal.role),
        },
        "permissions": sorted(authz.PERMISSIONS.get(principal.role, ())),
    }


@router.get("/session")
def session(request: Request, state: ApiState = Depends(get_state)):
    """
    Who the caller is, for the interface to decide what to show. Never an
    error: a visitor who is not signed in gets authenticated = false.
    """

    try:
        principal = get_principal(request, state)
    except HTTPException:
        principal = None

    return describe(principal, state.mode)


@router.post("/auth/login")
def login(body: LoginBody, response: Response, state: ApiState = Depends(get_state)):

    if state.mode != auth.MODE_REQUIRED:
        raise HTTPException(status_code=400, detail="Sign-in is not used in local development mode.")

    try:
        opened = state.users().login(body.username, body.password)
    except AuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from None

    response.set_cookie(
        SESSION_COOKIE, opened["token"], httponly=True, samesite="strict",
        secure=state.settings.cookie_secure, max_age=int(auth.SESSION_LIFETIME.total_seconds()), path="/",
    )

    return describe(opened["principal"], state.mode)


@router.post("/auth/logout")
def logout(request: Request, response: Response, state: ApiState = Depends(get_state)):

    if state.mode == auth.MODE_REQUIRED:
        state.users().logout(request.cookies.get(SESSION_COOKIE))

    response.delete_cookie(SESSION_COOKIE, path="/")

    return {"authenticated": False}


# ------------------------------------------------------------
# Accounts (administrators)
# ------------------------------------------------------------

def accounts(state):

    if state.mode != auth.MODE_REQUIRED:
        raise HTTPException(status_code=400, detail="Local development mode has no user accounts.")

    return state.users()


@router.get("/admin/users")
def list_users(principal=Depends(require(authz.ADMIN_USERS)), state: ApiState = Depends(get_state)):

    return [
        {key: user[key] for key in ("username", "role", "active", "locked_until", "created_at", "password_changed_at")}
        for user in accounts(state).users(principal)
    ]


@router.post("/admin/users", status_code=201)
def create_user(body: NewUserBody, principal=Depends(require(authz.ADMIN_USERS)), state: ApiState = Depends(get_state)):

    try:
        name = accounts(state).create_user(body.username, body.password, body.role, actor=principal)
    except AuthError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    return {"username": name, "role": body.role}


@router.patch("/admin/users/{username}")
def change_user(
    username: str, body: ChangeUserBody, principal=Depends(require(authz.ADMIN_USERS)),
    state: ApiState = Depends(get_state),
):

    try:
        accounts(state).change_user(
            username, actor=principal, role=body.role, active=body.active, password=body.password
        )
    except AuthError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    return {"username": username, "updated": True}


@router.get("/admin/events")
def account_events(principal=Depends(require(authz.ADMIN_USERS)), state: ApiState = Depends(get_state)):

    return accounts(state).events(principal, limit=200)


@router.post("/admin/verify-history")
def verify_history(principal=Depends(require(authz.ADMIN_MAINTENANCE)), state: ApiState = Depends(get_state)):

    from exception_workflow import ExceptionWorkflow

    return ExceptionWorkflow(state.service.review_store()).verify_history(principal)
