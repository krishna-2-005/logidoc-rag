"""
Observability (src/observability.py, src/metrics.py, src/redaction.py):
request and correlation IDs, structured JSON logs without sensitive values,
and the operational metrics recorded by the API, the model client, the
ingestion and the freight audit. Synthetic data in temporary folders.

Run from the project root:
    python -m pytest tests/test_observability.py
"""

import io
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import ingestion_manager  # noqa: E402
import metrics  # noqa: E402
import model_client  # noqa: E402
import observability  # noqa: E402
from redaction import redact  # noqa: E402
from test_api import Rig, pdf_upload  # noqa: E402
from test_ingestion_manager import fake_embedding  # noqa: E402

HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture
def logs():
    """Every log line of the test as parsed JSON (the production format)."""

    root = logging.getLogger()
    level = root.level
    access = logging.getLogger("uvicorn.access").disabled
    stream = io.StringIO()
    handlers = observability.configure_logging("json", "INFO", stream=stream)

    def lines():
        return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]

    yield lines

    for handler in handlers:
        root.removeHandler(handler)
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = access


@pytest.fixture
def rig(tmp_path):

    return Rig(tmp_path, "required")


# ============================================================
# REQUEST AND CORRELATION IDS
# ============================================================

def test_every_response_carries_a_request_id(rig):

    reply = rig.client().get("/api/health")

    assert HEX32.match(reply.headers["x-request-id"])
    assert reply.headers["x-correlation-id"] == reply.headers["x-request-id"]
    assert rig.client().get("/api/health").headers["x-request-id"] != reply.headers["x-request-id"]


def test_a_valid_id_from_the_client_or_proxy_is_kept_and_an_invalid_one_replaced(rig):

    client = rig.client()

    kept = client.get("/api/v1/health", headers={"X-Request-ID": "nginx-7f3a.42", "X-Correlation-ID": "erp:order-991"})
    assert kept.headers["x-request-id"] == "nginx-7f3a.42" and kept.headers["x-correlation-id"] == "erp:order-991"

    for bad in ("has spaces", "x" * 65, "semi;colon", "<script>"):
        reply = client.get("/api/health", headers={"X-Request-ID": bad})
        assert HEX32.match(reply.headers["x-request-id"]), bad


def test_error_responses_carry_the_request_id_too(rig):

    for reply in (rig.client().get("/api/overview"), rig.client("bob").get("/api/admin/backups"),
                  rig.client("alice").get("/api/no-such-route")):
        assert reply.status_code in (401, 403, 404) and HEX32.match(reply.headers["x-request-id"])


# ============================================================
# STRUCTURED LOGS
# ============================================================

def test_one_json_line_per_request_with_the_route_template_and_no_query(rig, logs):

    client = rig.client("alice")
    reply = client.get("/api/history/shipments/SHJ-2026-001", params={"search": "secret-term"})

    [line] = [line for line in logs() if line["component"] == "logidoc.access"
              and line.get("endpoint") == "/api/history/shipments/{shipment_id}"]

    assert line["request_id"] == reply.headers["x-request-id"] == line["correlation_id"]
    assert line["method"] == "GET" and line["status"] == reply.status_code and line["outcome"] == "success"
    assert isinstance(line["duration_ms"], (int, float)) and line["level"] == "INFO"
    assert line["timestamp"].endswith("Z")
    assert "secret-term" not in json.dumps(logs()) and "SHJ-2026-001" not in line["message"]


def test_a_refused_request_is_logged_with_its_error_category(rig, logs):

    rig.client("bob").post("/api/admin/backups", json={})

    [line] = [line for line in logs() if line.get("endpoint") == "/api/admin/backups"]

    assert line["status"] == 403 and line["outcome"] == "client_error" and line["error_category"] == "permission"
    assert line["level"] == "WARNING"


def test_an_unhandled_error_is_a_bare_500_logged_without_secrets(rig, logs, monkeypatch):

    def explode():
        raise RuntimeError(r"C:\secret\folder\database.sqlite3 password=hunter2 mail ops@acme.test")

    monkeypatch.setattr(rig.state.service, "snapshot", explode)

    reply = rig.client("alice").get("/api/overview")

    assert reply.status_code == 500 and reply.json() == {"detail": "Something went wrong on the server."}
    assert HEX32.match(reply.headers["x-request-id"])

    errors = [line for line in logs() if line["level"] == "ERROR"]
    text = json.dumps(errors)

    assert any(line.get("error_type") == "RuntimeError" and "traceback" in line for line in errors)
    assert all(line.get("request_id") == reply.headers["x-request-id"] for line in errors)
    assert "hunter2" not in text and "secret\\\\folder" not in text and "ops@acme.test" not in text
    assert "<path>/database.sqlite3" in text
    assert any(line.get("error_category") == "internal" for line in errors)


