"""
Shipment risk analytics (docs/SHIPMENT_RISK_ANALYTICS.md).

An explainable, deterministic score from 0 to 100 for each shipment, made only of
evidence the application already holds. No language model is involved and no
score is ever an allegation: a duplicate invoice number is an indicator to
check, never a finding of fraud.

The model (DEFAULT_MODEL, or LOGIDOC_RISK_MODEL as JSON) is versioned: it names
its version, and its digest (SHA-256 of its canonical JSON) is returned with
every score. Changing a weight or a threshold without changing the version is
refused, so a version always means one set of weights. The same evidence and the
same model give the same score, which is how a score from recorded history is
reproduced.

Signals (points are the default model's; each signal is capped at its max)

    audit_discrepancies  unresolved discrepancy exceptions: by severity
    unverified_values    unresolved values that could not be verified, and a
                         shipment that could not be reconciled
    duplicate_invoice    the freight audit found the invoice number on another
                         shipment's invoice (an indicator to check)
    financial_exposure   the amounts at stake in unresolved exceptions, per
                         currency (never added across currencies): banded
    missing_documents    required documents (BOL, invoice, POD) missing
    freight_outcome      the freight audit's overall outcome

A signal whose evidence is not available (the completeness register cannot be
read, the freight audit did not run, review states are not recorded in
history) scores nothing and is listed as unavailable; the score is then
"partial". Missing evidence is never read as "no risk" silently.

Categories: score >= critical -> critical, >= high -> high, >= medium ->
medium, otherwise low (thresholds from the model).

Two kinds of evidence

    current   live_evidence: the audit as it is now, with each exception's review
              state (resolved, dismissed or waived exceptions do not count) and
              the completeness register
    recorded  recorded_evidence: one recorded snapshot of the audit history (its
              findings and its freight audit). History records no review states
              and no completeness, so for trends every finding counts and
              missing_documents is unavailable. A recorded snapshot never
              changes, so its score under one model version never changes either.
"""

import hashlib
import json
import os
from collections import Counter, defaultdict
from typing import Literal

NOT_STATED = "Not stated"

# The values the API accepts (validated there: anything else is a 422)
CategoryName = Literal["low", "medium", "high", "critical"]
SortName = Literal["score", "shipment", "carrier", "date"]
OrderName = Literal["asc", "desc"]
PeriodName = Literal["month", "week"]

UNRESOLVED = ("open", "in_review", "disputed")
DISCREPANCY = "Discrepancy"
REVIEW = "Needs review"
DUPLICATE_CODE = "freight-duplicate-invoice"
RECONCILIATION_CODE = "reconciliation"
CATEGORIES = ("low", "medium", "high", "critical")
REQUIRED_DOCUMENTS = ("BOL", "INVOICE", "POD")
FREIGHT_OUTCOMES = ("MATCHED", "MATCHED_WITH_TOLERANCE", "DISCREPANCY", "INSUFFICIENT_EVIDENCE", "DUPLICATE", "PENDING_REVIEW")

SIGNALS = ("audit_discrepancies", "unverified_values", "duplicate_invoice", "financial_exposure", "missing_documents",
           "freight_outcome")

LABELS = {
    "audit_discrepancies": "Audit discrepancies",
    "unverified_values": "Values not verified",
    "duplicate_invoice": "Duplicate invoice indicator",
    "financial_exposure": "Amount at stake",
    "missing_documents": "Missing documents",
    "freight_outcome": "Freight audit outcome",
}

DESCRIPTIONS = {
    "audit_discrepancies": "Discrepancy exceptions not yet resolved, dismissed or waived, by severity.",
    "unverified_values": "Values that could not be verified, and a shipment that could not be reconciled, not yet resolved.",
    "duplicate_invoice": "The freight audit found this invoice number on another shipment's invoice. An indicator to "
                         "check, not a finding of fraud.",
    "financial_exposure": "The amounts at stake in unresolved exceptions, per currency (never added across currencies), "
                          "in bands.",
    "missing_documents": "Required documents (BOL, invoice, POD) that the shipment does not have.",
    "freight_outcome": "The freight audit's overall outcome (its invoice against the rate confirmation and BOL/POD).",
}

