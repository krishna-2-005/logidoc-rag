"""
ASGI middleware for the transport rules (docs/PRODUCTION_DEPLOYMENT.md).

    BodyLimit    a request body larger than the limit is refused with 413
                 before it is read in full: LOGIDOC_MAX_REQUEST_KB for JSON
                 and other bodies, the whole-upload limit for multipart
                 uploads (each file is still checked against its own limit
                 by the route). Both a declared Content-Length and the bytes
                 actually received are checked
    HttpsPolicy  with LOGIDOC_HTTPS_REDIRECT=1, a plain-HTTP request is
                 redirected (308) to HTTPS, except the health and readiness
                 checks (probes on the machine itself); HTTPS responses carry
                 Strict-Transport-Security for LOGIDOC_HSTS_SECONDS. Behind a
                 reverse proxy the scheme comes from X-Forwarded-Proto, trusted
                 only from LOGIDOC_FORWARDED_ALLOW_IPS (uvicorn's
                 ProxyHeadersMiddleware, added by api.app)
"""

import json

from fastapi import HTTPException

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PROBES = ("/api/health", "/api/ready")


class RequestTooLarge(HTTPException):
    """An HTTPException, so FastAPI passes it on as it is while it reads the body (413, not 400)."""

    def __init__(self, limit):

        super().__init__(status_code=413, detail=f"The request is larger than the {describe(limit)} limit.")


def describe(size):

    return f"{size / 1048576:g} MB" if size >= 1048576 else f"{size // 1024} KB"


def header(scope, name):

    for key, value in scope.get("headers", []):
        if key.decode("latin-1").lower() == name:
            return value.decode("latin-1")

    return None


async def reply(send, status, detail, headers=()):

    body = json.dumps({"detail": detail}).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *headers]})
    await send({"type": "http.response.body", "body": body})


class BodyLimit:

    def __init__(self, app, limit, upload_limit):

        self.app = app
        self.limit = limit
        self.upload_limit = upload_limit

    async def __call__(self, scope, receive, send):

        if scope["type"] != "http" or scope.get("method") in SAFE_METHODS:
            return await self.app(scope, receive, send)

        content_type = (header(scope, "content-type") or "").lower()
        limit = self.upload_limit if content_type.startswith("multipart/form-data") else self.limit
        declared = header(scope, "content-length")

        if declared is not None:
            try:
                too_large = int(declared) > limit
            except ValueError:
                return await reply(send, 400, "The Content-Length header is not a number.")
            if too_large:
                scope.setdefault("state", {})["error_category"] = "payload_too_large"
                return await reply(send, 413, RequestTooLarge(limit).detail)

        received = 0
        started = {"value": False}

        async def counted():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestTooLarge(limit)
            return message

        async def tracked(message):
            if message["type"] == "http.response.start":
                started["value"] = True
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except RequestTooLarge as error:
            if started["value"]:
                raise
            scope.setdefault("state", {})["error_category"] = "payload_too_large"
            await reply(send, 413, error.detail)


def host_name(value):
    """The host of a Host header, without its port ("[::1]:8000" -> "[::1]")."""

    value = (value or "").strip().lower()

    if value.startswith("["):
        return value[:value.find("]") + 1] if "]" in value else value

    return value.split(":", 1)[0]


class TrustedHosts:
    """
    Answers only the host names allowed (exact, or *.domain for its
    subdomains); any other Host header gets 400 in the usual error shape.
    "*" allows every host (development).
    """

    def __init__(self, app, allowed):

        self.app = app
        self.any = "*" in allowed
        self.exact = {host.lower() for host in allowed if not host.startswith("*.")}
        self.suffixes = tuple(host[1:].lower() for host in allowed if host.startswith("*."))

    async def __call__(self, scope, receive, send):

        if scope["type"] != "http" or self.any:
            return await self.app(scope, receive, send)

        host = host_name(header(scope, "host"))

        if host in self.exact or (self.suffixes and host.endswith(self.suffixes)):
            return await self.app(scope, receive, send)

        scope.setdefault("state", {})["error_category"] = "bad_request"
        await reply(send, 400, "Invalid host header.")


class HttpsPolicy:

    def __init__(self, app, redirect=False, hsts_seconds=0):

        self.app = app
        self.redirect = redirect
        self.hsts = f"max-age={int(hsts_seconds)}; includeSubDomains".encode() if hsts_seconds else None

    async def __call__(self, scope, receive, send):

        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        secure = scope.get("scheme") == "https"

        if self.redirect and not secure and scope.get("path") not in PROBES:
            host = header(scope, "host") or ""
            query = scope.get("query_string", b"").decode("latin-1")
            location = f"https://{host}{scope.get('root_path', '')}{scope.get('path', '/')}" + (f"?{query}" if query else "")
            return await reply(send, 308, "HTTPS is required.", [(b"location", location.encode("latin-1"))])

        if not (secure and self.hsts):
            return await self.app(scope, receive, send)

        async def with_hsts(message):
            if message["type"] == "http.response.start":
                message = dict(message, headers=[*message.get("headers", []), (b"strict-transport-security", self.hsts)])
            await send(message)

        await self.app(scope, receive, with_hsts)
