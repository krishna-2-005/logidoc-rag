"""
Environment profiles and startup validation (docs/PRODUCTION_DEPLOYMENT.md).

LOGIDOC_ENV selects the profile:

    development  (default) everything optional; sign-in may be off (local
                 mode, loopback only); readable text logs
    test         as development; background jobs (backup schedule, monitor)
                 never start
    production   nothing falls back to a development default: sign-in
                 required, Secure cookies, HTTPS origins, trusted host names,
                 and the data, index and backup folders all set explicitly
                 to absolute paths; JSON logs. Values copied unchanged from
                 the example files (example.com, change-me, <...>) are
                 refused

validate(environ) returns {"environment", "errors", "warnings"}; the server
refuses to start on any error (src/api/serve.py, ApiSettings.check_startup).
Messages name settings, never their values: a value can be a secret.

This module reads only the environment it is given; it imports nothing of
the application, so it runs before config.py parses a value.
"""

import ipaddress
import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

DEVELOPMENT = "development"
TEST = "test"
PRODUCTION = "production"
ENVIRONMENTS = (DEVELOPMENT, TEST, PRODUCTION)

FALSE = ("0", "false", "no", "off")
TRUE = ("1", "true", "yes", "on")

# Values left from .env.example / deploy/logidoc.production.env.example
PLACEHOLDER = re.compile(r"(?i)(example\.(com|org|net)|change[-_ ]?me|<[^>]*>|replace[-_ ]?this|your[-_]domain)")

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# name: (low, high) for whole-number settings
INTEGERS = {
    "LOGIDOC_API_PORT": (1, 65535),
    "LOGIDOC_MAX_UPLOAD_MB": (1, 2048),
    "LOGIDOC_MAX_UPLOAD_FILES": (1, 200),
    "LOGIDOC_MAX_REQUEST_KB": (16, 102400),
    "LOGIDOC_MIN_NOTE_LENGTH": (1, 10000),
    "LOGIDOC_HSTS_SECONDS": (0, 63072000),
    "LOGIDOC_MONITOR_INTERVAL": (10, 86400),
    "LOGIDOC_BACKUP_KEEP_DAILY": (0, 10000),
    "LOGIDOC_BACKUP_KEEP_WEEKLY": (0, 10000),
    "LOGIDOC_BACKUP_KEEP_MONTHLY": (0, 10000),
    "LOGIDOC_BACKUP_MIN_FREE_MB": (0, 1000000),
    "LOGIDOC_BACKUP_RETRY_MINUTES": (1, 10080),
    "LOGIDOC_ALERT_DISK_WARNING_MB": (0, 10000000),
    "LOGIDOC_ALERT_DISK_CRITICAL_MB": (0, 10000000),
    "LOGIDOC_ALERT_WINDOW_SECONDS": (10, 86400),
    "LOGIDOC_ALERT_5XX_COUNT": (1, 1000000),
    "LOGIDOC_ALERT_DB_ERROR_COUNT": (1, 1000000),
    "LOGIDOC_ALERT_LATENCY_MS": (1, 3600000),
    "LOGIDOC_ALERT_MIN_REQUESTS": (1, 1000000),
    "LOGIDOC_ALERT_OLLAMA_FAILURES": (1, 1000),
    "LOGIDOC_ALERT_BACKUP_MAX_AGE_HOURS": (1, 8760),
    "LOGIDOC_ALERT_COOLDOWN_SECONDS": (0, 604800),
    "LOGIDOC_WEBHOOK_TIMEOUT_SECONDS": (1, 60),
    "LOGIDOC_WEBHOOK_MAX_ATTEMPTS": (1, 30),
    "LOGIDOC_WEBHOOK_ROTATION_GRACE_HOURS": (0, 720),
}

# Webhook signing key (src/webhooks.py): signing secrets are derived from it, never stored
WEBHOOK_KEY_MIN = 32

NUMBERS = ("LOGIDOC_OLLAMA_CONNECT_TIMEOUT", "LOGIDOC_EMBED_TIMEOUT", "LOGIDOC_LLM_TIMEOUT")

SWITCHES = ("LOGIDOC_COOKIE_SECURE", "LOGIDOC_HTTPS_REDIRECT", "LOGIDOC_REQUIRE_APPROVAL", "LOGIDOC_BACKGROUND_JOBS",
            "LOGIDOC_WEBHOOK_ALLOW_HTTP", "LOGIDOC_WEBHOOK_ALLOW_PRIVATE")