DEFAULT_MODEL = {
    "version": "risk-2026.11-1",
    "thresholds": {"medium": 25, "high": 50, "critical": 75},
    "signals": {
        "audit_discrepancies": {"max": 30, "points": {"HIGH": 12, "MEDIUM": 6, "LOW": 3}},
        "unverified_values": {"max": 10, "points": {"reconciliation": 10, "value": 4}},
        "duplicate_invoice": {"max": 20, "points": {"indicator": 20}},
        "financial_exposure": {"max": 15, "bands": [[1000, 15], [250, 10], [0.01, 5]]},
        "missing_documents": {"max": 15, "points": {"BOL": 5, "INVOICE": 5, "POD": 5}},
        "freight_outcome": {"max": 10, "points": {"DISCREPANCY": 10, "PENDING_REVIEW": 6, "INSUFFICIENT_EVIDENCE": 4,
                                                  "MATCHED_WITH_TOLERANCE": 2, "MATCHED": 0, "DUPLICATE": 0}},
    },
}


class ModelError(ValueError):
    """The risk model cannot be used (it says why)."""


# ============================================================
# THE MODEL
# ============================================================

def canonical(model):

    return json.dumps(model, sort_keys=True, separators=(",", ":"))


def digest_of(model):

    return hashlib.sha256(canonical(model).encode("utf-8")).hexdigest()[:16]


def whole(value, name, low=0, high=100):

    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ModelError(f"{name} must be a whole number from {low} to {high}.")

    return value


def validate(model):
    """The model as given, checked: every rule a score depends on is a known, bounded number."""

    if not isinstance(model, dict) or set(model) != {"version", "thresholds", "signals"}:
        raise ModelError("The risk model needs exactly: version, thresholds, signals.")

    version = model["version"]

    if not isinstance(version, str) or not version.strip() or len(version) > 40:
        raise ModelError("The risk model's version must be a name of 1 to 40 characters.")

    thresholds = model["thresholds"]

    if not isinstance(thresholds, dict) or set(thresholds) != {"medium", "high", "critical"}:
        raise ModelError("thresholds needs exactly: medium, high, critical.")

    medium, high, critical = (whole(thresholds[name], f"thresholds.{name}", 1) for name in ("medium", "high", "critical"))

    if not medium < high < critical:
        raise ModelError("The thresholds must rise: medium < high < critical.")

    signals = model["signals"]

    if not isinstance(signals, dict) or set(signals) != set(SIGNALS):
        raise ModelError(f"signals needs exactly: {', '.join(SIGNALS)}.")

    for name, rule in signals.items():
        if not isinstance(rule, dict):
            raise ModelError(f"signals.{name} must be an object.")

        whole(rule.get("max"), f"signals.{name}.max")

        if name == "financial_exposure":
            bands = rule.get("bands")
            if not isinstance(bands, list) or not bands:
                raise ModelError("signals.financial_exposure.bands must list [amount, points] from the highest amount down.")
            amounts = []
            for band in bands:
                if (not isinstance(band, list) or len(band) != 2 or isinstance(band[0], bool)
                        or not isinstance(band[0], (int, float)) or band[0] <= 0):
                    raise ModelError("Each financial_exposure band is [amount above 0, points].")
                whole(band[1], "signals.financial_exposure.bands points")
                amounts.append(band[0])
            if amounts != sorted(amounts, reverse=True):
                raise ModelError("financial_exposure bands must go from the highest amount down.")
            continue

        points = rule.get("points")
        expected = {"audit_discrepancies": {"HIGH", "MEDIUM", "LOW"}, "unverified_values": {"reconciliation", "value"},
                    "duplicate_invoice": {"indicator"}, "missing_documents": set(REQUIRED_DOCUMENTS),
                    "freight_outcome": set(FREIGHT_OUTCOMES)}[name]

        if not isinstance(points, dict) or set(points) != expected:
            raise ModelError(f"signals.{name}.points needs exactly: {', '.join(sorted(expected))}.")

        for key, value in points.items():
            whole(value, f"signals.{name}.points.{key}")

    return model


