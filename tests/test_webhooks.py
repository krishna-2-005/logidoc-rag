"""
Webhooks: signatures and their verification, destination safety (SSRF),
HTTPS, delivery, retries with back-off, permanent failures, duplicate
prevention, only committed changes becoming events, secret handling and the
permissions at the API.

Synthetic shipments in a temporary folder; the receiver and DNS are fakes
(tests/integration_support.py) except in the one end-to-end test, which sends
real HTTP to a receiver on this machine.

Run from the project root:
    python -m pytest tests/test_webhooks.py
"""

import http.server
import json
import logging
import socket
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from integration_support import ALICE, BOB, RECEIVER, SIGNING_KEY, integration_rig, register

import url_safety  # noqa: E402
import webhooks  # noqa: E402


@pytest.fixture
def rig(tmp_path):

    return integration_rig(tmp_path)


def runtime(rig):

    return rig.state.integrations


def deliveries(rig, **filters):

    return rig.client("alice").get("/api/integrations/deliveries", params={"limit": 200, **filters}).json()["deliveries"]


# ============================================================
# SIGNATURES
# ============================================================

BODY = b'{"id":"evt_1","type":"carrier.created"}'


def signed(secret, now=1_800_000_000, event_id="evt_1", body=BODY):

    return {webhooks.TIMESTAMP_HEADER: str(now), webhooks.EVENT_HEADER: event_id,
            webhooks.SIGNATURE_HEADER: webhooks.signature_header([secret], now, event_id, body)}


def test_a_signature_verifies_with_the_secret_only():

    headers = signed("whsec_right")

    assert webhooks.verify_signature("whsec_right", headers, BODY, now=1_800_000_010) == "evt_1"

    with pytest.raises(webhooks.SignatureError, match="No signature matches"):
        webhooks.verify_signature("whsec_wrong", headers, BODY, now=1_800_000_010)


def test_a_changed_body_or_event_id_breaks_the_signature():

    headers = signed("whsec_right")

    with pytest.raises(webhooks.SignatureError):
        webhooks.verify_signature("whsec_right", headers, BODY + b" ", now=1_800_000_010)

    with pytest.raises(webhooks.SignatureError):
        webhooks.verify_signature("whsec_right", {**headers, webhooks.EVENT_HEADER: "evt_2"}, BODY, now=1_800_000_010)


def test_an_old_or_future_timestamp_is_refused():

    headers = signed("whsec_right", now=1_800_000_000)

    with pytest.raises(webhooks.SignatureError, match="tolerance"):
        webhooks.verify_signature("whsec_right", headers, BODY, now=1_800_000_000 + webhooks.TOLERANCE_SECONDS + 1)

    with pytest.raises(webhooks.SignatureError, match="tolerance"):
        webhooks.verify_signature("whsec_right", headers, BODY, now=1_800_000_000 - webhooks.TOLERANCE_SECONDS - 1)


@pytest.mark.parametrize("missing", [webhooks.TIMESTAMP_HEADER, webhooks.SIGNATURE_HEADER, webhooks.EVENT_HEADER])
def test_a_missing_or_malformed_header_is_refused(missing):

    headers = signed("whsec_right")
    headers.pop(missing)

    with pytest.raises(webhooks.SignatureError):
        webhooks.verify_signature("whsec_right", headers, BODY, now=1_800_000_000)

    with pytest.raises(webhooks.SignatureError):
        webhooks.verify_signature("whsec_right", {**signed("whsec_right"), webhooks.TIMESTAMP_HEADER: "soon"}, BODY)


def test_a_replayed_event_is_recognised_by_the_receiver():

    clock = [1000.0]
    guard = webhooks.ReplayGuard(ttl_seconds=600, clock=lambda: clock[0])

    assert guard.seen("evt_1") is False
    assert guard.seen("evt_1") is True             # the same delivery again (a retry or a replay)
    assert guard.seen("evt_2") is False

    clock[0] += 601                                # beyond the signature tolerance a replay fails verification anyway
    assert guard.seen("evt_1") is False


