# Shipment risk analytics

An explainable, deterministic score from 0 to 100 for each shipment, made only of evidence the application already
holds. No language model is involved, and no score is an allegation.

Code: `src/risk_analytics.py` (model, evidence, score, overview, trends), `src/api/routes_risk.py` (API),
`frontend/src/pages/Risk.tsx` (page *Shipment Risk*).

## 1. Signals

Each signal is capped at its maximum. The maxima of the built-in model add up to 100.

| Signal | What counts | Built-in points (max) |
|---|---|---|
| Audit discrepancies | discrepancy exceptions still open, in review or disputed, by severity | high 12, medium 6, low 3 (30) |
| Values not verified | values that could not be verified, not yet resolved; a shipment that could not be reconciled | value 4, not reconciled 10 (10) |
| Duplicate invoice indicator | the freight audit found the invoice number on another shipment's invoice | 20 (20) |
| Amount at stake | the amounts at stake in unresolved exceptions, totalled per currency, in bands | ≥ 1,000: 15; ≥ 250: 10; > 0: 5 (15) |
| Missing documents | BOL, invoice or POD missing (document completeness) | 5 each (15) |
| Freight audit outcome | the freight audit's overall outcome | discrepancy 10, pending review 6, insufficient evidence 4, matched with tolerance 2 (10) |

Rules:

- **Reviewed exceptions don't count.** Resolved, dismissed and waived exceptions add nothing. They are listed
  under "not counted" in the explanation.
- **A duplicate invoice number is an indicator to check before paying, never a finding of fraud**, and the text
  says so. A freight audit outcome of `DUPLICATE` adds nothing of its own: it is counted once, as the indicator.
- **Amounts are never added across currencies.** Each currency's total is banded on its own, and the highest band
  counts. A discrepancy without a determinable amount is not counted as 0; the explanation says how many there
  are.
- **Missing evidence is never "no risk".** A signal whose evidence is not available scores nothing, is listed as
  unavailable, and makes the score **partial**. That happens when:
  - the completeness register has no entry for the shipment;
  - the freight audit did not run;
  - the exceptions could not be read.

Categories, with the built-in thresholds:

| Category | Score |
|---|---|
| low | below 25 |
| medium | 25 or more |
| high | 50 or more |
| critical | 75 or more |

## 2. The model is versioned

`LOGIDOC_RISK_MODEL` (JSON) replaces the built-in model (`risk-2026.11-1`). It names its `version`, `thresholds`
(medium < high < critical, from 1 to 100) and every signal's rule.

- **Validated at startup.** An invalid model stops the server with the reason (`environment.validate`), and the
  risk API answers 503 while it is in force.
- **A new version for new weights.** A model that keeps the built-in version but changes a weight or a threshold
  is refused.
- **Every score carries the model's version and digest** (SHA-256 of its canonical JSON). So a score is
  reproducible: the same evidence under the same version gives the same score, and a changed model is
  recognisable.

## 3. Current risk and recorded risk

- **Current** (the dashboard, the table, the explanation): the audit as it is now, each exception with its review
  state, and the completeness register.
- **Recorded** (the trend): what recorded audit runs found, in each recorded snapshot's findings and freight audit.
  - Recorded snapshots never change, so a period's figures never change under one model version (tested).
  - History records neither review states nor completeness, so for trends every recorded finding counts and
    missing documents are not scored.
  - Each shipment counts once per period (month or ISO week), at its latest run in it.

## 4. API (reviewers and administrators, `exceptions.view`; also under `/api/v1`)

| Method and path | Answer |
|---|---|
| `GET /api/risk/model` | `RiskModelView`: version, digest, thresholds, every signal's rule |
| `GET /api/risk/overview?search=&category=&carrier=&start=&end=` | `RiskOverview`: totals, category counts, score histogram, exceptions by category and review state, carriers, signals |
| `GET /api/risk/shipments?...&min_score=&max_score=&partial=&sort=&order=&offset=&limit=` | `RiskShipmentPage`: a page (up to 200) of scored shipments with their main factors |
| `GET /api/risk/shipments/{id}` | `RiskShipment`: every factor with its points, detail, evidence references and what was not counted |
| `GET /api/risk/trends?period=month\|week` | `RiskTrends` from recorded runs (empty without history) |

Dates filter on the shipment date (BOL pickup, else POD delivery, else invoice date), as Carrier Intelligence
dates a shipment.

## 5. Limitations

- **Static weights.** The built-in weights are a starting point, not calibrated against outcomes. Changing them
  is a configuration decision, with a new version.
- **Single-currency bands.** The amount bands are in the amount's own currency; there is no exchange rate.
- **TMS differences are not a signal yet.** Open TMS differences are not scored; they are shown on the shipment
  and on Correction approvals.
