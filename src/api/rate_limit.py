"""
Application-level rate limits for the expensive and sensitive operations of
the API (docs/RATE_LIMITING.md).

Each rule is a token bucket per caller: `limit` requests, refilled evenly over
`period` seconds, so a burst up to the limit passes and a steady flow above it
is refused. A refused request gets 429 with Retry-After (whole seconds until
the next request would pass) and is never run.

Who the caller is:
    client   the client address, as the server sees it. Behind a reverse proxy
             that address comes from X-Forwarded-For only when the proxy is in
             LOGIDOC_FORWARDED_ALLOW_IPS (uvicorn's ProxyHeadersMiddleware,
             which takes the right-most address it does not trust): a client
             cannot choose its own address by sending the header. Nothing here
             reads X-Forwarded-For.
    session  the signed-in session (a digest of its cookie, never the cookie),
             or the client address without one.

Sign-in counts failed attempts only (any answer of 400 or more): people
signing in normally from one office address are never slowed down, and a
guessing client is, on top of the per-account lockout (src/auth.py).

Memory is bounded: at most `max_keys` buckets are kept, the least recently
used dropped first (a dropped bucket starts full again). Limits are per server
process; several processes behind one proxy each apply them, and the proxy's
own limits (deploy/nginx/logidoc.conf) apply to all of them together.
"""

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

log = logging.getLogger("logidoc.api")

CLIENT = "client"
SESSION = "session"

ALL = "all"
FAILURES = "failures"


@dataclass(frozen=True)
class Rule:

    name: str
    methods: frozenset
    pattern: str
    limit: int
    period: float
    by: str = SESSION
    counts: str = ALL
    label: str = ""
    compiled: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):

        if self.limit < 1 or self.period <= 0:
            raise ValueError(f"Rate limit {self.name}: limit and period must be positive.")

        object.__setattr__(self, "compiled", re.compile(self.pattern))

    def matches(self, method, path):

        return method in self.methods and self.compiled.fullmatch(path) is not None


# The paths are the /api form: /api/v1/... is resolved to it before the limits are applied
RULES = (
    Rule("sign_in", frozenset({"POST"}), r"/api/auth/login", 10, 60, by=CLIENT, counts=FAILURES,
         label="failed sign-in attempts"),
    Rule("assistant", frozenset({"POST"}), r"/api/(chat|assistant)", 20, 60, label="questions to the assistant"),
    Rule("documents", frozenset({"POST"}),
         r"/api/(ingestion/upload|ingestion/[^/]+/retry|documents/(replace|reprocess|remove))", 30, 60,
         label="document uploads and changes"),
    Rule("audit_jobs", frozenset({"POST"}),
         r"/api/(extractions/(run|recompute)|history/runs|shipments/[^/]+/re-audit|admin/backups"
         r"|admin/backups/[^/]+/verify|admin/verify-history|integrations/connections/[^/]+/(sync|test)"
         r"|integrations/webhooks/[^/]+/test|integrations/jobs/[^/]+/(run|retry))", 30, 60,
         label="audit runs, extractions, backups and integration runs"),
    Rule("exports", frozenset({"GET"}),
         r"/api/(reporting/[^/]+\.(csv|pdf)|reports/(shipments/)?[^/]+\.(csv|pdf)|trends/export\.csv)",
         60, 60, label="report downloads"),
    Rule("accounts", frozenset({"POST", "PATCH"}), r"/api/admin/users(/[^/]+)?", 20, 60, label="account changes"),
)


class Limiter:
    """Token buckets by (rule, caller), at most max_keys of them. Thread-safe."""

    def __init__(self, max_keys=10000, clock=time.monotonic):

        self.max_keys = max_keys
        self.clock = clock
        self.lock = threading.Lock()
        self.buckets = OrderedDict()

    def bucket(self, rule, key, now):
        """(tokens, last refill) of a bucket, refilled up to now. Must hold the lock."""

        tokens, last = self.buckets.get((rule.name, key), (float(rule.limit), now))
        tokens = min(float(rule.limit), tokens + (now - last) * rule.limit / rule.period)

        return tokens

    def wait(self, rule, tokens):
        """Seconds until one token is back."""

        # Rounded first: 0.95 of a token at 20 s a token is 1 s to wait, not 2 by a floating-point hair
        return max(1, math.ceil(round((1 - tokens) * rule.period / rule.limit, 6)))

    def check(self, rule, key):
        """None when a request may pass, else the seconds to wait (Retry-After). Takes nothing."""

        with self.lock:
            tokens = self.bucket(rule, key, self.clock())

        return None if tokens >= 1 else self.wait(rule, tokens)

    def take(self, rule, key):
        """Takes one token: None when the request may pass, else the seconds to wait (nothing taken)."""

        with self.lock:

            now = self.clock()
            tokens = self.bucket(rule, key, now)

            if tokens < 1:
                self.store(rule, key, tokens, now)
                return self.wait(rule, tokens)

            self.store(rule, key, tokens - 1, now)

            return None

    def store(self, rule, key, tokens, now):

        self.buckets[(rule.name, key)] = (tokens, now)
        self.buckets.move_to_end((rule.name, key))

        while len(self.buckets) > self.max_keys:
            self.buckets.popitem(last=False)


def caller(scope, rule, cookie_name):
    """The caller's key for a rule: the client address, or a digest of the session cookie."""

    host = (scope.get("client") or ("unknown", 0))[0] or "unknown"

    if rule.by == SESSION:

        for name, value in scope.get("headers") or ():

            if name == b"cookie":

                for part in value.decode("latin-1").split(";"):
                    cookie, _, content = part.strip().partition("=")

                    if cookie == cookie_name and content:
                        return "session:" + hashlib.sha256(content.encode("latin-1")).hexdigest()[:32]

    return "client:" + host


class RateLimit:
    """
    ASGI middleware applying RULES (or the rules given) to /api requests. Placed
    inside the version prefix resolution and the proxy header handling, so it
    sees /api/... paths and the client address the server trusts.
    """

    def __init__(self, app, rules=RULES, limiter=None, cookie_name="logidoc_session", registry=None):

        self.app = app
        self.rules = tuple(rules)
        self.limiter = limiter or Limiter()
        self.cookie_name = cookie_name
        self.registry = registry

    async def __call__(self, scope, receive, send):

        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)

        rule = next((rule for rule in self.rules if rule.matches(scope["method"], scope["path"])), None)

        if rule is None:
            return await self.app(scope, receive, send)

        key = caller(scope, rule, self.cookie_name)
        wait = self.limiter.check(rule, key) if rule.counts == FAILURES else self.limiter.take(rule, key)

        if wait is not None:
            return await self.refuse(rule, wait, send)

        if rule.counts == ALL:
            return await self.app(scope, receive, send)

        async def counted(message):

            if message["type"] == "http.response.start" and 400 <= message["status"] and message["status"] != 429:
                self.limiter.take(rule, key)

            await send(message)

        return await self.app(scope, receive, counted)

    async def refuse(self, rule, wait, send):

        if self.registry is not None:
            self.registry.inc("logidoc_rate_limited_total", {"rule": rule.name})

        log.warning("Rate limit reached: %s (retry after %s s).", rule.name, wait)

        body = json.dumps({
            "detail": f"Too many {rule.label or 'requests'}: try again in {wait} s.",
        }).encode("utf-8")

        await send({
            "type": "http.response.start", "status": 429,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                        (b"retry-after", str(wait).encode())],
        })
        await send({"type": "http.response.body", "body": body})
