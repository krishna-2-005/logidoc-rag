"""
Runtime reliability of the API (readiness audit findings D1, D1b and D2).

- An upload or a replacement runs its ingestion in a worker thread
  (api/routes_ingestion.in_worker), so the API keeps answering other
  requests while documents are embedded; the ingestion lock still lets only
  one writer embed at a time.
- Every call to Ollama has a time limit (src/model_client.py). A stopped or
  stuck service ends the request with a readable error (503 or 504, or a
  failed file in an upload summary) that names no host or library; nothing
  is half-written and a failure is never retried without end.
- An error answer from Ollama is classified by its HTTP status (model not
  installed, busy, failed, rejected). A failed model service stops the rest
  of an upload; every file keeps a copy and can be retried later.
- Every PyMuPDF document in the five PDF places is opened under the
  process-wide PDF lock (src/pdf_access.py), and the ingestion lock is never
  taken while holding it.

The model service is never the real one: embeddings are deterministic
stand-ins, a local socket that accepts connections and never answers plays a
stuck Ollama, and an httpx mock transport plays one that is down or answers.
Waiting uses events, not sleeps, wherever possible. Every folder and
database is temporary (tests/conftest.py checks the real ones).

Run from the project root:
    python -m pytest tests/test_runtime_reliability.py
"""

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import shutil
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio.to_thread
import httpx
import ollama
import pymupdf
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import audit_pdf_export  # noqa: E402
import config  # noqa: E402
import document_extraction  # noqa: E402
import document_loader  # noqa: E402
import document_validation  # noqa: E402
import ingestion_journal  # noqa: E402
import ingestion_manager  # noqa: E402
import ingestion_service  # noqa: E402
import model_client  # noqa: E402
import pdf_access  # noqa: E402
import rag  # noqa: E402
import vector_store  # noqa: E402
from test_api import CLIENT, Rig  # noqa: E402
from test_api import A as REPORTED_SHIPMENT  # noqa: E402
from test_ingestion_manager import fake_embedding, make_bol  # noqa: E402


# A health request must come back within this while a document is being
# embedded. Generous for a slow machine: before the fix it waited for the
# whole embedding (3.4 s in the audit's probe; here, until the gate opens).
HEALTH_LIMIT = 2.0

# The longest a gated embedding waits to be released, which bounds a failing test
GATE_LIMIT = 10.0

# The time limit the model client gets in these tests (seconds)
TEST_TIME_LIMIT = 0.5

SHIPMENT = "SHJ-2026-555"

# Printed on every make_bol page: must never reach an error message or a log
DOCUMENT_TEXT = "Test Freight Lines"


# ============================================================
# HELPERS
# ============================================================

class Gate:
    """
    A stand-in embedding. With hold=True its first call waits until
    released (at most GATE_LIMIT). It records how many embeddings ran at the
    same time and whether the ingestion lock was held during each.
    """

    def __init__(self, hold=True, pause=0.0):

        self.hold = hold
        self.pause = pause
        self.entered = threading.Event()
        self.release = threading.Event()
        self.guard = threading.Lock()
        self.calls = 0
        self.active = 0
        self.most_active = 0
        self.lock_held = []

    def __call__(self, text):

        with self.guard:
            self.calls += 1
            first = self.calls == 1
            self.active += 1
            self.most_active = max(self.most_active, self.active)
            self.lock_held.append(ingestion_manager.INGESTION_LOCK.locked())

        try:

            self.entered.set()

            if self.hold and first:
                self.release.wait(GATE_LIMIT)

            time.sleep(self.pause)

            return fake_embedding(text)

        finally:

            with self.guard:
                self.active -= 1


def client_for(rig):

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://testserver", headers=CLIENT, timeout=60,
    )


def health_while_embedding(rig, gate, send):
    """
    Starts send(client) (an upload or a replacement), waits until its first
    embedding has started, asks for /api/health and only then lets the
    embedding go on. Returns (the health reply, the seconds it took, whether
    the operation was still running when health answered, the operation's
    reply).
    """

    async def scenario():

        async with client_for(rig) as client:

            operation = asyncio.create_task(send(client))

            assert await anyio.to_thread.run_sync(gate.entered.wait, GATE_LIMIT), "the embedding never started"

            started = time.perf_counter()
            health = await client.get("/api/health")
            seconds = time.perf_counter() - started
            still_running = not operation.done()

            gate.release.set()

            return health, seconds, still_running, await operation

    return asyncio.run(scenario())


def pdf(folder, name, shipment=SHIPMENT, pieces=5):

    folder.mkdir(exist_ok=True)

    return make_bol(folder / name, shipment, pieces).read_bytes()


def upload(client, name, data):

    return client.post("/api/ingestion/upload", files=[("files", (name, data, "application/pdf"))])


def vector_ids(rig):

    return sorted(ingestion_manager.get_destination_collection(rig.locations.chroma_dir).get()["ids"])


