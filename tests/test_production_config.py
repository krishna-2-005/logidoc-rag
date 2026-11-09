"""
Production configuration (src/environment.py, src/api/core.py,
src/api/middleware.py, deploy/): environment validation, trusted hosts,
HTTPS redirection and HSTS behind a reverse proxy, request-size limits,
and the deployment files. No server is started and nothing is exposed:
the application is driven in-process.

Run from the project root:
    python -m pytest tests/test_production_config.py
"""

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config  # noqa: E402
import environment  # noqa: E402
from api.core import ApiSettings  # noqa: E402
from auth import ConfigError  # noqa: E402
from test_api import CLIENT, Rig  # noqa: E402

DEPLOY = PROJECT_ROOT / "deploy"


def production(**changes):
    """A complete production environment (synthetic values)."""

    values = {
        "LOGIDOC_ENV": "production", "LOGIDOC_AUTH_MODE": "required",
        "LOGIDOC_CORS_ORIGINS": "https://logidoc.acme-freight.test",
        "LOGIDOC_TRUSTED_HOSTS": "logidoc.acme-freight.test",
        "LOGIDOC_DATA_DIR": "/var/lib/logidoc/data", "LOGIDOC_CHROMA_DIR": "/var/lib/logidoc/chroma_db",
        "LOGIDOC_BACKUP_DIR": "/var/backups/logidoc", "LOGIDOC_BACKUP_SCHEDULE": "daily@02:30",
        "LOGIDOC_ALERT_WEBHOOK_URL": "https://alerts.acme-freight.test/hook/s3cr3t-token",
    }
    values.update(changes)

    return {name: value for name, value in values.items() if value is not None}


def env_file(path):

    values = {}

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()

    return values


# ============================================================
# ENVIRONMENT VALIDATION
# ============================================================

def test_a_complete_production_environment_passes():

    report = environment.validate(production())

    assert report == {"environment": "production", "errors": [], "warnings": []}


def test_development_needs_nothing():

    assert environment.validate({}) == {"environment": "development", "errors": [], "warnings": []}
    assert environment.validate({"LOGIDOC_ENV": "test"})["errors"] == []


@pytest.mark.parametrize("name", environment.PRODUCTION_REQUIRED)
def test_production_never_falls_back_to_a_development_default(name):

    errors = environment.validate(production(**{name: None}))["errors"]

    assert any(name in error and "must be set in production" in error for error in errors)


@pytest.mark.parametrize("changes, expected", [
    ({"LOGIDOC_AUTH_MODE": "local"}, "LOGIDOC_AUTH_MODE must be required"),
    ({"LOGIDOC_CORS_ORIGINS": "http://logidoc.acme-freight.test"}, "HTTPS origins only"),
    ({"LOGIDOC_CORS_ORIGINS": "*"}, "'*' is not allowed"),
    ({"LOGIDOC_TRUSTED_HOSTS": "*"}, "not a catch-all wildcard"),
    ({"LOGIDOC_DATA_DIR": "data"}, "LOGIDOC_DATA_DIR must be an absolute path"),
    ({"LOGIDOC_BACKUP_DIR": "/var/lib/logidoc/data/backups"}, "must not be inside LOGIDOC_DATA_DIR"),
    ({"LOGIDOC_CHROMA_DIR": "/var/lib/logidoc/data"}, "must be different folders"),
    ({"LOGIDOC_COOKIE_SECURE": "0"}, "cannot be turned off in production"),
    ({"LOGIDOC_ALERT_WEBHOOK_URL": "http://alerts.acme-freight.test/hook"}, "must use HTTPS"),
    ({"LOGIDOC_TRUSTED_HOSTS": "logidoc.example.com"}, "placeholder value"),
    ({"LOGIDOC_BACKUP_DIR": "/var/backups/<your backups>"}, "placeholder value"),
    ({"LOGIDOC_ENV": "staging"}, "LOGIDOC_ENV must be one of"),
])
def test_insecure_or_unfinished_production_settings_are_refused(changes, expected):

    errors = environment.validate(production(**changes))["errors"]

    assert any(expected in error for error in errors), errors


