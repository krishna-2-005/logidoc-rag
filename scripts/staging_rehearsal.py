"""
Staging deployment rehearsal: the real server, in the production profile, on an isolated folder of
synthetic data (docs/STAGING_REHEARSAL.md).

    python scripts/staging_rehearsal.py --work NEW_FOLDER      the whole rehearsal
    python scripts/staging_rehearsal.py --work NEW_FOLDER --report report.json

NEW_FOLDER must not exist. Everything the rehearsal creates is inside it and is kept for inspection:
data, index, backups, a restored copy, logs and the report. The installation's own data folder,
index and databases are refused as a work folder, and every LOGIDOC_* setting of the calling
environment is dropped, so nothing real is opened, upgraded or changed. No production secret and no
real document is used: accounts and the signing key are generated for the run.

What runs for real: `python src/api/serve.py` with LOGIDOC_ENV=production (startup validation,
sign-in, Secure cookies, trusted host, the React build, SQLite, ChromaDB, the scheduler and the
background loop), scripts/manage_users.py and scripts/manage_backups.py.

What is simulated, and said so in every result:
- the HTTPS reverse proxy: requests go to the loopback port with the Host and X-Forwarded-Proto
  headers a proxy would send (TLS, certificates and the proxy's own limits are not exercised);
- the model service: a stand-in for Ollama on a loopback port, with deterministic embeddings and a
  fixed answer (no model is loaded);
- outbound webhooks: configuration and the refusal of unsafe destinations are checked, but nothing
  is delivered (production allows public HTTPS destinations only);
- the TMS: no vendor and no sandbox (the sandbox adapter is refused in production).

Steps (each PASS, FAIL or SIMULATED):
   1 configuration            7 correction and controlled re-audit
   2 startup and shutdown     8 risk and reporting
   3 frontend and API         9 webhooks and scheduler
   4 sign-in and roles       10 monitoring and health
   5 document ingestion      11 backup, verification and isolated restore
   6 freight audit and       12 migration guard on old-schema databases
     exceptions

Exit status: 0 no step failed; 1 a step failed; 2 refused or the server did not start.
"""

import argparse
import hashlib
import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

HOST_NAME = "staging.logidoc.test"
SHIPMENT = "SHJ-2026-101"
KEY = "SHJ2026101"
INVOICE = "INVOICE_SHJ-2026-101.pdf"
MODELS = ("nomic-embed-text:latest", "nemotron-3-nano:4b")

PASS, FAIL, SIMULATED = "PASS", "FAIL", "SIMULATED"


class Refused(Exception):
    pass


# ============================================================
# Results
# ============================================================

class Results:

    def __init__(self):
        self.items = []

    def add(self, step, name, outcome, detail=""):
        self.items.append({"step": step, "check": name, "outcome": outcome, "detail": detail})
        print(f"  {outcome:<9} {step:>2}  {name}: {detail}", flush=True)

    def check(self, step, name, passed, detail=""):
        self.add(step, name, PASS if passed else FAIL, detail)
        return passed

    @property
    def failed(self):
        return [item for item in self.items if item["outcome"] == FAIL]


# ============================================================
# The stand-in model service (deterministic, loopback only)
# ============================================================