def load_model(text=None):
    """
    The risk model: LOGIDOC_RISK_MODEL (JSON) when set, else DEFAULT_MODEL. A
    model that reuses the default's version with other content is refused: a
    version must mean one set of weights.
    """

    text = os.environ.get("LOGIDOC_RISK_MODEL", "") if text is None else text

    if not str(text).strip():
        return DEFAULT_MODEL

    try:
        model = json.loads(text)
    except ValueError as error:
        raise ModelError(f"LOGIDOC_RISK_MODEL is not valid JSON: {error.msg}.") from None

    validate(model)

    if model["version"] == DEFAULT_MODEL["version"] and digest_of(model) != digest_of(DEFAULT_MODEL):
        raise ModelError(f"The model changes weights or thresholds but keeps the version {model['version']!r}: "
                         "give it a new version, so earlier scores stay reproducible.")

    return model


def model_view(model):
    """The model as the API shows it."""

    return {
        "version": model["version"], "digest": digest_of(model), "thresholds": dict(model["thresholds"]),
        "signals": [{"signal": name, "label": LABELS[name], "description": DESCRIPTIONS[name], "max": model["signals"][name]["max"],
                     "rule": {key: value for key, value in model["signals"][name].items() if key != "max"}}
                    for name in SIGNALS],
    }


def category_of(score, model):

    thresholds = model["thresholds"]

    if score >= thresholds["critical"]:
        return "critical"

    if score >= thresholds["high"]:
        return "high"

    if score >= thresholds["medium"]:
        return "medium"

    return "low"


# ============================================================
# EVIDENCE
# ============================================================

def code_of(exception_key):

    return str(exception_key).split("|", 1)[-1]


def live_evidence(item, exceptions, completeness_row, completeness_known=True):
    """
    The evidence of one shipment as the audit holds it now. exceptions: the
    shipment's exceptions with their review state (AuditService.exceptions());
    completeness_row: its completeness register entry (None: it has none);
    completeness_known False: the register could not be read.
    """

    audit = item.get("freight_audit")

    return {
        "basis": "current",
        "reconciled": bool((item.get("result") or {}).get("success")),
        "exceptions": [
            {"key": e["exception_key"], "kind": e.get("kind"), "category": e.get("category"), "severity": e.get("severity"),
             "status": e.get("review_status") or "open", "impact": e.get("financial_impact")}
            for e in exceptions
        ],
        "statuses_known": True,
        # None: not known (the register could not be read, or has no entry for the shipment), never "nothing missing"
        "missing_documents": list(completeness_row.get("missing") or []) if completeness_known and completeness_row is not None else None,
        "freight_outcome": (audit or {}).get("outcome"),
        "freight_ran": bool(audit) and (audit or {}).get("outcome") is not None,
    }


def recorded_evidence(snapshot, freight=None):
    """
    The evidence of one recorded snapshot (HistoryStore.observations() row) and
    its recorded freight audit ({"outcome", "financial_impact"} or None).
    History records no review state and no completeness.
    """

    exposure = []
    impact = (freight or {}).get("financial_impact") or {}

    for basis in ("billed_above_contract", "without_evidence", "pending_review"):
        amount = impact.get(basis)
        if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
            exposure.append({"amount": float(amount), "currency": impact.get("currency") or NOT_STATED, "basis": basis})

    difference = snapshot.get("invoice_difference")

    if isinstance(difference, (int, float)) and not isinstance(difference, bool) and abs(difference) >= 0.005:
        exposure.append({"amount": abs(float(difference)), "currency": snapshot.get("currency") or NOT_STATED,
                         "basis": "invoice_arithmetic"})

    return {
        "basis": "recorded",
        "reconciled": bool(snapshot.get("reconciled")),
        "exceptions": [
            {"key": f"{snapshot['shipment_key']}|{f['finding_key'].split('|', 1)[-1]}", "kind": f.get("kind"),
             "category": f.get("category"), "severity": f.get("severity"), "status": None, "impact": None}
            for f in snapshot.get("findings") or []
        ],
        "statuses_known": False,
        "exposure": exposure,
        "missing_documents": None,
        "freight_outcome": (freight or {}).get("outcome"),
        "freight_ran": freight is not None and (freight or {}).get("outcome") is not None,
    }


