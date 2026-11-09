"""
Response contracts of the integration-facing endpoints.

The models describe exactly what the endpoints already returned (captured
from the running API before they were written), so adding them changed no
response: every route that uses one is declared with
response_model_exclude_unset=True, which returns the fields the source set
and nothing else, and tests/test_api_contracts.py checks, endpoint by
endpoint, that the JSON with the model equals the JSON without it.

Conventions

    Optional fields are explicit (`X | None`). A value the audit could not
    determine is null, never 0. Counts are integers; amounts are decimal
    numbers in the currency named beside them (a currency that was not stated
    on the documents is null or "Not stated"; nothing is converted). Dates are
    ISO 8601 text. Fields read from documents whose type depends on the field
    (an extracted value: text, a number, a date, a weight object) are JSON
    values.

Errors

    Every error is {"detail": "<message>"} with the HTTP status (401 not
    signed in, 403 not allowed, 404 not found, 409 conflict, 422 a rule or a
    value was refused, 503 a store or the model service is not usable, 500
    an unexpected error with no stack trace or path). Request validation
    errors use the same shape (api/app.py joins them into one message).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """Base of every response model (unknown fields are ignored, never invented)."""

    model_config = ConfigDict(extra="ignore")


class ErrorResponse(Contract):
    detail: str


ERRORS = {
    401: {"model": ErrorResponse, "description": "Not signed in"},
    403: {"model": ErrorResponse, "description": "Not allowed for this role"},
    404: {"model": ErrorResponse, "description": "Not found"},
    422: {"model": ErrorResponse, "description": "A rule or a value was refused"},
}

ReviewStatus = Literal["open", "in_review", "disputed", "resolved", "dismissed", "waived"]
FreightOutcome = Literal["MATCHED", "MATCHED_WITH_TOLERANCE", "DISCREPANCY", "INSUFFICIENT_EVIDENCE", "DUPLICATE", "PENDING_REVIEW"]


class RateFigure(Contract):
    """count / n; rate is null (never 0) when n is 0; low and high: the 95% Wilson interval."""

    count: int
    n: int
    rate: float | None
    low: float | None
    high: float | None


class FinancialImpact(Contract):
    amount: float
    currency: str | None
    basis: str


# ============================================================
# CARRIER INTELLIGENCE
# ============================================================

class CarrierMoney(Contract):
    currency: str
    invoices_with_total: int
    invoiced_total: float | None
    invoices_compared_with_contract: int
    contracted_total: float | None
    audited_billed_total: float | None
    verified_variance: float | None
    average_invoice_variance: float | None
    cost_per_shipment: float | None


class DeliveryFigures(Contract):
    completed: int
    measured: int
    on_time: int
    late: int
    on_time_rate: RateFigure
    average_delay_days: float | None
    not_measured: dict[str, int]


class FinancialFigures(Contract):
    by_currency: list[CarrierMoney]
    invoices: int
    invoices_total_not_read: int


class DocumentFigures(Contract):
    shipments: int
    complete: int
    missing_bol: int
    missing_invoice: int
    missing_pod: int
    completeness_rate: RateFigure


class AuditFigures(Contract):
    invoices: int
    audited_against_contract: int
    without_contract: int
    matched: int
    discrepant: int
    pending_review: int
    insufficient_evidence: int
    duplicate: int
    audit_pass_rate: RateFigure
    arithmetic_checked: int
    arithmetic_match: int
    arithmetic_mismatch: int
    arithmetic_needs_review: int
    invoice_accuracy_rate: RateFigure


class ExceptionAmount(Contract):
    currency: str
    amount: float
    exceptions: int


class RepeatedException(Contract):
    category: str
    shipments: list[str]


class ExceptionFigures(Contract):
    total: int
    open: int
    by_status: dict[str, int]
    by_category: dict[str, int]
    exceptions_per_shipment: float | None
    repeated: list[RepeatedException]
    amounts: dict[Literal["open", "disputed", "waived", "resolved"], list[ExceptionAmount]]


class CarrierFigures(Contract):
    shipments: int
    delivery: DeliveryFigures
    financial: FinancialFigures
    documents: DocumentFigures
    audit: AuditFigures
    exceptions: ExceptionFigures


class InvoicedMonth(Contract):
    currency: str
    total: float
    invoices: int


class CarrierMonth(Contract):
    month: str
    shipments: int
    measured: int
    on_time: int
    late: int
    on_time_rate: RateFigure
    invoiced: list[InvoicedMonth]
    arithmetic_mismatches: int
    contract_discrepancies: int
    exceptions: int


class Period(Contract):
    start: str | None
    end: str | None


class CarrierCard(CarrierFigures):
    carrier_key: str
    carrier: str
    names: list[str]
    registered: "RegisteredCarrier | None" = None


class ConflictingShipment(Contract):
    shipment_id: str
    names: list[str]


class Unattributed(Contract):
    not_stated: list[str]
    conflicting: list[ConflictingShipment]
    undated_excluded: list[str]


class CarrierIntelligenceResponse(Contract):
    period: Period
    definitions: dict[str, str]
    carriers: list[CarrierCard]
    unattributed: Unattributed
    possible_duplicates: list[list[str]]
    totals: CarrierFigures
    monthly: list[CarrierMonth]


class ShipmentException(Contract):
    exception_key: str
    category: str
    severity: str | None
    review_status: ReviewStatus
    financial_impact: FinancialImpact | None


class CarrierShipmentRow(Contract):
    shipment_id: str
    shipment_key: str
    carrier_key: str | None
    carrier: str
    carrier_names: list[str]
    date: str | None
    date_basis: Literal["pickup", "delivery", "invoice"] | None
    final_status: str | None
    completed: bool
    delivery_status: str | None
    measured: bool
    not_measured_reason: str | None
    days_late: int | None
    invoices: int
    invoice_total: float | None
    currency: str
    arithmetic_status: str | None
    arithmetic_difference: float | None
    has_contract: bool
    freight_outcome: FreightOutcome | None
    contracted: float | None
    contract_variance: float | None
    complete: bool
    missing: list[str]
    exceptions: list[ShipmentException]


class CarrierDetailResponse(CarrierCard):
    identifier: str | None
    contact: str | None
    identity_note: str
    period: Period
    definitions: dict[str, str]
    monthly: list[CarrierMonth]
    shipment_count: int
    offset: int
    limit: int
    shipment_rows: list[CarrierShipmentRow]


# ============================================================
# FREIGHT AUDIT
# ============================================================

class FreightSource(Contract):
    file: str
    page: int | None
    document_type: str
    lines: list[str] | None = None
    corrected: bool | None = None


class FreightEvidence(Contract):
    source: str | None
    page: int | None
    document_type: str | None
    text: str


class FreightCharge(Contract):
    charge: str
    kind: Literal["charge", "total", "arithmetic"]
    rule: str
    expected: float | None
    billed: float | None
    variance: float | None
    currency: str | None
    expected_source: FreightSource | None
    billed_source: FreightSource | None
    outcome: FreightOutcome
    explanation: str
    evidence: list[FreightEvidence]


class FreightCheckValue(Contract):
    document_type: str
    file: str
    page: int | None = None
    value: Any = None
    state: str | None = None


class FreightCheck(Contract):
    check: str
    outcome: FreightOutcome
    explanation: str
    values: list[FreightCheckValue]


class FreightDocuments(Contract):
    contract: list[str]
    execution: list[str]
    invoice: list[str]


class FreightImpact(Contract):
    currency: str | None
    billed_above_contract: float
    without_evidence: float
    pending_review: float


class Tolerance(Contract):
    absolute: float
    percent: float


class CorrectionApplied(Contract):
    correction_id: int
    file: str
    document_type: str
    field: str
    original_state: str
    original_value: Any
    corrected_value: Any
    proposed_by: str | None
    approved_by: str | None
    decided_at: str | None


class CorrectionIgnored(Contract):
    correction_id: int
    file: str
    field: str
    reason: str


class InputField(Contract):
    state: str
    value: Any
    page: int | None
    corrected: bool


class InputLineItem(Contract):
    description: str
    amount: float
    page: int | None


class FreightInput(Contract):
    file: str
    sha256: str
    document_type: str
    status: str
    extractor_version: str
    fields: dict[str, InputField]
    line_items: list[InputLineItem]


class FreightAuditResult(Contract):
    """outcome null: the audit did not run (explanation says why)."""

    outcome: FreightOutcome | None
    explanation: list[str]
    documents: FreightDocuments
    currency: str | None
    charges: list[FreightCharge]
    checks: list[FreightCheck]
    financial_impact: FreightImpact | None
    tolerances: dict[str, Tolerance]
    corrections_applied: list[CorrectionApplied] | None = None
    corrections_ignored: list[CorrectionIgnored] | None = None
    inputs: list[FreightInput] | None = None
    audited_at: str | None = None


class FreightAuditResponse(Contract):
    shipment_id: str
    shipment_key: str
    freight_audit: FreightAuditResult | None


# ============================================================
# AUDIT HISTORY
# ============================================================

class AuditState(Contract):
    state: str
    reason: str | None


class Finding(Contract):
    snapshot_id: int
    finding_key: str
    kind: str
    category: str
    severity: str
    summary: str
    difference: float | None
    unit: str | None
    signature: str


class ChangedFinding(Finding):
    previous_difference: float | None


class RunReference(Contract):
    run_id: int
    created_at: str
    label: str | None


class Snapshot(Contract):
    id: int
    run_id: int
    shipment_key: str
    shipment_id: str
    created_at: str
    input_fingerprint: str
    digest: str
    reconciled: int
    error: str | None
    final_status: str | None
    quantity_status: str | None
    delivery_status: str | None
    invoice_status: str | None
    bol_pieces: int | None
    pod_pieces: int | None
    piece_difference: int | None
    expected_date: str | None
    actual_date: str | None
    days_late: int | None
    invoice_total: float | None
    line_item_sum: float | None
    invoice_difference: float | None
    discrepancy_count: int
    review_count: int
    audit_states: dict[str, AuditState]
    carrier: str | None
    currency: str | None
    findings: list[Finding]
    runs: list[RunReference]
    freight_audit: FreightAuditResult | None


class ValueChange(Contract):
    field: str
    label: str
    previous: Any
    current: Any


class StateChange(Contract):
    audit: str
    previous: str
    current: str


class SnapshotChanges(Contract):
    first: bool
    status_change: list[str | None] | None
    input_changed: bool
    new: list[Finding]
    reappeared: list[Finding]
    changed: list[ChangedFinding]
    persistent: list[Finding]
    resolved: list[Finding]
    unverified: list[Finding]
    value_changes: list[ValueChange]
    state_changes: list[StateChange]


class TimelineEntry(Contract):
    snapshot: Snapshot
    changes: SnapshotChanges


# ============================================================
# EXCEPTIONS
# ============================================================

class ReviewRow(Contract):
    exception_key: str
    shipment_key: str
    shipment_id: str
    category: str
    status: ReviewStatus
    signature: str
    reviewer: str | None
    note: str | None
    needs_recheck: int
    absent_since: str | None
    created_at: str
    updated_at: str
    assignee: str | None
    assigned_at: str | None
    assigned_by: str | None
    evidence_refs: list[str]
    resolved_by: str | None
    resolved_at: str | None
    resolved_authenticated: int | None
    approval_state: Literal["pending", "approved", "rejected"] | None
    approval_by: str | None
    approval_at: str | None
    approval_note: str | None


class ExceptionListItem(ReviewRow):
    age_days: int
    detected: bool
    description: str | None
    severity: str | None
    kind: str | None
    financial_impact: FinancialImpact | None
    recommended_action: str | None
    audit_outcome: FreightOutcome | None
    evidence: list[str]
    invoice_numbers: list[str]
    allowed_actions: list[str]


class ExceptionList(Contract):
    exceptions: list[ExceptionListItem]
    total: int
    assignees: list[str]
    categories: list[str]
    statuses: list[ReviewStatus]


class ReviewEvent(Contract):
    id: int
    exception_key: str
    event_type: str
    from_status: str | None
    to_status: str
    reviewer: str | None
    note: str | None
    signature: str | None
    created_at: str
    actor: str | None
    actor_role: str | None
    authenticated: int | None
    assignee: str | None
    evidence_refs: list[str]
    approval_state: str | None
    prev_hash: str | None
    event_hash: str | None


class ActionResult(Contract):
    review: ReviewRow
    allowed_actions: list[str]


# ============================================================
# CARRIER MASTER DATA
# ============================================================

class RegisteredCarrier(Contract):
    """A carrier's master data: validated identifiers (SCAC letters, MC and USDOT digits) and contact."""

    id: int
    legal_name: str
    trading_name: str | None
    scac: str | None
    mc_number: str | None
    dot_number: str | None
    contact_name: str | None
    contact_email: str | None
    contact_phone: str | None


