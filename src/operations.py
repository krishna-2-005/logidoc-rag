"""
The server's operations: metrics, alerts, the backup runner, the monitor
and the schedule, wired to one ApiState (docs/AUTOMATED_BACKUP_OPERATIONS.md,
docs/OBSERVABILITY_AND_MONITORING.md).

    Operations.tick()      runs a scheduled backup when one is due, then the
                           monitor when its interval has passed
    Operations.start()     a daemon thread calling tick() every 30 s, started
                           by the server (src/api/serve.py) and never by the
                           tests or LOGIDOC_ENV=test
    start_backup()         a backup now, in its own thread (POST
                           /api/admin/backups); refuses while one runs

Backups made here hold the ingestion lock while documents, databases and the
index are copied, so no upload or document change can interleave: the copy
of the vector index is consistent with the documents.
"""

import logging
import shutil
import threading
import time
from pathlib import Path

import alerts as alerts_module
import backup
import backup_automation
import metrics as metrics_module
import readiness

log = logging.getLogger("logidoc.operations")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TICK_SECONDS = 30


def ingestion_quiesce():
    """The ingestion lock: no upload, replacement or removal runs while it is held."""

    import ingestion_manager

    return ingestion_manager.INGESTION_LOCK


class Operations:

    def __init__(self, state, backup_settings, alert_settings, *, registry=None, configuration=None,
                 monitor_interval=300, clock=time.monotonic, list_models=None, disk_usage=shutil.disk_usage):

        self.state = state
        self.backup_settings = backup_settings
        self.registry = registry or metrics_module.REGISTRY
        self.alerts = alerts_module.AlertManager.from_settings(alert_settings, metrics=self.registry)
        self.monitor_interval = monitor_interval
        self.clock = clock
        self.list_models = list_models
        self.disk_usage = disk_usage
        self.last_monitor = None
        self.thread = None
        self.stopping = threading.Event()

        self.runner = backup_automation.BackupRunner(
            backup.Sources.from_locations(state.locations, state.user_db), backup_settings,
            quiesce=ingestion_quiesce, include_index=True, configuration=configuration,
            application=backup.application_version(PROJECT_ROOT), on_failure=self.backup_failed,
            on_success=self.backup_succeeded, metrics=self.registry, disk_usage=disk_usage,
            on_verification=self.backups_verified,
        )

    # --------------------------------------------------------
    # Backups
    # --------------------------------------------------------

    def backup_failed(self, category, message):

        self.alerts.fire("backup_failed", alerts_module.CRITICAL, f"The backup failed ({category}): {message}")

    def backup_succeeded(self, manifest):

        self.alerts.resolve("backup_failed")
        log.info("Backup %s completed (%s files).", manifest["backup_id"], manifest["totals"]["files"],
                 extra={"operation": "backup", "outcome": "success", "backup_id": manifest["backup_id"]})

    def backups_verified(self, results):
        """A verification or drill that failed raises the critical alert; a clean round resolves it."""

        failed = [result for result in results if not result["ok"]]

        if failed:
            names = ", ".join(f"{result['backup_id']} ({result['kind'].replace('_', ' ')}: "
                              f"{', '.join(result.get('categories') or ['failed'])})" for result in failed[:3])
            self.alerts.fire("backup_verification_failed", alerts_module.CRITICAL,
                             f"{len(failed)} backup check(s) failed: {names}.")
        elif results:
            self.alerts.resolve("backup_verification_failed")

    def start_verification(self, backup_id, drill=False):
        """A check of one backup now, in its own thread; raises backup.BackupBusy or backup.RestoreRefused at once."""

        if self.runner.running:
            raise backup.BackupBusy("A backup or a verification is already running.")

        backup.find_backup(self.backup_settings.root, backup_id)

        def work():
            try:
                self.runner.verify(backup_id, drill=drill, origin=backup.MANUAL)
            except backup.BackupBusy:
                pass
            except Exception:
                log.exception("The backup verification could not run.")

        thread = threading.Thread(target=work, name="logidoc-backup-verification", daemon=True)
        thread.start()

        return thread

    def start_backup(self, label=None):
        """A manual backup in its own thread; raises backup.BackupBusy while one runs."""

        if self.runner.running:
            raise backup.BackupBusy("A backup is already running.")

        def work():
            try:
                self.runner.run(backup.MANUAL, label)
            except backup.BackupBusy:
                pass  # another one started first: the runner never overlaps
            except Exception:
                log.exception("The manual backup could not run.")

        thread = threading.Thread(target=work, name="logidoc-manual-backup", daemon=True)
        thread.start()

        return thread

    # --------------------------------------------------------
    # Monitor
    # --------------------------------------------------------

    def disk_folders(self):

        return {"data": Path(self.state.locations.raw_dir).parent, "index": Path(self.state.locations.chroma_dir),
                "backups": Path(self.backup_settings.root)}

    def monitor_once(self):
        """Measures disk space, the model service, the recent requests and the backups, and applies the alert rules."""

        free = {role: self.disk_usage(readiness.nearest_existing(folder)).free / 1048576
                for role, folder in self.disk_folders().items()}

        for role, megabytes in free.items():
            self.registry.set("logidoc_disk_free_bytes", megabytes * 1048576, {"volume": role})

        model = readiness.safely(readiness.check_model_service, self.list_models)
        self.registry.set("logidoc_ollama_available", 1 if model.get("ok") else 0)

        self.alerts.evaluate(
            disk_free_mb=free,
            window=self.registry.window_stats(self.alerts.settings.window_seconds),
            ollama_ok=bool(model.get("ok")),
            backup_status=self.runner.status(),
            schedule_enabled=self.backup_settings.schedule.enabled,
        )

    def tick(self):

        try:
            self.runner.run_if_due()
        except Exception:
            log.exception("The scheduled backup could not run.")

        try:
            self.runner.verify_if_due()
        except Exception:
            log.exception("The scheduled backup verification could not run.")

        now = self.clock()

        if self.last_monitor is None or now - self.last_monitor >= self.monitor_interval:
            self.last_monitor = now
            try:
                self.monitor_once()
            except Exception:
                log.exception("The monitor could not complete its checks.")

    def start(self, tick_seconds=TICK_SECONDS):

        if self.thread is not None:
            return

        self.stopping.clear()

        def loop():
            while not self.stopping.is_set():
                self.tick()
                self.stopping.wait(tick_seconds)

        self.thread = threading.Thread(target=loop, name="logidoc-operations", daemon=True)
        self.thread.start()
        log.info("Background jobs started: backups %s, monitor every %s s.", self.backup_settings.schedule.describe(),
                 self.monitor_interval)

    def stop(self):

        self.stopping.set()

        if self.thread is not None:
            self.thread.join(timeout=10)
            self.thread = None