def assert_reveals_nothing(text, *also):
    """
    A message a client or the log sees: no address, library, stack trace or
    document text.
    """

    for secret in ("127.0.0.1", "localhost", "11434", "ollama", "http://", "https://", "Traceback", DOCUMENT_TEXT, *also):
        assert secret.lower() not in text.lower(), secret


class SilentService:
    """
    A local server that accepts connections and never answers: a stuck Ollama.
    """

    def __init__(self):

        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.connections = []
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):

        while True:

            try:
                connection, _ = self.listener.accept()
            except OSError:
                return

            self.connections.append(connection)

    def close(self):

        self.listener.close()

        for connection in self.connections:
            connection.close()


@pytest.fixture
def silent_service(monkeypatch):
    """
    Model clients that talk to a SilentService with a short time limit.
    """

    service = SilentService()

    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{service.port}")
    monkeypatch.setattr(model_client, "CLIENTS", {})
    monkeypatch.setattr(config, "EMBED_TIMEOUT", TEST_TIME_LIMIT)
    monkeypatch.setattr(config, "LLM_TIMEOUT", TEST_TIME_LIMIT)

    yield service

    service.close()


def refuse(request):
    """
    A mock-transport handler for a model service that is not running.
    """

    raise httpx.ConnectError("[WinError 10061] No connection could be made")


def mock_service(monkeypatch, handler):
    """
    Both model clients answer through handler (an httpx mock transport).
    Returns the list of requests they make.
    """

    requests = []

    def record(request):

        requests.append(request)

        return handler(request)

    for kind in (model_client.EMBEDDING, model_client.LANGUAGE_MODEL):
        monkeypatch.setitem(model_client.CLIENTS, kind, ollama.Client(transport=httpx.MockTransport(record)))

    return requests


def finishes(call, limit=GATE_LIMIT):
    """
    Runs call() in a thread and returns (seconds, its result, the error it
    raised). Fails instead of hanging when the call has no time limit (the
    silent_service fixture then closes the connection, which ends the call).
    """

    outcome = {}

    def run():

        started = time.perf_counter()

        try:
            outcome["result"] = call()
        except Exception as error:
            outcome["error"] = error

        outcome["seconds"] = time.perf_counter() - started

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(limit)

    assert not worker.is_alive(), f"still waiting after {limit} s: the call has no time limit"

    return outcome["seconds"], outcome.get("result"), outcome.get("error")


# ============================================================
# D1 / D1b: THE EVENT LOOP STAYS FREE
# ============================================================

def test_the_api_answers_other_requests_while_an_upload_is_embedding(tmp_path, monkeypatch):

    rig = Rig(tmp_path, "local")
    gate = Gate()
    monkeypatch.setattr(ingestion_manager, "create_embedding", gate)
    data = pdf(tmp_path / "source", "BOL_NEW.pdf")

    health, seconds, still_running, reply = health_while_embedding(rig, gate, lambda client: upload(client, "BOL_NEW.pdf", data))

    assert health.status_code == 200 and still_running and seconds < HEALTH_LIMIT
    assert reply.status_code == 200 and reply.json()["indexed"] == 1
    assert (rig.raw / "BOL_NEW.pdf").read_bytes() == data
    assert gate.lock_held and all(gate.lock_held)


def test_the_api_answers_other_requests_while_a_replacement_is_embedding(tmp_path, monkeypatch):

    rig = Rig(tmp_path, "local")
    source = tmp_path / "source"

    monkeypatch.setattr(ingestion_manager, "create_embedding", fake_embedding)
    assert upload(rig.client(), "BOL_NEW.pdf", pdf(source, "BOL_NEW.pdf")).json()["indexed"] == 1

    gate = Gate()
    monkeypatch.setattr(ingestion_manager, "create_embedding", gate)
    corrected = pdf(source, "BOL_FIXED.pdf", pieces=7)

    health, seconds, still_running, reply = health_while_embedding(rig, gate, lambda client: client.post(
        "/api/documents/replace", params={"name": "BOL_NEW.pdf"}, files={"file": ("BOL_NEW.pdf", corrected, "application/pdf")},
    ))

    assert health.status_code == 200 and still_running and seconds < HEALTH_LIMIT
    assert reply.status_code == 200 and reply.json()["action"] == "replaced"
    assert (rig.raw / "BOL_NEW.pdf").read_bytes() == corrected
    assert gate.lock_held and all(gate.lock_held)


def test_concurrent_uploads_still_embed_one_at_a_time_under_the_ingestion_lock(tmp_path, monkeypatch):

    rig = Rig(tmp_path, "local")
    gate = Gate(hold=False, pause=0.05)
    monkeypatch.setattr(ingestion_manager, "create_embedding", gate)
    source = tmp_path / "source"
    files = {"BOL_ONE.pdf": pdf(source, "BOL_ONE.pdf", "SHJ-2026-561", 3), "BOL_TWO.pdf": pdf(source, "BOL_TWO.pdf", "SHJ-2026-562", 4)}

    async def scenario():

        async with client_for(rig) as client:
            return await asyncio.gather(*(upload(client, name, data) for name, data in files.items()))

    replies = asyncio.run(scenario())

    assert [reply.status_code for reply in replies] == [200, 200]
    assert sum(reply.json()["indexed"] for reply in replies) == 2
    assert gate.calls >= 2 and gate.most_active == 1 and all(gate.lock_held)

    summary = rig.client().get("/api/ingestion/register").json()["summary"]

    assert summary["indexed"] == 2 and summary["open_errors"] == 0
    assert all((rig.raw / name).read_bytes() == data for name, data in files.items())


