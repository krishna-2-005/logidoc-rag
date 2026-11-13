"""
The TMS/ERP integration framework with the sandbox TMS: connection tests,
authentication failures, paging, incremental syncs, duplicates, changed
records, rate limits, timeouts, idempotency, the stored cursor, provenance,
manual corrections that imports never overwrite, and isolation from the real
data.

Everything runs on temporary folders: a sandbox folder apart from the
application's data folder.

Run from the project root:
    python -m pytest tests/test_tms_integration.py
"""

import json
import sqlite3
import threading
from contextlib import closing

import pytest

from integration_support import ALICE, BOB, integration_rig

import integration_framework as framework  # noqa: E402
import sandbox_tms  # noqa: E402
from integration_store import later, parse_time  # noqa: E402


@pytest.fixture
def rig(tmp_path):

    return integration_rig(tmp_path)


def service(rig):

    return rig.state.integrations.integrations


def configure(rig, **settings):

    reply = rig.client("alice").post("/api/integrations/connections", json={
        "name": "Sandbox TMS", "provider": "sandbox_tms", "settings": {"page_size": 10, **settings}})
    assert reply.status_code == 201, reply.text

    return reply.json()["id"]


def sync(rig, integration_id):
    """A sync in this thread (the API's sync now runs the same code in its own thread)."""

    integrations = service(rig)

    return integrations.execute(integration_id, integrations.claim(integration_id, "manual", "alice"))


VALID = {"carriers": 5, "shipments": 37}


# ============================================================
# CONNECTION AND AUTHENTICATION
# ============================================================

def test_the_sandbox_connects_with_its_credential(rig):

    integration_id = configure(rig)
    alice = rig.client("alice")

    result = alice.post(f"/api/integrations/connections/{integration_id}/test").json()
    integration = alice.get(f"/api/integrations/connections/{integration_id}").json()

    assert result["ok"] is True and result["category"] is None
    assert integration["connection_status"] == "connected" and integration["credential_configured"] is True
    assert integration["credential_env"] == "LOGIDOC_INTEGRATION_SANDBOX_TOKEN"
    assert "sandbox-token" not in json.dumps(integration)                     # the credential is never shown


@pytest.mark.parametrize("setup", ["wrong", "missing", "simulated"])
def test_an_authentication_failure_stops_the_sync(rig, setup):

    if setup == "wrong":
        rig.state.environ["LOGIDOC_INTEGRATION_SANDBOX_TOKEN"] = "not-the-token"
    elif setup == "missing":
        rig.state.environ.pop("LOGIDOC_INTEGRATION_SANDBOX_TOKEN")

    integration_id = configure(rig, simulate_auth_failure=setup == "simulated")
    test = rig.client("alice").post(f"/api/integrations/connections/{integration_id}/test").json()
    run = sync(rig, integration_id)
    integration = service(rig).integration(integration_id)

    assert test["ok"] is False and test["category"] == "authentication_failed"
    assert run["status"] == "failed" and run["error_category"] == "authentication_failed" and run["fetched"] == 0
    assert integration["connection_status"] == "auth_failed" and integration["consecutive_failures"] == 1
    assert integration["cursor"] == {}


# ============================================================
# PAGING, INCREMENTAL SYNC, CHANGES
# ============================================================

def test_the_first_sync_pages_through_everything_and_rejects_malformed_records(rig):

    integration_id = configure(rig, page_size=7)
    run = sync(rig, integration_id)

    assert run["status"] == "partial"                                          # some records were refused
    assert run["fetched"] == 46 and run["imported"] == 42 and run["rejected"] == 4
    assert run["pages"] == 1 + 6                                                # 6 carriers in one page, 40 shipments in six
    reasons = {item["external_id"]: item["reason"] for item in run["rejections"]}
    assert set(reasons) == {"SBX-CAR-006", "SBX-SHP-0013", "SBX-SHP-0027", "SBX-SHP-0039"}
    assert "SCAC" in reasons["SBX-CAR-006"] and "before pickup_date" in reasons["SBX-SHP-0027"]
    assert service(rig).integration(integration_id)["records"] == VALID