def test_secrets_are_derived_never_stored_and_differ_per_endpoint_and_version():

    key = SIGNING_KEY.encode()
    first = webhooks.derive_secret(key, 1, 1, "salt")

    assert first.startswith("whsec_") and first == webhooks.derive_secret(key, 1, 1, "salt")
    assert len({first, webhooks.derive_secret(key, 2, 1, "salt"), webhooks.derive_secret(key, 1, 2, "salt"),
                webhooks.derive_secret(key, 1, 1, "other"), webhooks.derive_secret(b"x" * 40, 1, 1, "salt")}) == 5


# ============================================================
# DESTINATION SAFETY (SSRF)
# ============================================================

@pytest.mark.parametrize("url", [
    "https://localhost/hook", "https://api.localhost/hook", "https://printer.local/hook", "https://db.internal/hook",
    "https://127.0.0.1/hook", "https://10.1.2.3/hook", "https://172.16.0.9/hook", "https://192.168.1.10/hook",
    "https://169.254.169.254/latest/meta-data", "https://metadata.google.internal/computeMetadata",
    "https://[::1]/hook", "https://[::ffff:127.0.0.1]/hook", "https://[fd00::1]/hook", "https://[fe80::1]/hook",
    "https://100.64.0.1/hook", "https://0.0.0.0/hook", "https://224.0.0.1/hook", "https://[64:ff9b::a00:1]/hook",
    "https://2130706433/hook", "https://0x7f.0x0.0x0.0x1/hook", "https://intranet/hook",
    "https://user:password@erp.partner-example.net/hook", "https://erp.partner-example.net:22/hook",
    "https://erp.partner-example.net/hook#fragment", "ftp://erp.partner-example.net/hook", "file:///etc/passwd",
    "https://erp.partner-example.net/hook with space", "",
])
def test_unsafe_destinations_are_refused_when_saved(url):

    with pytest.raises(url_safety.UnsafeDestination):
        url_safety.check_url(url)


@pytest.mark.parametrize("url", ["https://erp.partner-example.net/hook", "https://erp.partner-example.net:8443/x?a=1",
                                 "https://93.184.216.34/hook"])
def test_public_https_destinations_are_accepted(url):

    assert url_safety.check_url(url)[0] == "https"


def test_http_is_refused_unless_a_development_switch_allows_it():

    with pytest.raises(url_safety.UnsafeDestination, match="HTTPS"):
        url_safety.check_url("http://erp.partner-example.net/hook")

    development = url_safety.Policy.from_env({"LOGIDOC_WEBHOOK_ALLOW_HTTP": "1"}, production=False)
    assert url_safety.check_url("http://erp.partner-example.net/hook", development)[0] == "http"

    # Production ignores both switches (and src/environment.py refuses to start with them)
    production = url_safety.Policy.from_env({"LOGIDOC_WEBHOOK_ALLOW_HTTP": "1", "LOGIDOC_WEBHOOK_ALLOW_PRIVATE": "1"},
                                            production=True)
    assert production == url_safety.Policy()


def test_a_name_resolving_to_a_private_address_is_refused_at_delivery_time():
    """DNS can change after the URL was saved: every address is checked again before each attempt."""

    def private(host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    with pytest.raises(url_safety.UnsafeDestination, match="private"):
        url_safety.resolve_destination("https://erp.partner-example.net/hook", resolver=private)

    destination = url_safety.resolve_destination("https://erp.partner-example.net/hook",
                                                 resolver=lambda h, p, type=None: [(2, 1, 6, "", ("93.184.216.34", p))])
    assert (destination.host, destination.address, destination.port) == ("erp.partner-example.net", "93.184.216.34", 443)


def test_a_rebinding_destination_is_never_contacted(rig):

    alice = rig.client("alice")
    endpoint = register(alice)["endpoint"]
    rig.resolver.names["erp.partner-example.net"] = "169.254.169.254"

    result = alice.post(f"/api/integrations/webhooks/{endpoint['id']}/test").json()

    assert result["status"] == "failed" and result["last_error"] == "destination_refused"
    assert rig.receiver.requests == []
    assert "link-local" in result["attempt_log"][0]["detail"]


def test_the_api_refuses_unsafe_destinations(rig):

    alice = rig.client("alice")

    for url in ("https://169.254.169.254/latest", "http://erp.partner-example.net/hook", "https://localhost:8443/x"):
        reply = alice.post("/api/integrations/webhooks", json={"name": "x", "url": url, "event_types": ["carrier.created"]})
        assert reply.status_code == 422 and reply.json()["detail"].startswith("The destination is refused")

    endpoint = register(alice)["endpoint"]
    assert alice.patch(f"/api/integrations/webhooks/{endpoint['id']}", json={"url": "https://10.0.0.1/x"}).status_code == 422


# ============================================================
# DELIVERY
# ============================================================

def test_a_committed_change_is_delivered_signed_to_the_checked_address(rig):

    alice = rig.client("alice")
    created = register(alice)
    secret = created["secret"]

    assert alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc", "scac": "SWFT",
                                                     "contact_email": "ops@swift.example"}).status_code == 201

    service = runtime(rig).webhooks
    assert service.collect(only=["carrier_events"]) == {"carrier_events": 1}
    assert service.deliver_due(("pending",)) == {"succeeded": 1, "retrying": 0, "failed": 0}

    [request] = rig.receiver.requests
    body = json.loads(request["body"])
    headers = request["headers"]

    assert request["destination"].address == "93.184.216.34" and request["destination"].host == "erp.partner-example.net"
    assert body["type"] == "carrier.created" and body["api_version"] == "1" and body["id"].startswith("evt_")
    assert body["data"]["carrier"]["scac"] == "SWFT" and body["data"]["actor"] == "alice"
    assert headers["LogiDoc-Event-Type"] == "carrier.created" and headers["LogiDoc-Delivery-Attempt"] == "1"
    assert webhooks.verify_signature(secret, headers, request["body"],
                                     now=int(headers[webhooks.TIMESTAMP_HEADER])) == body["id"]


