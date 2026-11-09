"""
Structured logs and request tracking (docs/OBSERVABILITY_AND_MONITORING.md).

Logs

    configure_logging() sends every logger through one handler: JSON lines
    (LOGIDOC_LOG_FORMAT=json, the production default) or readable text
    (text, the development default), at LOGIDOC_LOG_LEVEL (INFO), to stderr
    and, with LOGIDOC_LOG_FILE, also to a rotating file (10 MB x 5). Each
    JSON line holds timestamp, level, component (the logger), message,
    request_id and correlation_id when a request is being served, and the
    request fields: endpoint (the route template), method, status,
    duration_ms, outcome and error_category. Every message, field and
    traceback passes through redaction.redact: no credential, URL path,
    folder of a file path or e-mail address reaches a log, and long text is
    cut. Request bodies, document text, prompts and answers are never logged.

Requests (RequestContext, ASGI)

    Every response carries X-Request-ID: the client's own when it sent a
    valid one (letters, digits, . _ : -, at most 64), else a new one; and
    X-Correlation-ID: the client's, else the request ID (a reverse proxy or
    an integration passes its own to follow one operation across systems).
    One log line per API request (no query string, no body), the request
    metrics (count, duration, errors by category), and an unhandled error
    becomes {"detail": "Something went wrong on the server."} with status
    500, logged with its (redacted) traceback.
"""

import contextvars
import json
import logging
import logging.handlers
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import metrics as metrics_module
from redaction import redact

REQUEST_ID = contextvars.ContextVar("logidoc_request_id", default=None)
CORRELATION_ID = contextvars.ContextVar("logidoc_correlation_id", default=None)

ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

REQUEST_HEADER = "X-Request-ID"
CORRELATION_HEADER = "X-Correlation-ID"

# Record attributes copied into a JSON line when a log call sets them (extra={...})
FIELDS = ("endpoint", "operation", "method", "status", "duration_ms", "outcome", "error_category", "backup_id",
          "alert", "severity", "files", "shipments")

HANDLER_TAG = "_logidoc_handler"

INTERNAL_ERROR = "Something went wrong on the server."

access_log = logging.getLogger("logidoc.access")
error_log = logging.getLogger("logidoc.api")


# ============================================================
# LOG FORMAT
# ============================================================

def context_ids(record):

    return (getattr(record, "request_id", None) or REQUEST_ID.get(),
            getattr(record, "correlation_id", None) or CORRELATION_ID.get())


class JsonFormatter(logging.Formatter):

    def format(self, record):

        entry = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "component": record.name,
            "message": redact(record.getMessage()),
        }

        request_id, correlation_id = context_ids(record)

        if request_id:
            entry["request_id"] = request_id
        if correlation_id:
            entry["correlation_id"] = correlation_id

        for field in FIELDS:
            if hasattr(record, field):
                value = getattr(record, field)
                entry[field] = redact(value, limit=300) if isinstance(value, str) else value

        if record.exc_info and record.exc_info[0] is not None:
            entry["error_type"] = record.exc_info[0].__name__
            entry["traceback"] = redact(self.formatException(record.exc_info), limit=8000)

        return json.dumps(entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):

    def __init__(self):

        super().__init__("%(asctime)s %(levelname)-7s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record):

        request_id, _ = context_ids(record)
        message = redact(record.getMessage())
        extras = " ".join(f"{field}={getattr(record, field)}" for field in FIELDS if hasattr(record, field))
        text = f"{self.formatTime(record, self.datefmt)} {record.levelname:<7} {record.name}"

        if request_id:
            text += f" [{request_id}]"

        text += f" {message}" + (f" ({extras})" if extras else "")

        if record.exc_info and record.exc_info[0] is not None:
            text += "\n" + redact(self.formatException(record.exc_info), limit=8000)

        return text


def configure_logging(log_format="text", level="INFO", stream=None, log_file=None):
    """
    One handler for every logger (replaces the one set up before, so calling
    it twice is safe). Returns the handlers added.
    """

    root = logging.getLogger()

    for handler in list(root.handlers):
        if getattr(handler, HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()

    formatter = JsonFormatter() if log_format == "json" else TextFormatter()
    handlers = [logging.StreamHandler(stream or sys.stderr)]

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=10 * 1048576, backupCount=5,
                                                              encoding="utf-8"))

    for handler in handlers:
        setattr(handler, HANDLER_TAG, True)
        handler.setFormatter(formatter)
        root.addHandler(handler)

    root.setLevel(level)

    # Uvicorn's own loggers go through the same handler; its access log is replaced by RequestContext
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True

    logging.getLogger("uvicorn.access").disabled = True

    return handlers