def test_an_upload_and_a_reprocess_on_different_threads_never_embed_at_the_same_time(tmp_path, monkeypatch):

    # The upload runs on the ingestion worker, reprocess (a plain route) on the
    # shared thread pool: only the ingestion lock keeps them apart
    rig = Rig(tmp_path, "local")
    stored = sorted(rig.raw.glob("*.pdf"))[0].name        # a fixture PDF, stored but not indexed yet
    gate = Gate(hold=False, pause=0.05)
    monkeypatch.setattr(ingestion_manager, "create_embedding", gate)
    data = pdf(tmp_path / "source", "BOL_NEW.pdf")

    async def scenario():

        async with client_for(rig) as client:
            return await asyncio.gather(
                upload(client, "BOL_NEW.pdf", data), client.post("/api/documents/reprocess", params={"name": stored}),
            )

    uploaded, reprocessed = asyncio.run(scenario())

    assert uploaded.status_code == 200 and uploaded.json()["indexed"] == 1
    assert reprocessed.status_code == 200 and reprocessed.json()["chunks"] > 0
    assert gate.most_active == 1 and all(gate.lock_held)


def test_an_upload_and_a_replacement_on_separate_event_loops_wait_for_the_ingestion_lock(tmp_path, monkeypatch):

    # Each TestClient request runs on its own event loop, so the per-loop ingestion
    # slot cannot keep these two apart: only the ingestion lock does
    rig = Rig(tmp_path, "local")
    source = tmp_path / "source"

    monkeypatch.setattr(ingestion_manager, "create_embedding", fake_embedding)
    assert upload(rig.client(), "BOL_OLD.pdf", pdf(source, "BOL_OLD.pdf", "SHJ-2026-571", 3)).json()["indexed"] == 1

    new = pdf(source, "BOL_NEW.pdf", "SHJ-2026-572", 4)
    corrected = pdf(source, "BOL_OLD_FIXED.pdf", "SHJ-2026-573", 5)
    upload_embedding, replace_embedding, release = threading.Event(), threading.Event(), threading.Event()

    def embedding(text):

        if "SHJ-2026-572" in text:
            upload_embedding.set()
            release.wait(GATE_LIMIT)

        if "SHJ-2026-573" in text:
            replace_embedding.set()

        return fake_embedding(text)

    monkeypatch.setattr(ingestion_manager, "create_embedding", embedding)
    replies = {}

    def send(name, request):
        replies[name] = request()

    uploader = threading.Thread(target=send, args=("upload", lambda: upload(rig.client(), "BOL_NEW.pdf", new)))
    replacer = threading.Thread(target=send, args=("replace", lambda: rig.client().post(
        "/api/documents/replace", params={"name": "BOL_OLD.pdf"},
        files={"file": ("BOL_OLD.pdf", corrected, "application/pdf")},
    )))

    uploader.start()
    assert upload_embedding.wait(GATE_LIMIT)            # the upload is embedding and holds the lock

    replacer.start()
    kept_out = not replace_embedding.wait(1.0)          # so the replacement must not embed yet

    release.set()
    uploader.join(GATE_LIMIT * 3)
    replacer.join(GATE_LIMIT * 3)

    assert kept_out and replace_embedding.is_set()
    assert replies["upload"].status_code == 200 and replies["upload"].json()["indexed"] == 1
    assert replies["replace"].status_code == 200 and replies["replace"].json()["action"] == "replaced"
    assert (rig.raw / "BOL_OLD.pdf").read_bytes() == corrected


def test_an_upload_whose_client_goes_away_still_finishes_and_is_recorded(tmp_path, monkeypatch):

    rig = Rig(tmp_path, "local")
    gate = Gate()
    monkeypatch.setattr(ingestion_manager, "create_embedding", gate)
    data = pdf(tmp_path / "source", "BOL_NEW.pdf")

    async def scenario():

        async with client_for(rig) as client:

            request = asyncio.create_task(upload(client, "BOL_NEW.pdf", data))
            assert await anyio.to_thread.run_sync(gate.entered.wait, GATE_LIMIT)

            request.cancel()                        # the client goes away mid-upload
            await asyncio.sleep(0.3)
            waited_for_the_work = not request.done()   # the request waits for the worker, not abandoning it

            gate.release.set()

            try:
                await request
            except asyncio.CancelledError:
                pass

            return waited_for_the_work, (rig.raw / "BOL_NEW.pdf").exists()

    waited_for_the_work, stored_when_cancel_ended = asyncio.run(scenario())

    assert waited_for_the_work and stored_when_cancel_ended

    # The operation ran to its own end: stored, indexed, recorded, nothing left unfinished
    files = rig.client().get("/api/ingestion/register").json()["files"]

    assert [(item["filename"], item["status"]) for item in files] == [("BOL_NEW.pdf", "indexed")]
    assert (rig.raw / "BOL_NEW.pdf").read_bytes() == data and vector_ids(rig)
    assert not ingestion_journal.unfinished_operations(ingestion_journal.read_journal(rig.raw)["records"])