def test_freight_audit_and_exception_events_come_from_recorded_history(rig):

    alice = rig.client("alice")
    register(alice)
    service = runtime(rig).webhooks

    # Exceptions are recorded as open when the queue is read; a run is recorded; a review changes a status
    exceptions = alice.get("/api/exceptions").json()["exceptions"]
    assert alice.post("/api/history/runs", json={"label": "Month end"}).status_code == 201
    key = exceptions[0]["exception_key"]
    assert alice.post(f"/api/exceptions/{key}/actions", json={"action": "dispute", "note": "Raised with the carrier."}).status_code == 200

    created = service.collect()
    service.deliver_due(("pending",), limit=500)
    types = [json.loads(request["body"])["type"] for request in rig.receiver.requests]

    assert types.count("exception.created") == len(exceptions) == created["review_events"] - 1
    assert types.count("exception.status_changed") == 1
    completed = [json.loads(r["body"])["data"] for r in rig.receiver.requests if json.loads(r["body"])["type"] == "freight_audit.completed"]
    discrepancies = [r for r in rig.receiver.requests if json.loads(r["body"])["type"] == "freight_audit.discrepancy_detected"]

    assert completed and all(item["outcome"] and item["run_id"] and item["shipment_id"] for item in completed)
    assert len(discrepancies) == sum(item["outcome"] == "DISCREPANCY" for item in completed)

    changed = next(json.loads(r["body"])["data"] for r in rig.receiver.requests
                   if json.loads(r["body"])["type"] == "exception.status_changed")
    assert (changed["from_status"], changed["to_status"], changed["actor"]) == ("open", "disputed", "alice")
    assert "note" not in changed                                   # free text stays inside the application


def test_carrier_update_and_approved_link_events(rig):

    alice, dave = rig.client("alice"), rig.client("dave")
    register(alice)
    carrier = alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"}).json()
    alice.patch(f"/api/carrier-registry/{carrier['id']}", json={"scac": "SWFT"})
    request = alice.post("/api/carrier-registry/links", json={"name": "Swift Lines", "carrier_id": carrier["id"],
                                                              "reason": "Same remit-to address on every invoice."}).json()
    dave.post(f"/api/carrier-registry/links/{request['id']}/decision", json={"decision": "approve"})

    service = runtime(rig).webhooks
    service.collect(only=["carrier_events"])
    service.deliver_due(("pending",))
    bodies = {json.loads(r["body"])["type"]: json.loads(r["body"])["data"] for r in rig.receiver.requests}

    assert bodies["carrier.updated"]["changes"] == {"scac": {"before": None, "after": "SWFT"}}
    assert bodies["carrier_name_link.approved"]["approved_by"] == "dave"
    assert bodies["carrier_name_link.approved"]["document_name"] == "Swift Lines"
    # A proposal is not an approval: no event for it
    assert set(bodies) == {"carrier.created", "carrier.updated", "carrier_name_link.approved"}