# ============================================================
# THE SCORE
# ============================================================

def factor(name, model, status, points=0, detail="", evidence=(), not_counted=()):

    cap = model["signals"][name]["max"]

    return {"signal": name, "label": LABELS[name], "status": status, "points": min(points, cap), "max": cap,
            "uncapped_points": points, "detail": detail, "evidence": list(evidence), "not_counted": list(not_counted)}


def unresolved(exception, statuses_known):

    return exception["status"] in UNRESOLVED if statuses_known else True


def exception_reference(exception):

    return {"type": "exception", "reference": exception["key"], "category": exception["category"],
            "severity": exception["severity"], "status": exception["status"]}


def score(evidence, model=None):
    """
    The score of one shipment's evidence under a model: {"score", "category",
    "partial", "factors", "unavailable", "model": {version, digest}}. Pure:
    the same evidence and model always give the same result.
    """

    model = model or DEFAULT_MODEL
    rules = model["signals"]
    known = evidence["statuses_known"]
    exceptions = evidence.get("exceptions")
    factors = []

    if exceptions is None:
        for name in ("audit_discrepancies", "unverified_values", "duplicate_invoice", "financial_exposure"):
            factors.append(factor(name, model, "unavailable", detail="The exceptions could not be read."))
    else:
        counted = [e for e in exceptions if unresolved(e, known)]
        closed = [exception_reference(e) for e in exceptions if not unresolved(e, known)]

        # --- discrepancies (a duplicate invoice is its own signal) -------
        found = [e for e in counted if e["kind"] == DISCREPANCY and code_of(e["key"]) != DUPLICATE_CODE]
        points = sum(rules["audit_discrepancies"]["points"].get(str(e["severity"]).upper(), 0) for e in found)
        factors.append(factor(
            "audit_discrepancies", model, "scored" if points else "clear", points,
            f"{len(found)} unresolved discrepanc{'y' if len(found) == 1 else 'ies'}"
            + (f": {', '.join(sorted({e['category'] for e in found}))}." if found else "."),
            [exception_reference(e) for e in found],
            [c for c in closed if c["category"] and not str(c["reference"]).endswith(DUPLICATE_CODE)],
        ))

        # --- values not verified ------------------------------------------
        review = [e for e in counted if e["kind"] == REVIEW]
        points = sum(rules["unverified_values"]["points"]["reconciliation" if code_of(e["key"]) == RECONCILIATION_CODE else "value"]
                     for e in review)
        factors.append(factor(
            "unverified_values", model, "scored" if points else "clear", points,
            (f"{len(review)} value(s) or check(s) could not be verified." if review else "Every audited value was verified."),
            [exception_reference(e) for e in review],
        ))

        # --- duplicate invoice indicator ----------------------------------
        duplicates = [e for e in counted if code_of(e["key"]) == DUPLICATE_CODE]
        points = rules["duplicate_invoice"]["points"]["indicator"] if duplicates else 0
        factors.append(factor(
            "duplicate_invoice", model, "scored" if duplicates else "clear", points,
            ("The invoice number also appears on another shipment's invoice. Check it before paying; this is an "
             "indicator, not a finding of fraud." if duplicates else "No duplicate invoice number was found."),
            [exception_reference(e) for e in duplicates],
        ))

        # --- amount at stake, per currency --------------------------------
        if evidence["basis"] == "recorded":
            amounts = evidence.get("exposure") or []
            unknown = 0
        else:
            amounts = [{"amount": float(e["impact"]["amount"]), "currency": e["impact"].get("currency") or NOT_STATED,
                        "basis": e["key"]} for e in counted
                       if e.get("impact") and isinstance(e["impact"].get("amount"), (int, float)) and e["impact"]["amount"] > 0]
            unknown = sum(1 for e in counted if e["kind"] == DISCREPANCY and not e.get("impact"))

        by_currency = defaultdict(float)

        for item in amounts:
            by_currency[item["currency"]] += item["amount"]

        best, worst_currency = 0, None

        for currency, total in sorted(by_currency.items()):
            band = next((points for limit, points in rules["financial_exposure"]["bands"] if total >= limit), 0)
            if band > best:
                best, worst_currency = band, currency

        totals = ", ".join(f"{currency} {total:,.2f}" for currency, total in sorted(by_currency.items()))
        detail = (f"At stake: {totals}" + (f" (scored on {worst_currency}; currencies are never added)" if len(by_currency) > 1 else "")
                  + "." if by_currency else "No amount is at stake in an unresolved exception.")

        if unknown:
            detail += f" {unknown} discrepanc{'y has' if unknown == 1 else 'ies have'} no determinable amount (not counted as 0)."

        factors.append(factor(
            "financial_exposure", model, "scored" if best else "clear", best, detail,
            [{"type": "amount", "reference": item["basis"], "amount": round(item["amount"], 2), "currency": item["currency"]}
             for item in amounts],
        ))

    # --- missing documents --------------------------------------------------
    missing = evidence.get("missing_documents")

    if missing is None:
        factors.append(factor("missing_documents", model, "unavailable",
                              detail=("Not recorded in the audit history." if evidence["basis"] == "recorded"
                                      else "The document completeness register could not be read.")))
    else:
        points = sum(rules["missing_documents"]["points"].get(kind, 0) for kind in missing)
        factors.append(factor(
            "missing_documents", model, "scored" if points else "clear", points,
            (f"Missing: {', '.join(missing)}." if missing else "BOL, invoice and POD are present."),
            [{"type": "document", "reference": kind} for kind in missing],
        ))

    # --- freight audit outcome ----------------------------------------------
    outcome = evidence.get("freight_outcome")

    if not evidence.get("freight_ran") or outcome is None:
        factors.append(factor("freight_outcome", model, "unavailable", detail="The freight audit did not run for this shipment."))
    else:
        points = rules["freight_outcome"]["points"].get(outcome, 0)
        factors.append(factor(
            "freight_outcome", model, "scored" if points else "clear", points,
            f"Freight audit outcome: {outcome.replace('_', ' ').lower()}"
            + (" (counted as the duplicate invoice indicator)." if outcome == "DUPLICATE" else "."),
            [{"type": "freight_audit", "reference": outcome}],
        ))

    total = min(100, sum(item["points"] for item in factors))
    unavailable = [item["signal"] for item in factors if item["status"] == "unavailable"]

    return {
        "score": total, "category": category_of(total, model), "partial": bool(unavailable), "factors": factors,
        "unavailable": unavailable, "basis": evidence["basis"],
        "model": {"version": model["version"], "digest": digest_of(model)},
    }