class RegisteredCarrierRecord(RegisteredCarrier):
    created_by: str
    created_at: str
    updated_at: str
    document_names: list[str]


class LinkRequest(Contract):
    id: int
    name_key: str
    display_name: str
    carrier_id: int | None
    reason: str
    proposed_by: str
    proposed_at: str
    decision: Literal["approved", "rejected"] | None
    decided_by: str | None
    decided_at: str | None
    decision_note: str | None


class CarrierRegistryResponse(Contract):
    carriers: list[RegisteredCarrierRecord]
    requests: list[LinkRequest]
    pending: int


class RegistryEvent(Contract):
    id: int
    at: str
    actor: str
    action: str
    carrier_id: int | None
    request_id: int | None
    before: Any
    after: Any


CarrierCard.model_rebuild()
CarrierIntelligenceResponse.model_rebuild()
CarrierDetailResponse.model_rebuild()


# ============================================================
# INTEGRATION FEED
# ============================================================

class FeedShipment(Contract):
    """One shipment of a recorded run: changed is false when the run found it as the earlier snapshot."""

    shipment_key: str
    shipment_id: str
    snapshot_id: int
    changed: bool
    final_status: str | None
    carrier: str | None
    currency: str | None
    invoice_total: float | None
    discrepancy_count: int
    review_count: int
    freight_outcome: FreightOutcome | None