class ModelStandIn(BaseHTTPRequestHandler):
    """Answers the Ollama routes the application calls. Nothing is loaded or generated."""

    def log_message(self, *arguments):
        pass

    def reply(self, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply({"models": [{"name": name, "model": name} for name in MODELS]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        text = str(body.get("prompt") or body.get("input") or "")
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vector = [round(byte / 255, 6) for byte in digest[:8]]

        if self.path.endswith("/api/embeddings"):
            self.reply({"embedding": vector})
        elif self.path.endswith("/api/embed"):
            self.reply({"embeddings": [vector]})
        else:
            self.reply({"model": MODELS[1], "created_at": "2026-01-01T00:00:00Z", "done": True,
                        "message": {"role": "assistant", "content": "Stand-in answer: no model is loaded in the rehearsal."}})


def free_port():

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def port_open(port):

    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


# ============================================================
# The isolated environment
# ============================================================

def check_work_folder(work):

    import config

    work = Path(work).resolve()
    protected = [Path(place).resolve() for place in (PROJECT_ROOT / "data", PROJECT_ROOT / "chroma_db", config.DATA_DIR,
                                                      config.CHROMA_DIR, config.BACKUP_DIR)]

    for place in protected:
        if work == place or place in work.parents or work in place.parents:
            raise Refused("The work folder is, or holds, the installation's own data: choose a folder outside the project.")

    if work.exists():
        raise Refused("The work folder already exists; give a new one (nothing is overwritten).")

    return work


def environment(root, port, model_port, signing_key):
    """The production settings of the rehearsal: only what is listed here, plus what Python and the OS need."""

    kept = {name: value for name, value in os.environ.items() if not name.startswith(("LOGIDOC_", "OLLAMA_"))}
    kept.update({
        "LOGIDOC_ENV": "production",
        "LOGIDOC_AUTH_MODE": "required",
        "LOGIDOC_API_HOST": "127.0.0.1",
        "LOGIDOC_API_PORT": str(port),
        "LOGIDOC_TRUSTED_HOSTS": HOST_NAME,
        "LOGIDOC_CORS_ORIGINS": f"https://{HOST_NAME}",
        "LOGIDOC_FORWARDED_ALLOW_IPS": "127.0.0.1",
        "LOGIDOC_DATA_DIR": str(root / "data"),
        "LOGIDOC_CHROMA_DIR": str(root / "chroma_db"),
        "LOGIDOC_BACKUP_DIR": str(root / "backups"),
        "LOGIDOC_BACKUP_SCHEDULE": "daily@02:30",
        "LOGIDOC_LOG_FILE": str(root / "logs" / "api.log"),
        "LOGIDOC_WEBHOOK_SECRET_KEY": signing_key,
        "OLLAMA_HOST": f"http://127.0.0.1:{model_port}",
        "PYTHONIOENCODING": "utf-8",
    })

    return kept


def run(arguments, env, timeout=300):

    return subprocess.run([sys.executable, *arguments], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


class Server:
    """`python src/api/serve.py` as its own process."""

    def __init__(self, env, port, log_path):
        self.env, self.port, self.log_path = env, port, log_path
        self.process = None
        self.log = None

    def start(self, wait=90):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(self.log_path, "ab")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen([sys.executable, str(PROJECT_ROOT / "src" / "api" / "serve.py")], cwd=PROJECT_ROOT,
                                        env=self.env, stdout=self.log, stderr=subprocess.STDOUT, creationflags=flags)
        deadline = time.time() + wait

        while time.time() < deadline:
            if self.process.poll() is not None:
                return False
            if port_open(self.port):
                return True
            time.sleep(0.3)

        return False

    def stop(self):
        """Stops it as an operator would (Ctrl+Break / SIGINT). Returns its exit code."""

        if self.process is None:
            return None

        if self.process.poll() is None:
            self.process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            try:
                self.process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

        if self.log is not None:
            self.log.close()
            self.log = None

        return self.process.returncode


class Session:
    """A browser behind the proxy: the proxy's headers on every request, and the session cookie kept by hand
    (it is Secure, so an HTTP client would not send it over the loopback connection itself)."""

    def __init__(self, port):
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=120, headers={
            "Host": HOST_NAME, "X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.10", "X-LogiDoc-Client": "web"})
        self.cookie = None
        self.cookie_flags = ""

    def request(self, method, url, **options):
        headers = dict(options.pop("headers", {}) or {})
        if self.cookie:
            headers["Cookie"] = self.cookie
        return self.client.request(method, url, headers=headers, **options)

    def get(self, url, **options):
        return self.request("GET", url, **options)

    def post(self, url, **options):
        return self.request("POST", url, **options)

    def login(self, username, password):
        reply = self.client.post("/api/auth/login", json={"username": username, "password": password})
        header = reply.headers.get("set-cookie", "")
        if reply.status_code == 200 and header:
            self.cookie = header.split(";", 1)[0]
            self.cookie_flags = header
        return reply


# ============================================================
# The steps
# ============================================================

def walk(results, root, env, port, passwords):
    """Steps 3 to 10 against the running server."""

    visitor = Session(port)
    alice, dave, bob = Session(port), Session(port), Session(port)

    # 3. Frontend and API
    page = visitor.get("/")
    built = (PROJECT_ROOT / "frontend" / "dist" / "index.html").is_file()
    if built:
        results.check(3, "the React build is served", page.status_code == 200 and "<div id=\"root\"" in page.text,
                      f"GET / {page.status_code}, Content-Security-Policy {'set' if 'content-security-policy' in page.headers else 'missing'}")
        asset = [part.split('"')[0] for part in page.text.split('src="/')[1:]]
        got = visitor.get("/" + asset[0]) if asset else None
        results.check(3, "its script loads", bool(got) and got.status_code == 200, f"{asset[0] if asset else 'no script'}")
    else:
        results.add(3, "the React build is served", FAIL, "frontend/dist is missing: run npm run build first")

    health = visitor.get("/api/health")
    results.check(3, "the API answers", health.status_code == 200 and health.json() == {"status": "ok"}
                  and health.headers.get("api-version") == "1", f"/api/health {health.status_code}, API-Version {health.headers.get('api-version')}")
    other_host = httpx.get(f"http://127.0.0.1:{port}/api/health", headers={"Host": "attacker.example"}, timeout=30)
    results.check(3, "an unknown host name is refused", other_host.status_code == 400, f"Host attacker.example: {other_host.status_code}")
    docs = visitor.get("/api/docs")
    results.check(3, "interactive API documentation is off in production", docs.status_code == 404, f"/api/docs {docs.status_code}")

    # 4. Sign-in and roles
    results.check(4, "no data without a session", visitor.get("/api/shipments").status_code == 401, "GET /api/shipments 401")
    wrong = Session(port).login("alice", "not the password")
    results.check(4, "a wrong password is refused", wrong.status_code == 401, f"{wrong.status_code}")
    signed = [session.login(name, passwords[name]).status_code for session, name in ((alice, "alice"), (dave, "dave"), (bob, "bob"))]
    flags = alice.cookie_flags.lower()
    results.check(4, "administrators and a reviewer sign in", signed == [200, 200, 200], f"{signed}")
    results.check(4, "the session cookie is Secure, HttpOnly and SameSite=Strict",
                  all(flag in flags for flag in ("secure", "httponly", "samesite=strict")), "flags of Set-Cookie")
    who = bob.get("/api/session").json()
    results.check(4, "the session names the role", who.get("authenticated") and who["user"]["role"] == "reviewer", f"bob: {who.get('user', {}).get('role')}")
    refused = [bob.get("/api/admin/users").status_code, bob.post("/api/history/runs", json={"label": "x"}).status_code]
    results.check(4, "a reviewer is refused administrator operations", refused == [403, 403], f"{refused}")
    no_csrf = httpx.post(f"http://127.0.0.1:{port}/api/history/runs", json={"label": "x"}, timeout=30,
                         headers={"Host": HOST_NAME, "X-Forwarded-Proto": "https", "Cookie": alice.cookie})
    results.check(4, "a change without the CSRF header is refused", no_csrf.status_code == 403, f"{no_csrf.status_code}")
    release_seen = alice.get("/api/settings").json().get("release")
    results.check(4, "the server reports its release to a signed-in user", bool(release_seen), f"release {release_seen}")

    # 5. Document ingestion (synthetic PDFs, embeddings from the stand-in)
    from test_freight_audit import write_contracted_shipment

    incoming = root / "incoming"
    incoming.mkdir()
    write_contracted_shipment(incoming)
    files = [("files", (path.name, path.read_bytes(), "application/pdf")) for path in sorted(incoming.glob("*.pdf"))]
    results.check(5, "a reviewer may not upload", bob.post("/api/ingestion/upload", files=files).status_code == 403, "403")
    uploaded = alice.post("/api/ingestion/upload", files=files)
    body = uploaded.json() if uploaded.status_code == 200 else {}
    types = {item["filename"]: item["document_type"] for item in body.get("files", [])}
    results.check(5, "four synthetic PDFs are stored, classified and indexed",
                  body.get("indexed") == 4 and sorted(types.values()) == ["BOL", "INVOICE", "POD", "RATE_CONFIRMATION"],
                  f"indexed {body.get('indexed')}, types {sorted(types.values())}")
    results.add(5, "embeddings", SIMULATED, "computed by the stand-in model service, not by Ollama")
    garbage = alice.post("/api/ingestion/upload", files=[("files", ("broken.pdf", b"not a pdf", "application/pdf"))])
    refused_file = garbage.json() if garbage.status_code == 200 else {}
    results.check(5, "an unusable file is reported, not stored",
                  refused_file.get("indexed") == 0 and refused_file.get("unsupported", 0) + refused_file.get("failed", 0) == 1
                  and not (root / "data" / "raw" / "broken.pdf").exists(),
                  f"status {(refused_file.get('files') or [{}])[0].get('status')} ({(refused_file.get('files') or [{}])[0].get('code')})")

    # 6. Freight audit and exceptions
    shipment = alice.get(f"/api/shipments/{SHIPMENT}").json()
    audit = alice.get(f"/api/shipments/{SHIPMENT}/freight-audit").json()["freight_audit"]
    fuel = next((item for item in audit["charges"] if item["charge"] == "Fuel surcharge"), {})
    results.check(6, "the shipment is reconciled", shipment.get("result", {}).get("success") is True, f"final status {shipment.get('result', {}).get('final_status')}")
    results.check(6, "the freight audit finds the fuel surcharge above its contract",
                  audit["outcome"] == "DISCREPANCY" and fuel.get("outcome") == "DISCREPANCY", f"outcome {audit['outcome']}")
    first = alice.post("/api/history/runs", json={"label": "Rehearsal month end"})
    results.check(6, "the audit is recorded", first.status_code == 201, f"run {first.json().get('run_id') if first.status_code == 201 else first.status_code}")
    exceptions = {row["category"]: row for row in alice.get("/api/exceptions").json()["exceptions"]}
    detention = exceptions.get("Freight audit: Detention Charge")
    steps = []
    if detention:
        key = detention["exception_key"]
        steps = [bob.post(f"/api/exceptions/{key}/actions", json=body).status_code for body in (
            {"action": "dismiss", "note": "Not a real problem at all."},
            {"action": "assign", "assignee": "bob"}, {"action": "start"},
            {"action": "resolve", "note": "Detention confirmed by the consignee.", "evidence": ["POD_SHJ-2026-101.pdf p.1"]})]
        history = [event["event_type"] for event in alice.get(f"/api/exceptions/{key}/history").json()]
    else:
        history = []
    results.check(6, "an exception is worked by a reviewer, who may not dismiss it", steps == [403, 200, 200, 200], f"{steps}")
    results.check(6, "its history is recorded and verifies", history == ["detected", "assign", "start", "resolve"]
                  and alice.post("/api/admin/verify-history").json().get("ok") is True, f"{history}")

    # 7. Correction and controlled re-audit
    extraction_id = alice.post("/api/extractions/run", json={"document": INVOICE}).json().get("id")
    lines = alice.get(f"/api/extractions/{extraction_id}").json()["line_items"]
    index = next(item["index"] for item in lines if item["extracted"]["description"] == "Fuel Surcharge")
    proposal = alice.post(f"/api/extractions/{extraction_id}/line-items/{index}/corrections",
                          json={"amount": "180.00", "reason": "Credit note CN-17 reduces the fuel surcharge to the contracted amount.", "based_on": None})
    correction = proposal.json().get("id") if proposal.status_code == 200 else None
    own = alice.post(f"/api/extractions/corrections/{correction}/decision", json={"decision": "approve"}).status_code
    other = dave.post(f"/api/extractions/corrections/{correction}/decision", json={"decision": "approve", "note": "Credit note checked."}).status_code
    results.check(7, "a correction needs a second administrator", (proposal.status_code, own, other) == (200, 403, 200),
                  f"proposed {proposal.status_code}, own approval {own}, other administrator {other}")
    preview = alice.get(f"/api/shipments/{SHIPMENT}/re-audit").json()
    confirmed = alice.post(f"/api/shipments/{SHIPMENT}/re-audit", json={"token": preview.get("token"), "reason": "Credit note CN-17 applied."})
    after = alice.get(f"/api/shipments/{SHIPMENT}/freight-audit").json()["freight_audit"]
    fuel_after = next((item for item in after["charges"] if item["charge"] == "Fuel surcharge"), {})
    results.check(7, "the controlled re-audit is previewed and recorded", preview.get("needed") is True and confirmed.status_code == 201,
                  f"needed {preview.get('needed')}, recorded {confirmed.status_code}")
    results.check(7, "the audit now reads the approved amount", fuel_after.get("outcome") != "DISCREPANCY"
                  and [item["correction_id"] for item in after["corrections_applied"]] == [correction], f"fuel surcharge {fuel_after.get('outcome')}")
    timeline = alice.get(f"/api/history/shipments/{SHIPMENT}").json()
    results.check(7, "the first audit is kept beside the new one", len(timeline) == 2, f"{len(timeline)} snapshots")

    # 8. Risk and reporting
    risk = alice.get(f"/api/risk/shipments/{SHIPMENT}").json()
    report = alice.get("/api/reporting/freight-audit-summary", params={"shipment": SHIPMENT}).json()
    row = (report.get("rows") or [{}])[0]
    exported = bob.get("/api/reporting/freight-audit-summary.csv", params={"shipment": SHIPMENT})
    pdf = bob.get("/api/reporting/freight-audit-summary.pdf", params={"shipment": SHIPMENT})
    results.check(8, "the risk score is explained", risk.get("score") == sum(f["points"] for f in risk.get("factors", [])),
                  f"score {risk.get('score')} ({risk.get('category')}), model {risk.get('model', {}).get('version')}")
    results.check(8, "the report agrees with the risk and the invoice", row.get("risk_score") == risk.get("score") and row.get("invoice_total") == 1675.0,
                  f"invoice total {row.get('invoice_total')}, risk {row.get('risk_score')}")
    results.check(8, "CSV and PDF download for a reviewer", exported.status_code == 200 and SHIPMENT in exported.text
                  and pdf.status_code == 200 and pdf.content.startswith(b"%PDF"), f"CSV {exported.status_code}, PDF {pdf.status_code}")

    # 9. Webhooks and scheduler
    settings = alice.get("/api/integrations/webhooks/settings")
    configuration = settings.json() if settings.status_code == 200 else {}
    results.check(9, "webhook delivery is configured for production",
                  configuration.get("delivery_enabled") is True and configuration.get("problems") == []
                  and configuration.get("https_only") is True and configuration.get("private_destinations_allowed") is False,
                  "signing key set, HTTPS only, private destinations refused")
    unsafe = [alice.post("/api/integrations/webhooks", json={"name": "unsafe", "url": url, "event_types": ["exception.created"]}).status_code
              for url in ("http://127.0.0.1:9000/hook", "https://127.0.0.1/hook", "https://10.0.0.5/hook", "http://partner.example/hook")]
    results.check(9, "loopback, private and plain-HTTP webhook destinations are refused", all(code == 422 for code in unsafe), f"{unsafe}")
    results.add(9, "webhook delivery", SIMULATED, "not exercised: production delivers to public HTTPS destinations only; covered by tests/test_webhooks.py with a stand-in receiver")
    jobs = alice.get("/api/integrations/jobs")
    scheduler = jobs.json() if jobs.status_code == 200 else {}
    results.check(9, "the scheduler reports its state", jobs.status_code == 200 and scheduler.get("configured") is False
                  and scheduler.get("jobs") == [], "idle by design: it starts with the first webhook endpoint or integration")
    results.add(9, "scheduled job runs", SIMULATED, "not exercised: no endpoint or integration is configured here; covered by tests/test_scheduler.py")
    results.check(9, "a reviewer cannot manage integrations", bob.get("/api/integrations/jobs").status_code == 403, "403")
    results.add(9, "TMS integration", SIMULATED, "no vendor is connected, and the synthetic sandbox adapter is refused in production")

    # 10. Monitoring and health
    ready = visitor.get("/api/ready")
    checks = ready.json().get("checks", {}) if ready.status_code in (200, 503) else {}
    results.check(10, "readiness", ready.status_code == 200 and ready.json().get("status") in ("ready", "degraded"),
                  f"{ready.json().get('status')}; " + ", ".join(f"{name} {'ok' if value.get('ok') else 'not ok'}" for name, value in sorted(checks.items())))
    results.check(10, "readiness names no path", str(root) not in ready.text and "sqlite" not in ready.text.lower(), "status words only")
    metrics = alice.get("/api/admin/metrics")
    results.check(10, "metrics for administrators only", metrics.status_code == 200 and "logidoc_http_requests_total" in metrics.text
                  and bob.get("/api/admin/metrics").status_code == 403, f"administrator {metrics.status_code}, reviewer 403")
    alerts = alice.get("/api/admin/alerts")
    results.check(10, "alerts are readable", alerts.status_code == 200, f"{alerts.status_code}")
    request_id = visitor.get("/api/health", headers={"X-Request-ID": "rehearsal-0001"}).headers.get("x-request-id")
    results.check(10, "request IDs are carried", request_id == "rehearsal-0001", f"{request_id}")

    # In-application backup (the server holds the data folder's lock, so this is the way while it runs)
    started = alice.post("/api/admin/backups", json={"label": "rehearsal, server running"})
    deadline = time.time() + 120
    listed = []
    while time.time() < deadline:
        listed = [item for item in alice.get("/api/admin/backups").json().get("backups", []) if item.get("usable")]
        if listed:
            break
        time.sleep(1)
    results.check(11, "an administrator makes a backup while the server runs", started.status_code == 202 and bool(listed),
                  f"started {started.status_code}, usable backups {len(listed)}")

    return {"alice": alice, "risk": risk.get("score"), "report_row": row, "backup_id": listed[0]["backup_id"] if listed else None}


def log_checks(results, root, passwords, signing_key):

    path = root / "logs" / "api.log"
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    lines = [line for line in text.splitlines() if line.strip()]
    parsed = 0

    for line in lines:
        try:
            json.loads(line)
            parsed += 1
        except ValueError:
            pass

    results.check(10, "the log file holds JSON lines", bool(lines) and parsed == len(lines), f"{parsed} of {len(lines)} lines")
    leaked = [name for name, secret in (*passwords.items(), ("signing key", signing_key)) if secret in text]
    results.check(10, "no password or signing key is logged", not leaked, "searched the log file for every generated secret")
    results.check(10, "no document text is logged", "Driver held at consignee" not in text and "Fuel Surcharge: $200.00" not in text, "searched for text of the synthetic documents")


def backup_and_restore(results, root, env, seen):
    """Step 11, with the server stopped: the command line, then a restored copy compared with the original."""

    made = run(["scripts/manage_backups.py", "create", "--label", "rehearsal"], env)
    results.check(11, "manage_backups.py create", made.returncode == 0, (made.stdout.strip().splitlines() or [made.stderr.strip()[:120]])[-1][:120])

    import backup

    backups = [item for item in backup.list_backups(root / "backups") if item["usable"]]
    newest = backups[0]["backup_id"] if backups else None
    verified = run(["scripts/manage_backups.py", "verify", newest], env) if newest else None
    results.check(11, "manage_backups.py verify", bool(verified) and verified.returncode == 0, f"{len(backups)} usable backups")
    restored = root / "restored"
    done = run(["scripts/manage_backups.py", "restore", newest, "--target", str(restored)], env) if newest else None
    results.check(11, "manage_backups.py restore into a new folder", bool(done) and done.returncode == 0 and (restored / "data" / "raw").is_dir(),
                  "restored" if done and done.returncode == 0 else (done.stderr.strip()[:160] if done else "no backup"))
    refused = run(["scripts/manage_backups.py", "restore", newest, "--target", str(root / "data")], env) if newest else None
    results.check(11, "a restore over the live data folder is refused", bool(refused) and refused.returncode != 0, "refused")
    results.check(11, "the document text cache is in no backup", not [p for p in (root / "backups").rglob("*") if "page_text" in p.name]
                  and not (restored / "data" / "cache").exists(), "neither in a backup nor in the restored copy")

    return restored if done and done.returncode == 0 else None


def restored_matches(results, restored, env, port, passwords, seen):
    """The restored copy, served by a second server: the same business content."""

    copy_env = dict(env, LOGIDOC_DATA_DIR=str(restored / "data"), LOGIDOC_CHROMA_DIR=str(restored / "chroma_db"),
                    LOGIDOC_BACKUP_DIR=str(restored / "backups"), LOGIDOC_LOG_FILE=str(restored / "logs" / "api.log"),
                    LOGIDOC_API_PORT=str(port))
    server = Server(copy_env, port, restored / "logs" / "server-output.log")

    if not results.check(11, "the restored copy starts", server.start(), "its own server on another port"):
        server.stop()
        return

    try:
        alice = Session(port)
        signed = alice.login("alice", passwords["alice"]).status_code
        risk = alice.get(f"/api/risk/shipments/{SHIPMENT}").json().get("score")
        row = (alice.get("/api/reporting/freight-audit-summary", params={"shipment": SHIPMENT}).json().get("rows") or [{}])[0]
        timeline = alice.get(f"/api/history/shipments/{SHIPMENT}").json()
        results.check(11, "the restored accounts sign in", signed == 200, f"{signed}")
        results.check(11, "the restored copy gives the same risk and report", risk == seen["risk"] and row == seen["report_row"],
                      f"risk {risk} (was {seen['risk']}), report row {'identical' if row == seen['report_row'] else 'different'}")
        results.check(11, "the restored history is whole", len(timeline) == 2 and alice.post("/api/admin/verify-history").json().get("ok") is True,
                      f"{len(timeline)} snapshots, review history verifies")
    finally:
        server.stop()


def old_schema_databases(data):
    """A review database at schema version 2 and an audit history at version 3, as an older release wrote them."""

    import audit_history
    import review_store

    data.mkdir(parents=True)
    (data / "raw").mkdir()

    with closing(sqlite3.connect(data / "audit_reviews.sqlite3")) as connection:
        connection.executescript(review_store.SCHEMA_V1)
        connection.execute("INSERT INTO schema_info (version) VALUES (2)")
        for statement in review_store.MIGRATION_V2:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO exception_reviews (exception_key, shipment_key, shipment_id, category, status, signature, note, "
            "created_at, updated_at) VALUES ('K|invoice', 'K', 'K-1', 'Invoice mismatch', 'dismissed', 'sig', "
            "'Duplicate of a corrected invoice.', '2026-10-01T00:00:00+00:00', '2026-10-02T00:00:00+00:00')")
        connection.commit()

    with closing(sqlite3.connect(data / "audit_history.sqlite3")) as connection:
        connection.executescript(audit_history.SCHEMA)
        connection.execute("INSERT INTO schema_info VALUES (1)")
        for statement in (*audit_history.MIGRATION_V2, *audit_history.MIGRATION_V3):
            connection.execute(statement)
        connection.execute("UPDATE schema_info SET version = 3")
        connection.execute("INSERT INTO audit_runs (created_at, label, shipments_checked, snapshots_written) "
                           "VALUES ('2026-10-01T09:00:00+00:00', 'old', 0, 0)")
        connection.commit()


def versions(data):

    found = {}

    for name in ("audit_reviews", "audit_history"):
        with closing(sqlite3.connect(f"{(data / (name + '.sqlite3')).resolve().as_uri()}?mode=ro", uri=True)) as connection:
            found[name] = connection.execute("SELECT version FROM schema_info").fetchone()[0]

    return found


def migration_guard(results, root, env, port):
    """Step 12: an installation of an older release, started by this one."""

    old = root / "old-installation"
    old_schema_databases(old / "data")
    old_env = dict(env, LOGIDOC_DATA_DIR=str(old / "data"), LOGIDOC_CHROMA_DIR=str(old / "chroma_db"),
                   LOGIDOC_BACKUP_DIR=str(old / "backups"), LOGIDOC_LOG_FILE=str(old / "logs" / "api.log"), LOGIDOC_API_PORT=str(port))
    before = {path.name: path.read_bytes() for path in (old / "data").glob("*.sqlite3")}

    refused = run(["src/api/serve.py"], old_env, timeout=120)
    results.check(12, "without a backup the server does not start", refused.returncode == 4 and "manage_backups.py create" in refused.stderr,
                  f"exit code {refused.returncode}, with the steps to follow")
    results.check(12, "and nothing was upgraded or written", {path.name: path.read_bytes() for path in (old / "data").glob("*.sqlite3")} == before
                  and versions(old / "data") == {"audit_reviews": 2, "audit_history": 3}, f"{versions(old / 'data')}")

    made = run(["scripts/manage_backups.py", "create", "--label", "before upgrade"], old_env)
    results.check(12, "the operator makes a backup", made.returncode == 0, "manage_backups.py create")

    server = Server(old_env, port, old / "logs" / "server-output.log")
    started = server.start()
    try:
        upgraded = versions(old / "data") if started else {}
        import audit_history
        import review_store
        results.check(12, "with a fresh verified backup the upgrade runs at startup", started
                      and upgraded == {"audit_reviews": review_store.SCHEMA_VERSION, "audit_history": audit_history.SCHEMA_VERSION}, f"{upgraded}")
        health = httpx.get(f"http://127.0.0.1:{port}/api/health", headers={"Host": HOST_NAME}, timeout=30) if started else None
        results.check(12, "and the upgraded installation serves", bool(health) and health.status_code == 200, "GET /api/health")
    finally:
        server.stop()


def rehearse(work, report_path=None):

    root = check_work_folder(work)
    root.mkdir(parents=True)
    results = Results()
    signing_key = secrets.token_urlsafe(48)
    passwords = {name: secrets.token_urlsafe(18) for name in ("alice", "dave", "bob")}
    port, restored_port, old_port, model_port = free_port(), free_port(), free_port(), free_port()
    env = environment(root, port, model_port, signing_key)

    models = ThreadingHTTPServer(("127.0.0.1", model_port), ModelStandIn)
    threading.Thread(target=models.serve_forever, daemon=True).start()
    print(f"Staging rehearsal in {root} (synthetic data; production profile; simulated proxy and model service)", flush=True)

    try:
        # 1. Configuration
        checked = run(["src/api/serve.py", "--check"], env)
        results.check(1, "the production settings are valid", checked.returncode == 0, checked.stdout.strip().splitlines()[0] if checked.stdout.strip() else checked.stderr.strip()[:160])
        broken = run(["src/api/serve.py", "--check"], dict(env, LOGIDOC_AUTH_MODE="local", LOGIDOC_FORWARDED_ALLOW_IPS="*"))
        results.check(1, "unsafe production settings stop the start", broken.returncode == 2 and "LOGIDOC_AUTH_MODE" in broken.stderr
                      and "LOGIDOC_FORWARDED_ALLOW_IPS" in broken.stderr, f"exit code {broken.returncode}, each setting named")
        version = run(["src/api/serve.py", "--version"], env)
        results.check(1, "the release is identified", version.returncode == 0 and version.stdout.startswith("LogiDoc-RAG "), version.stdout.strip())

        accounts = []
        for name, role in (("alice", "admin"), ("dave", "admin"), ("bob", "reviewer")):
            made = run(["scripts/manage_users.py", "create", name, "--role", role, "--password-env", "REHEARSAL_PASSWORD"],
                       dict(env, REHEARSAL_PASSWORD=passwords[name]))
            accounts.append(made.returncode)
        results.check(1, "accounts are created from the command line", accounts == [0, 0, 0], "passwords from the environment, never an argument")

        # 2. Startup
        server = Server(env, port, root / "logs" / "server-output.log")

        if not results.check(2, "the server starts", server.start(), f"python src/api/serve.py on 127.0.0.1:{port}"):
            server.stop()
            return finish(results, root, report_path, 2)

        second = run(["src/api/serve.py"], env, timeout=60)
        results.check(2, "a second server on the same data is refused", second.returncode == 3, f"exit code {second.returncode}")

        try:
            seen = walk(results, root, env, port, passwords)
        finally:
            code = server.stop()

        output = (root / "logs" / "server-output.log").read_text(encoding="utf-8", errors="replace")
        results.check(2, "the server stops cleanly", "Application shutdown complete" in output and not port_open(port),
                      "shutdown completed in its log, port released")
        log_checks(results, root, passwords, signing_key)

        cache = root / "data" / "cache" / "page_text.cache"
        results.check(5, "the document text cache was built", cache.is_file(), "data/cache/page_text.cache")

        # 11. Backup and restore (server stopped)
        restored = backup_and_restore(results, root, env, seen)

        if restored is not None:
            restored_matches(results, restored, env, restored_port, passwords, seen)

        # 12. Migration guard
        migration_guard(results, root, env, old_port)

    finally:
        models.shutdown()

    return finish(results, root, report_path, 1 if results.failed else 0)


def finish(results, root, report_path, code):

    import release

    counts = {name: sum(item["outcome"] == name for item in results.items) for name in (PASS, FAIL, SIMULATED)}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "release": release.VERSION,
        "profile": "production",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "simulated": ["HTTPS reverse proxy (headers only)", "model service (stand-in for Ollama)", "webhook delivery", "TMS"],
        "counts": counts,
        "results": results.items,
    }
    target = Path(report_path) if report_path else root / "staging-rehearsal-report.json"
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"RESULT: {'FAIL' if counts[FAIL] else 'PASS'}  ({counts[PASS]} passed, {counts[FAIL]} failed, {counts[SIMULATED]} simulated)")

    return code


def main(argv=None):

    parser = argparse.ArgumentParser(description="Staging deployment rehearsal on isolated synthetic data.")
    parser.add_argument("--work", required=True, help="a new folder for everything the rehearsal creates")
    parser.add_argument("--report", help="where to write the JSON report (default: in the work folder)")
    arguments = parser.parse_args(argv)

    try:
        return rehearse(arguments.work, arguments.report)
    except Refused as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
