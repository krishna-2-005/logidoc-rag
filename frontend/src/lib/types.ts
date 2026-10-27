/**
 * The shapes the API returns (src/api). They are read-only views: the
 * interface displays what the audit decided and never recalculates it.
 * A value the audit does not have is null, and is shown as "n/a", never 0.
 */

export type Role = "admin" | "reviewer"

export interface Session {
  mode: "local" | "required"
  authenticated: boolean
  user: { username: string; role: Role; label: string } | null
  permissions: string[]
}

export type FinalStatus = "DISCREPANCIES DETECTED" | "NEEDS REVIEW" | "NO DISCREPANCIES" | "NOT RECONCILED" | string

export interface Kpis {
  identified: number
  reconciled: number
  with_discrepancies: number
  needs_review: number
  no_discrepancies: number
  invoice_total: number | null
  invoice_total_covered: number
  invoice_variance: number | null
  invoice_variance_net: number | null
  invoice_variance_covered: number
  discrepancy_rate: number | null
}

export interface ExposureSummary {
  invoices: number
  verified: number
  mismatches: number
  unverifiable: number
  totals_read: number
  totals_missing: number
  stated_total: number | null
  net_difference: number | null
  absolute_variance: number | null
  amount_under_review: number | null
  unverifiable_without_total: number
  mixed_currency: boolean
  currencies: string[]
}

export interface ReviewCounts {
  open: number
  in_review: number
  resolved: number
  dismissed: number
}

export interface Overview {
  empty: boolean
  message?: string
  kpis: Kpis
  metric_definitions: Record<string, string>
  status_counts: Record<string, number>
  category_counts: Record<string, number>
  carriers: { carrier: string; shipments: number; with_discrepancies: number }[]
  exposure: ExposureSummary
  exposure_note: string
  completeness: { ready: number; incomplete: number; unusable_files: number; unidentified_files: number }
  reviews: ReviewCounts
  open_ingestion_errors: number
  unreadable_files: { filename: string; status: string; error: string | null }[]
  unidentified_sources: string[]
}

export interface ShipmentRow {
  shipment_id: string
  shipment_key: string
  reconciled: boolean
  error: string | null
  carrier: string | null
  origin: string | null
  destination: string | null
  quantity_status: string | null
  delivery_status: string | null
  invoice_status: string | null
  final_status: FinalStatus
  invoice_total: number | null
  invoice_difference: number | null
  discrepancy_count: number
  review_count: number
  discrepancy_categories: string[]
  needs_review: boolean
}

export interface ShipmentList {
  shipments: ShipmentRow[]
  total: number
  carriers: string[]
  statuses: string[]
  categories: string[]
}

export interface Evidence {
  source?: string
  page?: number | null
  document_type?: string
  text?: string
  field?: string
  [key: string]: unknown
}

export interface QuantityAudit {
  bol: number | null
  pod: number | null
  difference: number | null
  status: string
  reason: string
  evidence: Evidence[]
}

export interface DeliveryAudit {
  expected: string | null
  actual: string | null
  status: string
  days_late: number | null
  reason: string
  evidence: Evidence[]
}

export interface InvoiceAudit {
  base_freight: number | null
  fuel_surcharge: number | null
  detention_charge: number | null
  other_charges: number | null
  total: number | null
  line_items: [string, number | null][]
  line_item_sum: number | null
  difference: number | null
  breakdown: string | null
  status: string
  reason: string
  evidence: Evidence[]
}

export interface ReconciliationResult {
  success: boolean
  error?: string
  shipment_id?: string
  final_status?: FinalStatus
  carrier?: { bol: string | null; invoice: string | null; pod: string | null; bol_invoice_status: string; bol_pod_status: string }
  route?: { origin: string | null; destination: string | null; invoice_origin: string | null; invoice_destination: string | null; origin_status: string; destination_status: string }
  quantity?: QuantityAudit
  delivery?: DeliveryAudit
  invoice?: InvoiceAudit
  discrepancies?: { category: string; description: string }[]
  review_items?: { category: string; reason: string }[]
  unreadable_files?: string[]
}