def test_only_subscribed_types_reach_an_endpoint(rig):

    alice = rig.client("alice")
    register(alice, event_types=["carrier.updated"])
    alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"})

    runtime(rig).webhooks.collect(only=["carrier_events"])

    assert deliveries(rig) == []


def test_history_before_the_first_endpoint_is_not_replayed(rig):

    alice = rig.client("alice")
    alice.post("/api/carrier-registry", json={"legal_name": "Earlier Carrier Inc"})
    register(alice)
    alice.post("/api/carrier-registry", json={"legal_name": "Later Carrier Inc"})

    runtime(rig).webhooks.collect(only=["carrier_events"])
    runtime(rig).webhooks.deliver_due(("pending",))

    assert [json.loads(r["body"])["data"]["carrier"]["legal_name"] for r in rig.receiver.requests] == ["Later Carrier Inc"]


# ============================================================
# ONLY COMMITTED CHANGES, ONCE
# ============================================================

def test_a_refused_change_creates_no_event(rig):

    alice, bob = rig.client("alice"), rig.client("bob")
    register(alice)
    alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc", "scac": "SWFT"})

    assert alice.post("/api/carrier-registry", json={"legal_name": "Other Inc", "scac": "SWFT"}).status_code == 409
    assert bob.post("/api/carrier-registry", json={"legal_name": "Reviewer Inc"}).status_code == 403
    assert alice.post("/api/carrier-registry", json={"legal_name": "Bad SCAC Inc", "scac": "S1"}).status_code == 422

    assert runtime(rig).webhooks.collect(only=["carrier_events"]) == {"carrier_events": 1}


def test_an_uncommitted_change_is_not_visible_to_the_collector(rig):

    alice = rig.client("alice")
    register(alice)
    alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"})
    path = rig.locations.carrier_registry_path()

    # Another writer is in the middle of a transaction that is then rolled back
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO carrier_events (at, actor, action, carrier_id, after) VALUES ('t', 'x', 'create', 99, '{}')")

    try:
        assert runtime(rig).webhooks.collect(only=["carrier_events"]) == {"carrier_events": 1}
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert runtime(rig).webhooks.collect(only=["carrier_events"]) == {"carrier_events": 0}


def test_reading_a_log_twice_never_creates_a_second_event(rig):

    alice = rig.client("alice")
    register(alice)
    alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"})
    service = runtime(rig).webhooks

    assert service.collect(only=["carrier_events"]) == {"carrier_events": 1}

    # The cursor is moved back, as after a crash between reading and saving it: the same rows come again
    with closing(sqlite3.connect(rig.locations.integration_path())) as connection, connection:
        connection.execute("UPDATE source_cursors SET position = 0 WHERE source = 'carrier_events'")

    assert service.collect(only=["carrier_events"]) == {"carrier_events": 0}
    assert len(deliveries(rig)) == 1

    with closing(sqlite3.connect(rig.locations.integration_path())) as connection:
        assert connection.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM webhook_events")


def test_the_event_id_is_deterministic():

    assert webhooks.event_id_of("carrier_events", "carrier_event:7", "carrier.created") == \
        webhooks.event_id_of("carrier_events", "carrier_event:7", "carrier.created")
    assert webhooks.event_id_of("carrier_events", "carrier_event:7", "carrier.created") != \
        webhooks.event_id_of("carrier_events", "carrier_event:8", "carrier.created")


# ============================================================
# RETRIES AND FAILURES
# ============================================================

def one_delivery(rig, answers):

    alice = rig.client("alice")
    register(alice, event_types=["carrier.created"])
    alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"})
    service = runtime(rig).webhooks
    service.collect(only=["carrier_events"])
    rig.receiver.answers = list(answers)

    return service, deliveries(rig)[0]["id"]


