"""
Automated backups on top of src/backup.py: when a backup is due, one runner
that never overlaps, a status record, failure reporting and retention.

    schedule     LOGIDOC_BACKUP_SCHEDULE: off | daily@HH:MM (UTC) |
                 every:<N>h. A failed scheduled backup is tried again after
                 LOGIDOC_BACKUP_RETRY_MINUTES (default 60), not in a loop
    status       <backups>/backup-status.json, written atomically after every
                 attempt: the last attempt, the last success and the last
                 failure, and how many failed in a row
    retention    only scheduled backups are ever removed, and only after a
                 successful backup: the newest one of each of the last
                 LOGIDOC_BACKUP_KEEP_DAILY days, LOGIDOC_BACKUP_KEEP_WEEKLY
                 ISO weeks and LOGIDOC_BACKUP_KEEP_MONTHLY months are kept
                 (default 7, 4, 6), and always the newest usable backup.
                 Manual backups, incomplete ones, anything that is not a
                 LogiDoc-RAG backup folder and anything outside the backup
                 folder are never removed. Nothing is ever removed to make
                 room: a backup without enough space fails and alerts
    overlap      one run at a time in this process (a lock taken without
                 waiting), one per backup folder across processes
                 (backup.create_backup's file lock)
"""

import json
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import backup
from redaction import redact

STATUS_NAME = "backup-status.json"

SCHEDULE_PATTERN = re.compile(r"^(?:(off)|daily@([01]\d|2[0-3]):([0-5]\d)|every:([1-9]\d{0,3})h)$")


# ============================================================
# SETTINGS
# ============================================================

@dataclass(frozen=True)
class Schedule:
    kind: str                       # off | daily | interval
    at: time | None = None          # daily, UTC
    every: timedelta | None = None  # interval

    @property
    def enabled(self):
        return self.kind != "off"

    def describe(self):
        if self.kind == "daily":
            return f"daily at {self.at.strftime('%H:%M')} UTC"
        if self.kind == "interval":
            return f"every {int(self.every.total_seconds() // 3600)} h"
        return "off"


def parse_schedule(text):
    """off | daily@HH:MM | every:<N>h; raises ValueError for anything else."""

    value = (text or "off").strip().lower()
    match = SCHEDULE_PATTERN.match(value)

    if not match:
        raise ValueError("LOGIDOC_BACKUP_SCHEDULE must be off, daily@HH:MM (UTC) or every:<hours>h, e.g. daily@02:30.")

    if match.group(1):
        return Schedule("off")

    if match.group(2):
        return Schedule("daily", at=time(int(match.group(2)), int(match.group(3))))

    return Schedule("interval", every=timedelta(hours=int(match.group(4))))


@dataclass(frozen=True)
class Retention:
    daily: int = 7
    weekly: int = 4
    monthly: int = 6


def positive_int(environ, name, default, low=0, high=10_000):

    raw = (environ.get(name) or "").strip()

    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number.") from None

    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}.")

    return value


@dataclass(frozen=True)
class BackupSettings:
    root: Path
    schedule: Schedule
    retention: Retention
    min_free_mb: int = 1024
    retry_minutes: int = 60

    @staticmethod
    def from_env(environ, default_root):
        """Raises ValueError for a malformed value (environment.validate reports it at startup)."""

        root = (environ.get("LOGIDOC_BACKUP_DIR") or "").strip()

        return BackupSettings(
            root=Path(root) if root else Path(default_root),
            schedule=parse_schedule(environ.get("LOGIDOC_BACKUP_SCHEDULE")),
            retention=Retention(
                positive_int(environ, "LOGIDOC_BACKUP_KEEP_DAILY", 7),
                positive_int(environ, "LOGIDOC_BACKUP_KEEP_WEEKLY", 4),
                positive_int(environ, "LOGIDOC_BACKUP_KEEP_MONTHLY", 6),
            ),
            min_free_mb=positive_int(environ, "LOGIDOC_BACKUP_MIN_FREE_MB", 1024, 0, 1_000_000),
            retry_minutes=positive_int(environ, "LOGIDOC_BACKUP_RETRY_MINUTES", 60, 1, 10_080),
        )


# ============================================================
# WHEN A BACKUP IS DUE
# ============================================================

def latest_slot(schedule, now):
    """The most recent moment a daily backup was scheduled for, at or before now."""

    today = datetime.combine(now.astimezone(timezone.utc).date(), schedule.at, tzinfo=timezone.utc)

    return today if now >= today else today - timedelta(days=1)


