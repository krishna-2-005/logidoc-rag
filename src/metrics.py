"""
In-process operational metrics: counters, gauges and histograms with a few
fixed labels, kept in memory since the process started (nothing is
invented: a metric that nothing recorded is simply absent).

    REGISTRY.inc("logidoc_http_requests_total", {"method": "GET", "route": "/api/carriers", "status": "2xx"})
    REGISTRY.observe("logidoc_http_request_duration_seconds", 0.042, {"route": "/api/carriers"})
    REGISTRY.set("logidoc_ollama_available", 1)

snapshot() gives JSON (GET /api/admin/metrics), prometheus() the text
exposition format (GET /api/admin/metrics?format=prometheus) for a
scraper. The recent window (recent_requests) feeds the alert rules:
failures and latency over the last minutes.

Labels are fixed words and route templates (/api/shipments/{shipment_id}),
never a raw path, a file name or a user.
"""

import math
import threading
import time
from collections import deque

# Seconds: fast API reads up to model calls (120 s by default)
BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)

HELP = {
    "logidoc_http_requests_total": ("counter", "API requests by method, route template and status class."),
    "logidoc_http_request_duration_seconds": ("histogram", "API request duration by route template."),
    "logidoc_http_errors_total": ("counter", "API error responses by sanitized error category."),
    "logidoc_ingestion_files_total": ("counter", "Uploaded files by final ingestion status."),
    "logidoc_pdf_processing_seconds": ("histogram", "Time to ingest one uploaded PDF (validation, text, index)."),
    "logidoc_llm_request_duration_seconds": ("histogram", "Ollama calls by kind (embedding, language model) and outcome."),
    "logidoc_ollama_available": ("gauge", "1 when the model service answered its last check with both models, else 0."),
    "logidoc_freight_audit_duration_seconds": ("histogram", "Time to run the freight audit over all shipments."),
    "logidoc_database_errors_total": ("counter", "Store errors (unusable, locked or failing SQLite stores)."),
    "logidoc_backup_runs_total": ("counter", "Backup attempts by outcome."),
    "logidoc_backup_duration_seconds": ("histogram", "Backup duration."),
    "logidoc_backup_last_success_timestamp_seconds": ("gauge", "Unix time of the last successful backup."),
    "logidoc_disk_free_bytes": ("gauge", "Free bytes on the volume of the data, index or backup folder."),
    "logidoc_readiness": ("gauge", "1 for the current readiness status (ready, degraded, not_ready), 0 for the others."),
    "logidoc_alerts_total": ("counter", "Alerts raised by rule and severity."),
}


def label_key(labels):

    return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))


class Histogram:

    __slots__ = ("count", "total", "minimum", "maximum", "buckets")

    def __init__(self):

        self.count = 0
        self.total = 0.0
        self.minimum = math.inf
        self.maximum = 0.0
        self.buckets = [0] * len(BUCKETS)

    def observe(self, value):

        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

        for index, bound in enumerate(BUCKETS):
            if value <= bound:
                self.buckets[index] += 1

    def as_dict(self):

        return {
            "count": self.count, "sum": round(self.total, 6),
            "min": round(self.minimum, 6) if self.count else None, "max": round(self.maximum, 6) if self.count else None,
            "mean": round(self.total / self.count, 6) if self.count else None,
        }