def test_a_second_sync_reads_only_what_changed(rig):

    integration_id = configure(rig)
    sync(rig, integration_id)

    unchanged = sync(rig, integration_id)
    assert (unchanged["status"], unchanged["fetched"], unchanged["imported"], unchanged["updated"]) == ("succeeded", 0, 0, 0)

    changed = rig.client("alice").post(f"/api/integrations/connections/{integration_id}/sandbox/changes",
                                       json={"count": 3}).json()["changed"]
    run = sync(rig, integration_id)

    assert len(changed) == 3 and (run["fetched"], run["updated"], run["imported"]) == (3, 3, 0)
    records = service(rig).records(integration_id, "shipments", search=changed[0])["records"]
    detail = service(rig).record(integration_id, records[0]["id"])
    assert [version["change"] for version in detail["versions"]] == ["updated", "created"]
    assert detail["versions"][0]["run_id"] == run["id"] and detail["last_run_id"] == run["id"]


def test_the_cursor_is_saved_and_survives_a_restart(rig):

    integration_id = configure(rig)
    run = sync(rig, integration_id)
    cursor = service(rig).integration(integration_id)["cursor"]

    # No audit run is recorded in this installation, so no audit-run position yet
    assert set(cursor) == {"carriers", "shipments"} and cursor["shipments"]["id"] == "SBX-SHP-0040"
    assert run["cursor_after"] == cursor and run["cursor_before"] == {}

    # A new process reads the stored cursor
    rig.state.integrations_cache = None
    assert sync(rig, integration_id)["fetched"] == 0


def test_rereading_a_page_changes_nothing(rig):
    """A crash after storing a page's records but before saving the cursor: the page comes again."""

    integration_id = configure(rig)
    sync(rig, integration_id)
    path = rig.sandbox / "sandbox_imports.sqlite3"

    with closing(sqlite3.connect(path)) as connection:
        versions = connection.execute("SELECT COUNT(*) FROM external_record_versions").fetchone()[0]

    with closing(sqlite3.connect(rig.locations.integration_path())) as connection, connection:
        connection.execute("UPDATE integrations SET cursor = '{}' WHERE id = ?", (integration_id,))

    run = sync(rig, integration_id)

    assert (run["imported"], run["updated"], run["unchanged"]) == (0, 0, 42)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM external_record_versions").fetchone()[0] == versions
    assert service(rig).integration(integration_id)["records"] == VALID


def test_a_duplicate_in_one_answer_is_kept_once(rig):

    integration_id = configure(rig, duplicate_records=True)
    run = sync(rig, integration_id)

    assert run["duplicates"] == run["pages"] - 1 or run["duplicates"] == run["pages"]
    assert run["duplicates"] > 0 and service(rig).integration(integration_id)["records"] == VALID


# ============================================================
# RATE LIMITS AND TIMEOUTS
# ============================================================

def test_a_rate_limit_is_retried_after_the_partners_wait(rig):

    integration_id = configure(rig, rate_limit_every=3)
    run = sync(rig, integration_id)

    assert run["retries"] >= 2 and run["imported"] == 42
    assert rig.sleeps and all(seconds == 1 for seconds in rig.sleeps)          # Retry-After: 1 s


def test_a_timeout_is_retried_and_a_lasting_one_fails_the_run_keeping_the_progress(rig):

    integration_id = configure(rig, timeout_every=4)
    assert sync(rig, integration_id)["imported"] == 42

    stuck = configure_named(rig, "Unreachable TMS", timeout_every=1)
    run = sync(rig, stuck)
    integration = service(rig).integration(stuck)

    assert run["status"] == "failed" and run["error_category"] == "timeout" and run["retries"] == framework.PAGE_RETRIES
    assert integration["connection_status"] == "error" and integration["cursor"] == {}
    assert integration["next_sync_at"] > integration["last_failure_at"]          # backs off, never hammers


def configure_named(rig, name, **settings):

    reply = rig.client("alice").post("/api/integrations/connections", json={
        "name": name, "provider": "sandbox_tms", "settings": {"page_size": 10, **settings}})
    assert reply.status_code == 201, reply.text

    return reply.json()["id"]


