"""
LogiDoc-RAG HTTP API application

    python -m uvicorn api.app:app --app-dir src       (development)
    python src/api/serve.py                            (checks the configuration first)

create_app() builds the FastAPI application: the routers, CORS limited to
the configured origins, the CSRF header check, security headers, error
handlers that never return a stack trace, and (when the React build exists)
the single-page frontend.
"""

import logging
import sys
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

import config
from api import routes_assistant, routes_audit, routes_auth, routes_extraction, routes_ingestion, routes_workflow
from api.core import ApiSettings, ApiState, csrf_guard
from audit_service import Locations
from auth import ConfigError
from extraction_store import STORE_STATE_ERRORS
from review_store import Policy


log = logging.getLogger("logidoc.api")

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
    )


def create_app(state=None, settings=None):
    """
    The application. state: an ApiState (the tests build one on
    temporary folders); by default the paths of config.py.
    """

    settings = settings or (state.settings if state else ApiSettings.from_env())
    state = state or default_state(settings)

    app = FastAPI(
        title="LogiDoc-RAG API", version="1.0",
        docs_url=None if state.settings.cookie_secure else "/api/docs",
        redoc_url=None, openapi_url=None if state.settings.cookie_secure else "/api/openapi.json",
    )

    app.state.logidoc = state

    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"], allow_headers=["Content-Type", "X-LogiDoc-Client"],
        max_age=600,
    )

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
                   routes_assistant.router, routes_extraction.router, routes_extraction.history_router):
        app.include_router(router, dependencies=[Depends(csrf_guard)])

    @app.get("/api/health")
    def health():

        return {"status": "ok"}

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError):

        problems = [
            f"{'.'.join(str(part) for part in item['loc'][1:])}: {item['msg']}" if len(item["loc"]) > 1 else item["msg"]
            for item in error.errors()
        ]

        return JSONResponse(status_code=422, content={"detail": "; ".join(problems) or "Invalid request."})

    async def store_not_usable(request: Request, error: Exception):

        # Unreadable, too new, invalid, awaiting an upgrade, or an upgrade that failed: never an empty answer.
        # The messages name no path and no document text.
        return JSONResponse(status_code=503, content={"detail": str(error)})

    for kind in STORE_STATE_ERRORS:
        app.add_exception_handler(kind, store_not_usable)

    @app.exception_handler(ConfigError)
    async def misconfigured(request: Request, error: ConfigError):

        return JSONResponse(status_code=500, content={"detail": str(error)})

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):

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