export interface ReviewRow {
  exception_key: string
  shipment_key: string
  shipment_id: string
  category: string
  status: "open" | "in_review" | "resolved" | "dismissed"
  assignee: string | null
  assigned_at: string | null
  evidence_refs: string[]
  approval_state: "pending" | "approved" | "rejected" | null
  approval_by: string | null
  resolved_by: string | null
  needs_recheck: number
  absent_since: string | null
  created_at: string
  updated_at: string
  note: string | null
  age_days?: number
}

export type WorkflowAction = "assign" | "start" | "note" | "resolve" | "dismiss" | "reopen" | "approve" | "reject"

export interface ExceptionListItem extends ReviewRow {
  detected: boolean
  description: string | null
  severity: string | null
  kind: string | null
  allowed_actions: WorkflowAction[]
}

export interface ExceptionList {
  exceptions: ExceptionListItem[]
  total: number
  assignees: string[]
  categories: string[]
  statuses: string[]
}

export interface ReviewEvent {
  id: number
  exception_key: string
  event_type: string
  from_status: string | null
  to_status: string
  actor: string | null
  actor_role: string | null
  authenticated: number
  assignee: string | null
  note: string | null
  evidence_refs: string[]
  created_at: string
  shipment_id?: string
  category?: string
}

export interface ExceptionDetail {
  exception_key: string
  shipment_id: string
  category: string
  kind: string
  severity: string
  description: string
  evidence: string[]
  review: ReviewRow
  allowed_actions: WorkflowAction[]
  history: ReviewEvent[]
}

export interface EvidenceRow {
  source: string | null
  page: number | null
  document_type: string | null
  shipment_id: string | null
  text: string | null
}

/** The deterministic explanation of one exception (never language-model text). */
export interface Explanation {
  exception_key: string
  shipment_id: string
  category: string
  kind: string
  severity: string
  finding: string
  expected: string | null
  observed: string | null
  difference_text: string | null
  rule: string | null
  reason: string | null
  conflicting: boolean
  sources: EvidenceRow[]
  evidence_note: string | null
  excluded_foreign_evidence: number
  next_step: string | null
  caveat: string | null
}

export interface DocumentFile {
  filename: string
  status: string
  status_text: string
  document_type: string | null
  shipment_id: string | null
  shipment_key: string | null
  total_pages: number | null
  text_pages: number | null
  size: number
  sha256: string
  error: string | null
  references: { shipment_id: string; audit: string; page: number | null; document_type: string | null; text: string | null }[]
  ingestion: { status: string; code: string | null; message: string | null; attempts: number; updated_at: string } | null
}

export interface CompletenessRow {
  shipment_id: string
  shipment_key: string
  readiness: "ready" | "incomplete"
  documents: number
  indexed_documents: number | null
  files: string[]
  by_type: Record<string, string[]>
  missing: string[]
  warnings: string[]
  next_step: string
}

export interface Completeness {
  shipments: CompletenessRow[]
  unidentified: { filename: string; document_type: string | null; message: string }[]
  unusable: { filename: string; status: string; message: string; error: string | null; pages: number | null; pages_with_text: number | null }[]
  duplicates: string[][]
}

export interface DocumentsPayload {
  files: DocumentFile[]
  completeness: Completeness
}

export interface Snapshot {
  id: number
  shipment_id: string
  created_at: string
  final_status: string | null
  discrepancy_count: number
  review_count: number
  carrier: string | null
  [key: string]: unknown
}

export interface Findings {
  finding_key: string
  category: string
  summary: string
  [key: string]: unknown
}

export interface SnapshotChanges {
  first: boolean
  input_changed: boolean
  status_change: [string | null, string | null] | null
  new: Findings[]
  reappeared: Findings[]
  persistent: Findings[]
  changed: Findings[]
  resolved: Findings[]
  unverified: Findings[]
  value_changes: { field: string; label: string; previous: unknown; current: unknown }[]
  state_changes: { audit: string; previous: string; current: string }[]
}

