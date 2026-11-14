# TMS shipments in the audit

How a shipment record imported from a TMS (the TMS/ERP integration framework,
[TMS_ERP_INTEGRATION_FRAMEWORK.md](TMS_ERP_INTEGRATION_FRAMEWORK.md)) is compared with the audited shipment it
names, how a difference is decided, and how approved corrections stay protected.

Code: `src/tms_audit.py` (matching, comparison, decisions), `src/audit_service.py` (where the audit reads it),
`src/audit_inputs.py` (a chosen TMS value as an audit input), `src/api/routes_audit_review.py` (API), screen:
the shipment page's **TMS shipment record** section, and **TMS differences to decide** on Correction approvals.

## 1. Matching

- A record matches the audited shipment whose key equals its reference's key (`identifier_key`, the same
  normalisation as everywhere else: `ACME-2026-17` and `acme 2026 17` match). There is no fuzzy matching and no
  manual link.
- Only **enabled** integrations are read, each from where its records were imported. A sandbox integration's
  records stay in the sandbox folder.
- Two records naming one shipment (two integrations, or a duplicate in the TMS): nothing is compared or used,
  and the shipment shows both records until the duplicate is resolved in the TMS.
- Without an integration store nothing is read or created.

## 2. What is compared

| Field | TMS field | The documents' value | Read by a calculation |
|---|---|---|---|
| Expected pieces | `pieces` | BOL Number of Pieces | quantity audit |
| Actual delivery date | `delivery_date`, only once the TMS status is `delivered` | POD Delivery Date | delivery audit |
| Pickup date | `pickup_date` | the BOL's pickup date | no (cross-check) |
| Carrier | `carrier_name` | the BOL's carrier | no |
| Origin, destination | `origin`, `destination` | the BOL's | no |
| Agreed freight charge | `freight_charge` and `currency` | the rate confirmation's agreed rate and currency | no |

Values are compared in one form: text ignores case and spacing, dates are days, money is rounded to the cent.
The screen shows both values as written.

## 3. States

| State | Meaning | Open |
|---|---|---|
| `agrees` | the same value | no |
| `not_compared` | the TMS states nothing (or the shipment is not delivered yet) | no |
| `conflict` | the values differ and nobody has decided | **yes** |
| `document_missing` | only the TMS states it: the documents' value is missing, unreadable or conflicting | **yes** |
| `resolved_document` | an administrator kept the documents' value | no |
| `resolved_tms` | an administrator chose the TMS value: the audit reads it | no |
| `protected` | an approved reviewer correction is in force: the TMS value is shown, never used | no |

**An open difference changes no result.** The audit keeps reading the documents. The field's recorded input
carries the warning `tms_conflict` and the TMS value beside it.

## 4. Deciding

An administrator (`extraction.approve`, signed in) decides an open difference, with a reason of at least 10
characters:

- **Keep the documents' value:** for any field. The audit is unchanged and the difference is closed.
- **Use the TMS value:** only for expected pieces and the actual delivery date, the fields a calculation
  reads. The audit then reads the TMS value, recorded as source `tms_resolution`, with the record and the
  decision beside it.

A decision names the record and the two values it was made on. It is refused (409) when:

- either value changed since it was shown;
- the same difference was already decided;
- the field is protected by an approved correction;
- several records match.

Decisions are append-only (the integration store's `tms_resolutions`, schema version 3). **A decision applies
only while the record, its TMS value and the documents' value are the ones it was made on.** A sync that brings
another value, or a changed document, reopens the difference; the old decision stays in the history.

## 5. Correction protection

- **An approved correction always wins.** A field with an approved reviewer correction is `protected`: the TMS
  value is never read and cannot be chosen, even when it was chosen earlier.
- **A value typed by hand in LogiDoc is never chosen.** An administrator can correct LogiDoc's copy of a TMS
  record (`record_overrides`, one person, kept across syncs). Such a value is shown, marked "corrected by hand",
  but can never be chosen into a calculation. Otherwise one person could change an audited value without the
  second approval a correction needs. Propose a document correction instead.
- **Syncs never override a person's decision silently.** A changed TMS value reopens the difference, and the
  audit goes back to the documents until someone decides again.

## 6. Recording

A chosen TMS value changes what the audit reads at once. The audit history records it only through the
**controlled re-audit** ([CORRECTION_AWARE_AUDIT.md](CORRECTION_AWARE_AUDIT.md), section 10).

- The recorded run's inputs keep the TMS record beside each compared field (`run_inputs.tms`, history schema
  version 6): integration, record, change time, sync run, value, state and the decision used.
- The snapshot digest includes the decisions used (`applied_tms`, only when there are some, so earlier history
  does not look changed).

## 7. API

| Method and path | Who | Answer |
|---|---|---|
| `GET /api/shipments/{id}/tms` | reviewers | `TmsShipmentView`: the record, each comparison, the decision history |
| `POST /api/shipments/{id}/tms/resolutions` | administrators | 201 `TmsResolutionResult` (`re_audit`: `required` when the audit now reads another value); 409, 422 as above |
| `GET /api/tms/conflicts` | reviewers | `TmsConflictQueue`: every open difference |

Body of a decision: `{"field", "choice": "document" | "tms", "reason", "record_id", "tms_value",
"document_value"}`, with the two values exactly as the comparison returned them.

## 8. Limitations

- Matching by reference only. A TMS that holds another reference (a PRO number) for the shipment matches nothing.
- Only two fields can be chosen into a calculation. The cross-checks are reported, not consumed.
- Deciding is one administrator's decision with a reason (a choice between two received values), not two-person.
  A value someone typed can never be chosen.
- Only the sandbox TMS exists; no real TMS was connected.
