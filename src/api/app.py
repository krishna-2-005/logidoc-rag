"""
LogiDoc-RAG HTTP API application

    python -m uvicorn api.app:app --app-dir src       (development)
    python src/api/serve.py                            (checks the configuration first)

create_app() builds the FastAPI application: the routers, CORS limited to
the configured origins, the CSRF header check, security headers, error
handlers that never return a stack trace, and (when the React build exists)
the single-page frontend.

Middleware, outermost first:

    security headers         X-Content-Type-Options, X-Frame-Options, ...
    ProxyHeadersMiddleware   the client's scheme and address from
                             X-Forwarded-*, trusted only from
                             LOGIDOC_FORWARDED_ALLOW_IPS
    ApiVersions              /api/v1/... served as /api/...
    RequestContext           request IDs, access log, metrics, unhandled
                             errors as a bare 500 (src/observability.py)
    TrustedHosts             LOGIDOC_TRUSTED_HOSTS (and the loopback names,
                             for probes on the machine itself)
    HttpsPolicy              optional redirect to HTTPS, HSTS
    BodyLimit                413 for a body over the limit
    CORSMiddleware           the configured origins only
"""

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import config
import metrics
import readiness
from api import (routes_assistant, routes_audit, routes_auth, routes_carriers, routes_extraction, routes_ingestion,
                 routes_operations, routes_workflow)
from api.core import ApiSettings, ApiState, csrf_guard
from api.middleware import BodyLimit, HttpsPolicy, TrustedHosts
from observability import RequestContext, set_error_category
from audit_service import Locations
from auth import ConfigError
from extraction_store import STORE_STATE_ERRORS
from model_client import ModelRejected, ModelServiceError, ModelTimeout
from review_store import Policy


log = logging.getLogger("logidoc.api")

# The version of the API contract. /api/v1/... is the same API as /api/...:
# integrations pin /api/v1 (and see API-Version on every /api response); the
# web application keeps using /api. A change that is not backward compatible
# is published as /api/v2 beside it, never by changing v1 (docs/
# ENTERPRISE_INTEGRATION_READINESS.md, "Versioning").
API_VERSION = "1"
VERSIONED_PREFIX = f"/api/v{API_VERSION}/"


