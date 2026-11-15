"""
Rate limits of the expensive and sensitive operations (src/api/rate_limit.py,
docs/RATE_LIMITING.md): 429 with Retry-After, per caller, never trusting a
client's own X-Forwarded-For, bounded memory, and normal use untouched.

Everything runs on temporary folders (tests/test_api.py's Rig).
"""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import metrics  # noqa: E402
from api import rate_limit  # noqa: E402
from api.rate_limit import CLIENT, FAILURES, RULES, Limiter, Rule  # noqa: E402
from test_api import CLIENT as CLIENT_HEADERS  # noqa: E402
from test_api import PASSWORD, Rig  # noqa: E402


def rule_named(name):

    return next(rule for rule in RULES if rule.name == name)


class Clock:

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ============================================================
# The buckets
# ============================================================

def test_a_bucket_lets_a_burst_through_then_refuses_with_the_wait():

    clock = Clock()
    limiter = Limiter(clock=clock)
    rule = Rule("test", frozenset({"GET"}), r"/api/x", 3, 60)

    assert [limiter.take(rule, "a") for _ in range(3)] == [None, None, None]
    assert limiter.take(rule, "a") == 20          # one request back every 60 / 3 s

    clock.now += 19
    assert limiter.take(rule, "a") == 1

    clock.now += 1
    assert limiter.take(rule, "a") is None        # refilled evenly, not all at once
    assert limiter.take(rule, "a") == 20

    clock.now += 600
    assert [limiter.take(rule, "a") for _ in range(4)] == [None, None, None, 20]   # never above the limit


def test_callers_and_rules_have_their_own_buckets():

    limiter = Limiter(clock=Clock())
    first = Rule("first", frozenset({"GET"}), r"/api/x", 1, 60)
    second = Rule("second", frozenset({"GET"}), r"/api/y", 1, 60)

    assert limiter.take(first, "a") is None
    assert limiter.take(first, "a") is not None
    assert limiter.take(first, "b") is None
    assert limiter.take(second, "a") is None


def test_memory_is_bounded_least_recently_used_first():

    limiter = Limiter(max_keys=3, clock=Clock())
    rule = Rule("test", frozenset({"GET"}), r"/api/x", 1, 60)

    for key in ("a", "b", "c", "d"):
        limiter.take(rule, key)

    assert len(limiter.buckets) == 3
    assert ("test", "a") not in limiter.buckets
    assert limiter.take(rule, "a") is None        # forgotten: starts full again


def test_a_rule_needs_a_positive_limit_and_period():

    import pytest

    with pytest.raises(ValueError):
        Rule("bad", frozenset({"GET"}), r"/api/x", 0, 60)

    with pytest.raises(ValueError):
        Rule("bad", frozenset({"GET"}), r"/api/x", 1, 0)


def test_the_rules_cover_the_expensive_and_sensitive_operations():

    covered = {
        ("POST", "/api/auth/login"): "sign_in",
        ("POST", "/api/chat"): "assistant",
        ("POST", "/api/assistant"): "assistant",
        ("POST", "/api/ingestion/upload"): "documents",
        ("POST", "/api/ingestion/7/retry"): "documents",
        ("POST", "/api/documents/replace"): "documents",
        ("POST", "/api/documents/reprocess"): "documents",
        ("POST", "/api/documents/remove"): "documents",
        ("POST", "/api/extractions/run"): "audit_jobs",
        ("POST", "/api/extractions/recompute"): "audit_jobs",
        ("POST", "/api/history/runs"): "audit_jobs",
        ("POST", "/api/shipments/SHJ-1/re-audit"): "audit_jobs",
        ("POST", "/api/admin/backups"): "audit_jobs",
        ("POST", "/api/admin/backups/b1/verify"): "audit_jobs",
        ("POST", "/api/admin/verify-history"): "audit_jobs",
        ("POST", "/api/integrations/connections/3/sync"): "audit_jobs",
        ("POST", "/api/integrations/webhooks/3/test"): "audit_jobs",
        ("POST", "/api/integrations/jobs/x/run"): "audit_jobs",
        ("GET", "/api/reporting/discrepancies.csv"): "exports",
        ("GET", "/api/reporting/discrepancies.pdf"): "exports",
        ("GET", "/api/reports/exceptions.csv"): "exports",
        ("GET", "/api/reports/shipments/SHJ-1.pdf"): "exports",
        ("GET", "/api/trends/export.csv"): "exports",
        ("POST", "/api/admin/users"): "accounts",
        ("PATCH", "/api/admin/users/bob"): "accounts",
    }

    for (method, path), name in covered.items():
        found = [rule.name for rule in RULES if rule.matches(method, path)]
        assert found == [name], (method, path, found)

    # Ordinary reads and previews are not limited
    for method, path in (("GET", "/api/exceptions"), ("GET", "/api/reporting/discrepancies"),
                         ("GET", "/api/shipments/SHJ-1"), ("GET", "/api/session"), ("POST", "/api/auth/logout")):
        assert not [rule.name for rule in RULES if rule.matches(method, path)], (method, path)


# ============================================================
# Through the API
# ============================================================

def test_failed_sign_ins_are_limited_per_client_and_answer_429_with_retry_after(tmp_path):

    rig = Rig(tmp_path, "required")
    client = rig.client()
    limit = rule_named("sign_in").limit
    before = metrics.REGISTRY.value("logidoc_rate_limited_total", {"rule": "sign_in"}) or 0

    for _ in range(limit):
        assert client.post("/api/auth/login", json={"username": "nobody", "password": "wrong guess"}).status_code == 401

    refused = client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})

    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1
    assert "try again" in refused.json()["detail"]
    assert refused.headers["X-Content-Type-Options"] == "nosniff"
    assert metrics.REGISTRY.value("logidoc_rate_limited_total", {"rule": "sign_in"}) == before + 1

    # Another client address is not affected
    other = TestClient(rig.app, raise_server_exceptions=False, headers=CLIENT_HEADERS, client=("203.0.113.9", 4000))
    assert other.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200


