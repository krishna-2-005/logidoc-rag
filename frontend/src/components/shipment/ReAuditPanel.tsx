import { History } from "lucide-react"
import { useRef, useState, type FormEvent } from "react"
import { Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { errorMessage } from "@/components/common/states"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Label, Textarea } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { can, useReAudit, useReAuditPreview, useSession } from "@/hooks/queries"
import { ApiError } from "@/lib/api"
import { NA, dateTime, money } from "@/lib/format"
import type { FreightImpact, ReAuditFinding, ReAuditPreview } from "@/lib/types"

/** The server refuses a shorter reason. */
export const MIN_RE_AUDIT_REASON = 10

function shown(value: unknown): string {
  if (value === null || value === undefined) return NA
  if (typeof value === "number") return value.toLocaleString("en-US", { maximumFractionDigits: 2 })
  return String(value)
}

function impactRows(before: FreightImpact | null | undefined, after: FreightImpact | null | undefined) {
  return [
    ["Billed above the contract", before?.billed_above_contract, after?.billed_above_contract],
    ["Billed without evidence", before?.without_evidence, after?.without_evidence],
    ["Pending review", before?.pending_review, after?.pending_review],
  ] as const
}

function FindingList({ title, items }: { title: string; items: ReAuditFinding[] }) {
  if (items.length === 0) return null

  return (
    <div>
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title} ({items.length})
      </p>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm">
        {items.map((item) => (
          <li key={item.finding_key} className="break-words">
            {item.summary}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Whether a preview concerns the controlled re-audit at all (it is shown only then). */
export function reAuditRelevant(preview: ReAuditPreview): boolean {
  return preview.needed && (preview.recorded !== null || preview.corrections.line_items.length > 0 || preview.tms_resolutions.length > 0)
}

/**
 * The controlled re-audit of one shipment: what recording it would change
 * against the latest recorded run, then an administrator's confirmation with a
 * reason. Exactly the previewed state is recorded: when the audit changed in
 * between, the server refuses it and the new preview is shown instead.
 */
export function ReAuditPanel({ shipmentId }: { shipmentId: string }) {
  const session = useSession()
  const canView = can(session.data, "history.view")
  const canRecord = can(session.data, "history.record")
  const preview = useReAuditPreview(shipmentId, canView)
  const reAudit = useReAudit()
  const [reason, setReason] = useState("")
  const [reasonError, setReasonError] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [recorded, setRecorded] = useState<string | null>(null)
  const inFlight = useRef(false)

  if (!canView) return null

  if (preview.isError) {
    return (
      <Section title="Re-audit">
        <Alert tone="warn">
          <p>Whether a re-audit is waiting could not be checked: {errorMessage(preview.error)}</p>
        </Alert>
      </Section>
    )
  }

  const data = preview.data

  if (!data || (!reAuditRelevant(data) && !recorded)) return null

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current) return

    if (reason.trim().length < MIN_RE_AUDIT_REASON) {
      setReasonError(`Write why the shipment is re-audited (at least ${MIN_RE_AUDIT_REASON} characters).`)
      return
    }

    setReasonError(null)
    setProblem(null)
    inFlight.current = true

    try {
      const result = await reAudit.mutateAsync({ shipmentId, token: data.token, reason: reason.trim() })
      setRecorded(
        `Re-audit recorded as run #${result.run_id}${result.previous_run_id ? `; it supersedes run #${result.previous_run_id}, which keeps its snapshot and inputs` : ""}.`,
      )
      setReason("")
    } catch (error) {
      setProblem(errorMessage(error))
      if (error instanceof ApiError && error.status === 409) preview.refetch()
    } finally {
      inFlight.current = false
    }
  }

  if (recorded && !data.needed) {
    return (
      <Section title="Re-audit">
        <Alert tone="ok" role="status">
          <p>{recorded}</p>
        </Alert>
      </Section>
    )
  }

  const { findings } = data
  const anyFinding = findings.new.length + findings.resolved.length + findings.changed.length + findings.unverified.length > 0

  return (
    <Section
      title="Re-audit"
      description="Approved charge corrections and TMS values chosen by an administrator are recorded in the audit history only by a re-audit that an administrator previews and confirms."
    >
      <Card className="space-y-5 p-4" data-testid="re-audit">
        <div className="flex items-start gap-3">
          <History className="mt-0.5 size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <p className="text-sm">
            {data.recorded
              ? `The latest recorded run (#${data.recorded.run_id}, ${dateTime(data.recorded.recorded_at)}) differs from what the audit reads now.`
              : "Nothing has been recorded for this shipment yet: confirming records its first run."}{" "}
            Recording changes nothing in the documents; earlier runs keep their snapshots.
          </p>
        </div>

        <Table aria-label="Recorded and current result">
          <TableHeader>
            <TableRow>
              <TableHead>What</TableHead>
              <TableHead>Recorded</TableHead>
              <TableHead>Now</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow>
              <TableCell>Final status</TableCell>
              <TableCell>{data.recorded ? <StatusBadge status={data.recorded.final_status} /> : NA}</TableCell>
              <TableCell>
                <StatusBadge status={data.current.final_status} />
              </TableCell>
            </TableRow>
            <TableRow>
              <TableCell>Freight audit</TableCell>
              <TableCell>{data.recorded?.freight_outcome ? <StatusBadge status={data.recorded.freight_outcome} /> : NA}</TableCell>
              <TableCell>{data.current.freight_outcome ? <StatusBadge status={data.current.freight_outcome} /> : NA}</TableCell>
            </TableRow>
            {impactRows(data.recorded?.financial_impact, data.current.financial_impact).map(([name, before, after]) => (
              <TableRow key={name}>
                <TableCell>{name}</TableCell>
                <TableCell className="tabular">{data.recorded ? money(before, data.recorded.financial_impact?.currency) : NA}</TableCell>
                <TableCell className="tabular">{money(after, data.current.financial_impact?.currency)}</TableCell>
              </TableRow>
            ))}
            {data.value_changes.map((change) => (
              <TableRow key={change.field} data-testid={`change-${change.field}`}>
                <TableCell>{change.label}</TableCell>
                <TableCell className="tabular">{shown(change.previous)}</TableCell>
                <TableCell className="tabular font-medium">{shown(change.current)}</TableCell>
              </TableRow>
            ))}
            {data.state_changes.map((change) => (
              <TableRow key={change.audit}>
                <TableCell>{change.audit} audit state</TableCell>
                <TableCell>{change.previous}</TableCell>
                <TableCell className="font-medium">{change.current}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {anyFinding && (
          <div className="grid gap-3 sm:grid-cols-2">
            <FindingList title="New findings" items={findings.new} />
            <FindingList title="Resolved findings" items={findings.resolved} />
            <FindingList title="Changed findings" items={findings.changed} />
            <FindingList title="No longer verified" items={findings.unverified} />
          </div>
        )}

        <div className="flex flex-wrap gap-2 text-xs">
          {data.corrections.added.map((id) => (
            <Badge key={id} tone="info">
              {data.corrections.line_items.includes(id) ? "Charge correction" : "Correction"} #{id}
            </Badge>
          ))}
          {data.tms_resolutions.map((id) => (
            <Badge key={`tms-${id}`} tone="info">
              TMS decision #{id}
            </Badge>
          ))}
        </div>

        {data.open_tms_conflicts > 0 && (
          <Alert tone="warn">
            <p>{data.open_tms_conflicts} TMS difference(s) of this shipment are still undecided. The re-audit records the documents' values for them.</p>
          </Alert>
        )}

        {canRecord ? (
          <form onSubmit={submit} className="space-y-3" noValidate>
            <div className="space-y-1.5">
              <Label htmlFor="re-audit-reason">Reason (required)</Label>
              <Textarea
                id="re-audit-reason"
                value={reason}
                maxLength={500}
                onChange={(event) => setReason(event.target.value)}
                aria-invalid={!!reasonError}
                aria-describedby={reasonError ? "re-audit-reason-error" : undefined}
                disabled={reAudit.isPending}
              />
              {reasonError && (
                <p id="re-audit-reason-error" role="alert" className="text-xs font-medium text-bad">
                  {reasonError}
                </p>
              )}
            </div>
            {problem && (
              <Alert tone="bad" role="alert">
                <p>{problem} The preview above was reloaded: check it before confirming again.</p>
              </Alert>
            )}
            <Button type="submit" disabled={reAudit.isPending || preview.isFetching}>
              {reAudit.isPending ? "Recording…" : "Confirm and record the re-audit"}
            </Button>
          </form>
        ) : (
          <p className="text-xs text-muted-foreground">An administrator who can record audit history confirms the re-audit.</p>
        )}
      </Card>
    </Section>
  )
}