class ApiVersions:
    """
    ASGI middleware: serves /api/v1/<path> as /api/<path> (the same route,
    permissions and CSRF rule) and adds API-Version to every /api response.
    An unknown version (/api/v2/...) matches no route: 404.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):

        if scope["type"] != "http" or not scope["path"].startswith("/api"):
            return await self.app(scope, receive, send)

        if scope["path"].startswith(VERSIONED_PREFIX):
            path = "/api/" + scope["path"][len(VERSIONED_PREFIX):]
            scope = dict(scope, path=path, raw_path=path.encode("utf-8"))

        async def versioned(message):
            if message["type"] == "http.response.start":
                message = dict(message, headers=[*message.get("headers", []), (b"api-version", API_VERSION.encode())])
            await send(message)

        return await self.app(scope, receive, versioned)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def default_state(settings):

    return ApiState(
        settings,
        Locations.from_config(config),
        config.AUTH_DB_PATH,
        Policy(min_note=config.MIN_NOTE_LENGTH, require_approval=config.REQUIRE_APPROVAL),
        backup_dir=config.BACKUP_DIR,
    )


LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def allowed_hosts(settings):

    hosts = list(settings.trusted_hosts)

    return hosts if "*" in hosts else hosts + [host for host in LOOPBACK_HOSTS if host not in hosts]


def create_app(state=None, settings=None):
    """
    The application. state: an ApiState (the tests build one on
    temporary folders); by default the paths of config.py.
    """

    settings = settings or (state.settings if state else ApiSettings.from_env())
    state = state or default_state(settings)

    @asynccontextmanager
    async def lifespan(application):

        # The backup schedule and the monitor run only in the server process (serve.py), never in the tests
        if state.settings.background_jobs:
            state.operations.start()

        try:
            yield
        finally:
            if state.operations_cache is not None:
                state.operations_cache.stop()

    app = FastAPI(
        title="LogiDoc-RAG API", version="1.0",
        docs_url=None if state.settings.cookie_secure else "/api/docs",
        redoc_url=None, openapi_url=None if state.settings.cookie_secure else "/api/openapi.json",
        lifespan=lifespan,
    )

    app.state.logidoc = state

    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Content-Type", "X-LogiDoc-Client", "X-Request-ID", "X-Correlation-ID"],
        expose_headers=["API-Version", "X-Request-ID", "X-Correlation-ID"], max_age=600,
    )

    # Added innermost first: each add_middleware wraps the ones before it
    app.add_middleware(BodyLimit, limit=settings.max_request_bytes, upload_limit=settings.max_upload_request_bytes)
    app.add_middleware(HttpsPolicy, redirect=settings.https_redirect, hsts_seconds=settings.hsts_seconds)
    app.add_middleware(TrustedHosts, allowed=allowed_hosts(settings))
    app.add_middleware(RequestContext, registry=metrics.REGISTRY)

    # The version prefix is resolved before the request is tracked, CORS, CSRF and routing see it
    app.add_middleware(ApiVersions)

    if settings.forwarded_allow_ips:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.forwarded_allow_ips)

    @app.middleware("http")
    async def secure(request: Request, call_next):

        response = await call_next(request)

        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)

        if request.url.path.startswith("/api"):
            response.headers.setdefault("Cache-Control", "no-store")
        else:
            response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)

        return response

    for router in (routes_auth.router, routes_audit.router, routes_workflow.router, routes_ingestion.router,
                   routes_assistant.router, routes_extraction.router, routes_extraction.history_router,
                   routes_carriers.router, routes_operations.router):
        app.include_router(router, dependencies=[Depends(csrf_guard)])

    @app.get("/api/health")
    def health():

        # Alive: the process answers. Never touches a store or a service
        return {"status": "ok"}

    @app.get("/api/ready")
    def ready():

        # /api/health says the process answers; this says whether the work can be done (src/readiness.py).
        # Status words only, never a path, host or model name; 503 only when a folder or a database is unusable.
        # An offline model service is "degraded", never "not_ready"
        thresholds = state.operations.alerts.settings
        report = readiness.readiness(
            state.locations, extra_stores={"auth": state.user_db},
            disk_folders={"data": Path(state.locations.raw_dir).parent, "index": state.locations.chroma_dir,
                          "backups": state.operations.backup_settings.root},
            warning_mb=thresholds.disk_warning_mb, critical_mb=thresholds.disk_critical_mb,
        )

        for status in (readiness.READY, readiness.DEGRADED, readiness.NOT_READY):
            metrics.REGISTRY.set("logidoc_readiness", 1 if report["status"] == status else 0, {"status": status})

        metrics.REGISTRY.set("logidoc_ollama_available", 1 if report["checks"]["model_service"].get("ok") else 0)

        return JSONResponse(status_code=503 if report["status"] == readiness.NOT_READY else 200, content=report)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError):

        set_error_category(request, "validation")

        problems = [
            f"{'.'.join(str(part) for part in item['loc'][1:])}: {item['msg']}" if len(item["loc"]) > 1 else item["msg"]
            for item in error.errors()
        ]

        return JSONResponse(status_code=422, content={"detail": "; ".join(problems) or "Invalid request."})

    async def store_not_usable(request: Request, error: Exception):

        # Unreadable, too new, invalid, awaiting an upgrade, or an upgrade that failed: never an empty answer.
        # The messages name no path and no document text.
        set_error_category(request, "database")

        return JSONResponse(status_code=503, content={"detail": str(error)})

    for kind in STORE_STATE_ERRORS:
        app.add_exception_handler(kind, store_not_usable)

    @app.exception_handler(ModelServiceError)
    async def model_not_usable(request: Request, error: ModelServiceError):

        # Ollama stopped, too slow, misconfigured or refusing (model_client): the message names no host or
        # library; the cause is logged without a stack trace (it holds no document text)
        log.warning("Model service error in %s %s: %s (%s)", request.method, request.url.path, error, error.__cause__)
        set_error_category(request, "model_service")

        status = 504 if isinstance(error, ModelTimeout) else 422 if isinstance(error, ModelRejected) else 503

        return JSONResponse(status_code=status, content={"detail": str(error)})

    @app.exception_handler(ConfigError)
    async def misconfigured(request: Request, error: ConfigError):

        set_error_category(request, "configuration")

        return JSONResponse(status_code=500, content={"detail": str(error)})

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):

        # Reached only by an error outside RequestContext (which answers the others the same way).
        # The cause goes to the server log; the client never sees a stack trace or a path
        log.exception("Unhandled error in %s %s", request.method, request.url.path)

        return JSONResponse(status_code=500, content={"detail": "Something went wrong on the server."})

    mount_frontend(app, settings)

    return app


def mount_frontend(app, settings):
    """
    Serves the React build (frontend/dist) and falls back to index.html for
    client-side routes. API paths that do not exist stay 404.
    """

    root = settings.frontend_dir

    if not root:
        return

    assets = root / "assets"

    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):

        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found.")

        target = (root / path).resolve()

        if path and target.is_file() and root.resolve() in target.parents:
            return FileResponse(target)

        return FileResponse(index)


def build_default_app():
    """
    The application for uvicorn's factory mode.
    """

    return create_app()


def __getattr__(name):

    # `uvicorn api.app:app` builds the application on first use, so
    # importing this module (the tests) never opens a database
    if name == "app":
        return build_default_app()

    raise AttributeError(name)
