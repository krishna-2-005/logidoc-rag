"""
Lightweight alerting (docs/OBSERVABILITY_AND_MONITORING.md): rules with
configurable thresholds, evaluated by the server's monitor every
LOGIDOC_MONITOR_INTERVAL seconds, and notifiers that send what fires.

Rules (key: severity, default threshold, setting)

    backup_failed       critical  every failed backup (raised by the runner)
    backup_stale        warning   no successful backup for 26 h while a schedule
                                  is on (LOGIDOC_ALERT_BACKUP_MAX_AGE_HOURS)
    disk_low            warning   under 2048 MB free (LOGIDOC_ALERT_DISK_WARNING_MB)
                        critical  under 512 MB free (LOGIDOC_ALERT_DISK_CRITICAL_MB)
                                  on the data, index or backup volume
    api_failures        critical  5 or more 5xx responses in the window
                                  (LOGIDOC_ALERT_5XX_COUNT; window
                                  LOGIDOC_ALERT_WINDOW_SECONDS, 300)
    database_errors     critical  1 or more store errors in the window
                                  (LOGIDOC_ALERT_DB_ERROR_COUNT)
    ai_service_down     warning   the model service failed 3 checks in a row
                                  (LOGIDOC_ALERT_OLLAMA_FAILURES); the audit and
                                  documents keep working, answers and uploads
                                  do not
    latency_high        warning   95th percentile over 5000 ms with at least 20
                                  requests in the window
                                  (LOGIDOC_ALERT_LATENCY_MS, LOGIDOC_ALERT_MIN_REQUESTS)

An alert fires once, then again only after LOGIDOC_ALERT_COOLDOWN_SECONDS
(3600) while it lasts; a rule that clears resolves its alert (logged).

Notifiers: the log (logger logidoc.alerts, always), and a webhook when
LOGIDOC_ALERT_WEBHOOK_URL is set (HTTPS in production): a JSON POST with
the alert, 5 s timeout, never retried in a loop, its URL never logged.
Messages hold no path, credential or document text.
"""

import json
import logging
import threading
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from redaction import redact

log = logging.getLogger("logidoc.alerts")

WARNING = "warning"
CRITICAL = "critical"


def number(environ, name, default, cast=int, low=0.0):

    raw = (environ.get(name) or "").strip()

    if not raw:
        return default

    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number.") from None

    if value < low:
        raise ValueError(f"{name} must be at least {low:g}.")

    return value


@dataclass(frozen=True)
class AlertSettings:
    disk_warning_mb: int = 2048
    disk_critical_mb: int = 512
    window_seconds: int = 300
    server_errors: int = 5
    database_errors: int = 1
    latency_ms: int = 5000
    min_requests: int = 20
    ollama_failures: int = 3
    backup_max_age_hours: int = 26
    cooldown_seconds: int = 3600
    webhook_url: str | None = field(default=None, repr=False)

    @staticmethod
    def from_env(environ):
        """Raises ValueError for a malformed value."""

        return AlertSettings(
            disk_warning_mb=number(environ, "LOGIDOC_ALERT_DISK_WARNING_MB", 2048),
            disk_critical_mb=number(environ, "LOGIDOC_ALERT_DISK_CRITICAL_MB", 512),
            window_seconds=number(environ, "LOGIDOC_ALERT_WINDOW_SECONDS", 300, low=10),
            server_errors=number(environ, "LOGIDOC_ALERT_5XX_COUNT", 5, low=1),
            database_errors=number(environ, "LOGIDOC_ALERT_DB_ERROR_COUNT", 1, low=1),
            latency_ms=number(environ, "LOGIDOC_ALERT_LATENCY_MS", 5000, low=1),
            min_requests=number(environ, "LOGIDOC_ALERT_MIN_REQUESTS", 20, low=1),
            ollama_failures=number(environ, "LOGIDOC_ALERT_OLLAMA_FAILURES", 3, low=1),
            backup_max_age_hours=number(environ, "LOGIDOC_ALERT_BACKUP_MAX_AGE_HOURS", 26, low=1),
            cooldown_seconds=number(environ, "LOGIDOC_ALERT_COOLDOWN_SECONDS", 3600),
            webhook_url=(environ.get("LOGIDOC_ALERT_WEBHOOK_URL") or "").strip() or None,
        )


def utc_now():

    return datetime.now(timezone.utc)


class LogNotifier:

    def send(self, alert):

        level = logging.CRITICAL if alert["severity"] == CRITICAL else logging.WARNING
        log.log(level, "ALERT %s: %s", alert["key"], alert["message"],
                extra={"alert": alert["key"], "severity": alert["severity"], "outcome": alert["state"]})


class WebhookNotifier:
    """POSTs {"source": "logidoc-rag", "alert": {...}} as JSON. The URL is a secret: never logged or returned."""

    def __init__(self, url, timeout=5.0, opener=None):

        self.url = url
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def send(self, alert):

        body = json.dumps({"source": "logidoc-rag", "alert": alert}).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "logidoc-rag-alerts"})

        try:
            with self.opener(request, timeout=self.timeout) as response:
                response.read(1024)
        except Exception as error:
            # The URL is left out: only the kind of failure
            log.warning("The alert webhook could not be reached (%s).", type(error).__name__)
            return False

        return True


