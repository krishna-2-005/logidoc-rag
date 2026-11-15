"""
Shipment risk analytics: the deterministic score, its thresholds, missing
evidence, currency separation, the versioned model, reproducible scores from
recorded history, and the API with its permissions (docs/SHIPMENT_RISK_ANALYTICS.md).

Synthetic evidence and synthetic PDFs in a temporary folder; no network, no model.

Run from the project root:
    python -m pytest tests/test_risk_analytics.py
"""

import copy
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (PROJECT_ROOT, PROJECT_ROOT / "src", PROJECT_ROOT / "tests"):

    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import environment  # noqa: E402
import risk_analytics as risk  # noqa: E402
from auth import UserStore  # noqa: E402
from authz import ADMIN  # noqa: E402
from test_api import PASSWORD, Rig  # noqa: E402
from test_freight_audit import write_contracted_shipment  # noqa: E402

KEY = "SHP1"


def exception(code, kind=risk.DISCREPANCY, severity="HIGH", status="open", amount=None, currency="USD", category="Invoice"):

    return {"key": f"{KEY}|{code}", "kind": kind, "category": category, "severity": severity, "status": status,
            "impact": {"amount": amount, "currency": currency, "basis": "test"} if amount is not None else None}


def evidence(exceptions=(), missing=(), outcome="MATCHED", statuses_known=True):

    return {"basis": "current", "reconciled": True, "exceptions": list(exceptions), "statuses_known": statuses_known,
            "missing_documents": None if missing is None else list(missing), "freight_outcome": outcome,
            "freight_ran": outcome is not None}


def points(result, name):

    return next(item for item in result["factors"] if item["signal"] == name)


# ============================================================
# THE SCORE
# ============================================================

def test_every_signal_adds_its_points_and_the_category_follows_the_thresholds():

    result = risk.score(evidence([
        exception("quantity", severity="HIGH"), exception("delivery", severity="HIGH", category="Delivery"),
        exception("freight-duplicate-invoice", severity="HIGH", category="Freight audit: duplicate invoice"),
        exception("freight-fuel", severity="MEDIUM", amount=1200.0, category="Freight audit: fuel"),
    ], missing=["POD"], outcome="DISCREPANCY"))

    # 12 + 12 + 6 (discrepancies) + 20 (duplicate) + 15 (USD 1,200) + 5 (POD) + 10 (outcome)
    assert [points(result, name)["points"] for name in risk.SIGNALS] == [30, 0, 20, 15, 5, 10]
    assert result["score"] == 80 and result["category"] == "critical" and result["partial"] is False
    assert result["model"] == {"version": risk.DEFAULT_MODEL["version"], "digest": risk.digest_of(risk.DEFAULT_MODEL)}
    assert [item["reference"] for item in points(result, "duplicate_invoice")["evidence"]] == [f"{KEY}|freight-duplicate-invoice"]


def test_a_signal_is_capped_at_its_maximum():

    result = risk.score(evidence([exception(f"code{n}") for n in range(5)]))
    factor = points(result, "audit_discrepancies")

    assert (factor["uncapped_points"], factor["points"], factor["max"]) == (60, 30, 30)


@pytest.mark.parametrize("value, category", [(0, "low"), (24, "low"), (25, "medium"), (49, "medium"), (50, "high"),
                                             (74, "high"), (75, "critical"), (100, "critical")])
def test_the_threshold_boundaries(value, category):

    assert risk.category_of(value, risk.DEFAULT_MODEL) == category


def test_resolved_dismissed_and_waived_exceptions_do_not_count_and_are_listed():

    result = risk.score(evidence([exception("quantity", status="resolved"), exception("delivery", status="waived"),
                                  exception("invoice", status="dismissed"), exception("route", status="disputed", severity="MEDIUM")]))
    factor = points(result, "audit_discrepancies")

    assert factor["points"] == 6 and [item["reference"] for item in factor["evidence"]] == [f"{KEY}|route"]
    assert sorted(item["status"] for item in factor["not_counted"]) == ["dismissed", "resolved", "waived"]