def test_an_unavailable_system_is_normalized():

    assert framework.normalize(TimeoutError()).category == "timeout"
    assert framework.normalize(ConnectionRefusedError()).category == "unavailable"
    assert framework.normalize(ValueError("bad json")).category == "invalid_response"
    assert framework.normalize(RuntimeError("x")).category == "integration_error"
    assert framework.normalize(framework.RateLimited("slow", 5)).retry_after == 5


# ============================================================
# PROVENANCE AND MANUAL CORRECTIONS
# ============================================================

def test_every_record_keeps_its_provenance(rig):

    integration_id = configure(rig)
    run = sync(rig, integration_id)
    record = service(rig).records(integration_id, "shipments", search="SBX-SHP-0001")["records"][0]

    assert record["integration_id"] == integration_id and record["provider"] == "sandbox_tms"
    assert record["external_id"] == "SBX-SHP-0001" and record["source_updated_at"].startswith("2026-10-01T09:01")
    assert record["first_run_id"] == record["last_run_id"] == run["id"]
    assert record["data"]["reference"] == "SBX-PRO-10001" and record["data"]["carrier_external_id"] == "SBX-CAR-001"


def test_a_manual_correction_is_never_overwritten_by_an_import(rig):

    integration_id = configure(rig)
    sync(rig, integration_id)
    alice = rig.client("alice")
    record = service(rig).records(integration_id, "shipments", search="SBX-SHP-0001")["records"][0]
    url = f"/api/integrations/connections/{integration_id}/records/{record['id']}"

    assert rig.client("bob").post(url + "/corrections", json={"field": "status", "value": "exception",
                                                               "reason": "Damaged freight, confirmed by phone."}).status_code == 403
    assert alice.post(url + "/corrections", json={"field": "external_id", "value": "X", "reason": "Not allowed field."}).status_code == 422
    assert alice.post(url + "/corrections", json={"field": "status", "value": "exception", "reason": "short"}).status_code == 422

    corrected = alice.post(url + "/corrections", json={"field": "status", "value": "exception",
                                                       "reason": "Damaged freight, confirmed by phone."}).json()
    assert corrected["data"]["status"] == "exception" and corrected["corrected_fields"] == ["status"]

    # The partner then changes the same shipment
    changed = alice.post(f"/api/integrations/connections/{integration_id}/sandbox/changes", json={"count": 1}).json()["changed"]
    assert changed == ["SBX-SHP-0001"]
    run = sync(rig, integration_id)
    detail = alice.get(url).json()

    assert run["updated"] == 1 and run["conflicts"] == 1
    assert detail["data"]["status"] == "exception"                              # the correction wins
    assert detail["received"]["status"] == "dispatched"                         # what the partner sent is kept
    assert detail["versions"][0]["kept_fields"] == ["status"]

    # Clearing the correction gives the partner's value back
    cleared = alice.post(url + "/corrections", json={"field": "status", "clear": True,
                                                     "reason": "Carrier confirmed the delivery went ahead."}).json()
    assert cleared["data"]["status"] == "dispatched" and cleared["corrected_fields"] == []
    assert [item["cleared"] for item in cleared["override_history"]] == [True, False]


# ============================================================
# AUDIT RUNS TO THE PARTNER
# ============================================================

def test_recorded_audit_runs_are_handed_over_once(rig):

    alice = rig.client("alice")
    assert alice.post("/api/history/runs", json={"label": "First"}).status_code == 201
    integration_id = configure(rig)

    sync(rig, integration_id)
    system = sandbox_tms.SandboxTMS(rig.sandbox)
    assert [run["run_id"] for run in system.received_runs()] == [1]
    assert service(rig).integration(integration_id)["cursor"]["audit_runs"] == {"after": 1}

    sync(rig, integration_id)
    assert len(system.received_runs()) == 1


# ============================================================
# ISOLATION FROM THE REAL DATA
# ============================================================

