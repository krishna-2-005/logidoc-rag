import { Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { Alert } from "@/components/ui/alert"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { dateTime, NA } from "@/lib/format"
import type { FreightAudit, FreightCharge, FreightSource } from "@/lib/types"

function money(value: number | null, currency: string | null) {
  if (value === null || value === undefined) return NA
  const text = value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return currency ? `${currency} ${text}` : text
}

function signed(value: number | null, currency: string | null) {
  if (value === null || value === undefined) return NA
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${money(Math.abs(value), currency)}`
}

function where(source: FreightSource | null) {
  if (!source) return null
  return `${source.file}${source.page ? `, page ${source.page}` : ""}`
}

function documentList(names: string[]) {
  return names.length ? names.join(", ") : "none"
}

function shown(value: unknown) {
  if (value === null || value === undefined) return NA
  if (typeof value === "number") return value.toLocaleString("en-US", { maximumFractionDigits: 2, minimumFractionDigits: Number.isInteger(value) ? 0 : 2 })
  if (typeof value === "string") return value
  return JSON.stringify(value)
}

function ChargeRow({ item }: { item: FreightCharge }) {
  return (
    <TableRow>
      <TableCell className="align-top">
        <p className="font-medium">{item.charge}</p>
        <p className="text-xs text-muted-foreground">Rule: {item.rule.replace(/_/g, " ")}</p>
      </TableCell>
      <TableCell className="align-top tabular-nums">
        {money(item.expected, item.currency)}
        {item.expected_source && <p className="text-xs text-muted-foreground">{where(item.expected_source)}</p>}
      </TableCell>
      <TableCell className="align-top tabular-nums">
        {money(item.billed, item.currency)}
        {item.billed_source && <p className="text-xs text-muted-foreground">{where(item.billed_source)}</p>}
      </TableCell>
      <TableCell className="align-top tabular-nums">{signed(item.variance, item.currency)}</TableCell>
      <TableCell className="align-top">
        <StatusBadge status={item.outcome} />
      </TableCell>
      <TableCell className="align-top text-sm">
        {item.explanation}
        {item.evidence.map((evidence, index) => (
          <p key={index} className="mt-1 text-xs text-muted-foreground">
            {evidence.document_type} {evidence.source}
            {evidence.page ? `, page ${evidence.page}` : ""}: “{evidence.text}”
          </p>
        ))}
      </TableCell>
    </TableRow>
  )
}

/**
 * The carrier invoice matched against the rate confirmation (contract) and the
 * BOL/POD (execution), charge by charge, with why each line got its outcome.
 * Nothing here approves, disputes or pays anything.
 */
export function FreightAuditPanel({ audit }: { audit: FreightAudit | null | undefined }) {
  if (!audit) return null

  return (
    <Section
      title="Freight audit: invoice against contract"
      description="The carrier invoice compared with the rate confirmation and the BOL and POD, charge by charge. A finding is for a person to decide on; nothing is approved or paid automatically."
    >
      <FreightAuditBody audit={audit} label="Freight audit" />
    </Section>
  )
}

/** The audit itself, also shown read-only for a recorded audit in the shipment's history */
export function FreightAuditBody({ audit, label }: { audit: FreightAudit; label: string }) {
  const impact = audit.financial_impact
  const applied = audit.corrections_applied ?? []
  const ignored = audit.corrections_ignored ?? []

  return (
    <div className="space-y-4" aria-label={label}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">Outcome:</span>
        {audit.outcome ? <StatusBadge status={audit.outcome} /> : <span className="text-sm">Not audited</span>}
      </div>

      {audit.audited_at && <p className="text-xs text-muted-foreground">Audited {dateTime(audit.audited_at)}</p>}

      <ul className="list-disc space-y-1 pl-5 text-sm" aria-label="Why">
        {audit.explanation.map((reason, index) => (
          <li key={index}>{reason}</li>
        ))}
      </ul>

      <dl className="grid gap-2 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-xs text-muted-foreground">Contract (rate confirmation)</dt>
          <dd>{documentList(audit.documents.contract)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Execution (BOL, POD)</dt>
          <dd>{documentList(audit.documents.execution)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Invoice</dt>
          <dd>{documentList(audit.documents.invoice)}</dd>
        </div>
      </dl>

      {impact && (impact.billed_above_contract > 0 || impact.without_evidence > 0 || impact.pending_review > 0) && (
        <Alert tone="warn">
          <p className="text-sm">
            At stake: {money(impact.billed_above_contract, impact.currency)} billed above the contract,{" "}
            {money(impact.without_evidence, impact.currency)} billed without supporting evidence and{" "}
            {money(impact.pending_review, impact.currency)} waiting for a decision.
          </p>
        </Alert>
      )}

      {audit.charges.length > 0 && (
        <Table aria-label="Charge-level comparison">
          <TableHeader>
            <TableRow>
              <TableHead>Charge</TableHead>
              <TableHead>Contract</TableHead>
              <TableHead>Billed</TableHead>
              <TableHead>Variance</TableHead>
              <TableHead>Outcome</TableHead>
              <TableHead>Why</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {audit.charges.map((item, index) => (
              <ChargeRow key={`${item.charge}-${index}`} item={item} />
            ))}
          </TableBody>
        </Table>
      )}

      {applied.length > 0 && (
        <Table aria-label="Approved corrections used">
          <TableHeader>
            <TableRow>
              <TableHead>Correction</TableHead>
              <TableHead>Field</TableHead>
              <TableHead>Extracted</TableHead>
              <TableHead>Used (approved)</TableHead>
              <TableHead>Approved by</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {applied.map((item) => (
              <TableRow key={item.correction_id}>
                <TableCell>#{item.correction_id}</TableCell>
                <TableCell>
                  {item.field.replace(/_/g, " ")}
                  <p className="text-xs text-muted-foreground">{item.file}</p>
                </TableCell>
                <TableCell>{item.original_state === "EXTRACTED" ? shown(item.original_value) : item.original_state.toLowerCase()}</TableCell>
                <TableCell className="font-medium">{shown(item.corrected_value)}</TableCell>
                <TableCell>
                  {item.approved_by ?? NA}
                  {item.proposed_by && <p className="text-xs text-muted-foreground">proposed by {item.proposed_by}</p>}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {ignored.length > 0 && (
        <Alert tone="info">
          {ignored.map((item) => (
            <p key={item.correction_id} className="text-sm">
              Correction #{item.correction_id} ({item.field.replace(/_/g, " ")} of {item.file}) not used: {item.reason}
            </p>
          ))}
        </Alert>
      )}

      {audit.checks.length > 0 && (
        <Table aria-label="Reference checks">
          <TableHeader>
            <TableRow>
              <TableHead>Check</TableHead>
              <TableHead>Outcome</TableHead>
              <TableHead>Why</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {audit.checks.map((item, index) => (
              <TableRow key={`${item.check}-${index}`}>
                <TableCell className="align-top font-medium">{item.check}</TableCell>
                <TableCell className="align-top">
                  <StatusBadge status={item.outcome} />
                </TableCell>
                <TableCell className="align-top text-sm">{item.explanation}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {(audit.inputs ?? []).length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer font-medium text-primary">Documents this audit read ({(audit.inputs ?? []).length})</summary>
          <ul className="mt-2 space-y-1">
            {(audit.inputs ?? []).map((item) => (
              <li key={item.file} className="rounded bg-muted/70 px-2 py-1 font-mono text-muted-foreground">
                {item.document_type} {item.file} · sha256 {item.sha256.slice(0, 16)}… · extractor {item.extractor_version}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