def test_a_timeout_is_retried_later_with_back_off_and_jitter(rig):

    service, delivery_id = one_delivery(rig, [socket.timeout("timed out")])
    before = datetime.now(timezone.utc)

    service.deliver_due(("pending",))
    detail = service.delivery(delivery_id)
    wait = (datetime.fromisoformat(detail["next_attempt_at"]) - before).total_seconds()

    assert detail["status"] == "retrying" and detail["last_error"] == "timeout" and detail["attempts"] == 1
    assert 30 * 0.5 - 2 <= wait <= 30 + 2                                    # base 30 s, jitter 0.5 to 1
    assert detail["attempt_log"][0]["outcome"] == "retry"

    # Not due yet: nothing is sent; the delay grows with each attempt and stays within the maximum
    assert service.deliver_due(("retrying",)) == {"succeeded": 0, "retrying": 0, "failed": 0}
    service.jitter = lambda: 1.0
    assert [service.retry_delay(n) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert service.retry_delay(30) == service.settings.max_delay_seconds
    service.jitter = lambda: 0.0
    assert service.retry_delay(1) == service.settings.base_delay_seconds // 2


def test_a_delivery_gives_up_after_its_attempts(rig):

    service, delivery_id = one_delivery(rig, [503] * 10)
    service.settings.max_attempts = 3

    for _ in range(3):
        with closing(sqlite3.connect(rig.locations.integration_path())) as connection, connection:
            connection.execute("UPDATE webhook_deliveries SET next_attempt_at = '2000-01-01T00:00:00+00:00'")
        service.deliver_due(("pending", "retrying"))

    detail = service.delivery(delivery_id)

    assert detail["status"] == "failed" and detail["attempts"] == 3 and detail["last_error"] == "http_5xx"
    assert "Gave up after 3 attempts" in detail["attempt_log"][-1]["detail"]
    assert [item["outcome"] for item in detail["attempt_log"]] == ["retry", "retry", "failed"]


@pytest.mark.parametrize("status, category", [(400, "http_4xx"), (401, "http_4xx"), (410, "http_4xx"),
                                              (301, "redirect_not_followed"), (307, "redirect_not_followed")])
def test_a_permanent_answer_is_not_retried(rig, status, category):

    service, delivery_id = one_delivery(rig, [status])
    service.deliver_due(("pending",))
    detail = service.delivery(delivery_id)

    assert detail["status"] == "failed" and detail["last_error"] == category and detail["next_attempt_at"] is None
    assert len(rig.receiver.requests) == 1


def test_a_rate_limit_waits_at_least_what_the_receiver_asks(rig):

    service, delivery_id = one_delivery(rig, [(429, {"retry-after": "600"})])
    before = datetime.now(timezone.utc)
    service.deliver_due(("pending",))
    detail = service.delivery(delivery_id)

    assert detail["status"] == "retrying" and detail["last_error"] == "http_429"
    assert (datetime.fromisoformat(detail["next_attempt_at"]) - before).total_seconds() >= 598


def test_an_administrator_retries_a_failed_delivery(rig):

    service, delivery_id = one_delivery(rig, [400, 200])
    service.deliver_due(("pending",))
    alice, bob = rig.client("alice"), rig.client("bob")

    assert bob.post(f"/api/integrations/deliveries/{delivery_id}/retry").status_code == 403
    retried = alice.post(f"/api/integrations/deliveries/{delivery_id}/retry").json()

    assert retried["status"] == "succeeded" and retried["manual_retries"] == 1 and retried["last_retry_by"] == "alice"
    assert [item["trigger"] for item in retried["attempt_log"]] == ["schedule", "manual"]
    assert alice.post(f"/api/integrations/deliveries/{delivery_id}/retry").status_code == 422      # succeeded: nothing to retry


def test_an_interrupted_attempt_is_sent_again_after_a_restart(rig):

    service, delivery_id = one_delivery(rig, [])
    assert service.lease(delivery_id, "schedule") is not None

    # The process stopped during the attempt: the lease runs out
    with closing(sqlite3.connect(rig.locations.integration_path())) as connection, connection:
        connection.execute("UPDATE webhook_deliveries SET lease_until = '2000-01-01T00:00:00+00:00'")

    assert service.recover() == 1
    assert service.deliver_due(("retrying",)) == {"succeeded": 1, "retrying": 0, "failed": 0}
    detail = service.delivery(delivery_id)

    assert [item["error"] for item in detail["attempt_log"]] == ["interrupted", None]
    assert detail["status"] == "succeeded" and detail["attempts"] == 2


def test_a_disabled_endpoint_receives_nothing_new_and_an_archived_one_keeps_its_history(rig):

    alice = rig.client("alice")
    endpoint = register(alice, event_types=["carrier.created"])["endpoint"]
    service = runtime(rig).webhooks

    alice.post("/api/carrier-registry", json={"legal_name": "First Inc"})
    service.collect(only=["carrier_events"])
    assert alice.patch(f"/api/integrations/webhooks/{endpoint['id']}", json={"enabled": False}).json()["enabled"] is False

    # Waiting deliveries are held, and new events create none
    assert service.deliver_due(("pending",)) == {"succeeded": 0, "retrying": 0, "failed": 0}
    alice.post("/api/carrier-registry", json={"legal_name": "Second Inc"})
    service.collect(only=["carrier_events"])
    assert len(deliveries(rig)) == 1

    archived = alice.post(f"/api/integrations/webhooks/{endpoint['id']}/archive").json()
    assert archived["archived"] and not archived["enabled"]
    assert [d["status"] for d in deliveries(rig)] == ["cancelled"]
    assert alice.get("/api/integrations/webhooks").json()["endpoints"] == []
    assert len(alice.get("/api/integrations/webhooks", params={"include_archived": True}).json()["endpoints"]) == 1
    assert alice.patch(f"/api/integrations/webhooks/{endpoint['id']}", json={"enabled": True}).status_code == 422
    actions = [change["action"] for change in alice.get(f"/api/integrations/webhooks/{endpoint['id']}").json()["changes"]]
    assert actions == ["archive", "disable", "create"]


def test_a_test_delivery_is_attempted_once_and_counted_apart(rig):

    alice = rig.client("alice")
    endpoint = register(alice)["endpoint"]
    rig.receiver.answers = [500]

    result = alice.post(f"/api/integrations/webhooks/{endpoint['id']}/test").json()

    assert result["event_type"] == "webhook.test" and result["test"] is True
    assert result["status"] == "failed" and result["next_attempt_at"] is None           # never queued for retries
    assert runtime(rig).webhooks.deliver_due(("pending", "retrying")) == {"succeeded": 0, "retrying": 0, "failed": 0}
    assert alice.get(f"/api/integrations/webhooks/{endpoint['id']}").json()["stats"]["deliveries"] == 0


# ============================================================
# SECRETS AND PERMISSIONS
# ============================================================

def test_the_secret_is_shown_once_and_never_again(rig, caplog):

    caplog.set_level(logging.DEBUG)
    alice = rig.client("alice")
    created = register(alice, url=RECEIVER + "?token=receiver-token-123")
    secret = created["secret"]
    endpoint_id = created["endpoint"]["id"]
    alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc", "contact_email": "ops@swift.example",
                                              "contact_phone": "+1 555 010 2000"})
    runtime(rig).webhooks.collect(only=["carrier_events"])
    runtime(rig).webhooks.deliver_due(("pending",))
    delivery_id = deliveries(rig)[0]["id"]

    rotated = alice.post(f"/api/integrations/webhooks/{endpoint_id}/rotate-secret").json()
    assert rotated["secret"] != secret and rotated["endpoint"]["secret_version"] == 2 and rotated["endpoint"]["rotation_in_progress"]

    shown = json.dumps([alice.get("/api/integrations/webhooks").json(), alice.get(f"/api/integrations/webhooks/{endpoint_id}").json(),
                        alice.get(f"/api/integrations/deliveries/{delivery_id}").json(),
                        alice.get("/api/integrations/deliveries").json(), alice.get("/api/integrations/jobs").json()])

    for value in (secret, rotated["secret"], SIGNING_KEY, "receiver-token-123", "salt"):
        assert value not in shown
    assert value not in caplog.text
    assert secret not in caplog.text and rotated["secret"] not in caplog.text and SIGNING_KEY not in caplog.text

    preview = alice.get(f"/api/integrations/deliveries/{delivery_id}").json()["payload_preview"]
    assert preview["data"]["carrier"]["contact_email"] == "[redacted]" and preview["data"]["carrier"]["contact_phone"] == "[redacted]"
    assert preview["data"]["carrier"]["legal_name"] == "Swift Lines Inc"

    with closing(sqlite3.connect(rig.locations.integration_path())) as connection:
        stored = json.dumps([list(row) for row in connection.execute("SELECT * FROM webhook_endpoints")])
    assert secret not in stored and rotated["secret"] not in stored and SIGNING_KEY not in stored