def test_missing_evidence_is_unavailable_never_zero_risk():

    result = risk.score(evidence([], missing=None, outcome=None))

    assert result["unavailable"] == ["missing_documents", "freight_outcome"] and result["partial"] is True
    assert points(result, "missing_documents")["status"] == "unavailable"
    assert points(result, "freight_outcome")["detail"] == "The freight audit did not run for this shipment."

    nothing = risk.score({**evidence(), "exceptions": None})
    assert nothing["unavailable"][:4] == ["audit_discrepancies", "unverified_values", "duplicate_invoice", "financial_exposure"]


def test_live_evidence_without_a_completeness_entry_is_unknown_not_complete():

    item = {"shipment_key": KEY, "result": {"success": True}, "freight_audit": {"outcome": "MATCHED"}}

    assert risk.live_evidence(item, [], None)["missing_documents"] is None
    assert risk.live_evidence(item, [], {"missing": []})["missing_documents"] == []
    assert risk.live_evidence(item, [], {"missing": []}, completeness_known=False)["missing_documents"] is None


def test_amounts_are_never_added_across_currencies():

    result = risk.score(evidence([exception("a", amount=800.0, currency="USD"), exception("b", amount=800.0, currency="EUR")]))
    factor = points(result, "financial_exposure")

    # 800 USD and 800 EUR: each is in the 250 band; added they would wrongly reach the 1,000 band
    assert factor["points"] == 10
    assert "EUR 800.00, USD 800.00" in factor["detail"] and "never added" in factor["detail"]


def test_a_duplicate_invoice_is_an_indicator_never_an_allegation():

    result = risk.score(evidence([exception("freight-duplicate-invoice")], outcome="DUPLICATE"))
    words = " ".join(item["detail"] for item in result["factors"]).casefold()

    assert "not a finding of fraud" in words and "fraudulent" not in words
    assert points(result, "freight_outcome")["points"] == 0              # counted once, as the indicator


def test_the_same_evidence_and_model_always_give_the_same_score():

    given = evidence([exception("quantity"), exception("x", kind=risk.REVIEW, severity="MEDIUM")], missing=["BOL"])

    assert risk.score(copy.deepcopy(given)) == risk.score(copy.deepcopy(given))


# ============================================================
# THE VERSIONED MODEL
# ============================================================

def model_with(**changes):

    model = copy.deepcopy(risk.DEFAULT_MODEL)
    model.update(changes)

    return model


def test_the_model_is_versioned_and_a_changed_weight_needs_a_new_version():

    assert risk.load_model("") is risk.DEFAULT_MODEL

    changed = model_with(thresholds={"medium": 20, "high": 40, "critical": 60})
    with pytest.raises(risk.ModelError, match="give it a new version"):
        risk.load_model(json.dumps(changed))

    changed["version"] = "risk-custom-2"
    loaded = risk.load_model(json.dumps(changed))
    assert loaded["version"] == "risk-custom-2" and risk.digest_of(loaded) != risk.digest_of(risk.DEFAULT_MODEL)

    # The same evidence under another model version: another, equally reproducible score
    given = evidence([exception("quantity"), exception("delivery")])
    assert risk.score(given, loaded)["category"] == "medium" and risk.score(given)["category"] == "low"
    assert risk.score(given, loaded)["model"]["version"] == "risk-custom-2"


@pytest.mark.parametrize("text, words", [
    ("{not json", "not valid JSON"),
    (json.dumps({"version": "x"}), "needs exactly"),
    (json.dumps(model_with(version="v2", thresholds={"medium": 50, "high": 40, "critical": 60})), "must rise"),
    (json.dumps(model_with(version="v2", thresholds={"medium": 0, "high": 40, "critical": 60})), "from 1 to 100"),
])
def test_an_invalid_model_is_refused_and_stops_the_server(text, words):

    with pytest.raises(risk.ModelError, match=words):
        risk.load_model(text)

    errors = environment.validate({"LOGIDOC_RISK_MODEL": text})["errors"]
    assert any(error.startswith("LOGIDOC_RISK_MODEL:") for error in errors)