class Registry:

    def __init__(self, window=10_000, clock=time.time):

        self.lock = threading.Lock()
        self.clock = clock
        self.window = window
        self.reset()

    def reset(self):

        with self.lock:
            self.started_at = self.clock()
            self.counters = {}
            self.gauges = {}
            self.histograms = {}
            # (time, route, status, duration): the alert rules' recent window
            self.recent_requests = deque(maxlen=self.window)
            # (time, category)
            self.recent_errors = deque(maxlen=self.window)

    # --------------------------------------------------------
    # Recording
    # --------------------------------------------------------

    def inc(self, name, labels=None, amount=1):

        with self.lock:
            series = self.counters.setdefault(name, {})
            key = label_key(labels)
            series[key] = series.get(key, 0) + amount

    def set(self, name, value, labels=None):

        with self.lock:
            self.gauges.setdefault(name, {})[label_key(labels)] = float(value)

    def observe(self, name, value, labels=None):

        with self.lock:
            series = self.histograms.setdefault(name, {})
            key = label_key(labels)
            series.setdefault(key, Histogram()).observe(max(float(value), 0.0))

    def request(self, method, route, status, duration):
        """One API request: its count, duration, and (5xx) its error."""

        status_class = f"{int(status) // 100}xx"
        self.inc("logidoc_http_requests_total", {"method": method, "route": route, "status": status_class})
        self.observe("logidoc_http_request_duration_seconds", duration, {"route": route})

        with self.lock:
            self.recent_requests.append((self.clock(), route, int(status), float(duration)))

    def error(self, category):

        self.inc("logidoc_http_errors_total", {"category": category})

        with self.lock:
            self.recent_errors.append((self.clock(), category))

    # --------------------------------------------------------
    # Reading
    # --------------------------------------------------------

    def value(self, name, labels=None):
        """A counter's or gauge's current value (0 / None when never recorded)."""

        with self.lock:
            if name in self.counters:
                return self.counters[name].get(label_key(labels), 0)
            if name in self.gauges:
                return self.gauges[name].get(label_key(labels))

        return None

    def window_stats(self, seconds, now=None):
        """Requests, 5xx responses, database errors and the 95th percentile duration in the last `seconds`."""

        now = now if now is not None else self.clock()
        since = now - seconds

        with self.lock:
            requests = [entry for entry in self.recent_requests if entry[0] >= since]
            errors = [entry for entry in self.recent_errors if entry[0] >= since]

        durations = sorted(entry[3] for entry in requests)
        p95 = durations[min(len(durations) - 1, math.ceil(0.95 * len(durations)) - 1)] if durations else None

        return {
            "requests": len(requests),
            "server_errors": sum(1 for entry in requests if entry[2] >= 500),
            "database_errors": sum(1 for entry in errors if entry[1] == "database"),
            "p95_seconds": p95,
        }

    def snapshot(self):

        with self.lock:
            return {
                "started_at": self.started_at,
                "uptime_seconds": round(self.clock() - self.started_at, 3),
                "counters": {name: [{"labels": dict(key), "value": value} for key, value in sorted(series.items())]
                             for name, series in sorted(self.counters.items())},
                "gauges": {name: [{"labels": dict(key), "value": value} for key, value in sorted(series.items())]
                           for name, series in sorted(self.gauges.items())},
                "histograms": {name: [{"labels": dict(key), **histogram.as_dict()}
                                      for key, histogram in sorted(series.items())]
                               for name, series in sorted(self.histograms.items())},
            }

    def prometheus(self):
        """The Prometheus text exposition format (version 0.0.4)."""

        def escape(value):
            return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

        def labels_text(key, extra=()):
            pairs = list(key) + list(extra)
            if not pairs:
                return ""
            return "{" + ",".join(f'{name}="{escape(value)}"' for name, value in pairs) + "}"

        lines = []

        with self.lock:

            for kind, store in (("counter", self.counters), ("gauge", self.gauges)):
                for name, series in sorted(store.items()):
                    lines.append(f"# HELP {name} {HELP.get(name, ('', name))[1]}")
                    lines.append(f"# TYPE {name} {kind}")
                    for key, value in sorted(series.items()):
                        lines.append(f"{name}{labels_text(key)} {value:g}")

            for name, series in sorted(self.histograms.items()):
                lines.append(f"# HELP {name} {HELP.get(name, ('', name))[1]}")
                lines.append(f"# TYPE {name} histogram")
                for key, histogram in sorted(series.items()):
                    for bound, count in zip(BUCKETS, histogram.buckets):
                        lines.append(f"{name}_bucket{labels_text(key, [('le', f'{bound:g}')])} {count}")
                    lines.append(f"{name}_bucket{labels_text(key, [('le', '+Inf')])} {histogram.count}")
                    lines.append(f"{name}_sum{labels_text(key)} {histogram.total:g}")
                    lines.append(f"{name}_count{labels_text(key)} {histogram.count}")

        return "\n".join(lines) + "\n"


REGISTRY = Registry()


class timed:
    """with timed("logidoc_freight_audit_duration_seconds"): ... records the duration, also when it raises."""

    def __init__(self, name, labels=None, registry=None):

        self.name = name
        self.labels = labels
        self.registry = registry

    def __enter__(self):

        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc):

        (self.registry or REGISTRY).observe(self.name, time.perf_counter() - self.started, self.labels)