export interface TimelineEntry {
  snapshot: Snapshot & { runs: { run_id: number; created_at: string }[]; findings: Findings[] }
  changes: SnapshotChanges
}

export interface ShipmentDetail {
  shipment_id: string
  shipment_key: string
  result: ReconciliationResult
  exceptions: (ExceptionListItem | (Record<string, unknown> & { exception_key: string; category: string; description: string; review_status: string }))[]
  explanations: Explanation[]
  documents: DocumentFile[]
  history: TimelineEntry[]
}

export interface Proportion {
  count: number
  n: number
  rate: number | null
  low: number | null
  high: number | null
}

export interface VarianceGroup {
  currency: string
  invoices_checked: number
  mismatches: number
  net: number | null
  absolute: number | null
  unknown_amount: number
}

export interface Figures {
  shipments: number
  delay: Proportion
  shortage: Proportion
  invoice_mismatch: Proportion
  discrepancy: Proportion
  needs_review: Proportion
  average_days_late: { value: number | null; n: number }
  variance: VarianceGroup[]
}

export interface Scorecard extends Figures {
  carrier: string
  sufficient: boolean
  rank: number | null
}

export interface Trends {
  empty: boolean
  insufficient_reason: string | null
  observations: number
  shipments: number
  period: string
  series: (Figures & { period: string })[]
  carriers: { carriers: Scorecard[]; ranked: number; notes: string[]; minimum_sample: number }
  categories: { category: string; shipments_affected: number; audit_observations: number; shipments_repeating: number; carriers: string[] }[]
  notes: string[]
  definitions: Record<string, string>
  options: { carriers: string[]; statuses: string[]; categories: string[]; first: string | null; last: string | null }
}

/** One invoice of GET /api/exposure */
export interface InvoiceExposure {
  shipment_id: string
  shipment_key: string
  carrier: string | null
  invoice_status: string | null
  classification: "verified" | "mismatch" | "unverifiable" | string
  stated_total: number | null
  line_item_sum: number | null
  signed_difference: number | null
  absolute_difference: number | null
  amount_under_review: number | null
  currency: string | null
  discrepancy_categories: string[]
}

export interface Exposure {
  rows: Record<string, unknown>[]
  summary: ExposureSummary
  by_carrier: { carrier: string; [key: string]: unknown }[]
  by_category: { category: string; [key: string]: unknown }[]
  metric_definitions: Record<string, string>
  note: string
}

export interface RunSupersedes {
  shipment_key: string
  old_run_id: number
  correction_ids: number[]
}

export interface RunSupersededBy {
  shipment_key: string
  new_run_id: number
  correction_ids: number[]
}

export interface AuditRun {
  id: number
  created_at: string
  label: string | null
  shipments_checked: number
  snapshots_written: number
  /** null for a run recorded before inputs were recorded */
  calculation_version: string | null
  supersedes: RunSupersedes[]
  superseded_by: RunSupersededBy[]
}

// ---------------------------------------------------------------------------
// Structured extraction, corrections and recorded audit inputs (src/api/routes_extraction.py)
// ---------------------------------------------------------------------------

export type FieldState = "EXTRACTED" | "MISSING" | "UNPARSEABLE" | "UNREADABLE" | "UNSUPPORTED" | "CONFLICTING"

export interface FieldAlternative {
  value: unknown
  page: number | null
  /** "unparseable" for an occurrence that could not be read */
  problem?: string
}

export interface ExtractedField {
  name: string
  state: FieldState
  /** typed: number, ISO date text, text, {name, address_lines}, {amount, unit}; null unless EXTRACTED */
  value: unknown
  /** always null in stored extractions: the document text is never kept */
  raw_text: string | null
  source_file: string | null
  /** null when it is not known: never guessed */
  page: number | null
  method: string | null
  validation: "valid" | "warning" | "invalid" | "not_checked"
  basis: string | null
  warnings: string[]
  alternatives: FieldAlternative[]
  needs_review: boolean
}