# ============================================================
# REQUEST CONTEXT
# ============================================================

def valid_id(value):

    return bool(value) and bool(ID_PATTERN.match(value))


def category_for(status, error=None):
    """A fixed word for an error response, never its message."""

    if error is not None:
        return "database" if isinstance(error, sqlite3.Error) else "internal"

    return {400: "bad_request", 401: "authentication", 403: "permission", 404: "not_found", 405: "method_not_allowed",
            409: "conflict", 413: "payload_too_large", 415: "unsupported_media", 422: "validation",
            429: "rate_limited", 503: "unavailable", 504: "timeout"}.get(status, "internal" if status >= 500 else "client_error")


def outcome_of(status):

    return "success" if status < 400 else "client_error" if status < 500 else "server_error"


class RequestContext:
    """
    ASGI middleware: request and correlation IDs, one access log line and the
    request metrics per API request, and unhandled errors turned into a
    logged 500 without details. Exception handlers name a precise error
    category by setting request.state.error_category.
    """

    def __init__(self, app, registry=None):

        self.app = app
        self.registry = registry

    async def __call__(self, scope, receive, send):

        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        request_id = headers.get("x-request-id") if valid_id(headers.get("x-request-id")) else uuid.uuid4().hex
        correlation_id = headers.get("x-correlation-id") if valid_id(headers.get("x-correlation-id")) else request_id

        REQUEST_ID.set(request_id)
        CORRELATION_ID.set(correlation_id)

        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        started = time.perf_counter()
        sent = {"status": None}

        async def send_with_ids(message):

            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
                message = dict(message, headers=[
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode("ascii")),
                    (b"x-correlation-id", correlation_id.encode("ascii")),
                ])

            await send(message)

        error = None

        try:
            await self.app(scope, receive, send_with_ids)

        except Exception as raised:

            error = raised
            error_log.error("Unhandled error in %s %s", scope.get("method"), self.endpoint(scope), exc_info=True)

            if sent["status"] is None:
                body = json.dumps({"detail": INTERNAL_ERROR}).encode("utf-8")
                await send_with_ids({"type": "http.response.start", "status": 500, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": body})
            else:
                raise

        finally:
            self.finish(scope, sent["status"] or 500, time.perf_counter() - started, error)

    @staticmethod
    def endpoint(scope):
        """The route template (/api/shipments/{shipment_id}), never the raw path."""

        route = scope.get("route")
        path = getattr(route, "path", None) or getattr(route, "path_format", None)

        if path:
            return path

        return "/api (unmatched)" if str(scope.get("path", "")).startswith("/api") else "(static)"

    def finish(self, scope, status, duration, error):

        endpoint = self.endpoint(scope)
        category = None

        if status >= 400:
            category = (scope.get("state") or {}).get("error_category") or category_for(status, error)

        registry = self.registry or metrics_module.REGISTRY

        if endpoint != "(static)":
            registry.request(scope.get("method", "GET"), endpoint, status, duration)
            if category:
                registry.error(category)
                if category == "database":
                    registry.inc("logidoc_database_errors_total")

        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 and status not in (401, 404) else logging.INFO

        if endpoint == "(static)" and status < 400:
            return

        access_log.log(level, "%s %s %s", scope.get("method"), endpoint, status, extra={
            "endpoint": endpoint, "method": scope.get("method"), "status": status,
            "duration_ms": round(duration * 1000, 1), "outcome": outcome_of(status),
            **({"error_category": category} if category else {}),
        })


def set_error_category(request, category):
    """Called by an exception handler: the category RequestContext logs and counts for this response."""

    try:
        request.state.error_category = category
    except Exception:
        pass