def test_the_text_format_is_redacted_too(rig):

    stream = io.StringIO()
    root = logging.getLogger()
    level = root.level
    handlers = observability.configure_logging("text", "INFO", stream=stream)

    try:
        logging.getLogger("logidoc.test").warning("token=abc123 at /var/lib/logidoc/data/auth.sqlite3")
    finally:
        for handler in handlers:
            root.removeHandler(handler)
        root.setLevel(level)

    assert "abc123" not in stream.getvalue() and "token=<redacted>" in stream.getvalue()
    assert "<path>/auth.sqlite3" in stream.getvalue()


@pytest.mark.parametrize("text, absent, present", [
    ("password=hunter2", "hunter2", "password=<redacted>"),
    ('api_key: "sk-123"', "sk-123", "api_key: <redacted>"),
    ("Authorization: Bearer eyJabc.def", "eyJabc", "<redacted>"),
    ("cookie=logidoc_session=abcdef", "abcdef", "<redacted>"),
    ("https://user:pw@hooks.acme.test/x/y", "pw@", "<credentials>"),
    ("POST https://hooks.acme.test/services/T0/SECRET failed", "SECRET", "https://hooks.acme.test/<redacted>"),
    ("mail ops@swiftlines.example", "ops@swiftlines.example", "<email>"),
    (r"open D:\LogiDoc-RAG\data\audit_history.sqlite3", "LogiDoc-RAG", "<path>/audit_history.sqlite3"),
    ("\\\\fileserver\\share\\logidoc\\x.pdf", "fileserver", "<path>/x.pdf"),
    ("read /home/ops/logidoc/data/raw/a.pdf", "/home/ops", "<path>/a.pdf"),
])
def test_redaction(text, absent, present):

    cleaned = redact(text)

    assert absent not in cleaned and present in cleaned


@pytest.mark.parametrize("error", [
    PermissionError(13, "Access is denied", r"C:\Users\kuchu\AppData\Local\Temp\x\.manifest.sha256.ab.tmp", None,
                    r"C:\Users\kuchu\AppData\Local\Temp\x\manifest.sha256"),
    FileNotFoundError(2, "No such file or directory", r"D:\LogiDoc-RAG\data\audit_history.sqlite3"),
    OSError(5, "Access is denied", "\\\\fileserver\\share\\logidoc\\data\\x.pdf"),
])
def test_redaction_of_pythons_own_error_messages(error):
    """Python quotes paths with repr in OSError messages (doubled backslashes); no folder may survive."""

    cleaned = redact(str(error))

    for folder in ("Users", "kuchu", "AppData", "Temp", "LogiDoc-RAG", "fileserver", "share", "logidoc"):
        assert folder not in cleaned, (folder, cleaned)

    assert "<path>/" in cleaned and Path(error.filename).name in cleaned


def test_redaction_keeps_api_routes_and_cuts_long_text():

    assert redact("GET /api/shipments/SHJ-1/freight-audit 500") == "GET /api/shipments/SHJ-1/freight-audit 500"
    assert redact("x" * 5000).endswith("(truncated)") and len(redact("x" * 5000)) < 2100


# ============================================================
# METRICS
# ============================================================

def counter(name, labels=None):

    return metrics.REGISTRY.value(name, labels) or 0


def histogram_count(name, labels=None):

    for item in metrics.REGISTRY.snapshot()["histograms"].get(name, []):
        if item["labels"] == (labels or {}):
            return item["count"]

    return 0


def test_requests_are_counted_by_route_template_and_status_class(rig):

    labels = {"method": "GET", "route": "/api/history/shipments/{shipment_id}", "status": "2xx"}
    before = counter("logidoc_http_requests_total", labels)
    timed = histogram_count("logidoc_http_request_duration_seconds", {"route": labels["route"]})

    client = rig.client("alice")
    client.get("/api/history/shipments/SHJ-2026-001")
    client.get("/api/v1/history/shipments/SHJ-2026-002")

    assert counter("logidoc_http_requests_total", labels) == before + 2
    assert histogram_count("logidoc_http_request_duration_seconds", {"route": labels["route"]}) == timed + 2