def test_during_a_rotation_both_secrets_sign_then_only_the_new_one(rig):

    alice = rig.client("alice")
    created = register(alice, event_types=["carrier.created"])
    endpoint_id = created["endpoint"]["id"]
    rotated = alice.post(f"/api/integrations/webhooks/{endpoint_id}/rotate-secret").json()["secret"]
    service = runtime(rig).webhooks

    alice.post("/api/carrier-registry", json={"legal_name": "First Inc"})
    service.collect(only=["carrier_events"])
    service.deliver_due(("pending",))
    first = rig.receiver.requests[-1]
    now = int(first["headers"][webhooks.TIMESTAMP_HEADER])

    assert first["headers"][webhooks.SIGNATURE_HEADER].count("v1=") == 2
    assert webhooks.verify_signature(created["secret"], first["headers"], first["body"], now=now)
    assert webhooks.verify_signature(rotated, first["headers"], first["body"], now=now)

    # The grace period ends
    with closing(sqlite3.connect(rig.locations.integration_path())) as connection, connection:
        connection.execute("UPDATE webhook_endpoints SET previous_secret_expires_at = '2000-01-01T00:00:00+00:00'")

    alice.post("/api/carrier-registry", json={"legal_name": "Second Inc"})
    service.collect(only=["carrier_events"])
    service.deliver_due(("pending",))
    second = rig.receiver.requests[-1]
    now = int(second["headers"][webhooks.TIMESTAMP_HEADER])

    assert second["headers"][webhooks.SIGNATURE_HEADER].count("v1=") == 1
    assert webhooks.verify_signature(rotated, second["headers"], second["body"], now=now)
    with pytest.raises(webhooks.SignatureError):
        webhooks.verify_signature(created["secret"], second["headers"], second["body"], now=now)


