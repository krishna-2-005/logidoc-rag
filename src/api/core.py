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
    hosts       LOGIDOC_TRUSTED_HOSTS: the Host names answered (any in
                development; required in production)
    size        LOGIDOC_MAX_REQUEST_KB caps a request body (1024 KB); an
                upload may carry LOGIDOC_MAX_UPLOAD_FILES files of
                LOGIDOC_MAX_UPLOAD_MB each
    HTTPS       behind a reverse proxy at LOGIDOC_FORWARDED_ALLOW_IPS
                (127.0.0.1), X-Forwarded-Proto says whether the client used
                HTTPS; LOGIDOC_HTTPS_REDIRECT=1 redirects plain HTTP, and
                Strict-Transport-Security is sent for LOGIDOC_HSTS_SECONDS
                (a year in production) on HTTPS responses

Authentication is only as safe as its transport: serve the API over
HTTPS (docs/PRODUCTION_DEPLOYMENT.md). LOGIDOC_ENV selects development,
test or production; production refuses to start without its settings
(src/environment.py).
"""

import ipaddress
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Depends, HTTPException, Request

import auth
import authz
import environment
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


def whole(environ, name, default):

    try:
        return int((environ.get(name) or "").strip() or default)
    except ValueError:
        # Reported by environment.validate at startup; the default keeps building the settings possible
        return default


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
    environment: str = environment.DEVELOPMENT
    trusted_hosts: tuple = ("*",)
    max_request_kb: int = 1024
    https_redirect: bool = False
    hsts_seconds: int = 0
    forwarded_allow_ips: str | None = "127.0.0.1"
    background_jobs: bool = False

    @staticmethod
    def from_env(environ=None):

        environ = os.environ if environ is None else environ

        origins = tuple(
            item.strip() for item in (environ.get("LOGIDOC_CORS_ORIGINS") or ",".join(DEFAULT_ORIGINS)).split(",")
            if item.strip()
        )

        if "*" in origins:
            raise ConfigError("LOGIDOC_CORS_ORIGINS must list origins; '*' is not allowed with credentials.")

        try:
            profile = environment.environment_of(environ)
        except ValueError:
            profile = environment.PRODUCTION  # an unknown profile gets the strictest defaults; check_startup refuses it

        production = profile == environment.PRODUCTION
        hosts = tuple(item.strip() for item in (environ.get("LOGIDOC_TRUSTED_HOSTS") or "").split(",") if item.strip())

        frontend = Path(environ.get("LOGIDOC_FRONTEND_DIR") or Path(__file__).resolve().parents[2] / "frontend" / "dist")

        return ApiSettings(
            cors_origins=origins,
            cookie_secure=production or truthy(environ.get("LOGIDOC_COOKIE_SECURE")),
            host=environ.get("LOGIDOC_API_HOST") or "127.0.0.1",
            port=whole(environ, "LOGIDOC_API_PORT", 8000),
            max_upload_mb=max(whole(environ, "LOGIDOC_MAX_UPLOAD_MB", 25), 1),
            max_upload_files=max(whole(environ, "LOGIDOC_MAX_UPLOAD_FILES", 20), 1),
            frontend_dir=frontend if frontend.is_dir() else None,
            environ=dict(environ),
            environment=profile,
            trusted_hosts=hosts or ("*",),
            max_request_kb=max(whole(environ, "LOGIDOC_MAX_REQUEST_KB", 1024), 16),
            https_redirect=truthy(environ.get("LOGIDOC_HTTPS_REDIRECT")),
            hsts_seconds=whole(environ, "LOGIDOC_HSTS_SECONDS", 31536000 if production else 0),
            forwarded_allow_ips=(environ.get("LOGIDOC_FORWARDED_ALLOW_IPS") or "127.0.0.1").strip(),
            # The backup schedule and the monitor: started by src/api/serve.py (never under LOGIDOC_ENV=test)
            background_jobs=profile != environment.TEST and truthy(environ.get("LOGIDOC_BACKGROUND_JOBS")),
        )

    @property
    def max_request_bytes(self):

        return self.max_request_kb * 1024

    @property
    def max_upload_request_bytes(self):
        """A whole upload: every file at its limit, plus the form's own overhead."""

        return self.max_upload_files * self.max_upload_mb * 1024 * 1024 + 1024 * 1024

    def check_startup(self):
        """
        Raises ConfigError for a configuration that must not start: any error
        of environment.validate (production without its settings, malformed
        values), and local mode (no sign-in) on a non-loopback address.
        Returns the sign-in mode.
        """

        report = environment.validate(self.environ or dict(os.environ))

        if report["errors"]:
            raise ConfigError("The configuration cannot start the server:\n  - " + "\n  - ".join(report["errors"]))

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

    def __init__(self, settings, locations, user_db, policy=None, service=None, environ=None, backup_dir=None):

        self.settings = settings
        self.locations = locations
        self.user_db = Path(user_db)
        self.policy = policy or Policy()
        self.service = service or AuditService(locations, self.policy)
        self.environ = environ if environ is not None else (settings.environ or None)
        self.user_store_cache = None
        # Without LOGIDOC_BACKUP_DIR: config.BACKUP_DIR for the real installation (api.app.default_state),
        # otherwise next to the data (a test's own temporary folder)
        self.backup_dir = Path(backup_dir) if backup_dir else Path(locations.raw_dir).parent / "backups"
        self.operations_cache = None
        self.operations_lock = threading.Lock()
        self.integrations_cache = None
        self.integrations_lock = threading.Lock()
        # Tests replace the webhook transport, the DNS resolver and the sync's sleep (never the server)
        self.integration_options = {}

    @property
    def mode(self):

        return auth.auth_mode(self.environ)

    @property
    def operations(self):
        """Metrics, alerts, backups and the monitor (src/operations.py), built on first use."""

        with self.operations_lock:

            if self.operations_cache is None:

                import alerts
                import backup_automation
                import config
                import operations

                environ = self.environ if self.environ is not None else dict(os.environ)
                self.operations_cache = operations.Operations(
                    self,
                    backup_automation.BackupSettings.from_env(environ, self.backup_dir),
                    alerts.AlertSettings.from_env(environ),
                    configuration=backup_automation.recovery_configuration(config, environ),
                    monitor_interval=int((environ.get("LOGIDOC_MONITOR_INTERVAL") or "").strip() or 300),
                )

            return self.operations_cache

    @property
    def integrations(self):
        """Webhooks, TMS/ERP integrations and the scheduler (src/integration_runtime.py), built on first use."""

        with self.integrations_lock:

            if self.integrations_cache is None:

                import integration_runtime
                import metrics

                environ = self.environ if self.environ is not None else dict(os.environ)
                self.integrations_cache = integration_runtime.IntegrationRuntime(
                    self, environ, metrics=metrics.REGISTRY, alerts=self.operations.alerts, **self.integration_options)

            return self.integrations_cache

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
