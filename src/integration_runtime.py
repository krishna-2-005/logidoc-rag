"""
The server's integration runtime: the webhook service, the TMS/ERP
integration service and the scheduler with its jobs, wired to one ApiState
(ApiState.integrations builds it on first use).

Scheduled jobs (intervals are the defaults; an administrator can change them)

    audit_feed.poll            60 s   the audit-run feed -> freight_audit events
    webhooks.collect_activity  30 s   exception reviews and the carrier registry
                                      -> exception and carrier events
    webhooks.deliver_pending   15 s   first attempts of new deliveries
    webhooks.retry_failed      60 s   interrupted deliveries back in the queue,
                                      then the retries that are due
    integrations.sync          60 s   every integration whose own sync interval
                                      has passed
    jobs.detect_overdue       300 s   late jobs, late syncs, deliveries waiting
                                      too long -> alerts
    integrations.health       300 s   failing webhook endpoints, integration
                                      connection tests -> alerts

The scheduler starts with the server's background jobs (src/api/serve.py;
never in tests or LOGIDOC_ENV=test) and does nothing until an administrator
configures a webhook endpoint or an integration.
"""

import logging
from datetime import timedelta
from pathlib import Path

import alerts as alerts_module
import environment
import integration_framework
import scheduler as scheduler_module
import webhooks
from integration_store import utc_now

log = logging.getLogger("logidoc.integrations")

STUCK_DELIVERY_SECONDS = 3600
FAILING_ENDPOINT_ATTEMPTS = 5