def test_sandbox_records_stay_in_the_sandbox_folder(rig):

    data = rig.locations.integration_path().parent
    before = sorted(path.name for path in data.iterdir())
    integration_id = configure(rig)
    sync(rig, integration_id)

    assert sorted(path.name for path in rig.sandbox.iterdir()) == ["sandbox_imports.sqlite3", "sandbox_tms.sqlite3"]
    assert sorted(path.name for path in data.iterdir()) == sorted(set(before) | {"integrations.sqlite3"})

    # The integration store holds the configuration and the runs, never an imported record
    with closing(sqlite3.connect(rig.locations.integration_path())) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "external_records" not in tables

    # Nothing was written into the audit, the reviews or the carrier registry
    assert not rig.locations.carrier_registry_path().exists()
    assert "SBX-" not in json.dumps(rig.client("alice").get("/api/shipments").json())


def test_the_sandbox_is_not_offered_without_its_own_folder(tmp_path):

    rig = integration_rig(tmp_path, sandbox=False)
    alice = rig.client("alice")
    [provider] = alice.get("/api/integrations/connections").json()["providers"]

    assert provider["available"] is False and "LOGIDOC_INTEGRATION_SANDBOX_DIR" in provider["unavailable_reason"]
    reply = alice.post("/api/integrations/connections", json={"name": "Sandbox", "provider": "sandbox_tms"})
    assert reply.status_code == 422 and not rig.locations.integration_path().exists()


def test_a_sandbox_folder_inside_the_data_folder_or_in_production_is_refused(tmp_path):

    data = tmp_path / "data"
    assert framework.sandbox_folder({"LOGIDOC_INTEGRATION_SANDBOX_DIR": str(data / "sandbox")}, [data])[0] is None
    assert framework.sandbox_folder({"LOGIDOC_INTEGRATION_SANDBOX_DIR": str(tmp_path)}, [data])[0] is None   # contains it
    assert framework.sandbox_folder({"LOGIDOC_INTEGRATION_SANDBOX_DIR": "relative/sandbox"}, [data])[0] is None
    assert framework.sandbox_folder({"LOGIDOC_INTEGRATION_SANDBOX_DIR": str(tmp_path / "sandbox")}, [data], production=True)[0] is None
    assert framework.sandbox_folder({"LOGIDOC_INTEGRATION_SANDBOX_DIR": str(tmp_path / "sandbox")}, [data])[0] is not None

    import environment

    production = environment.validate({"LOGIDOC_ENV": "production", "LOGIDOC_INTEGRATION_SANDBOX_DIR": str(tmp_path / "s")})
    assert any("LOGIDOC_INTEGRATION_SANDBOX_DIR cannot be set in production" in error for error in production["errors"])
    nested = environment.validate({"LOGIDOC_DATA_DIR": str(data), "LOGIDOC_INTEGRATION_SANDBOX_DIR": str(data / "s")})
    assert any("must not be inside LOGIDOC_DATA_DIR" in error for error in nested["errors"])


# ============================================================
# OVERLAP, RECOVERY, CONFIGURATION, PERMISSIONS
# ============================================================

def test_one_sync_of_an_integration_at_a_time(rig):

    integration_id = configure(rig)
    service(rig).claim(integration_id, "schedule", None)

    assert rig.client("alice").post(f"/api/integrations/connections/{integration_id}/sync").status_code == 409
    with pytest.raises(framework.SyncBusy):
        service(rig).claim(integration_id, "manual", "alice")


def test_an_interrupted_sync_is_recorded_and_resumes_from_the_saved_cursor(rig):

    integration_id = configure(rig)
    run_id = service(rig).claim(integration_id, "schedule", None)

    with closing(sqlite3.connect(rig.locations.integration_path())) as connection, connection:
        connection.execute("UPDATE integrations SET lease_until = '2000-01-01T00:00:00+00:00'")

    result = service(rig).run_due()

    assert service(rig).run(run_id)["status"] == "interrupted"
    assert result["synced"] == 1 and list(result["outcomes"].values()) == ["partial"]


def test_sync_now_runs_in_the_background_and_is_recorded(rig):

    integration_id = configure(rig)
    alice = rig.client("alice")
    reply = alice.post(f"/api/integrations/connections/{integration_id}/sync")

    assert reply.status_code == 202
    for worker in service(rig).workers:
        worker.join(60)

    run = alice.get(f"/api/integrations/sync-runs/{reply.json()['run_id']}").json()
    assert run["trigger"] == "manual" and run["requested_by"] == "alice" and run["imported"] == 42
    assert alice.get(f"/api/integrations/connections/{integration_id}/runs").json()["total"] == 1

    listed = alice.get("/api/integrations/connections").json()["integrations"][0]
    assert listed["records"] == VALID and listed["last_run"]["id"] == run["id"] and listed["totals"]["imported"] == 42


