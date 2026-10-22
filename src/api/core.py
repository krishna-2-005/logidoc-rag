"""
LogiDoc-RAG HTTP API: settings, shared state and request security

Everything a request cannot decide is decided here, on the server:

    paths       every folder and database comes from config.py (or the
                tests); no route takes a path from a client. Documents
                are looked up by name in the server's own listing.
    secrets     nothing secret is ever returned: the settings route
                lists a fixed set of harmless values, never a path, a
                database file or an environment value
    sessions    a signed-in user holds an HttpOnly, SameSite=Strict
                cookie with a random token; only the token's hash is
                stored (auth.UserStore). The cookie is Secure when
                LOGIDOC_COOKIE_SECURE=1 or LOGIDOC_ENV=production
    CSRF        every request that changes something must carry the
                header X-LogiDoc-Client. A browser page of another
                origin cannot send a custom header without a CORS
                preflight, which this API refuses for every origin not
                in LOGIDOC_CORS_ORIGINS; SameSite=Strict adds a second
                barrier
    CORS        only the origins in LOGIDOC_CORS_ORIGINS (default: the
                Vite development server on localhost); "*" is refused
                because the API uses credentials
    local mode  without LOGIDOC_AUTH_MODE=required nobody signs in, so
                the API only starts on a loopback address

Authentication is only as safe as its transport: serve the API over
HTTPS (see the README).
"""

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Depends, HTTPException, Request

import auth
import authz
from audit_service import AuditService
from auth import ConfigError, UserStore
from review_store import Policy


SESSION_COOKIE = "logidoc_session"
CLIENT_HEADER = "X-LogiDoc-Client"
CLIENT_VALUE = "web"

DEFAULT_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def truthy(value):

    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def is_loopback(host):

    if host in ("localhost",):
        return True

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class ApiSettings:

    cors_origins: tuple = DEFAULT_ORIGINS
    cookie_secure: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    max_upload_mb: int = 25
    max_upload_files: int = 20
    frontend_dir: Path | None = None
    environ: dict = field(default_factory=dict, repr=False)

    @staticmethod
    def from_env(environ=None):

        environ = os.environ if environ is None else environ

        origins = tuple(
            item.strip() for item in (environ.get("LOGIDOC_CORS_ORIGINS") or ",".join(DEFAULT_ORIGINS)).split(",")
            if item.strip()
        )

        if "*" in origins:
            raise ConfigError("LOGIDOC_CORS_ORIGINS must list origins; '*' is not allowed with credentials.")

        production = (environ.get("LOGIDOC_ENV") or "").strip().lower() == "production"

        frontend = Path(environ.get("LOGIDOC_FRONTEND_DIR") or Path(__file__).resolve().parents[2] / "frontend" / "dist")

        return ApiSettings(
            cors_origins=origins,
            cookie_secure=production or truthy(environ.get("LOGIDOC_COOKIE_SECURE")),
            host=environ.get("LOGIDOC_API_HOST") or "127.0.0.1",
            port=int(environ.get("LOGIDOC_API_PORT") or 8000),
            max_upload_mb=max(int(environ.get("LOGIDOC_MAX_UPLOAD_MB") or 25), 1),
            frontend_dir=frontend if frontend.is_dir() else None,
            environ=dict(environ),
        )

    def check_startup(self):
        """
        Raises ConfigError for a configuration that would expose the API
        without sign-in: local mode on a non-loopback address.
        """

        mode = auth.auth_mode(self.environ or None)

        if mode == auth.MODE_LOCAL and not is_loopback(self.host):
            raise ConfigError(
                f"Local development mode has no sign-in, so the API only starts on a loopback address, "
                f"not {self.host}. Set {auth.AUTH_MODE_ENV}=required to listen elsewhere."
            )

        return mode


class ApiState:
    """
    What the routes share: the audit service, the paths and the user
    store. Built once per application.
    """

    def __init__(self, settings, locations, user_db, policy=None, service=None, environ=None):

        self.settings = settings
        self.locations = locations
        self.user_db = Path(user_db)
        self.policy = policy or Policy()
        self.service = service or AuditService(locations, self.policy)
        self.environ = environ if environ is not None else (settings.environ or None)
        self.user_store_cache = None

    @property
    def mode(self):

        return auth.auth_mode(self.environ)

    def users(self):
        """
        The user store, opened on first use (never in local mode).
        """

        if self.user_store_cache is None:
            self.user_store_cache = UserStore(self.user_db)

        return self.user_store_cache


def get_state(request: Request) -> ApiState:

    return request.app.state.logidoc


def get_principal(request: Request, state: ApiState = Depends(get_state)):
    """
    Who is making the request. Raises 401 when sign-in is required and
    the session is missing, expired or revoked.
    """

    try:

        if state.mode == auth.MODE_LOCAL:
            return auth.local_principal(state.environ)

        principal = state.users().principal_for(request.cookies.get(SESSION_COOKIE))

    except ConfigError as error:
        raise HTTPException(status_code=500, detail=str(error)) from None

    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")

    return principal


def require(permission):
    """
    A route dependency: the principal must hold the permission (403
    otherwise).
    """

    def check(principal=Depends(get_principal)):

        try:
            return authz.require(principal, permission)
        except authz.PermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from None

    return check


def csrf_guard(request: Request):
    """
    Requests that change something must come from our own client.
    """

    if request.method not in SAFE_METHODS and request.headers.get(CLIENT_HEADER) != CLIENT_VALUE:
        raise HTTPException(status_code=403, detail=f"Missing {CLIENT_HEADER} header.")