# ============================================================
# D2: EVERY MODEL CALL HAS A TIME LIMIT
# ============================================================

@pytest.mark.parametrize("kind, call", [
    ("embedding", lambda: model_client.embeddings(model=config.EMBEDDING_MODEL, prompt=DOCUMENT_TEXT)),
    ("language model", lambda: model_client.chat(model=config.LLM_MODEL, messages=[{"role": "user", "content": DOCUMENT_TEXT}])),
])
def test_a_stuck_model_service_times_out_once_with_a_readable_error(silent_service, kind, call):

    seconds, _, error = finishes(call)

    assert isinstance(error, model_client.ModelTimeout) and seconds < 5
    assert str(error) == f"The {kind} service did not answer within {TEST_TIME_LIMIT:g} s (timed out)."
    assert isinstance(error.__cause__, httpx.TimeoutException)

    # Asked once: the client itself never retries
    assert len(silent_service.connections) == 1

    assert_reveals_nothing(str(error), str(silent_service.port))


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("[WinError 10061] No connection could be made"),
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
])
def test_an_unreachable_or_dropped_model_service_is_unavailable(monkeypatch, failure):

    def handler(request):
        raise failure

    requests = mock_service(monkeypatch, handler)

    with pytest.raises(model_client.ModelUnavailable) as embedding:
        vector_store.create_embedding(DOCUMENT_TEXT)

    with pytest.raises(model_client.ModelUnavailable) as answer:
        rag.call_llm(DOCUMENT_TEXT)

    assert str(embedding.value) == "The embedding service is unavailable (it could not be reached)."
    assert str(answer.value) == "The language model service is unavailable (it could not be reached)."

    # Still a ConnectionError, as the ollama library raised before
    assert isinstance(embedding.value, ConnectionError)
    assert len(requests) == 2

    assert_reveals_nothing(str(embedding.value) + str(answer.value))


def test_a_working_model_service_gives_the_same_answers_as_before(monkeypatch):

    def handler(request):

        if request.url.path == "/api/embeddings":
            return httpx.Response(200, json={"embedding": [0.25, 0.5]})

        return httpx.Response(200, json={
            "model": config.LLM_MODEL, "created_at": "2026-10-26T00:00:00Z", "done": True,
            "message": {"role": "assistant", "content": "  Net 30  "},
        })

    requests = mock_service(monkeypatch, handler)

    assert vector_store.create_embedding("a chunk") == [0.25, 0.5]
    assert rag.embed_query("a question") == [0.25, 0.5]
    assert rag.call_llm("a prompt") == "Net 30"

    sent = [json.loads(request.content) for request in requests]

    # The same models and payloads as the direct ollama calls, one request each
    assert [request.url.path for request in requests] == ["/api/embeddings", "/api/embeddings", "/api/chat"]
    assert sent[0] == {"model": config.EMBEDDING_MODEL, "prompt": "a chunk"}
    assert sent[1] == {"model": config.EMBEDDING_MODEL, "prompt": "a question"}
    assert sent[2]["model"] == config.LLM_MODEL and sent[2]["messages"] == [{"role": "user", "content": "a prompt"}]


@pytest.mark.parametrize("status, error, kind, message, service_down", [
    (404, 'model "nomic-embed-text" not found, try pulling it first', model_client.ModelMisconfigured,
     f"The embedding model ({config.EMBEDDING_MODEL}) is not installed on the model service.", True),
    (401, "unauthorized", model_client.ModelMisconfigured,
     "The embedding service refused access (HTTP 401); check its configuration.", True),
    (403, "forbidden", model_client.ModelMisconfigured,
     "The embedding service refused access (HTTP 403); check its configuration.", True),
    (500, "model runner has unexpectedly stopped, this may be due to resource limitations", model_client.ModelUnavailable,
     "The embedding service is unavailable (it failed with HTTP 500).", True),
    (502, "bad gateway", model_client.ModelUnavailable, "The embedding service is unavailable (it failed with HTTP 502).", True),
    (503, "server busy, please try again.  maximum pending requests exceeded", model_client.ModelBusy,
     "The embedding service is busy (HTTP 503); try again shortly.", False),
    (429, "too many requests", model_client.ModelBusy, "The embedding service is busy (HTTP 429); try again shortly.", False),
    (400, "invalid input", model_client.ModelRejected, "The embedding service rejected the request (HTTP 400).", False),
])
def test_an_error_answer_from_the_model_service_is_classified_by_its_status(monkeypatch, caplog, status, error, kind, message, service_down):

    requests = mock_service(monkeypatch, lambda request: httpx.Response(status, json={"error": error}))
    caplog.set_level(logging.WARNING, logger="logidoc.models")

    with pytest.raises(model_client.ModelServiceError) as caught:
        vector_store.create_embedding(DOCUMENT_TEXT)

    assert type(caught.value) is kind and str(caught.value) == message
    assert caught.value.service_down is service_down
    assert isinstance(caught.value.__cause__, ollama.ResponseError) and len(requests) == 1
    assert_reveals_nothing(str(caught.value))

    # The service's own words go to the server log; the request (document text) never does
    assert error in caplog.text and DOCUMENT_TEXT not in caplog.text