def test_the_scheduled_job_syncs_only_due_and_enabled_integrations(rig):

    first, second = configure(rig), configure_named(rig, "Disabled TMS")
    rig.client("alice").patch(f"/api/integrations/connections/{second}", json={"enabled": False})

    assert service(rig).run_due()["synced"] == 1
    assert service(rig).run_due()["synced"] == 0                                 # not due again before its interval
    assert service(rig).runs(second)["total"] == 0 and service(rig).runs(first)["total"] == 1


def test_configuration_is_checked_and_recorded(rig):

    alice = rig.client("alice")
    post = lambda **body: alice.post("/api/integrations/connections", json={"name": "x", "provider": "sandbox_tms", **body})

    assert post(provider="acme_tms").status_code == 422
    assert post(settings={"page_size": 0}).status_code == 422
    assert post(settings={"api_key": "abc"}).status_code == 422                 # no secret may be a setting
    assert post(credential_env="PATH").status_code == 422                        # only LOGIDOC_INTEGRATION_<NAME>
    assert post(sync_interval_seconds=5).status_code == 422

    integration_id = configure(rig)
    assert post(name="Sandbox TMS").status_code == 422                           # the name is taken
    changed = alice.patch(f"/api/integrations/connections/{integration_id}", json={"sync_interval_seconds": 600}).json()
    assert changed["sync_interval_seconds"] == 600
    actions = [item["action"] for item in alice.get(f"/api/integrations/connections/{integration_id}").json()["changes"]]
    assert actions == ["update", "create"]


def test_every_integration_route_needs_an_administrator(rig):

    integration_id = configure(rig)
    bob = rig.client("bob")

    for method, url in (("get", "/api/integrations/connections"), ("post", "/api/integrations/connections"),
                        ("get", f"/api/integrations/connections/{integration_id}"),
                        ("patch", f"/api/integrations/connections/{integration_id}"),
                        ("post", f"/api/integrations/connections/{integration_id}/test"),
                        ("post", f"/api/integrations/connections/{integration_id}/sync"),
                        ("get", f"/api/integrations/connections/{integration_id}/runs"),
                        ("get", "/api/integrations/sync-runs/1"),
                        ("get", f"/api/integrations/connections/{integration_id}/records"),
                        ("post", f"/api/integrations/connections/{integration_id}/sandbox/changes")):
        assert getattr(bob, method)(url, **({"json": {}} if method != "get" else {})).status_code == 403, url
        assert getattr(rig.client(None), method)(url, **({"json": {}} if method != "get" else {})).status_code == 401, url

    with pytest.raises(PermissionError):
        service(rig).sync_now(BOB, integration_id)
    with pytest.raises(PermissionError):
        service(rig).create(BOB, "x", "sandbox_tms")


def test_the_contract_mapping_refuses_rather_than_guesses():

    good = {"id": "A", "reference": "R", "status": "Delivered", "pickup_date": "2026-10-01", "delivery_date": "2026-10-03",
            "pieces": 3, "weight_kg": 10.5, "currency": "USD", "freight_charge": 100, "updated_at": "2026-10-03T10:00:00Z"}

    assert framework.map_shipment(good).status == "delivered"

    for change, message in ((({"status": "lost"}), "status"), ({"pickup_date": "10/01/2026"}, "date"),
                            ({"pieces": 2.5}, "whole"), ({"freight_charge": -1}, "negative"), ({"currency": "usd"}, "currency"),
                            ({"updated_at": "2026-10-03T10:00:00"}, "time zone"), ({"reference": " "}, "missing"),
                            ({"weight_kg": "heavy"}, "number")):
        with pytest.raises(ValueError, match=message):
            framework.map_shipment({**good, **change})

    with pytest.raises(ValueError, match="MC number"):
        framework.map_carrier({"id": "C", "legal_name": "X", "mc_number": "12AB", "updated_at": "2026-10-03T10:00:00Z"})
    assert framework.map_carrier({"id": "C", "legal_name": "X", "mc_number": "MC-12", "updated_at": "2026-10-03T10:00:00Z"}).mc_number == "12"


