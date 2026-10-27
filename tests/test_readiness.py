"""
/api/ready (src/readiness.py): can the server do its work right now?

ready when every check passes, degraded when only the model service or the
extraction store is not usable (200), not_ready when a folder is unusable
(503). Only status words are returned: never a path, host, model name or
exception message. Every folder and database is temporary; the model service
is a stand-in (a function, a tiny local HTTP server, a silent socket or a
closed port), so the real Ollama is never needed.

Run from the project root:
    python -m pytest tests/test_readiness.py
"""

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config  # noqa: E402
import model_client  # noqa: E402
import readiness  # noqa: E402
from test_api import Rig  # noqa: E402

BOTH_MODELS = {f"{config.EMBEDDING_MODEL}:latest", config.LLM_MODEL}


@pytest.fixture
def models(monkeypatch):
    """
    Sets what the model service reports (a set of names, or None: no answer).
    """

    reported = {"value": set(BOTH_MODELS)}
    monkeypatch.setattr(model_client, "installed_models", lambda: reported["value"])

    return reported


def ready(rig):

    reply = rig.client().get("/api/ready")

    return reply.status_code, reply.json(), reply.text


def assert_says_nothing_private(text, rig):

    for private in (str(rig.tmp), ":\\", "127.0.0.1", "11434", config.EMBEDDING_MODEL, config.LLM_MODEL, "Error", "Traceback"):
        assert private not in text, private


def test_everything_usable_is_ready(tmp_path, models):

    rig = Rig(tmp_path, "local")
    status, body, text = ready(rig)

    assert status == 200 and body["status"] == readiness.READY
    assert body["checks"] == {
        "documents": {"ok": True},
        "data_writable": {"ok": True},
        "extraction_store": {"ok": True, "state": "absent"},
        "model_service": {"ok": True, "reachable": True, "missing": []},
    }
    assert_says_nothing_private(text, rig)


def test_an_unreachable_model_service_is_degraded_not_down(tmp_path, models):

    models["value"] = None
    rig = Rig(tmp_path, "local")
    status, body, text = ready(rig)

    assert status == 200 and body["status"] == readiness.DEGRADED
    assert body["checks"]["model_service"] == {"ok": False, "reachable": False, "missing": []}
    assert body["checks"]["documents"]["ok"] and body["checks"]["data_writable"]["ok"]
    assert_says_nothing_private(text, rig)


@pytest.mark.parametrize("installed, missing", [
    ({config.LLM_MODEL}, [model_client.EMBEDDING]),
    ({f"{config.EMBEDDING_MODEL}:latest"}, [model_client.LANGUAGE_MODEL]),
    (set(), [model_client.EMBEDDING, model_client.LANGUAGE_MODEL]),
])
def test_a_missing_model_is_named_by_its_role_and_degrades(tmp_path, models, installed, missing):

    models["value"] = installed
    rig = Rig(tmp_path, "local")
    status, body, text = ready(rig)

    assert status == 200 and body["status"] == readiness.DEGRADED
    assert body["checks"]["model_service"] == {"ok": False, "reachable": True, "missing": missing}
    assert_says_nothing_private(text, rig)


def test_model_names_match_with_or_without_the_latest_tag():

    assert model_client.has_model({"nomic-embed-text:latest"}, "nomic-embed-text")
    assert model_client.has_model({"nomic-embed-text"}, "nomic-embed-text")
    assert model_client.has_model({"nemotron-3-nano:4b"}, "nemotron-3-nano:4b")
    assert not model_client.has_model({"nemotron-3-nano:8b"}, "nemotron-3-nano:4b")
    assert not model_client.has_model({"nomic-embed-text:v1"}, "nomic-embed-text")


def test_a_missing_document_folder_is_not_ready(tmp_path, models):

    rig = Rig(tmp_path, "local")

    for path in sorted(rig.raw.iterdir()):
        path.unlink()

    rig.raw.rmdir()

    status, body, text = ready(rig)

    assert status == 503 and body["status"] == readiness.NOT_READY
    assert body["checks"]["documents"] == {"ok": False}
    assert_says_nothing_private(text, rig)


def test_a_data_folder_that_refuses_new_files_is_not_ready(tmp_path, models, monkeypatch):

    rig = Rig(tmp_path, "local")
    monkeypatch.setattr(readiness, "writable", lambda folder: False)

    status, body, _ = ready(rig)

    assert status == 503 and body["status"] == readiness.NOT_READY
    assert body["checks"]["data_writable"] == {"ok": False}


def test_the_writable_check_leaves_no_file_behind(tmp_path):

    folder = tmp_path / "data"
    folder.mkdir()

    assert readiness.writable(folder) and list(folder.iterdir()) == []

    # A folder that does not exist yet is judged by the nearest existing one (the stores create it)
    assert readiness.writable(folder / "not" / "yet") and not (folder / "not").exists()


def test_an_unreadable_extraction_store_degrades_with_its_state(tmp_path, models):

    rig = Rig(tmp_path, "local")
    rig.locations.extraction_path().write_bytes(b"this is not a database")

    status, body, text = ready(rig)

    assert status == 200 and body["status"] == readiness.DEGRADED
    assert body["checks"]["extraction_store"] == {"ok": False, "state": "unavailable"}
    assert_says_nothing_private(text, rig)


def test_a_check_that_fails_unexpectedly_reports_not_ok_without_its_message(tmp_path, models, monkeypatch):

    rig = Rig(tmp_path, "local")

    def explode(locations):
        raise RuntimeError(f"cannot open {rig.tmp} at 127.0.0.1")

    monkeypatch.setattr(readiness, "check_extraction_store", explode)

    status, body, text = ready(rig)

    assert status == 200 and body["status"] == readiness.DEGRADED
    assert body["checks"]["extraction_store"] == {"ok": False}
    assert_says_nothing_private(text, rig)


def test_readiness_needs_no_sign_in_like_health(tmp_path, models):

    rig = Rig(tmp_path, "required")

    assert rig.client().get("/api/ready").status_code == 200
    assert rig.client().get("/api/health").json() == {"status": "ok"}


# ============================================================
# THE REAL MODEL-LIST CALL (no Ollama: local stand-ins)
# ============================================================

class Tags(BaseHTTPRequestHandler):

    def do_GET(self):

        body = json.dumps({"models": [{"model": "nomic-embed-text:latest", "name": "nomic-embed-text:latest"},
                                      {"model": "nemotron-3-nano:4b", "name": "nemotron-3-nano:4b"}]}).encode()

        self.send_response(200 if self.path == "/api/tags" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_the_model_list_is_read_from_the_service(monkeypatch):

    server = ThreadingHTTPServer(("127.0.0.1", 0), Tags)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{server.server_address[1]}")

    try:
        assert model_client.installed_models() == {"nomic-embed-text:latest", "nemotron-3-nano:4b"}
    finally:
        server.shutdown()
        server.server_close()


def test_a_silent_model_service_gives_no_list_within_the_limit(monkeypatch):

    listener = socket.create_server(("127.0.0.1", 0))
    held = []
    threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{listener.getsockname()[1]}")

    started = time.perf_counter()

    try:
        assert model_client.installed_models(limit=0.5) is None
        assert time.perf_counter() - started < 5
    finally:
        listener.close()

        for connection, _ in held:
            connection.close()


def test_a_closed_port_gives_no_list(monkeypatch):

    probe = socket.create_server(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{port}")

    assert model_client.installed_models(limit=3.0) is None