def test_every_model_call_in_src_goes_through_model_client():

    direct = re.compile(r"\bollama\.(embeddings|embed|chat|generate)\(")

    # *_backup.py files are git-ignored local copies, not part of the project
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}:{number}"
        for path in sorted((PROJECT_ROOT / "src").rglob("*.py"))
        if path.name != "model_client.py" and not path.name.endswith("_backup.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if direct.search(line)
    ]

    assert offenders == []


def test_the_time_limits_come_from_config():

    for kind, limit in ((model_client.EMBEDDING, config.EMBED_TIMEOUT), (model_client.LANGUAGE_MODEL, config.LLM_TIMEOUT)):

        timeout = model_client.client(kind)._client.timeout

        assert (timeout.read, timeout.connect) == (limit, config.OLLAMA_CONNECT_TIMEOUT)


# ============================================================
# D2 THROUGH THE API: A FAILED CALL CHANGES NOTHING
# ============================================================

def test_an_upload_while_the_model_service_is_stuck_fails_cleanly_and_can_be_retried(tmp_path, silent_service, monkeypatch):

    rig = Rig(tmp_path, "local")
    client = rig.client()

    # The real embedding call, through model_client, to the stuck service
    monkeypatch.setattr(ingestion_manager, "create_embedding", vector_store.create_embedding)
    data = pdf(tmp_path / "source", "BOL_NEW.pdf")
    before = vector_ids(rig)

    seconds, reply, _ = finishes(lambda: upload(client, "BOL_NEW.pdf", data), limit=60)

    assert reply.status_code == 200
    assert (reply.json()["indexed"], reply.json()["failed"]) == (0, 1)
    assert reply.json()["files"][0]["code"] == ingestion_service.MODEL_SERVICE_DOWN
    assert "timed out" in reply.text
    assert_reveals_nothing(reply.text, str(silent_service.port))

    # A failed model service is asked once, not retried
    assert len(silent_service.connections) == 1
    assert seconds < 10

    # Nothing written: no PDF, no vectors, no begun operation in the journal
    assert not (rig.raw / "BOL_NEW.pdf").exists()
    assert vector_ids(rig) == before
    assert not [record for record in ingestion_journal.read_journal(rig.raw)["records"]
                if record.get("event") == ingestion_journal.EVENT_BEGIN]

    # Kept for a retry, which works once the service answers again
    errors = client.get("/api/ingestion/errors").json()

    assert [(error["filename"], error["can_retry"]) for error in errors] == [("BOL_NEW.pdf", True)]

    monkeypatch.setattr(ingestion_manager, "create_embedding", fake_embedding)
    retried = client.post(f"/api/ingestion/{errors[0]['id']}/retry")

    assert retried.status_code == 200 and retried.json()["indexed"] == 1
    assert (rig.raw / "BOL_NEW.pdf").read_bytes() == data


def replacement_while_failing(tmp_path, monkeypatch, caplog):
    """
    Stores BOL_NEW.pdf, then asks to replace it while the model service fails
    (the caller has set that up). Returns (the reply, the rig, the stored
    bytes and vector ids from before).
    """

    rig = Rig(tmp_path, "local")
    client = rig.client()
    source = tmp_path / "source"

    with monkeypatch.context() as patch:
        patch.setattr(ingestion_manager, "create_embedding", fake_embedding)
        assert upload(client, "BOL_NEW.pdf", pdf(source, "BOL_NEW.pdf")).json()["indexed"] == 1

    stored, ids = (rig.raw / "BOL_NEW.pdf").read_bytes(), vector_ids(rig)

    monkeypatch.setattr(ingestion_manager, "create_embedding", vector_store.create_embedding)
    caplog.set_level(logging.WARNING, logger="logidoc.api")
    corrected = pdf(source, "BOL_FIXED.pdf", pieces=7)

    _, reply, _ = finishes(lambda: client.post("/api/documents/replace", params={"name": "BOL_NEW.pdf"},
                                               files={"file": ("BOL_NEW.pdf", corrected, "application/pdf")}))

    return reply, rig, stored, ids


def assert_nothing_changed(rig, stored, ids, caplog):

    assert (rig.raw / "BOL_NEW.pdf").read_bytes() == stored
    assert vector_ids(rig) == ids
    assert not list(rig.raw.glob(".BOL_NEW.pdf.*"))                 # no staged copy left
    assert not list(Path(rig.locations.archive_dir).rglob("*.pdf"))  # nothing archived: the swap never started

    logged = [record for record in caplog.records if record.name == "logidoc.api"]

    assert len(logged) == 1 and "POST /api/documents/replace" in logged[0].getMessage()
    assert not logged[0].exc_info
    assert DOCUMENT_TEXT not in caplog.text and "Traceback" not in caplog.text