class IntegrationRuntime:

    def __init__(self, state, environ, metrics=None, alerts=None, clock=utc_now, transport=None, resolver=None, sleep=None,
                 backup_dir=None):

        self.state = state
        self.environ = environ
        self.metrics = metrics
        self.alerts = alerts
        self.clock = clock
        production = state.settings.environment == environment.PRODUCTION
        path = Path(state.locations.integration_path())

        self.webhook_settings = webhooks.WebhookSettings.from_env(environ, production)
        options = {}
        if transport is not None:
            options["transport"] = transport
        if resolver is not None:
            options["resolver"] = resolver
        self.webhooks = webhooks.WebhookService(path, self.webhook_settings,
                                                sources=webhooks.EventSources(state.locations, state.policy),
                                                clock=clock, metrics=metrics, **options)

        protected = (Path(state.locations.raw_dir).parent, state.locations.chroma_dir, backup_dir or state.backup_dir)
        sandbox, reason = integration_framework.sandbox_folder(environ, protected, production)
        context = integration_framework.ProviderContext(sandbox_dir=sandbox, production=production, clock=clock,
                                                        **({"sleep": sleep} if sleep is not None else {}))
        self.integrations = integration_framework.IntegrationService(
            path, context, environ, feed_reader=self.read_feed, metrics=metrics, clock=clock, unavailable_reason=reason)

        self.scheduler = scheduler_module.Scheduler(path, self.specs(), clock=clock, metrics=metrics,
                                                    on_failure=self.job_failing, on_recovery=self.jobs_recovered)

    # --------------------------------------------------------
    # Jobs
    # --------------------------------------------------------

    def read_feed(self, after, limit):

        history = webhooks.EventSources(self.state.locations).history()

        if history is None:
            return {"runs": [], "next_after": after, "has_more": False}

        return history.feed(after=after, limit=limit)

    def specs(self):

        spec = scheduler_module.JobSpec

        return [
            spec("audit_feed.poll", "Reads newly recorded audit runs from the audit-run feed and creates the freight audit "
                 "events.", 60, lambda context: self.webhooks.collect(only=["audit_runs"], budget=20)),
            spec("webhooks.collect_activity", "Reads new exception review and carrier registry events and creates the "
                 "exception and carrier events.", 30,
                 lambda context: self.webhooks.collect(only=["review_events", "carrier_events"])),
            spec("webhooks.deliver_pending", "Makes the first attempt of every new webhook delivery.", 15,
                 lambda context: self.webhooks.deliver_due(("pending",), 50, context.should_stop)),
            spec("webhooks.retry_failed", "Returns interrupted deliveries to the queue and makes the retries that are due.",
                 60, self.retry_deliveries),
            spec("integrations.sync", "Synchronises every enabled integration whose sync interval has passed.", 60,
                 lambda context: self.integrations.run_due(context.should_stop), timeout_seconds=900),
            spec("jobs.detect_overdue", "Raises an alert for late jobs, late integration syncs and deliveries waiting too "
                 "long.", 300, self.detect_overdue),
            spec("integrations.health", "Checks failing webhook endpoints and tests every enabled integration's "
                 "connection.", 300, self.check_health),
        ]

    def retry_deliveries(self, context):

        recovered = self.webhooks.recover()
        result = self.webhooks.deliver_due(("retrying",), 50, context.should_stop)

        return {"recovered": recovered, **(result if isinstance(result, dict) else {})}

    def detect_overdue(self, context):

        jobs = [job["name"] for job in self.scheduler.overdue()]
        syncs = [integration["name"] for integration in self.integrations.overdue()]
        stuck = self.stuck_deliveries()

        if self.metrics is not None:
            self.metrics.set("logidoc_jobs_delayed", len(jobs))
            self.metrics.set("logidoc_integrations_overdue", len(syncs))
            self.metrics.set("logidoc_webhook_deliveries_stuck", stuck)

        self.alert("integration_jobs_overdue", bool(jobs or syncs or stuck), alerts_module.WARNING,
                   f"Late: {len(jobs)} job(s), {len(syncs)} integration sync(s); {stuck} webhook deliveries waiting over an hour.")

        return {"overdue_jobs": jobs, "overdue_syncs": syncs, "stuck_deliveries": stuck}

    def stuck_deliveries(self):

        store = self.webhooks.store()

        if store is None:
            return 0

        cutoff = (self.clock() - timedelta(seconds=STUCK_DELIVERY_SECONDS)).isoformat(timespec="seconds")

        with store.read() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM webhook_deliveries d JOIN webhook_endpoints w ON w.id = d.endpoint_id "
                "WHERE d.status IN ('pending', 'retrying') AND d.created_at < ? AND d.test = 0 AND w.enabled = 1 "
                "AND w.archived_at IS NULL AND d.attempts = 0", (cutoff,)).fetchone()[0]

    def check_health(self, context):

        endpoints = self.webhooks.health()
        failing_endpoints = [item["name"] for item in endpoints
                             if item["enabled"] and item["consecutive_failures"] >= FAILING_ENDPOINT_ATTEMPTS]
        connections = self.integrations.health() if not context.should_stop() else []
        failing_connections = [item["integration_id"] for item in connections if not item["ok"]]

        if self.metrics is not None:
            self.metrics.set("logidoc_webhook_endpoints_failing", len(failing_endpoints))
            self.metrics.set("logidoc_integrations_failing", len(failing_connections))

        self.alert("webhook_endpoint_failing", bool(failing_endpoints), alerts_module.WARNING,
                   f"{len(failing_endpoints)} webhook endpoint(s) failed their last {FAILING_ENDPOINT_ATTEMPTS} attempts or more.")
        self.alert("integration_connection_failing", bool(failing_connections), alerts_module.WARNING,
                   f"{len(failing_connections)} integration(s) failed their connection test.")

        return {"failing_endpoints": len(failing_endpoints), "failing_integrations": len(failing_connections)}

    def alert(self, key, firing, severity, message):

        if self.alerts is None:
            return

        if firing:
            self.alerts.fire(key, severity, message)
        else:
            self.alerts.resolve(key)

    def job_failing(self, name, failures, category):

        self.alert(f"job_failing:{name}", True, alerts_module.CRITICAL,
                   f"The scheduled job {name} failed {failures} times in a row ({category}).")

    def jobs_recovered(self, names):

        if self.metrics is not None:
            for name in names:
                self.metrics.inc("logidoc_jobs_interrupted_total", {"job": name})

    # --------------------------------------------------------
    # Life
    # --------------------------------------------------------

    def start(self):

        return self.scheduler.start()

    def stop(self):

        self.scheduler.stop()

    def status(self):

        status = self.scheduler.status()
        status["webhook_delivery"] = {"enabled": self.webhook_settings.delivery_enabled,
                                      "problems": self.webhook_settings.problems()}

        return status