# Development switches that weaken a protection: refused in production
DEVELOPMENT_ONLY = ("LOGIDOC_WEBHOOK_ALLOW_HTTP", "LOGIDOC_WEBHOOK_ALLOW_PRIVATE")

# Must be set explicitly in production (no development default)
PRODUCTION_REQUIRED = ("LOGIDOC_AUTH_MODE", "LOGIDOC_CORS_ORIGINS", "LOGIDOC_TRUSTED_HOSTS", "LOGIDOC_DATA_DIR",
                       "LOGIDOC_CHROMA_DIR", "LOGIDOC_BACKUP_DIR", "LOGIDOC_BACKUP_SCHEDULE")

FOLDERS = ("LOGIDOC_DATA_DIR", "LOGIDOC_CHROMA_DIR", "LOGIDOC_BACKUP_DIR")

SCHEDULE = re.compile(r"^(off|daily@([01]\d|2[0-3]):[0-5]\d|every:[1-9]\d{0,3}h)$")


def value_of(environ, name):

    return (environ.get(name) or "").strip()


def environment_of(environ):
    """The profile name; raises ValueError for an unknown one."""

    name = value_of(environ, "LOGIDOC_ENV").lower() or DEVELOPMENT

    if name not in ENVIRONMENTS:
        raise ValueError(f"LOGIDOC_ENV must be one of {', '.join(ENVIRONMENTS)}.")

    return name


def is_absolute(text):

    return PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute()


def normalized(text):

    return str(PureWindowsPath(text)).rstrip("\\/").casefold() if PureWindowsPath(text).is_absolute() else \
        str(PurePosixPath(text)).rstrip("/")


def nested(one, other):

    a, b = normalized(one), normalized(other)
    separator = "\\" if PureWindowsPath(one).is_absolute() else "/"

    return a == b or a.startswith(b + separator) or b.startswith(a + separator)