def test_a_replacement_while_the_model_service_is_down_answers_503_and_changes_nothing(tmp_path, monkeypatch, caplog):

    mock_service(monkeypatch, refuse)

    reply, rig, stored, ids = replacement_while_failing(tmp_path, monkeypatch, caplog)

    assert reply.status_code == 503
    assert reply.json() == {"detail": "The embedding service is unavailable (it could not be reached)."}
    assert_nothing_changed(rig, stored, ids, caplog)


def test_a_replacement_while_the_model_service_is_stuck_answers_504_and_changes_nothing(tmp_path, silent_service, monkeypatch, caplog):

    reply, rig, stored, ids = replacement_while_failing(tmp_path, monkeypatch, caplog)

    assert reply.status_code == 504
    assert reply.json() == {"detail": f"The embedding service did not answer within {TEST_TIME_LIMIT:g} s (timed out)."}
    assert_reveals_nothing(reply.text, str(silent_service.port))
    assert_nothing_changed(rig, stored, ids, caplog)


@pytest.mark.parametrize("status, expected", [(404, 503), (401, 503), (500, 503), (503, 503), (400, 422)])
def test_a_replacement_answers_an_error_from_the_model_service_with_a_matching_status(tmp_path, monkeypatch, caplog, status, expected):

    mock_service(monkeypatch, lambda request: httpx.Response(status, json={"error": "the service's own words"}))

    reply, rig, stored, ids = replacement_while_failing(tmp_path, monkeypatch, caplog)

    assert reply.status_code == expected and reply.json()["detail"].startswith("The embedding ")
    assert_reveals_nothing(reply.text, "the service's own words")
    assert_nothing_changed(rig, stored, ids, caplog)


# ============================================================
# A FAILED MODEL SERVICE STOPS THE REST OF THE UPLOAD
# ============================================================

def batch(tmp_path, count=3):
    """
    count BOLs for one upload, each for its own shipment.
    """

    source = tmp_path / "source"

    return [(f"BOL_B{i}.pdf", pdf(source, f"BOL_B{i}.pdf", f"SHJ-2026-58{i}", i + 2)) for i in range(count)]


def upload_batch(client, files):

    return client.post("/api/ingestion/upload", files=[("files", (name, data, "application/pdf")) for name, data in files])


def retry_state(client):
    """
    Each open error's file name and whether it can be retried.
    """

    return {item["filename"]: item["can_retry"] for item in client.get("/api/ingestion/errors").json()}


def test_when_the_model_service_is_down_the_rest_of_the_upload_is_not_tried_and_every_file_can_be_retried(tmp_path, monkeypatch):

    rig = Rig(tmp_path, "local")
    client = rig.client()
    requests = mock_service(monkeypatch, refuse)
    monkeypatch.setattr(ingestion_manager, "create_embedding", vector_store.create_embedding)

    files = batch(tmp_path)
    files.insert(1, ("notes.txt", b"hello"))          # not a PDF: still checked, which needs no model

    reply = upload_batch(client, files)
    records = {item["filename"]: item for item in reply.json()["files"]}

    assert reply.status_code == 200 and reply.json()["indexed"] == 0

    # Asked once; then the rest of the upload was not tried
    assert len(requests) == 1
    assert records["BOL_B0.pdf"]["code"] == ingestion_service.MODEL_SERVICE_DOWN
    assert records["notes.txt"]["status"] == "unsupported"
    assert [records[name]["code"] for name in ("BOL_B1.pdf", "BOL_B2.pdf")] == [ingestion_service.NOT_ATTEMPTED] * 2
    assert records["BOL_B1.pdf"]["attempts"] == 0

    # Nothing written, every PDF kept for a retry
    assert not list(rig.raw.glob("BOL_B*.pdf")) and vector_ids(rig) == []
    assert retry_state(client) == {"BOL_B0.pdf": True, "notes.txt": False, "BOL_B1.pdf": True, "BOL_B2.pdf": True}

    # Once the service is back, each one is retried from its kept copy
    monkeypatch.setattr(ingestion_manager, "create_embedding", fake_embedding)
    errors = {item["filename"]: item["id"] for item in client.get("/api/ingestion/errors").json()}

    for name in ("BOL_B0.pdf", "BOL_B1.pdf", "BOL_B2.pdf"):
        assert client.post(f"/api/ingestion/{errors[name]}/retry").json()["indexed"] == 1

    assert sorted(path.name for path in rig.raw.glob("BOL_B*.pdf")) == ["BOL_B0.pdf", "BOL_B1.pdf", "BOL_B2.pdf"]
    assert retry_state(client) == {"notes.txt": False}


