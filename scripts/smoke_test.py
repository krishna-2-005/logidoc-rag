"""
Pre-demo smoke test: start the real server on temporary folders and walk the main workflow.

    python scripts/smoke_test.py            the whole walk (the upload step needs Ollama)
    python scripts/smoke_test.py --keep     keep the temporary folder afterwards

Everything happens in a new temporary folder: LOGIDOC_DATA_DIR and LOGIDOC_CHROMA_DIR point there and
every LOGIDOC_*_DB override is removed, so data/, chroma_db/ and the real databases are never opened.

  1. starts `python src/api/serve.py` on a free loopback port (sign-in mode local)
  2. /api/health, then /api/ready (ready; degraded when Ollama is not usable)
  3. the React build is served at / (when frontend/dist exists)
  4. uploads the six sample PDFs from tests/fixtures (real embeddings through Ollama)
  5. the audit: SHJ-2026-001 has discrepancies, SHJ-2026-002 has none
  6. a structured extraction, the assistant's deterministic answer with its citations, the PDF report
  7. stops the server gracefully and checks that the port is free

When Ollama is not usable, step 4 is reported as WARN and the sample PDFs are copied into the temporary
document folder instead, so steps 5 and 6 still run.

Exit status: 0 no check failed (WARN allowed); 1 a check failed; 2 the server did not start.
"""

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = sorted((PROJECT_ROOT / "tests" / "fixtures").glob("shipment_SHJ-2026-00*/*.pdf"))
CLIENT = {"X-LogiDoc-Client": "web"}
EXPECTED_STATUS = {"SHJ-2026-001": "DISCREPANCIES DETECTED", "SHJ-2026-002": "NO DISCREPANCIES"}

results = []


def record(step, outcome, detail):

    results.append((step, outcome, detail))
    print(f"  {outcome:<4}  {step:<34} {detail}", flush=True)


def free_port():

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def port_open(port):

    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def environment(work, port):

    env = {key: value for key, value in os.environ.items() if not (key.startswith("LOGIDOC_") and key.endswith("_DB"))}
    env.pop("LOGIDOC_ENV", None)
    env.update({
        "LOGIDOC_DATA_DIR": str(work / "data"), "LOGIDOC_CHROMA_DIR": str(work / "chroma"),
        "LOGIDOC_API_HOST": "127.0.0.1", "LOGIDOC_API_PORT": str(port), "LOGIDOC_AUTH_MODE": "local",
        "PYTHONIOENCODING": "utf-8",
    })

    return env


def start(work, port, log):

    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

    return subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "src" / "api" / "serve.py")], cwd=PROJECT_ROOT,
        env=environment(work, port), stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
    )


def stop(server):

    if server.poll() is None:
        server.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)

    try:
        server.wait(timeout=30)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def walk(base, work):

    api = httpx.Client(base_url=base, headers=CLIENT, timeout=60)

    record("health", "PASS" if api.get("/api/health").json() == {"status": "ok"} else "FAIL", "/api/health")

    ready = api.get("/api/ready").json()
    models_ok = ready["checks"]["model_service"]["ok"]
    record("readiness", "PASS" if ready["status"] == "ready" else "WARN" if ready["status"] == "degraded" else "FAIL",
           f"{ready['status']}; model service ok: {models_ok}; extraction store: {ready['checks']['extraction_store'].get('state')}")

    dist = PROJECT_ROOT / "frontend" / "dist" / "index.html"
    page = api.get("/")

    if dist.is_file():
        record("React build served at /", "PASS" if page.status_code == 200 and 'id="root"' in page.text else "FAIL",
               f"HTTP {page.status_code}")
    else:
        record("React build served at /", "WARN", "no frontend/dist: run `npm run build` in frontend/")

    raw = work / "data" / "raw"

    if models_ok:
        reply = api.post("/api/ingestion/upload", timeout=600,
                         files=[("files", (path.name, path.read_bytes(), "application/pdf")) for path in FIXTURES])
        summary = reply.json() if reply.status_code == 200 else {}
        record("upload 6 sample PDFs (Ollama)", "PASS" if summary.get("indexed") == len(FIXTURES) else "FAIL",
               f"HTTP {reply.status_code}; indexed {summary.get('indexed')}, failed {summary.get('failed')}")
    else:
        for path in FIXTURES:
            shutil.copy2(path, raw / path.name)
        record("upload 6 sample PDFs (Ollama)", "WARN", "model service not usable: PDFs copied in instead")

    statuses = {row["shipment_id"]: row["final_status"] for row in api.get("/api/shipments").json()["shipments"]}
    record("audit of the two sample shipments", "PASS" if all(statuses.get(k) == v for k, v in EXPECTED_STATUS.items()) else "FAIL",
           "; ".join(f"{k}: {statuses.get(k)}" for k in EXPECTED_STATUS))

    detail = api.get("/api/shipments/SHJ-2026-001").json()
    record("shipment detail SHJ-2026-001", "PASS" if detail.get("exceptions") else "FAIL",
           f"{len(detail.get('exceptions') or [])} exception(s)")

    extraction = api.post("/api/extractions/run", json={"document": "INVOICE_SHJ-2026-001.pdf"})
    record("structured extraction (invoice)", "PASS" if extraction.status_code == 200 and extraction.json().get("status") == "extracted" else "FAIL",
           f"HTTP {extraction.status_code}; status {extraction.json().get('status') if extraction.status_code == 200 else '-'}")

    answer = api.post("/api/assistant", json={"question": "How many pieces were delivered?", "shipment_id": "SHJ-2026-001"}).json()
    record("assistant (deterministic, cited)",
           "PASS" if answer.get("status") == "answered" and "45" in (answer.get("answer") or "") and answer.get("citations") else "FAIL",
           (answer.get("answer") or "")[:60])

    report = api.get("/api/reports/shipments/SHJ-2026-001.pdf")
    record("PDF report download", "PASS" if report.status_code == 200 and report.content.startswith(b"%PDF") else "FAIL",
           f"HTTP {report.status_code}, {len(report.content)} bytes")


def main(argv=None):

    parser = argparse.ArgumentParser(description="Start the real server on temporary folders and check the main workflow.")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder")
    arguments = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="logidoc_smoke_"))
    (work / "data" / "raw").mkdir(parents=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    print(f"LogiDoc-RAG smoke test on temporary folders ({work}); the real data/ and chroma_db/ are not used")

    with open(work / "server.log", "w", encoding="utf-8") as log:

        server = start(work, port, log)
        started = time.time()

        while time.time() - started < 90:
            if server.poll() is not None:
                break
            try:
                if httpx.get(base + "/api/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)

        if server.poll() is not None or not port_open(port):
            stop(server)
            print(f"The server did not start; see {work / 'server.log'}")
            return 2

        record("server start", "PASS", f"{base} in {time.time() - started:.1f} s")

        try:
            walk(base, work)
        except Exception as error:
            record("walk", "FAIL", f"{type(error).__name__}: {error}"[:100])
        finally:
            stop(server)

    record("graceful stop", "PASS" if not port_open(port) else "FAIL", "port released" if not port_open(port) else "port still open")

    failed = [step for step, outcome, _ in results if outcome == "FAIL"]
    warned = [step for step, outcome, _ in results if outcome == "WARN"]

    if arguments.keep:
        print(f"Kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)

    print(f"RESULT: {'FAIL' if failed else 'PASS'}: {len(results) - len(failed) - len(warned)} passed, "
          f"{len(warned)} warning(s), {len(failed)} failed" + (f" ({', '.join(failed)})" if failed else ""))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