def is_loopback(host):

    if host == "localhost":
        return True

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate(environ):
    """{"environment", "errors": [...], "warnings": [...]}: errors stop the server from starting."""

    errors, warnings = [], []

    try:
        environment = environment_of(environ)
    except ValueError as error:
        return {"environment": None, "errors": [str(error)], "warnings": []}

    production = environment == PRODUCTION

    # Values that must parse whatever the profile
    for name, (low, high) in INTEGERS.items():
        raw = value_of(environ, name)
        if raw:
            try:
                number = int(raw)
            except ValueError:
                errors.append(f"{name} must be a whole number.")
                continue
            if not low <= number <= high:
                errors.append(f"{name} must be between {low} and {high}.")

    for name in NUMBERS:
        raw = value_of(environ, name)
        if raw:
            try:
                if float(raw) <= 0:
                    errors.append(f"{name} must be a positive number of seconds.")
            except ValueError:
                errors.append(f"{name} must be a number of seconds.")

    for name in SWITCHES:
        raw = value_of(environ, name).lower()
        if raw and raw not in TRUE + FALSE:
            errors.append(f"{name} must be 1 or 0.")

    mode = value_of(environ, "LOGIDOC_AUTH_MODE").lower()
    if mode and mode not in ("local", "required"):
        errors.append("LOGIDOC_AUTH_MODE must be local or required.")

    schedule = value_of(environ, "LOGIDOC_BACKUP_SCHEDULE").lower()
    if schedule and not SCHEDULE.match(schedule):
        errors.append("LOGIDOC_BACKUP_SCHEDULE must be off, daily@HH:MM (UTC) or every:<hours>h.")

    log_format = value_of(environ, "LOGIDOC_LOG_FORMAT").lower()
    if log_format and log_format not in ("json", "text"):
        errors.append("LOGIDOC_LOG_FORMAT must be json or text.")

    level = value_of(environ, "LOGIDOC_LOG_LEVEL").upper()
    if level and level not in LOG_LEVELS:
        errors.append(f"LOGIDOC_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}.")

    origins = [item.strip() for item in value_of(environ, "LOGIDOC_CORS_ORIGINS").split(",") if item.strip()]
    if "*" in origins:
        errors.append("LOGIDOC_CORS_ORIGINS must list origins; '*' is not allowed with credentials.")
    for origin in origins:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/"):
            errors.append("LOGIDOC_CORS_ORIGINS must hold origins such as https://logidoc.example.org (scheme and "
                          "host, no path).")
            break

    hosts = [item.strip() for item in value_of(environ, "LOGIDOC_TRUSTED_HOSTS").split(",") if item.strip()]

    webhook = value_of(environ, "LOGIDOC_ALERT_WEBHOOK_URL")
    if webhook:
        parts = urlsplit(webhook)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            errors.append("LOGIDOC_ALERT_WEBHOOK_URL must be an http(s) URL.")
        elif parts.scheme == "http" and not is_loopback(parts.hostname or ""):
            (errors if production else warnings).append(
                "LOGIDOC_ALERT_WEBHOOK_URL must use HTTPS (alerts would cross the network unencrypted).")

    folders = {name: value_of(environ, name) for name in FOLDERS if value_of(environ, name)}

    for name in ("LOGIDOC_BACKUP_DIR",):
        for other in ("LOGIDOC_DATA_DIR", "LOGIDOC_CHROMA_DIR"):
            if name in folders and other in folders and nested(folders[name], folders[other]):
                errors.append(f"{name} must not be inside {other} (or contain it): backups are kept apart from the data.")

    key = value_of(environ, "LOGIDOC_WEBHOOK_SECRET_KEY")
    if key and len(key) < WEBHOOK_KEY_MIN:
        errors.append(f"LOGIDOC_WEBHOOK_SECRET_KEY must be at least {WEBHOOK_KEY_MIN} characters (webhook signing secrets "
                      "are derived from it).")

    sandbox = value_of(environ, "LOGIDOC_INTEGRATION_SANDBOX_DIR")
    if sandbox:
        if not is_absolute(sandbox):
            errors.append("LOGIDOC_INTEGRATION_SANDBOX_DIR must be an absolute path.")
        for other in FOLDERS:
            if other in folders and nested(sandbox, folders[other]):
                errors.append(f"LOGIDOC_INTEGRATION_SANDBOX_DIR must not be inside {other} (or contain it): sandbox "
                              "records are kept apart from the real data.")

    if production:

        for name in PRODUCTION_REQUIRED:
            if not value_of(environ, name):
                errors.append(f"{name} must be set in production (no development default is used).")

        if mode and mode != "required":
            errors.append("LOGIDOC_AUTH_MODE must be required in production.")

        if value_of(environ, "LOGIDOC_COOKIE_SECURE").lower() in FALSE and value_of(environ, "LOGIDOC_COOKIE_SECURE"):
            errors.append("LOGIDOC_COOKIE_SECURE cannot be turned off in production (sessions travel over HTTPS only).")

        if any(urlsplit(origin).scheme != "https" for origin in origins):
            errors.append("LOGIDOC_CORS_ORIGINS must list HTTPS origins only in production.")

        if "*" in hosts or any(host.startswith("*") and host.count(".") < 2 for host in hosts):
            errors.append("LOGIDOC_TRUSTED_HOSTS must name the host names served, not a catch-all wildcard.")

        for name, path in folders.items():
            if not is_absolute(path):
                errors.append(f"{name} must be an absolute path in production.")

        for name in ("LOGIDOC_DATA_DIR",):
            if name in folders and "LOGIDOC_CHROMA_DIR" in folders and normalized(folders[name]) == normalized(folders["LOGIDOC_CHROMA_DIR"]):
                errors.append("LOGIDOC_DATA_DIR and LOGIDOC_CHROMA_DIR must be different folders.")

        for name, value in environ.items():
            if name.startswith("LOGIDOC_") and value and PLACEHOLDER.search(str(value)):
                errors.append(f"{name} still holds a placeholder value from an example file.")

        if schedule == "off":
            warnings.append("LOGIDOC_BACKUP_SCHEDULE is off: no automatic backups (run scripts/manage_backups.py create).")

        host = value_of(environ, "LOGIDOC_API_HOST") or "127.0.0.1"
        if not is_loopback(host):
            warnings.append("LOGIDOC_API_HOST is not a loopback address: the API should listen behind the HTTPS "
                            "reverse proxy on the same machine.")

        if not webhook:
            warnings.append("No LOGIDOC_ALERT_WEBHOOK_URL: alerts go to the log only.")

        for name in DEVELOPMENT_ONLY:
            if value_of(environ, name).lower() in TRUE:
                errors.append(f"{name} cannot be on in production (webhooks go to public HTTPS destinations only).")

        if sandbox:
            errors.append("LOGIDOC_INTEGRATION_SANDBOX_DIR cannot be set in production: the sandbox TMS is synthetic data.")

    return {"environment": environment, "errors": errors, "warnings": warnings}