def is_due(schedule, now, status):
    """
    Whether a scheduled backup should start now, from the status record
    (last success and last attempt). A failure is tried again only after
    retry_minutes (status["retry_minutes"], default 60).
    """

    if not schedule.enabled:
        return False

    success = moment((status or {}).get("last_success"))
    attempt = moment((status or {}).get("last_attempt"))
    failed = attempt is not None and (status.get("last_attempt") or {}).get("outcome") != "success"
    retry = timedelta(minutes=(status or {}).get("retry_minutes", 60))

    if failed and now - attempt < retry:
        return False

    if schedule.kind == "interval":
        return success is None or now - success >= schedule.every

    slot = latest_slot(schedule, now)

    return success is None or success < slot


def moment(entry):

    if not entry or not entry.get("at"):
        return None

    try:
        return backup.parse_iso(entry["at"])
    except ValueError:
        return None


# ============================================================
# STATUS
# ============================================================

def read_status(root):

    try:
        return json.loads((Path(root) / STATUS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record_attempt(root, outcome, at, origin, backup_id=None, category=None, message=None, retry_minutes=60):
    """Updates backup-status.json after an attempt and returns the new status."""

    status = read_status(root)
    entry = {"at": backup.iso(at), "outcome": outcome, "origin": origin, "backup_id": backup_id,
             "category": category, "message": redact(message, limit=300) if message else None}

    status["last_attempt"] = entry
    status["retry_minutes"] = retry_minutes

    if outcome == "success":
        status["last_success"] = entry
        status["consecutive_failures"] = 0
    else:
        status["last_failure"] = entry
        status["consecutive_failures"] = int(status.get("consecutive_failures") or 0) + 1

    Path(root).mkdir(parents=True, exist_ok=True)
    backup.write_atomic(Path(root) / STATUS_NAME, json.dumps(status, indent=2, sort_keys=True) + "\n")

    return status


# ============================================================
# RETENTION
# ============================================================

def retention_plan(backups, policy, now=None):
    """
    {"keep": [ids], "remove": [ids], "protected": [ids]} for the listed
    backups (backup.list_backups). Only usable scheduled backups can be
    removed; the newest usable backup of any origin is always kept.
    """

    usable = [item for item in backups if item["usable"]]
    scheduled = sorted((item for item in usable if item["origin"] == backup.SCHEDULED),
                       key=lambda item: item["created_at"], reverse=True)
    protected = [item["backup_id"] for item in backups if not (item["usable"] and item["origin"] == backup.SCHEDULED)]

    keep = set()

    if usable:
        keep.add(max(usable, key=lambda item: item["created_at"])["backup_id"])

    for limit, bucket in ((policy.daily, lambda d: d.date()),
                          (policy.weekly, lambda d: tuple(d.isocalendar())[:2]),
                          (policy.monthly, lambda d: (d.year, d.month))):
        seen = []
        for item in scheduled:
            key = bucket(backup.parse_iso(item["created_at"]))
            if key not in seen:
                seen.append(key)
                if len(seen) > limit:
                    break
                keep.add(item["backup_id"])

    remove = [item["backup_id"] for item in scheduled if item["backup_id"] not in keep]

    return {"keep": sorted(keep), "remove": remove, "protected": sorted(protected)}


def apply_retention(root, policy, dry_run=False, now=None):
    """
    Removes the backups the plan names, after checking each again: a
    complete scheduled backup folder directly in root, not a link, with a
    valid manifest. Returns the plan with "removed" and "skipped".
    """

    root = Path(root)
    plan = retention_plan(backup.list_backups(root), policy, now)
    plan["removed"], plan["skipped"] = [], []

    if dry_run:
        return plan

    for backup_id in plan["remove"]:

        folder = root / backup_id
        manifest, problems = backup.load_manifest(folder)

        if (folder.parent.resolve() != root.resolve() or backup.is_link(folder) or problems or not manifest
                or manifest.get("origin") != backup.SCHEDULED or manifest.get("status") != backup.COMPLETE
                or not backup.ID_PATTERN.match(folder.name)):
            plan["skipped"].append(backup_id)
            continue

        shutil.rmtree(folder)
        plan["removed"].append(backup_id)

    return plan


# ============================================================
# THE RUNNER
# ============================================================

class BackupRunner:
    """
    Runs backups for the server and the command line: never two at once in
    this process, the status recorded after each, retention after a success,
    failures reported to on_failure(category, message) (the alerts) and to
    the metrics.

    quiesce: the context manager that stops writes to the documents and the
    index while they are copied (the server passes its ingestion lock).
    include_index / index_skip_reason: see backup.create_backup.
    """

    def __init__(self, sources, settings, *, quiesce=None, include_index=True, index_skip_reason=None,
                 configuration=None, application=None, on_failure=None, on_success=None, metrics=None,
                 clock=None, disk_usage=shutil.disk_usage):

        self.sources = sources
        self.settings = settings
        self.quiesce = quiesce
        self.include_index = include_index
        self.index_skip_reason = index_skip_reason
        self.configuration = configuration
        self.application = application
        self.on_failure = on_failure
        self.on_success = on_success
        self.metrics = metrics
        self.clock = clock or backup.utc_now
        self.disk_usage = disk_usage
        self.lock = threading.Lock()

    @property
    def running(self):

        return self.lock.locked()

    def status(self):

        return read_status(self.settings.root)

    def due(self, now=None):

        return is_due(self.settings.schedule, now or self.clock(), self.status())

    def run(self, origin=backup.MANUAL, label=None):
        """
        One backup. Returns {"outcome": "success", "backup_id", "retention"}
        or {"outcome": "failed", "category", "message"}; raises
        backup.BackupBusy when one is already running here.
        """

        if not self.lock.acquire(blocking=False):
            raise backup.BackupBusy("A backup is already running.")

        started = self.clock()

        try:

            try:
                manifest, _ = backup.create_backup(
                    self.sources, self.settings.root, origin=origin, label=label, quiesce=self.quiesce,
                    include_index=self.include_index, index_skip_reason=self.index_skip_reason,
                    configuration=self.configuration, application=self.application,
                    min_free_bytes=self.settings.min_free_mb * 1048576, disk_usage=self.disk_usage, now=started,
                )
            except Exception as error:

                category, message = backup.category_of(error), backup.safe_message(error)
                record_attempt(self.settings.root, "failed", started, origin, category=category, message=message,
                               retry_minutes=self.settings.retry_minutes)
                self.count("failed", started)

                if self.on_failure:
                    self.on_failure(category, message)

                return {"outcome": "failed", "category": category, "message": message}

            record_attempt(self.settings.root, "success", started, origin, backup_id=manifest["backup_id"],
                           retry_minutes=self.settings.retry_minutes)
            self.count("success", started)

            try:
                retention = apply_retention(self.settings.root, self.settings.retention)
            except Exception as error:
                # The backup itself succeeded; a cleanup problem is reported, never fatal
                retention = {"error": backup.safe_message(error)}
                if self.on_failure:
                    self.on_failure("retention_failed", retention["error"])

            if self.on_success:
                self.on_success(manifest)

            return {"outcome": "success", "backup_id": manifest["backup_id"],
                    "index_included": manifest["components"]["vector_index"].get("included"),
                    "bytes": manifest["totals"]["bytes"], "retention": retention}

        finally:
            self.lock.release()

    def count(self, outcome, started):

        if self.metrics is None:
            return

        self.metrics.inc("logidoc_backup_runs_total", {"outcome": outcome})
        self.metrics.observe("logidoc_backup_duration_seconds", (self.clock() - started).total_seconds())

        if outcome == "success":
            self.metrics.set("logidoc_backup_last_success_timestamp_seconds", started.timestamp())

    def run_if_due(self, now=None):

        if self.running or not self.due(now):
            return None

        try:
            return self.run(backup.SCHEDULED)
        except backup.BackupBusy:
            return None


def recovery_configuration(config, environ):
    """The settings a restored installation needs to give the same results (no secret, no path)."""

    return {
        "auth_mode": (environ.get("LOGIDOC_AUTH_MODE") or "local").strip().lower(),
        "require_approval": bool(getattr(config, "REQUIRE_APPROVAL", False)),
        "min_note_length": getattr(config, "MIN_NOTE_LENGTH", None),
        "max_upload_mb": getattr(config, "MAX_UPLOAD_MB", None),
        "freight_tolerances": getattr(config, "FREIGHT_TOLERANCES", "") or "",
        "llm_model": getattr(config, "LLM_MODEL", None),
        "embedding_model": getattr(config, "EMBEDDING_MODEL", None),
        "collection_name": getattr(config, "COLLECTION_NAME", None),
        "chunk_size": getattr(config, "CHUNK_SIZE", None),
        "chunk_overlap": getattr(config, "CHUNK_OVERLAP", None),
        "top_k": getattr(config, "TOP_K", None),
    }