# ============================================================
# THE CURRENT AUDIT
# ============================================================

def assess(shipments, exceptions, completeness_rows, facts_rows, model=None, completeness_known=True):
    """
    Every shipment of the current audit scored: [{shipment_id, shipment_key,
    carrier_key, carrier, date, currency, score, category, partial, factors,
    unavailable, model}]. facts_rows: carrier_intelligence.shipment_rows (the
    carrier and date every other view uses).
    """

    model = model or DEFAULT_MODEL
    by_key = defaultdict(list)

    for exception in exceptions:
        by_key[exception["shipment_key"]].append(exception)

    completeness = {row["shipment_key"]: row for row in completeness_rows}
    facts = {row["shipment_key"]: row for row in facts_rows}
    result = []

    for item in shipments:
        key = item["shipment_key"]
        evidence = live_evidence(item, by_key.get(key, []), completeness.get(key), completeness_known)
        scored = score(evidence, model)
        fact = facts.get(key) or {}
        open_exceptions = sum(1 for e in by_key.get(key, []) if (e.get("review_status") or "open") in UNRESOLVED)

        result.append({
            "shipment_id": item["shipment_id"], "shipment_key": key, "carrier_key": fact.get("carrier_key"),
            "carrier": fact.get("carrier") or NOT_STATED, "date": fact["date"].isoformat() if fact.get("date") else None,
            "date_basis": fact.get("date_basis"), "currency": fact.get("currency") or NOT_STATED,
            "final_status": fact.get("final_status"), "freight_outcome": (item.get("freight_audit") or {}).get("outcome"),
            "open_exceptions": open_exceptions, **scored,
        })

    return result