class AlertManager:
    """
    Raises and resolves alerts by key, with a cooldown, keeping the active
    ones and the last 100 events for GET /api/admin/alerts.
    """

    def __init__(self, settings=None, notifiers=None, clock=utc_now, metrics=None):

        self.settings = settings or AlertSettings()
        self.notifiers = notifiers if notifiers is not None else [LogNotifier()]
        self.clock = clock
        self.metrics = metrics
        self.lock = threading.Lock()
        self.active = {}
        self.recent = deque(maxlen=100)
        self.ollama_failures = 0

    @staticmethod
    def from_settings(settings, metrics=None):

        notifiers = [LogNotifier()]

        if settings.webhook_url:
            notifiers.append(WebhookNotifier(settings.webhook_url))

        return AlertManager(settings, notifiers, metrics=metrics)

    def fire(self, key, severity, message):
        """Raises (or keeps) an alert; notifies when it is new, more severe, or its cooldown has passed."""

        now = self.clock()
        message = redact(message, limit=500)

        with self.lock:

            current = self.active.get(key)
            notify = (
                current is None or current["severity"] != severity
                or (now - datetime.fromisoformat(current["notified_at"])).total_seconds() >= self.settings.cooldown_seconds
            )

            alert = {
                "key": key, "severity": severity, "message": message, "state": "firing",
                "first_seen": current["first_seen"] if current else now.isoformat(),
                "last_seen": now.isoformat(),
                "notified_at": now.isoformat() if notify else current["notified_at"],
            }
            self.active[key] = alert

            if notify:
                self.recent.appendleft(dict(alert))

        if notify:
            if self.metrics is not None:
                self.metrics.inc("logidoc_alerts_total", {"rule": key, "severity": severity})
            self.notify(alert)

        return alert

    def resolve(self, key):

        with self.lock:
            alert = self.active.pop(key, None)
            if alert is None:
                return None
            alert = dict(alert, state="resolved", last_seen=self.clock().isoformat())
            self.recent.appendleft(alert)

        self.notify(alert)

        return alert

    def notify(self, alert):

        public = {name: alert[name] for name in ("key", "severity", "message", "state", "first_seen", "last_seen")}

        for notifier in self.notifiers:
            try:
                notifier.send(public)
            except Exception as error:
                log.warning("An alert notifier failed (%s).", type(error).__name__)

    def view(self):

        with self.lock:
            return {
                "active": sorted(({k: v for k, v in alert.items() if k != "notified_at"} for alert in self.active.values()),
                                 key=lambda alert: (alert["severity"] != CRITICAL, alert["key"])),
                "recent": [{k: v for k, v in alert.items() if k != "notified_at"} for alert in self.recent],
            }

    # --------------------------------------------------------
    # Rules
    # --------------------------------------------------------

    def check(self, key, firing, severity, message):

        if firing:
            return self.fire(key, severity, message)

        self.resolve(key)

        return None

    def evaluate(self, *, disk_free_mb=None, window=None, ollama_ok=None, backup_status=None, schedule_enabled=False):
        """
        Applies every rule to the measurements given (a measurement left out
        leaves its rule as it is). disk_free_mb: {volume role: MB};
        window: metrics.Registry.window_stats(); backup_status: the backup
        status record.
        """

        s = self.settings

        if disk_free_mb:
            lowest_role, lowest = min(disk_free_mb.items(), key=lambda item: item[1])
            if lowest < s.disk_critical_mb:
                self.fire("disk_low", CRITICAL, f"Only {lowest:.0f} MB free on the {lowest_role} volume "
                          f"(critical below {s.disk_critical_mb} MB). Free space before backups or uploads fail.")
            elif lowest < s.disk_warning_mb:
                self.fire("disk_low", WARNING, f"{lowest:.0f} MB free on the {lowest_role} volume "
                          f"(warning below {s.disk_warning_mb} MB).")
            else:
                self.resolve("disk_low")

        if window is not None:
            self.check("api_failures", window["server_errors"] >= s.server_errors, CRITICAL,
                       f"{window['server_errors']} server errors (5xx) in the last {s.window_seconds} s.")
            self.check("database_errors", window["database_errors"] >= s.database_errors, CRITICAL,
                       f"{window['database_errors']} database errors in the last {s.window_seconds} s; see the logs "
                       "and GET /api/ready.")
            slow = (window["requests"] >= s.min_requests and window["p95_seconds"] is not None
                    and window["p95_seconds"] * 1000 > s.latency_ms)
            self.check("latency_high", slow, WARNING,
                       f"95% of API requests took up to {(window['p95_seconds'] or 0) * 1000:.0f} ms over the last "
                       f"{s.window_seconds} s (threshold {s.latency_ms} ms).")

        if ollama_ok is not None:
            self.ollama_failures = 0 if ollama_ok else self.ollama_failures + 1
            self.check("ai_service_down", self.ollama_failures >= s.ollama_failures, WARNING,
                       f"The model service failed {self.ollama_failures} checks in a row: generated answers and "
                       "uploads are unavailable; the audit, documents and reports keep working.")

        if schedule_enabled and backup_status is not None:
            last = (backup_status.get("last_success") or {}).get("at")
            age = None
            if last:
                age = (self.clock() - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() / 3600
            self.check("backup_stale", age is None or age > s.backup_max_age_hours, WARNING,
                       "No successful backup yet." if age is None else
                       f"The last successful backup is {age:.0f} h old (limit {s.backup_max_age_hours} h).")