export interface ExtractedLineItem {
  description: string
  amount: number
  quantity: number | null
  rate: number | null
  page: number | null
  warnings: string[]
}

export interface ExtractionPayload {
  filename: string
  document_type: string
  status: "extracted" | "partial" | "needs_ocr" | "unreadable"
  pages: number | null
  pages_without_text: number[]
  extractor_version: string
  fields: Record<string, ExtractedField>
  line_items: ExtractedLineItem[]
  unclassified_amounts: { label: string; page: number | null; reason: string }[]
  excluded_amounts: { label: string; page: number | null; reason: string }[]
  /** charges stated as a percentage with no basis: no amount is calculated and the invoice needs review */
  percentage_charges?: { label: string; percent: number; page: number | null }[]
  warnings: string[]
  review_reasons: string[]
  error: string | null
}

export interface ExtractionSummary {
  id: number
  filename: string
  sha256: string
  extractor_version: string
  document_type: string
  status: ExtractionPayload["status"]
  shipment_id: string | null
  shipment_key: string | null
  created_at: string
  correction_count: number
  pending_count: number
  /** names this content was seen under; null while the store awaits its schema upgrade */
  alias_count?: number | null
  has_type_conflict?: boolean | null
}

export interface ExtractionAlias {
  id: number
  filename: string
  inferred_type: string
  relation: "original" | "same_type" | "type_conflict"
  first_seen_at: string
  seen_by: string | null
}

export interface ExtractionList {
  extractions: ExtractionSummary[]
  extractor_version: string
}

export type CorrectionStatus = "legacy" | "pending" | "approved" | "rejected"

export interface Correction {
  id: number
  extraction_id: number
  field_name: string
  previous: { value: unknown; state: FieldState | string; source: string }
  corrected: unknown
  reason: string
  actor: string
  authenticated: boolean
  at: string
  /** the correction of this field the proposer saw; null when there was none */
  based_on: number | null
  legacy: boolean
  status: CorrectionStatus
  decision_id?: number | null
  decision?: "approved" | "rejected" | null
  decision_note?: string | null
  decided_by?: string | null
  decided_at?: string | null
  filename?: string
  document_type?: string
  shipment_key?: string | null
  extractor_version?: string
}

export interface EffectiveField {
  extracted: { value: unknown; state: FieldState; page: number | null }
  proposed: Correction | null
  approved: Correction | null
  /** what a further proposal must name as based_on */
  latest_correction_id: number | null
  /** what an audit may consume: the approved correction, else the extracted value */
  value: unknown
  state: FieldState
  source: "extracted" | "approved_correction"
  page: number | null
  correction_id: number | null
}

export interface ExtractionDetail {
  id: number
  filename: string
  sha256: string
  extractor_version: string
  document_type: string
  status: ExtractionPayload["status"]
  shipment_id: string | null
  shipment_key: string | null
  created_at: string
  payload: ExtractionPayload
  effective: Record<string, EffectiveField>
  corrections: Correction[]
  limitations: string[]
  aliases?: ExtractionAlias[]
  /** false while the store awaits its schema upgrade (no alias table yet) */
  aliases_available?: boolean
}

export interface PendingList {
  pending: Correction[]
}

export interface DecisionResult {
  decision_id: number
  correction_id: number
  decision: "approved" | "rejected"
  note: string | null
  actor: string
  at: string
  proposed_by: string
  extraction_id: number
  field_name: string
  document_type: string
  filename: string
  shipment_key: string | null
  corrected: unknown
  recompute: {
    status: "not_needed" | "recorded" | "failed" | "no_recorded_history" | "no_calculation_consumes_this_field" | "no_shipment" | "not_authorized"
    run_id?: number | null
    shipments?: string[]
    message?: string
  }
}

export interface StaleShipment {
  shipment_key: string
  recorded_run_id: number
  missing_correction_ids: number[]
  stale_run_ids: number[]
}