def top_factors(row, limit=3):

    return [{"signal": item["signal"], "label": item["label"], "points": item["points"]}
            for item in sorted(row["factors"], key=lambda item: (-item["points"], SIGNALS.index(item["signal"])))
            if item["points"] > 0][:limit]


def overview(rows, exceptions, model=None):
    """
    The dashboard's figures over the given scored rows (already filtered):
    totals, the category distribution, a score histogram, the exception
    breakdown, each carrier's pattern and how often each signal scored.
    Every count says what it counts; a mean over no shipment is None.
    """

    model = model or DEFAULT_MODEL
    keys = {row["shipment_key"] for row in rows}
    categories = Counter(row["category"] for row in rows)
    scores = [row["score"] for row in rows]

    # Ten bands of ten points; the last one also holds 100
    histogram = [{"band": f"{start}-{start + 9 if start < 90 else 100}", "lower": start, "upper": start + 9 if start < 90 else 100,
                  "count": sum(1 for value in scores if start <= value <= (start + 9 if start < 90 else 100))}
                 for start in range(0, 100, 10)]

    breakdown = defaultdict(Counter)

    for exception in exceptions:
        if exception["shipment_key"] in keys:
            breakdown[exception["category"]][exception.get("review_status") or "open"] += 1

    carriers = defaultdict(list)

    for row in rows:
        carriers[(row["carrier_key"], row["carrier"])].append(row)

    carrier_rows = []

    for (key, name), members in carriers.items():
        signals = Counter(item["signal"] for row in members for item in row["factors"] if item["points"] > 0)
        carrier_rows.append({
            "carrier_key": key, "carrier": name, "shipments": len(members),
            "mean_score": round(sum(row["score"] for row in members) / len(members), 1),
            "high_risk": sum(1 for row in members if row["category"] in ("high", "critical")),
            "by_category": {category: sum(1 for row in members if row["category"] == category) for category in CATEGORIES},
            "partial": sum(1 for row in members if row["partial"]),
            "top_signals": [{"signal": name, "label": LABELS[name], "shipments": count}
                            for name, count in sorted(signals.items(), key=lambda item: (-item[1], item[0]))[:3]],
        })

    carrier_rows.sort(key=lambda item: (-item["high_risk"], -item["mean_score"], str(item["carrier"]).casefold()))

    signal_rows = []

    for name in SIGNALS:
        scored = [item for row in rows for item in row["factors"] if item["signal"] == name]
        signal_rows.append({"signal": name, "label": LABELS[name],
                            "shipments_scored": sum(1 for item in scored if item["points"] > 0),
                            "shipments_unavailable": sum(1 for item in scored if item["status"] == "unavailable"),
                            "points": sum(item["points"] for item in scored)})

    return {
        "model": {"version": model["version"], "digest": digest_of(model), "thresholds": dict(model["thresholds"])},
        "totals": {"shipments": len(rows), "high_risk": categories["high"] + categories["critical"],
                   "partial": sum(1 for row in rows if row["partial"]),
                   "mean_score": round(sum(scores) / len(scores), 1) if scores else None,
                   "by_category": {category: categories[category] for category in CATEGORIES}},
        "histogram": histogram,
        "exceptions": [{"category": category, "total": sum(counts.values()), "by_status": dict(sorted(counts.items()))}
                       for category, counts in sorted(breakdown.items(), key=lambda item: (-sum(item[1].values()), item[0]))],
        "carriers": carrier_rows,
        "signals": signal_rows,
    }