# ============================================================
# SYNC CONCURRENCY AND RECOVERY
# ============================================================

def expire_lease(rig, integration_id):

    with closing(sqlite3.connect(rig.locations.integration_path())) as connection, connection:
        connection.execute("UPDATE integrations SET lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?", (integration_id,))


def test_a_scheduled_sync_does_not_repeat_a_sync_that_finished_meanwhile(rig):
    """run_due reads the due list, then claims: a manual sync that completes in between is not run again."""

    integration_id = configure(rig)
    integrations = service(rig)
    claim = integrations.claim
    interleaved = {"done": False}

    def manual_sync_first(identifier, trigger, requested_by):
        if trigger == "schedule" and not interleaved["done"]:
            interleaved["done"] = True
            integrations.execute(identifier, claim(identifier, "manual", "alice"))      # an administrator's sync
        return claim(identifier, trigger, requested_by)

    integrations.claim = manual_sync_first
    result = integrations.run_due()

    assert result["synced"] == 0
    assert [run["trigger"] for run in integrations.runs(integration_id)["runs"]] == ["manual"]

    with pytest.raises(framework.SyncNotDue):
        claim(integration_id, "schedule", None)


def test_two_simultaneous_claims_start_one_sync(rig):

    integration_id = configure(rig)
    integrations = service(rig)
    gate = threading.Barrier(2)
    outcomes = []

    def claim():
        gate.wait()
        try:
            outcomes.append(integrations.claim(integration_id, "manual", "alice"))
        except framework.SyncBusy:
            outcomes.append("busy")

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(outcomes, key=str).count("busy") == 1 and integrations.runs(integration_id)["total"] == 1


def test_a_late_worker_neither_records_its_outcome_nor_releases_the_new_runs_lease(rig):
    """
    A sync that outlives its lease is recovered (marked interrupted) and a new
    sync starts. When the slow worker carries on, it must not import, move the
    cursor, rewrite its run or release the new run's lease.
    """

    integration_id = configure(rig)
    integrations = service(rig)
    old_run = integrations.claim(integration_id, "manual", "alice")
    late = framework.SyncEngine(integrations, integrations.integration(integration_id), old_run, lambda: False)

    expire_lease(rig, integration_id)
    assert integrations.recover() == 1
    new_run = integrations.claim(integration_id, "schedule", None)          # recovery made it due at once

    late.run()                                                               # the slow worker carries on

    assert integrations.run(old_run)["status"] == "interrupted"
    assert integrations.run(new_run)["status"] == "running"
    current = integrations.integration(integration_id)
    assert current["running"] is True and current["cursor"] == {} and current["records"] == {}
    with pytest.raises(framework.SyncBusy):
        integrations.claim(integration_id, "manual", "alice")


def test_a_manual_sync_after_a_crash_closes_the_crashed_run(rig):
    """
    An administrator syncs after a crash, before the scheduler's recovery: the
    crashed run must not stay 'running' in the history, and its worker, if it
    is still alive, must not write.
    """

    integration_id = configure(rig)
    integrations = service(rig)
    crashed = integrations.claim(integration_id, "schedule", None)
    late = framework.SyncEngine(integrations, integrations.integration(integration_id), crashed, lambda: False)
    expire_lease(rig, integration_id)

    new_run = integrations.claim(integration_id, "manual", "alice")          # no recover() in between
    late.run()

    old = integrations.run(crashed)
    assert old["status"] == "interrupted" and old["error_category"] == "interrupted" and old["finished_at"]
    assert integrations.run(new_run)["status"] == "running"
    assert [run["status"] for run in integrations.runs(integration_id)["runs"]].count("running") == 1
    assert integrations.integration(integration_id)["records"] == {}

    finished = integrations.execute(integration_id, new_run)
    assert finished["status"] in ("succeeded", "partial")
    assert integrations.run(crashed)["status"] == "interrupted"