class FeedRun(Contract):
    run_id: int
    created_at: str
    label: str | None
    shipments_checked: int
    snapshots_written: int
    shipments: list[FeedShipment]


class AuditRunFeed(Contract):
    """Recorded runs after the cursor, oldest first. Resume from next_after; has_more: call again."""

    runs: list[FeedRun]
    next_after: int
    has_more: bool


# ============================================================
# OPERATIONS (administrators: backups, alerts)
# ============================================================

class BackupError(Contract):
    category: str
    message: str


class BackupListed(Contract):
    """A backup in the backup folder. usable: complete, verified when made, manifest intact."""

    backup_id: str
    origin: Literal["manual", "scheduled"]
    created_at: str | None
    status: str
    usable: bool
    incomplete: bool
    label: str | None
    bytes: int | None
    error: BackupError | None
    problems: list[str]


class BackupAttempt(Contract):
    at: str
    outcome: Literal["success", "failed"]
    origin: str
    backup_id: str | None
    category: str | None
    message: str | None


class BackupStatus(Contract):
    last_attempt: BackupAttempt | None = None
    last_success: BackupAttempt | None = None
    last_failure: BackupAttempt | None = None
    consecutive_failures: int = 0


class RetentionPolicy(Contract):
    daily: int
    weekly: int
    monthly: int


class BackupOverview(Contract):
    schedule: str
    running: bool
    retention: RetentionPolicy
    status: BackupStatus
    backups: list[BackupListed]


class BackupStarted(Contract):
    started: bool


class Alert(Contract):
    key: str
    severity: Literal["warning", "critical"]
    message: str
    state: Literal["firing", "resolved"]
    first_seen: str
    last_seen: str


class AlertsView(Contract):
    active: list[Alert]
    recent: list[Alert]