# ============================================================
# RECORDED HISTORY
# ============================================================

def observation(run_id, at, findings, reconciled=1, snapshot_id=None, difference=None):

    return {"id": snapshot_id or run_id, "run_id": run_id, "observed_at": at, "shipment_key": KEY, "reconciled": reconciled,
            "invoice_difference": difference, "currency": "USD",
            "findings": [{"finding_key": f"{KEY}|{code}", "kind": kind, "category": "Invoice", "severity": severity}
                         for code, kind, severity in findings]}


def test_trends_score_recorded_snapshots_and_earlier_periods_never_change():

    october = observation(1, "2026-10-05T10:00:00+00:00", [("quantity", risk.DISCREPANCY, "HIGH")])
    freight = {1: {"outcome": "DISCREPANCY", "financial_impact": {"currency": "USD", "billed_above_contract": 300.0,
                                                                  "without_evidence": 0, "pending_review": 0}}}
    first = risk.trends([october], freight)

    assert first["points"] == [{"period": "2026-10", "shipments": 1, "mean_score": 32.0, "high_risk": 0,
                                "by_category": {"low": 0, "medium": 1, "high": 0, "critical": 0}}]
    assert first["signals_unavailable"] == ["missing_documents"]

    # A later run in November changes nothing about October
    november = observation(2, "2026-11-02T10:00:00+00:00", [])
    second = risk.trends([october, november], {**freight, 2: {"outcome": "MATCHED", "financial_impact": None}})
    assert second["points"][0] == first["points"][0] and second["points"][1]["mean_score"] == 0.0

    # Two runs in one period: the shipment counts once, at its latest run
    again = observation(3, "2026-10-20T10:00:00+00:00", [], snapshot_id=3)
    assert risk.trends([october, again], {**freight, 3: {"outcome": "MATCHED", "financial_impact": None}})["points"][0]["shipments"] == 1


def test_weekly_periods_use_iso_weeks():

    assert risk.period_of("2026-11-15T10:00:00+00:00", "week") == "2026-W46"
    assert risk.period_of("2026-11-15T10:00:00+00:00", "month") == "2026-11"


# ============================================================
# THE API
# ============================================================

@pytest.fixture
def rig(tmp_path):

    result = Rig(tmp_path, "required")
    UserStore(tmp_path / "auth.sqlite3").create_user("dave", PASSWORD, ADMIN)
    write_contracted_shipment(result.raw)
    result.state.service.invalidate()

    return result


def test_the_overview_counts_add_up(rig):

    bob = rig.client("bob")                                                  # reviewers read it
    overview = bob.get("/api/risk/overview").json()
    rows = bob.get("/api/risk/shipments", params={"limit": 200}).json()

    totals = overview["totals"]
    assert totals["shipments"] == rows["total"] == len(rows["rows"]) > 0
    assert sum(totals["by_category"].values()) == totals["shipments"] == sum(band["count"] for band in overview["histogram"])
    assert totals["high_risk"] == totals["by_category"]["high"] + totals["by_category"]["critical"]
    assert totals["mean_score"] == round(sum(row["score"] for row in rows["rows"]) / len(rows["rows"]), 1)
    assert sum(carrier["shipments"] for carrier in overview["carriers"]) == totals["shipments"]
    assert overview["model"]["version"] == risk.DEFAULT_MODEL["version"]


def test_each_score_is_the_sum_of_its_factors_with_evidence(rig):

    detail = rig.client("alice").get("/api/risk/shipments/SHJ-2026-101").json()

    assert detail["score"] == sum(item["points"] for item in detail["factors"])
    assert detail["category"] == risk.category_of(detail["score"], risk.DEFAULT_MODEL)
    assert [item["signal"] for item in detail["factors"]] == list(risk.SIGNALS)
    references = [ref["reference"] for item in detail["factors"] for ref in item["evidence"]]
    assert any(reference.startswith("SHJ2026101|") for reference in references)
    assert detail["thresholds"] == risk.DEFAULT_MODEL["thresholds"] and detail["basis"] == "current"