@pytest.mark.parametrize("changes, expected", [
    ({"LOGIDOC_API_PORT": "eighty"}, "LOGIDOC_API_PORT must be a whole number"),
    ({"LOGIDOC_API_PORT": "70000"}, "between 1 and 65535"),
    ({"LOGIDOC_LLM_TIMEOUT": "-5"}, "positive number of seconds"),
    ({"LOGIDOC_BACKUP_SCHEDULE": "nightly"}, "LOGIDOC_BACKUP_SCHEDULE must be"),
    ({"LOGIDOC_LOG_FORMAT": "xml"}, "LOGIDOC_LOG_FORMAT must be json or text"),
    ({"LOGIDOC_HTTPS_REDIRECT": "maybe"}, "must be 1 or 0"),
    ({"LOGIDOC_AUTH_MODE": "open"}, "must be local or required"),
])
def test_malformed_values_are_refused_in_every_profile(changes, expected):

    for profile in ("development", "production"):
        errors = environment.validate(production(LOGIDOC_ENV=profile, **changes))["errors"]
        assert any(expected in error for error in errors), (profile, errors)


def test_messages_name_settings_never_their_values():

    report = environment.validate(production(LOGIDOC_ALERT_WEBHOOK_URL="http://alerts.acme-freight.test/hook/s3cr3t",
                                             LOGIDOC_API_PORT="s3cr3t"))

    assert report["errors"] and "s3cr3t" not in " ".join(report["errors"] + report["warnings"])


def test_warnings_for_production_choices_that_are_allowed():

    warnings = environment.validate(production(LOGIDOC_BACKUP_SCHEDULE="off", LOGIDOC_API_HOST="0.0.0.0",
                                               LOGIDOC_ALERT_WEBHOOK_URL=None))["warnings"]

    assert any("no automatic backups" in warning for warning in warnings)
    assert any("not a loopback address" in warning for warning in warnings)
    assert any("alerts go to the log only" in warning for warning in warnings)


def test_the_server_refuses_to_start_on_an_error(monkeypatch, capsys):

    from api import serve

    for name in list(environment.PRODUCTION_REQUIRED):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOGIDOC_ENV", "production")

    assert serve.main([]) == 2

    error = capsys.readouterr().err
    assert "cannot start" in error and "LOGIDOC_TRUSTED_HOSTS must be set in production" in error


def test_the_check_option_validates_without_starting_anything(monkeypatch, capsys):

    from api import serve

    for name, value in production(LOGIDOC_BACKUP_SCHEDULE="off").items():
        monkeypatch.setenv(name, value)

    assert serve.main(["--check"]) == 0

    output = capsys.readouterr().out
    assert "Configuration valid for production." in output and "no automatic backups" in output
    assert "s3cr3t-token" not in output


def test_production_settings_and_startup():

    settings = ApiSettings.from_env(production())

    assert settings.environment == "production" and settings.cookie_secure and settings.hsts_seconds == 31536000
    assert settings.trusted_hosts == ("logidoc.acme-freight.test",) and not settings.background_jobs
    assert settings.check_startup() == "required"

    with pytest.raises(ConfigError, match="LOGIDOC_BACKUP_DIR must be set in production"):
        ApiSettings.from_env(production(LOGIDOC_BACKUP_DIR=None)).check_startup()

    # A test profile never runs the background jobs, even when asked to
    assert not ApiSettings.from_env({"LOGIDOC_ENV": "test", "LOGIDOC_BACKGROUND_JOBS": "1"}).background_jobs


# ============================================================
# TRUSTED HOSTS, HTTPS, HSTS
# ============================================================