def test_a_stuck_model_service_costs_one_time_limit_per_upload_not_one_per_file(tmp_path, silent_service, monkeypatch):

    rig = Rig(tmp_path, "local")
    monkeypatch.setattr(ingestion_manager, "create_embedding", vector_store.create_embedding)
    files = batch(tmp_path)

    seconds, reply, _ = finishes(lambda: upload_batch(rig.client(), files), limit=60)

    assert [item["code"] for item in reply.json()["files"]] == [
        ingestion_service.MODEL_SERVICE_DOWN, ingestion_service.NOT_ATTEMPTED, ingestion_service.NOT_ATTEMPTED,
    ]
    assert len(silent_service.connections) == 1

    # Before: every file, every attempt waited for the limit (3 x 3 x 0.5 s plus pauses)
    assert seconds < TEST_TIME_LIMIT * len(files) * ingestion_service.MAX_ATTEMPTS
    assert set(retry_state(rig.client()).values()) == {True}


def test_a_model_that_is_not_installed_stops_the_upload_and_keeps_the_files(tmp_path, monkeypatch):

    rig = Rig(tmp_path, "local")
    requests = mock_service(monkeypatch, lambda request: httpx.Response(
        404, json={"error": 'model "nomic-embed-text" not found, try pulling it first'}))
    monkeypatch.setattr(ingestion_manager, "create_embedding", vector_store.create_embedding)

    first, second = upload_batch(rig.client(), batch(tmp_path, 2)).json()["files"]

    assert len(requests) == 1
    assert (first["code"], second["code"]) == (ingestion_service.MODEL_SERVICE_DOWN, ingestion_service.NOT_ATTEMPTED)
    assert "is not installed on the model service" in first["message"]
    assert retry_state(rig.client()) == {"BOL_B0.pdf": True, "BOL_B1.pdf": True}


def test_a_busy_model_service_is_retried_with_pauses_and_the_upload_goes_on(tmp_path, monkeypatch):

    rig = Rig(tmp_path, "local")
    requests = mock_service(monkeypatch, lambda request: httpx.Response(
        503, json={"error": "server busy, please try again.  maximum pending requests exceeded"}))
    monkeypatch.setattr(ingestion_manager, "create_embedding", vector_store.create_embedding)

    files = upload_batch(rig.client(), batch(tmp_path, 2)).json()["files"]

    # As before this phase: each file tried MAX_ATTEMPTS times, then kept
    assert len(requests) == 2 * ingestion_service.MAX_ATTEMPTS
    assert [item["code"] for item in files] == [ingestion_service.RETRIES_EXHAUSTED] * 2
    assert retry_state(rig.client()) == {"BOL_B0.pdf": True, "BOL_B1.pdf": True}


def test_a_request_the_model_service_rejects_is_not_retried_and_the_upload_goes_on(tmp_path, monkeypatch):

    rig = Rig(tmp_path, "local")
    requests = mock_service(monkeypatch, lambda request: httpx.Response(400, json={"error": "invalid input"}))
    monkeypatch.setattr(ingestion_manager, "create_embedding", vector_store.create_embedding)

    files = upload_batch(rig.client(), batch(tmp_path, 2)).json()["files"]

    # About this request, not the service: no retry, no kept copy, the next file is still tried
    assert len(requests) == 2
    assert [item["code"] for item in files] == [ingestion_service.PROCESSING_FAILED] * 2
    assert retry_state(rig.client()) == {"BOL_B0.pdf": False, "BOL_B1.pdf": False}


# ============================================================
# ONE PYMUPDF DOCUMENT AT A TIME (pdf_access.PDF_LOCK)
# ============================================================

LOCKED_MODULES = (
    "document_validation.py", "document_loader.py", "document_extraction.py", "document_completeness.py",
    "audit_pdf_export.py",
)


class TrackingLock:
    """
    Stands in for pdf_access.PDF_LOCK and knows which thread holds it.
    """

    def __init__(self):

        self.lock = threading.RLock()
        self.owner = None
        self.depth = 0

    def __enter__(self):

        self.lock.acquire()
        self.owner, self.depth = threading.get_ident(), self.depth + 1

        return self

    def __exit__(self, *exc):

        self.depth -= 1

        if not self.depth:
            self.owner = None

        self.lock.release()

    def held_here(self):

        return self.owner == threading.get_ident()


class OrderedLock:
    """
    Stands in for the ingestion lock and records every time a thread takes
    it while holding the PDF lock (the order that could deadlock).
    """

    def __init__(self, pdf_lock):

        self.lock = threading.Lock()
        self.pdf_lock = pdf_lock
        self.violations = []

    def __enter__(self):

        if self.pdf_lock.held_here():
            self.violations.append(threading.get_ident())

        self.lock.acquire()

        return self

    def __exit__(self, *exc):

        self.lock.release()

    def locked(self):

        return self.lock.locked()


@pytest.fixture
def pdf_watch(monkeypatch):
    """
    Records every PyMuPDF document opened from the five locked modules, and
    whether the opening thread held the PDF lock. (PDFs the tests make are
    not counted.)
    """

    lock = TrackingLock()
    opened = []
    original = pymupdf.open

    def watched(*args, **kwargs):

        caller = Path(sys._getframe(1).f_code.co_filename).name

        if caller in LOCKED_MODULES:
            opened.append((caller, lock.held_here()))

        return original(*args, **kwargs)

    monkeypatch.setattr(pdf_access, "PDF_LOCK", lock)
    monkeypatch.setattr(pymupdf, "open", watched)

    return opened