def test_dismissing_an_exception_lowers_the_score(rig):

    alice = rig.client("alice")
    before = alice.get("/api/risk/shipments/SHJ-2026-001").json()
    counted = points(before, "audit_discrepancies")["evidence"]
    assert counted

    key = counted[0]["reference"]
    assert alice.post(f"/api/exceptions/{key}/actions", json={"action": "dismiss", "note": "Explained by the carrier's credit note."}).status_code == 200

    after = alice.get("/api/risk/shipments/SHJ-2026-001").json()
    assert after["score"] < before["score"]
    assert key in [item["reference"] for item in points(after, "audit_discrepancies")["not_counted"]]


def test_the_table_filters_sorts_and_pages(rig):

    alice = rig.client("alice")
    everything = alice.get("/api/risk/shipments", params={"limit": 200}).json()["rows"]
    assert [row["score"] for row in everything] == sorted((row["score"] for row in everything), reverse=True)

    page = alice.get("/api/risk/shipments", params={"limit": 2, "offset": 1, "sort": "shipment", "order": "asc"}).json()
    names = sorted((row["shipment_id"] for row in everything), key=str.casefold)
    assert [row["shipment_id"] for row in page["rows"]] == names[1:3] and page["total"] == len(everything)

    category = everything[0]["category"]
    only = alice.get("/api/risk/shipments", params={"category": category}).json()["rows"]
    assert only and all(row["category"] == category for row in only)
    assert alice.get("/api/risk/shipments", params={"search": "101"}).json()["rows"][0]["shipment_id"] == "SHJ-2026-101"
    assert alice.get("/api/risk/shipments", params={"min_score": 101}).status_code == 422
    assert alice.get("/api/risk/shipments", params={"category": "extreme"}).status_code == 422
    assert alice.get("/api/risk/shipments", params={"start": "2026-10-10", "end": "2026-10-01"}).status_code == 422
    assert alice.get("/api/risk/shipments/NOPE-1").status_code == 404


def test_trends_come_from_recorded_runs_and_repeat_exactly(rig):

    alice = rig.client("alice")
    assert alice.get("/api/risk/trends").json()["points"] == []             # nothing recorded yet

    assert alice.post("/api/history/runs", json={"label": "First"}).status_code == 201
    first = alice.get("/api/risk/trends").json()
    assert first["points"][0]["shipments"] == 4 and first == alice.get("/api/risk/trends").json()
    assert alice.get("/api/risk/trends", params={"period": "week"}).json()["period"] == "week"
    assert alice.get("/api/risk/trends", params={"period": "day"}).status_code == 422


def test_risk_needs_a_signed_in_user_and_a_bad_model_is_reported(rig, monkeypatch):

    for url in ("/api/risk/overview", "/api/risk/shipments", "/api/risk/model", "/api/risk/trends", "/api/v1/risk/overview"):
        assert rig.client().get(url).status_code == 401

    monkeypatch.setenv("LOGIDOC_RISK_MODEL", "{broken")
    reply = rig.client("alice").get("/api/risk/overview")
    assert reply.status_code == 503 and "risk model cannot be used" in reply.json()["detail"]


def test_without_documents_nothing_is_scored(tmp_path):

    rig = Rig(tmp_path, "required")
    for path in rig.raw.glob("*.pdf"):
        path.unlink()
    rig.state.service.invalidate()

    overview = rig.client("alice").get("/api/risk/overview").json()
    assert overview["totals"] == {"shipments": 0, "high_risk": 0, "partial": 0, "mean_score": None,
                                  "by_category": {"low": 0, "medium": 0, "high": 0, "critical": 0}}
    assert overview["carriers"] == [] and overview["exceptions"] == []