def matches(row, search="", categories=(), carrier=None, min_score=None, max_score=None, partial=None, start=None, end=None):
    """Whether a scored row passes the table's filters (dates compare the shipment date, ISO strings)."""

    if search and search.casefold() not in f"{row['shipment_id']} {row['carrier']}".casefold():
        return False

    if categories and row["category"] not in categories:
        return False

    if carrier and row["carrier_key"] != carrier:
        return False

    if min_score is not None and row["score"] < min_score:
        return False

    if max_score is not None and row["score"] > max_score:
        return False

    if partial is not None and row["partial"] != partial:
        return False

    if (start or end) and row["date"] is None:
        return False

    if start and row["date"] < start.isoformat():
        return False

    if end and row["date"] > end.isoformat():
        return False

    return True


SORTS = {
    "score": lambda row: (row["score"], row["shipment_id"]),
    "shipment": lambda row: row["shipment_id"].casefold(),
    "carrier": lambda row: (str(row["carrier"]).casefold(), row["shipment_id"]),
    "date": lambda row: (row["date"] or "", row["shipment_id"]),
}


# ============================================================
# RECORDED HISTORY (TRENDS)
# ============================================================

def period_of(moment, period):
    """The period an ISO timestamp falls in: "2026-11" (month) or "2026-W46" (ISO week)."""

    from datetime import datetime

    when = datetime.fromisoformat(str(moment))

    if period == "week":
        year, week, _ = when.isocalendar()
        return f"{year}-W{week:02d}"

    return f"{when.year}-{when.month:02d}"


def trends(observations, freight_by_snapshot, model=None, period="month"):
    """
    The risk of what recorded audit runs found, per period: each shipment's
    latest observation in the period scored from its recorded snapshot.
    observations: HistoryStore.observations(); freight_by_snapshot: {snapshot
    id: {"outcome", "financial_impact"}} (HistoryStore.freight_facts()).
    Recorded snapshots never change, so a period's figures never change under
    one model version.
    """

    model = model or DEFAULT_MODEL
    latest = {}

    for row in observations:
        key = (period_of(row["observed_at"], period), row["shipment_key"])
        if key not in latest or row["run_id"] >= latest[key]["run_id"]:
            latest[key] = row

    periods = defaultdict(list)

    for (name, _), row in latest.items():
        periods[name].append(score(recorded_evidence(row, freight_by_snapshot.get(row["id"])), model))

    points = []

    for name in sorted(periods):
        scored = periods[name]
        counts = Counter(item["category"] for item in scored)
        points.append({"period": name, "shipments": len(scored),
                       "mean_score": round(sum(item["score"] for item in scored) / len(scored), 1),
                       "high_risk": counts["high"] + counts["critical"],
                       "by_category": {category: counts[category] for category in CATEGORIES}})

    return {"model": {"version": model["version"], "digest": digest_of(model)}, "period": period, "points": points,
            "signals_used": [name for name in SIGNALS if name != "missing_documents"],
            "signals_unavailable": ["missing_documents"],
            "note": "Recorded history has no review states and no document completeness: every recorded finding counts, "
                    "and missing documents are not scored. Each shipment counts once per period, at its latest recorded run."}