def test_without_a_signing_key_nothing_is_registered_or_delivered(tmp_path):

    rig = integration_rig(tmp_path, signing_key=False)
    alice = rig.client("alice")
    settings = alice.get("/api/integrations/webhooks/settings").json()

    assert settings["delivery_enabled"] is False and "LOGIDOC_WEBHOOK_SECRET_KEY" in settings["problems"][0]
    reply = alice.post("/api/integrations/webhooks", json={"name": "ERP", "url": RECEIVER, "event_types": ["carrier.created"]})
    assert reply.status_code == 409 and "LOGIDOC_WEBHOOK_SECRET_KEY" in reply.json()["detail"]
    assert not rig.locations.integration_path().exists()                      # nothing was created
    assert rig.state.integrations.webhooks.deliver_due() == {"skipped": "delivery disabled: no signing key"}


def test_every_webhook_route_needs_an_administrator_and_the_client_header(rig):

    alice, bob = rig.client("alice"), rig.client("bob")
    endpoint_id = register(alice)["endpoint"]["id"]
    routes = [("get", "/api/integrations/webhooks/settings"), ("get", "/api/integrations/webhooks"),
              ("post", "/api/integrations/webhooks"), ("get", f"/api/integrations/webhooks/{endpoint_id}"),
              ("patch", f"/api/integrations/webhooks/{endpoint_id}"), ("post", f"/api/integrations/webhooks/{endpoint_id}/test"),
              ("post", f"/api/integrations/webhooks/{endpoint_id}/rotate-secret"),
              ("post", f"/api/integrations/webhooks/{endpoint_id}/archive"), ("get", "/api/integrations/deliveries"),
              ("get", "/api/integrations/deliveries/1"), ("post", "/api/integrations/deliveries/1/retry")]
    body = {"name": "x", "url": RECEIVER, "event_types": ["carrier.created"]}

    for method, url in routes:
        assert getattr(rig.client(None), method)(url, **({"json": body} if method != "get" else {})).status_code == 401, url
        assert getattr(bob, method)(url, **({"json": body} if method != "get" else {})).status_code == 403, url

    # The service checks the permission itself too
    with pytest.raises(PermissionError):
        rig.state.integrations.webhooks.create_endpoint(BOB, "x", RECEIVER, ["carrier.created"])

    bare = rig.client("alice")
    bare.headers.pop("X-LogiDoc-Client")
    assert bare.post("/api/integrations/webhooks", json=body).status_code == 403
    assert bare.post(f"/api/v1/integrations/webhooks/{endpoint_id}/archive").status_code == 403