def test_a_late_worker_never_moves_the_cursor_back(rig):

    integration_id = configure(rig)
    integrations = service(rig)
    old_run = integrations.claim(integration_id, "manual", "alice")
    one_page = iter([False, True])                                            # stops after its first page
    late = framework.SyncEngine(integrations, integrations.integration(integration_id), old_run,
                                lambda: next(one_page, True))

    expire_lease(rig, integration_id)
    integrations.recover()
    finished = integrations.execute(integration_id, integrations.claim(integration_id, "schedule", None))
    cursor = integrations.integration(integration_id)["cursor"]
    assert finished["status"] == "partial" and cursor["shipments"]["id"] == "SBX-SHP-0040"

    late.run()

    assert integrations.integration(integration_id)["cursor"] == cursor
    assert integrations.run(old_run)["status"] == "interrupted"


def test_an_older_version_of_a_record_never_replaces_a_newer_one(tmp_path):

    store = framework.ExternalRecordStore(tmp_path / "records.sqlite3")
    newer = {"external_id": "A", "reference": "R2", "source_updated_at": "2026-10-02T00:00:00+00:00"}
    older = {"external_id": "A", "reference": "R1", "source_updated_at": "2026-10-01T00:00:00+00:00"}

    assert store.apply(1, "p", 1, "shipments", newer, "t")[0] == "created"
    assert store.apply(1, "p", 2, "shipments", older, "t")[0] == "stale"
    assert store.apply(1, "p", 3, "shipments", newer, "t")[0] == "unchanged"

    record = store.records(1)["records"][0]
    assert record["data"]["reference"] == "R2" and record["last_run_id"] == 3
    assert [version["change"] for version in store.record(record["id"])["versions"]] == ["created"]


def test_a_stale_record_is_counted_in_the_run(rig):

    integration_id = configure(rig)
    sync(rig, integration_id)
    records = service(rig).records_store(service(rig).integration(integration_id))
    newest = service(rig).records(integration_id, "shipments", search="SBX-SHP-0001")["records"][0]

    # The partner sends an older copy of a record it has already sent in a newer state
    with closing(sqlite3.connect(records.path)) as connection, connection:
        connection.execute("UPDATE external_records SET source_updated_at = '2099-01-01T00:00:00+00:00', content_hash = 'x' "
                           "WHERE id = ?", (newest["id"],))
    with closing(sqlite3.connect(rig.locations.integration_path())) as connection, connection:
        connection.execute("UPDATE integrations SET cursor = '{}' WHERE id = ?", (integration_id,))

    run = sync(rig, integration_id)

    assert run["stale"] == 1 and run["updated"] == 0
    assert service(rig).record(integration_id, newest["id"])["source_updated_at"] == "2099-01-01T00:00:00+00:00"


def test_the_lease_is_renewed_while_a_long_sync_progresses(rig):

    integration_id = configure(rig)
    integrations = service(rig)
    run_id = integrations.claim(integration_id, "manual", "alice")
    engine = framework.SyncEngine(integrations, integrations.integration(integration_id), run_id, lambda: False)

    with closing(sqlite3.connect(rig.locations.integration_path())) as connection, connection:
        connection.execute("UPDATE integrations SET lease_until = ? WHERE id = ?",
                           (later(integrations.clock(), 5), integration_id))

    engine.save_progress()

    with closing(sqlite3.connect(rig.locations.integration_path())) as connection:
        lease, owner = connection.execute("SELECT lease_until, lease_owner FROM integrations WHERE id = ?",
                                          (integration_id,)).fetchone()
    assert (parse_time(lease) - integrations.clock()).total_seconds() > framework.SYNC_LEASE_SECONDS - 60
    assert owner == framework.lease_token(run_id)


def test_after_a_crash_one_recovery_and_one_new_sync(rig):

    integration_id = configure(rig)
    integrations = service(rig)
    crashed = integrations.claim(integration_id, "schedule", None)
    expire_lease(rig, integration_id)

    first, second = integrations.run_due(), integrations.run_due()

    assert first["synced"] == 1 and second["synced"] == 0
    statuses = [run["status"] for run in reversed(integrations.runs(integration_id)["runs"])]
    assert statuses == ["interrupted", "partial"] and integrations.run(crashed)["error_category"] == "interrupted"

    with closing(sqlite3.connect(rig.locations.integration_path())) as connection:
        assert connection.execute("SELECT lease_until, lease_owner FROM integrations").fetchone() == (None, None)