def test_only_trusted_host_names_are_answered(tmp_path):

    rig = Rig(tmp_path, "required", trusted_hosts=("logidoc.acme-freight.test", "*.branch.acme-freight.test"))

    refused = TestClient(rig.app, base_url="http://evil.test").get("/api/health")
    assert refused.status_code == 400 and refused.json() == {"detail": "Invalid host header."}
    assert "x-request-id" in refused.headers

    for allowed in ("http://logidoc.acme-freight.test", "https://LOGIDOC.acme-freight.test:443",
                    "http://ops.branch.acme-freight.test", "http://127.0.0.1:8000", "http://[::1]:8000"):
        assert TestClient(rig.app, base_url=allowed).get("/api/health").status_code == 200, allowed

    # A look-alike is not a subdomain
    assert TestClient(rig.app, base_url="http://evilbranch.acme-freight.test").get("/api/health").status_code == 400


def test_plain_http_is_redirected_to_https_except_the_probes(tmp_path):

    rig = Rig(tmp_path, "required", https_redirect=True)
    client = TestClient(rig.app, follow_redirects=False)

    reply = client.get("/api/overview?view=all")
    assert reply.status_code == 308 and reply.headers["location"] == "https://testserver/api/overview?view=all"
    assert client.get("/api/health").status_code == 200 and client.get("/api/ready").status_code == 200


def test_https_responses_carry_hsts(tmp_path):

    rig = Rig(tmp_path, "required", https_redirect=True, hsts_seconds=31536000)

    secure = TestClient(rig.app, base_url="https://testserver").get("/api/health")
    assert secure.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"

    assert "strict-transport-security" not in TestClient(rig.app, base_url="http://testserver").get("/api/health").headers


@pytest.mark.parametrize("trusted, redirected", [("*", False), ("127.0.0.1", True)])
def test_the_scheme_from_the_proxy_is_believed_only_from_a_trusted_proxy(tmp_path, trusted, redirected):

    rig = Rig(tmp_path, "required", https_redirect=True, hsts_seconds=600, forwarded_allow_ips=trusted)
    reply = TestClient(rig.app, follow_redirects=False).get("/api/session", headers={"X-Forwarded-Proto": "https"})

    assert (reply.status_code == 308) == redirected
    assert ("strict-transport-security" in reply.headers) == (not redirected)


def test_the_session_cookie_is_secure_in_production(tmp_path):

    rig = Rig(tmp_path, "required", cookie_secure=True)
    client = TestClient(rig.app, base_url="https://testserver", headers=CLIENT)
    reply = client.post("/api/auth/login", json={"username": "alice", "password": "correct horse battery"})

    cookie = reply.headers["set-cookie"].lower()
    assert reply.status_code == 200 and "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/docs").status_code == 404


# ============================================================
# REQUEST SIZE
# ============================================================