export interface StoreMigration {
  attempt: "none" | "failed" | "unfinished" | "succeeded" | "unverified"
  /** a fixed code, never free text */
  code: string | null
  at: string | null
  record: "ok" | "missing" | "ignored" | "unreadable"
}

export interface Staleness {
  stale: StaleShipment[]
  /** false while the correction store is damaged or from a newer version */
  corrections_available: boolean
  /** shipments whose stored extractions come from another extractor version than the current one */
  outdated_cross_checks?: { shipment_key: string; shipment_id: string; fields: string[]; extractor_versions: string[]; current_version: string }[]
  /** what the extraction store file is: absent, older, migration_pending, current, unavailable or migration_failed */
  store?: {
    state: "absent" | "older" | "migration_pending" | "current" | "unavailable" | "migration_failed"
    version: number | null
    reason: string
    /**
     * The migration command's durable status record, reconciled by the server with the database (the database decides).
     * attempt "failed" and "unfinished" are only ever sent for a store whose history the record was written for.
     */
    migration?: StoreMigration | null
  }
}

export interface RecomputeResult {
  recorded: boolean
  run_id: number | null
  shipments: string[]
}

export interface IntegrityReport {
  ok: boolean
  corrections_checked: number
  decisions_checked: number
  extractions_checked: number
  unchained_legacy: number
  problems: string[]
}

export interface RunInput {
  id: number
  run_id: number
  shipment_key: string
  shipment_id: string
  field_name: string
  /** typed value; null when unresolved */
  value: unknown
  source: "legacy_reader" | "approved_correction"
  resolved: boolean
  state: string
  correction_id: number | null
  extraction_id: number | null
  extractor_version: string | null
  document: string | null
  /** null when it is not known */
  page: number | null
  validation: string | null
  warnings: string[]
  consumed_by: string[]
  structured: { extraction_id: number; state: string; value: unknown; agrees: boolean | null } | null
  calculation_version: string
}

export interface RunInputs {
  run_id: number
  inputs: RunInput[]
  /** false for a run recorded before inputs were recorded (nothing is reconstructed) */
  recorded: boolean
  superseded_by: Record<string, number | null>
}

export interface IngestionRecord {
  id: number
  filename: string
  status: "queued" | "processing" | "indexed" | "partial" | "failed" | "unsupported"
  code: string | null
  message: string | null
  action: string | null
  document_type: string | null
  shipment_id: string | null
  pages: number | null
  text_pages: number | null
  chunks_indexed: number | null
  attempts: number
  resolved: number
  actor: string | null
  updated_at: string
  can_retry?: boolean
}

export interface IngestionRegister {
  files: IngestionRecord[]
  summary: Record<string, number>
  actions: { at: string; action: string; filename: string; detail: string | null; actor: string | null }[]
}

export interface UploadSummary {
  indexed: number
  partial: number
  duplicates: number
  failed: number
  unsupported: number
  corpus_changed: boolean
  errors: string[]
  files: IngestionRecord[]
}

export interface Settings {
  auth_mode: string
  resolution_needs_approval: boolean
  minimum_note_length: number
  resolution_needs_evidence: boolean
  upload_limit_mb: number
  max_files_per_upload: number
  language_model: string
  embedding_model: string
  ocr: string
  currency_handling: string
  editable_here: boolean
}

export interface UserAccount {
  username: string
  role: Role
  active: number
  locked_until: string | null
  created_at: string
  password_changed_at: string
}

export interface ChatReply {
  answer: string
  status: string | null
  answer_method: string | null
  scope: unknown
  evidence: Evidence[]
  generated: boolean
  notice: string
}

export interface AssistantReply {
  status: "answered" | "insufficient_evidence" | "clarification" | "refused"
  shipment_id: string | null
  answer: string
  findings: { label: string; value: string | number | null; status: string }[]
  citations: { source: string | null; page: number | null; document_type: string | null; shipment_id: string | null; text: string | null }[]
  uncertainty: string[]
  next_steps: string[]
  tools_used: { name: string; arguments: Record<string, unknown> }[]
  generated: boolean
}