def test_successful_sign_ins_are_never_limited(tmp_path):
    """One office address, many people signing in: only failures count."""

    rig = Rig(tmp_path, "required")

    for _ in range(rule_named("sign_in").limit * 2):
        assert rig.client().post("/api/auth/login", json={"username": "bob", "password": PASSWORD}).status_code == 200


def test_a_client_cannot_escape_the_limit_with_its_own_forwarded_for_header(tmp_path):
    """No trusted proxy in front: X-Forwarded-For is ignored, every request is the same client."""

    rig = Rig(tmp_path, "required")
    client = rig.client()

    for number in range(rule_named("sign_in").limit):
        reply = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong guess"},
                            headers={"X-Forwarded-For": f"198.51.100.{number}"})
        assert reply.status_code == 401

    reply = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong guess"},
                        headers={"X-Forwarded-For": "198.51.100.200"})
    assert reply.status_code == 429


def test_behind_a_trusted_proxy_the_forwarded_client_is_the_caller(tmp_path):
    """The proxy (trusted by LOGIDOC_FORWARDED_ALLOW_IPS) names the client: each client has its own budget."""

    rig = Rig(tmp_path, "required", forwarded_allow_ips="10.0.0.1")
    proxy = TestClient(rig.app, raise_server_exceptions=False, headers=CLIENT_HEADERS, client=("10.0.0.1", 4000))

    for _ in range(rule_named("sign_in").limit):
        assert proxy.post("/api/auth/login", json={"username": "nobody", "password": "wrong"},
                          headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 401

    assert proxy.post("/api/auth/login", json={"username": "nobody", "password": "wrong"},
                      headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 429
    # A client's own leftmost entry is not trusted: the proxy's right-most address is the caller
    assert proxy.post("/api/auth/login", json={"username": "nobody", "password": "wrong"},
                      headers={"X-Forwarded-For": "203.0.113.5, 198.51.100.1"}).status_code == 429
    assert proxy.post("/api/auth/login", json={"username": "nobody", "password": "wrong"},
                      headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 401


def test_expensive_operations_are_limited_per_session_and_the_rest_continues(tmp_path):

    rules = (Rule("exports", frozenset({"GET"}), r"/api/(reporting/[^/]+\.(csv|pdf)|reports/[^/]+\.(csv|pdf))", 3, 60,
                  label="report downloads"),)
    rig = Rig(tmp_path, "required", rate_limits=rules)
    bob, carol = rig.client("bob"), rig.client("carol")

    for _ in range(3):
        assert bob.get("/api/reporting/discrepancies.csv").status_code == 200

    refused = bob.get("/api/v1/reports/exceptions.csv")
    wait = int(refused.headers["Retry-After"])
    assert refused.status_code == 429
    assert 1 <= wait <= 20          # one download back every 60 / 3 s, less what has refilled since
    assert refused.json()["detail"] == f"Too many report downloads: try again in {wait} s."

    # The same person's other work, and another person, are not affected
    assert bob.get("/api/reporting/discrepancies").status_code == 200
    assert bob.get("/api/exceptions").status_code == 200
    assert carol.get("/api/reporting/discrepancies.csv").status_code == 200


def test_the_server_applies_the_built_in_rules_by_default(tmp_path):

    rig = Rig(tmp_path, "required")
    stack = rig.app.build_middleware_stack()

    while stack is not None and not isinstance(stack, rate_limit.RateLimit):
        stack = getattr(stack, "app", None)

    assert stack is not None and stack.rules == RULES
    assert rule_named("sign_in").by == CLIENT and rule_named("sign_in").counts == FAILURES


def test_trusting_every_forwarded_address_is_refused_in_production_and_warned_about_otherwise():

    import environment

    message = "LOGIDOC_FORWARDED_ALLOW_IPS must name the reverse proxy's addresses"

    production = environment.validate({"LOGIDOC_ENV": "production", "LOGIDOC_FORWARDED_ALLOW_IPS": "127.0.0.1, *"})
    development = environment.validate({"LOGIDOC_FORWARDED_ALLOW_IPS": "*"})
    named = environment.validate({"LOGIDOC_FORWARDED_ALLOW_IPS": "127.0.0.1,10.0.0.0/8"})

    assert any(message in error for error in production["errors"])
    assert any(message in warning for warning in development["warnings"])
    assert not any(message in text for text in named["errors"] + named["warnings"])


def test_the_nginx_example_limits_requests_and_replaces_the_forwarded_address():

    text = (PROJECT_ROOT / "deploy" / "nginx" / "logidoc.conf").read_text(encoding="utf-8")

    assert "limit_req_zone $binary_remote_addr zone=logidoc_sign_in" in text
    assert "limit_req_zone $binary_remote_addr zone=logidoc_api" in text and "limit_req_status 429;" in text
    assert "limit_req zone=logidoc_sign_in" in text and "limit_req zone=logidoc_api" in text
    # The client's own X-Forwarded-For is replaced, never passed on
    assert text.count("proxy_set_header X-Forwarded-For   $remote_addr;") == text.count("proxy_pass ")
    assert "$proxy_add_x_forwarded_for" not in text