def test_a_version_1_integration_store_is_upgraded_keeping_its_rows(tmp_path):

    import integration_store

    path = tmp_path / "integrations.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(integration_store.SCHEMA)
        connection.execute("INSERT INTO schema_info (version) VALUES (1)")
        connection.execute("INSERT INTO integrations (name, provider, settings, sync_interval_seconds, created_by, created_at, "
                           "updated_at) VALUES ('Old', 'sandbox_tms', '{}', 3600, 'alice', 't', 't')")

    store = integration_store.IntegrationStore(path)

    assert store.version() == integration_store.SCHEMA_VERSION == 2
    with closing(sqlite3.connect(path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(integrations)")}
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(sync_runs)")}
        assert connection.execute("SELECT name FROM integrations").fetchall() == [("Old",)]
    assert "lease_owner" in columns and "stale" in run_columns


# ============================================================
# THE HEALTH DASHBOARD (recorded runs only)
# ============================================================

def test_the_dashboard_reports_recorded_runs_records_retries_and_errors(rig):

    healthy = configure(rig, rate_limit_every=3)
    sync(rig, healthy)
    refused = configure_named(rig, "Refused TMS", simulate_auth_failure=True)
    sync(rig, refused)

    view = rig.client("alice").get("/api/integrations/monitoring/connections", params={"days": 7}).json()
    totals = view["totals"]

    assert (totals["runs"], totals["succeeded"], totals["partial"], totals["failed"]) == (2, 0, 1, 1)
    assert totals["success_rate"] == 0.0                                  # finished runs: one partial, one failed
    assert (totals["imported"], totals["rejected"]) == (42, 4) and totals["retries"] >= 2
    assert view["errors"] == [{"category": "authentication_failed", "runs": 1}]
    assert len(view["daily"]) == 7 and view["daily"][-1]["partial"] == 1 and view["daily"][-1]["imported"] == 42
    assert all(day["partial"] == day["failed"] == day["imported"] == 0 for day in view["daily"][:-1])

    named = {item["name"]: item for item in view["integrations"]}
    assert named["Sandbox TMS"]["window"]["imported"] == 42 and named["Sandbox TMS"]["records"] == VALID
    assert named["Refused TMS"]["window"]["error_categories"] == {"authentication_failed": 1}
    assert named["Refused TMS"]["connection_status"] == "auth_failed" and len(named["Refused TMS"]["recent_runs"]) == 1


def test_an_empty_dashboard_has_no_rate_and_no_invented_figures(rig):

    view = rig.client("alice").get("/api/integrations/monitoring/connections").json()

    assert view["totals"]["runs"] == 0 and view["totals"]["success_rate"] is None and view["integrations"] == []
    assert all(day["succeeded"] == day["failed"] == day["imported"] == 0 for day in view["daily"])
    assert rig.client("bob").get("/api/integrations/monitoring/connections").status_code == 403
    assert rig.client("alice").get("/api/integrations/monitoring/connections", params={"days": 91}).status_code == 422


def test_a_touch_without_a_change_is_not_a_new_version(tmp_path):

    store = framework.ExternalRecordStore(tmp_path / "records.sqlite3")
    values = {"external_id": "A", "reference": "R", "source_updated_at": "2026-10-01T00:00:00+00:00"}

    assert store.apply(1, "p", 1, "shipments", values, "t")[0] == "created"
    assert store.apply(1, "p", 2, "shipments", {**values, "source_updated_at": "2026-10-02T00:00:00+00:00"}, "t")[0] == "unchanged"
    # A real change comes with a newer change time; the same change dated before what is stored is stale
    assert store.apply(1, "p", 3, "shipments", {**values, "reference": "R2"}, "t")[0] == "stale"
    changed = {**values, "reference": "R2", "source_updated_at": "2026-10-03T00:00:00+00:00"}
    assert store.apply(1, "p", 4, "shipments", changed, "t")[0] == "updated"

    with closing(sqlite3.connect(tmp_path / "records.sqlite3")) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM external_record_versions")