def test_a_body_over_the_limit_is_refused_before_it_is_read(tmp_path):

    rig = Rig(tmp_path, "required", max_request_kb=16)
    client = rig.client()

    declared = client.post("/api/auth/login", content=b'{"username": "' + b"a" * 20_000 + b'"}',
                           headers={"Content-Type": "application/json"})
    assert declared.status_code == 413 and declared.json() == {"detail": "The request is larger than the 16 KB limit."}

    def chunks():
        for _ in range(40):
            yield b"x" * 1024

    streamed = client.post("/api/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert streamed.status_code == 413

    assert client.post("/api/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401


def test_uploads_have_their_own_larger_limit(tmp_path):

    rig = Rig(tmp_path, "required", max_upload_mb=1, max_upload_files=1)
    admin = rig.client("alice")

    huge = admin.post("/api/ingestion/upload", files=[("files", ("big.pdf", b"%PDF" + b"0" * (3 * 1048576), "application/pdf"))])
    assert huge.status_code == 413 and huge.json() == {"detail": "The request is larger than the 2 MB limit."}

    # Over the per-file limit but within the whole-upload limit: the route's own message
    over_file = admin.post("/api/ingestion/upload", files=[("files", ("a.pdf", b"%PDF" + b"0" * 1_200_000, "application/pdf"))])
    assert over_file.status_code == 413 and "a.pdf" in over_file.json()["detail"]


# ============================================================
# DEPLOYMENT FILES
# ============================================================

def upload_limit_mb():

    return ApiSettings().max_upload_request_bytes // 1048576


def test_the_nginx_configuration_terminates_tls_and_forwards_correctly():

    text = (DEPLOY / "nginx" / "logidoc.conf").read_text(encoding="utf-8")

    assert "return 301 https://$host$request_uri;" in text
    assert re.search(r"listen 443 ssl;", text) and "ssl_protocols TLSv1.2 TLSv1.3;" in text
    assert f"proxy_pass http://127.0.0.1:{ApiSettings().port};" in text
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in text and "proxy_set_header Host              $host;" in text
    assert "proxy_set_header X-Request-ID      $request_id;" in text
    assert f"client_max_body_size {upload_limit_mb()}m;" in text
    read_timeout = int(re.search(r"proxy_read_timeout (\d+)s;", text).group(1))
    assert read_timeout > config.LLM_TIMEOUT
    assert "/etc/letsencrypt/live/" in text and "TLSv1.1" not in text


def test_the_caddy_configuration_matches_the_application():

    text = (DEPLOY / "Caddyfile").read_text(encoding="utf-8")

    assert f"reverse_proxy 127.0.0.1:{ApiSettings().port}" in text
    assert f"max_size {upload_limit_mb()}MB" in text
    assert "health_uri /api/health" in text and "respond @internal 404" in text
    assert int(re.search(r"read_timeout (\d+)s", text).group(1)) > config.LLM_TIMEOUT


def test_the_service_unit_runs_one_process_with_settings_from_a_file():

    text = (DEPLOY / "logidoc-api.service").read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/logidoc/logidoc.env" in text and "ExecStart=" in text and "src/api/serve.py" in text
    assert "--workers" not in text and "NoNewPrivileges=true" in text and "ProtectSystem=strict" in text

    writable = re.search(r"ReadWritePaths=(.*)", text).group(1).split()
    example = env_file(DEPLOY / "logidoc.production.env.example")

    for name in ("LOGIDOC_DATA_DIR", "LOGIDOC_CHROMA_DIR", "LOGIDOC_BACKUP_DIR", "LOGIDOC_LOG_FILE"):
        assert any(example[name].startswith(folder) for folder in writable), name


def test_the_production_example_is_refused_until_its_placeholders_are_replaced():

    example = env_file(DEPLOY / "logidoc.production.env.example")
    errors = environment.validate(example)["errors"]

    assert errors and all("placeholder" in error for error in errors)

    finished = {name: value.replace("logidoc.example.com", "logidoc.acme-freight.test") for name, value in example.items()}
    assert environment.validate(finished)["errors"] == []


def test_every_setting_the_application_reads_is_documented():

    read = set()

    for path in [PROJECT_ROOT / "config.py", *(PROJECT_ROOT / "src").rglob("*.py"), *(PROJECT_ROOT / "scripts").glob("*.py")]:
        read |= set(re.findall(r"\b(LOGIDOC_[A-Z0-9_]+)\b", path.read_text(encoding="utf-8")))

    documented = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    missing = sorted(name for name in read if name not in documented and name != "LOGIDOC_")

    assert missing == []


def test_no_certificate_key_or_secret_value_is_shipped():

    files = [*DEPLOY.rglob("*"), PROJECT_ROOT / ".env.example"]

    for path in files:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "PRIVATE KEY" not in text and "BEGIN CERTIFICATE" not in text, path
            assert not re.search(r"(?im)^\s*LOGIDOC_ALERT_WEBHOOK_URL=https?://(?!<)", text), path

    assert not any(path.suffix in (".pem", ".key", ".crt") for path in DEPLOY.rglob("*"))