def test_unknown_event_types_and_names_are_refused(rig):

    alice = rig.client("alice")

    assert alice.post("/api/integrations/webhooks", json={"name": "x", "url": RECEIVER, "event_types": ["shipment.deleted"]}).status_code == 422
    assert alice.post("/api/integrations/webhooks", json={"name": "x", "url": RECEIVER, "event_types": []}).status_code == 422
    assert alice.post("/api/integrations/webhooks", json={"name": " ", "url": RECEIVER, "event_types": ["carrier.created"]}).status_code == 422
    with pytest.raises(webhooks.WebhookError):
        runtime(rig).webhooks.create_endpoint(ALICE, "x", RECEIVER, ["webhook.test"])


# ============================================================
# REAL HTTP (a receiver on this machine)
# ============================================================

def test_end_to_end_over_real_http_with_the_receivers_own_check(tmp_path):
    """The pinned transport against a real HTTP server; allowed only by the development switches."""

    received = []
    secret_box = {}
    guard = webhooks.ReplayGuard()

    class Handler(http.server.BaseHTTPRequestHandler):

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            try:
                event_id = webhooks.verify_signature(secret_box["secret"], dict(self.headers), body)
                status = 409 if guard.seen(event_id) else 204
            except webhooks.SignatureError:
                status = 401
            received.append((status, json.loads(body)["type"]))
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        rig = integration_rig(tmp_path, extra={"LOGIDOC_WEBHOOK_ALLOW_HTTP": "1", "LOGIDOC_WEBHOOK_ALLOW_PRIVATE": "1"})
        rig.state.integration_options = {}                       # the real transport and resolver
        alice = rig.client("alice")
        created = register(alice, url=f"http://127.0.0.1:{server.server_port}/hook", event_types=["carrier.created"])
        secret_box["secret"] = created["secret"]

        assert alice.post(f"/api/integrations/webhooks/{created['endpoint']['id']}/test").json()["status"] == "succeeded"
        alice.post("/api/carrier-registry", json={"legal_name": "Swift Lines Inc"})
        service = rig.state.integrations.webhooks
        service.collect(only=["carrier_events"])

        assert service.deliver_due(("pending",)) == {"succeeded": 1, "retrying": 0, "failed": 0}
        assert received == [(204, "webhook.test"), (204, "carrier.created")]

        # A wrong secret on the receiver's side refuses the delivery, which is then not retried (401 is permanent)
        secret_box["secret"] = "whsec_wrong"
        alice.post("/api/carrier-registry", json={"legal_name": "Blue Ridge Haulers"})
        service.collect(only=["carrier_events"])
        assert service.deliver_due(("pending",)) == {"succeeded": 0, "retrying": 0, "failed": 1}
    finally:
        server.shutdown()
        server.server_close()


def test_payload_preview_masks_sensitive_values_recursively():

    preview = webhooks.masked({"data": {"contact_email": "a@b.example", "nested": [{"api_token": "t", "x": 1}],
                                        "contact_name": "Pat"}})

    assert preview == {"data": {"contact_email": "[redacted]", "nested": [{"api_token": "[redacted]", "x": 1}],
                                "contact_name": "Pat"}}


def test_a_display_url_masks_query_values():

    assert webhooks.display_url("https://h.example/p?token=abc&x=1") == "https://h.example/p?token=[redacted]&x=[redacted]"
    assert webhooks.display_url("https://h.example/p") == "https://h.example/p"


def test_retry_after_and_status_classification():

    assert webhooks.seconds_of("120") == 120 and webhooks.seconds_of("soon") is None and webhooks.seconds_of(None) is None
    assert webhooks.classify_status(204)[0] is None
    assert webhooks.classify_status(503)[:2] == ("http_5xx", True)
    assert webhooks.classify_status(408)[:2] == ("http_408", True)
    assert webhooks.classify_status(404)[:2] == ("http_4xx", False)
    assert webhooks.classify_error(socket.gaierror())[:2] == ("dns_error", True)
    assert webhooks.classify_error(ConnectionResetError())[:2] == ("connection_error", True)
    assert webhooks.classify_error(url_safety.UnsafeDestination("x"))[:2] == ("destination_refused", False)
    assert datetime.now(timezone.utc) - timedelta(seconds=1) < datetime.now(timezone.utc)