@pytest.mark.parametrize("user, url, category", [
    (None, "/api/overview", "authentication"), ("bob", "/api/admin/metrics", "permission"),
    ("alice", "/api/carriers?start=yesterday", "validation"), ("alice", "/api/carriers/no such carrier", "not_found"),
])
def test_errors_are_counted_by_category(rig, user, url, category):

    before = counter("logidoc_http_errors_total", {"category": category})
    rig.client(user).get(url)

    assert counter("logidoc_http_errors_total", {"category": category}) == before + 1


def test_a_database_failure_is_counted_as_one(rig, monkeypatch):

    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rig.state.service, "snapshot", locked)
    before = counter("logidoc_database_errors_total"), counter("logidoc_http_errors_total", {"category": "database"})

    assert rig.client("alice").get("/api/overview").status_code == 500
    assert (counter("logidoc_database_errors_total"), counter("logidoc_http_errors_total", {"category": "database"})) \
        == (before[0] + 1, before[1] + 1)


def test_model_calls_are_timed_by_outcome(monkeypatch):

    class Service:
        def embeddings(self, model, prompt):
            return {"embedding": [0.1]}

    monkeypatch.setattr(model_client, "client", lambda kind: Service())
    ok = {"kind": model_client.EMBEDDING, "outcome": "success"}
    before = histogram_count("logidoc_llm_request_duration_seconds", ok)
    model_client.embeddings("m", "text")
    assert histogram_count("logidoc_llm_request_duration_seconds", ok) == before + 1

    class Down:
        def embeddings(self, model, prompt):
            raise ConnectionError("refused")

    monkeypatch.setattr(model_client, "client", lambda kind: Down())
    down = {"kind": model_client.EMBEDDING, "outcome": "ModelUnavailable"}
    before = histogram_count("logidoc_llm_request_duration_seconds", down)

    with pytest.raises(model_client.ModelUnavailable):
        model_client.embeddings("m", "text")

    assert histogram_count("logidoc_llm_request_duration_seconds", down) == before + 1


def test_ingestion_and_the_freight_audit_are_measured(rig, monkeypatch, tmp_path):

    monkeypatch.setattr(ingestion_manager, "create_embedding", fake_embedding)
    indexed = counter("logidoc_ingestion_files_total", {"outcome": "indexed"})
    processed = histogram_count("logidoc_pdf_processing_seconds")
    audits = histogram_count("logidoc_freight_audit_duration_seconds")

    admin = rig.client("alice")
    assert admin.post("/api/ingestion/upload", files=[pdf_upload(tmp_path)]).json()["indexed"] == 1
    admin.get("/api/overview")

    assert counter("logidoc_ingestion_files_total", {"outcome": "indexed"}) == indexed + 1
    assert histogram_count("logidoc_pdf_processing_seconds") == processed + 1
    assert histogram_count("logidoc_freight_audit_duration_seconds") > audits


def test_the_prometheus_text_is_well_formed():

    registry = metrics.Registry()
    registry.request("GET", '/api/x/{id}', 200, 0.2)
    registry.request("GET", '/api/x/{id}', 503, 7.0)
    registry.set("logidoc_ollama_available", 0)
    text = registry.prometheus()

    typed = set(re.findall(r"^# TYPE (\S+) (counter|gauge|histogram)$", text, re.M))
    assert ("logidoc_http_requests_total", "counter") in typed and ("logidoc_ollama_available", "gauge") in typed

    buckets = [int(value) for value in re.findall(r'^logidoc_http_request_duration_seconds_bucket\{.*\} (\d+)$', text, re.M)]
    assert buckets == sorted(buckets) and buckets[-1] == 2
    assert 'logidoc_http_request_duration_seconds_count{route="/api/x/{id}"} 2' in text
    assert registry.window_stats(300)["server_errors"] == 1


def test_metrics_only_count_what_happened():

    registry = metrics.Registry()

    assert registry.snapshot()["counters"] == {} and registry.snapshot()["histograms"] == {}
    assert registry.window_stats(300) == {"requests": 0, "server_errors": 0, "database_errors": 0, "p95_seconds": None}