def exercise_every_pdf_route(rig, tmp_path, monkeypatch):
    """
    Upload (validation, loader), replace and reprocess, an extraction run, the
    document list (completeness scan) and the PDF report.
    """

    client = rig.client()
    source = tmp_path / "source"
    monkeypatch.setattr(ingestion_manager, "create_embedding", fake_embedding)

    assert upload(client, "BOL_NEW.pdf", pdf(source, "BOL_NEW.pdf")).json()["indexed"] == 1
    assert client.post("/api/documents/replace", params={"name": "BOL_NEW.pdf"},
                       files={"file": ("BOL_NEW.pdf", pdf(source, "BOL_FIXED.pdf", pieces=7), "application/pdf")}).status_code == 200
    assert client.post("/api/documents/reprocess", params={"name": sorted(rig.raw.glob("*.pdf"))[0].name}).status_code == 200
    assert client.post("/api/extractions/run", json={"document": "BOL_NEW.pdf"}).status_code == 200
    assert client.get("/api/documents").status_code == 200

    report = client.get(f"/api/reports/shipments/{REPORTED_SHIPMENT}.pdf")

    assert report.status_code == 200 and report.content.startswith(b"%PDF")


def test_every_pymupdf_document_in_the_five_places_is_opened_under_the_pdf_lock(tmp_path, monkeypatch, pdf_watch):

    exercise_every_pdf_route(Rig(tmp_path, "local"), tmp_path, monkeypatch)

    seen = {name: [held for caller, held in pdf_watch if caller == name] for name in LOCKED_MODULES}

    assert all(seen.values()), {name: len(calls) for name, calls in seen.items()}      # every place was used
    assert all(all(calls) for calls in seen.values()), seen                            # always under the lock


def test_the_ingestion_lock_is_never_taken_while_holding_the_pdf_lock(tmp_path, monkeypatch, pdf_watch):

    ordered = OrderedLock(pdf_access.PDF_LOCK)
    monkeypatch.setattr(ingestion_manager, "INGESTION_LOCK", ordered)

    exercise_every_pdf_route(Rig(tmp_path, "local"), tmp_path, monkeypatch)

    assert ordered.violations == []


def test_a_report_that_fails_while_being_written_closes_its_document_and_releases_the_lock(tmp_path, monkeypatch):

    client = Rig(tmp_path, "local").client()
    reports = []
    make = audit_pdf_export.Report.__init__
    write_title = audit_pdf_export.Report.title

    def tracked(self, shipment_id):
        make(self, shipment_id)
        reports.append(self)

    def failing(self, *args, **kwargs):
        raise RuntimeError("Simulated failure while writing the report")

    monkeypatch.setattr(audit_pdf_export.Report, "__init__", tracked)
    monkeypatch.setattr(audit_pdf_export.Report, "title", failing)       # the first thing written

    failed = client.get(f"/api/reports/shipments/{REPORTED_SHIPMENT}.pdf")

    assert failed.status_code == 500 and "Simulated" not in failed.text
    assert len(reports) == 1 and reports[0].document.is_closed

    # The lock was released: another thread can take it at once
    assert pdf_access.PDF_LOCK.acquire(blocking=False)
    pdf_access.PDF_LOCK.release()

    # A report written normally afterwards: finish() closes the document and the
    # extra close() in audit_pdf is harmless, also when called again
    monkeypatch.setattr(audit_pdf_export.Report, "title", write_title)
    written = client.get(f"/api/reports/shipments/{REPORTED_SHIPMENT}.pdf")

    assert written.status_code == 200 and written.content.startswith(b"%PDF")
    assert len(reports) == 2 and reports[1].document.is_closed
    reports[1].close()


def test_parsing_from_many_threads_gives_the_sequential_results_under_the_lock(tmp_path, pdf_watch):

    work = tmp_path / "pdfs"
    work.mkdir()

    for source in (PROJECT_ROOT / "tests" / "fixtures").glob("shipment_*/*.pdf"):
        shutil.copy2(source, work / source.name)

    pdf(work, "BOL_GEN.pdf", "SHJ-2026-903", 9)

    def digest(value):
        value = dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    parsers = (
        lambda path: document_validation.validate_pdf(path.name, path.read_bytes()),
        document_loader.load_document,
        document_extraction.extract_document,
    )
    tasks = [(parse, path) for parse in parsers for path in sorted(work.glob("*.pdf"))]
    sequential = [digest(parse(path)) for parse, path in tasks]

    with ThreadPoolExecutor(max_workers=8) as pool:
        concurrent = list(pool.map(lambda task: digest(task[0](task[1])), tasks * 5))

    assert concurrent == sequential * 5
    assert pdf_watch and all(held for _, held in pdf_watch)
