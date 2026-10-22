import { useQueryClient } from "@tanstack/react-query"
import { Check, ShieldAlert, X } from "lucide-react"
import { useRef, useState, type FormEvent } from "react"
import { Link } from "react-router-dom"
import { PageHeader } from "@/components/common/PageHeader"
import { EmptyState, ErrorState, LoadingBlock, errorMessage } from "@/components/common/states"
import { MIN_REASON } from "@/components/extraction/CorrectionDialog"
import { CorrectionBadge, consumerOf, valueOrState } from "@/components/extraction/fields"
import { CorrectionStoreWarning, RecomputeStatus } from "@/components/extraction/RecomputeStatus"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label, Textarea } from "@/components/ui/input"
import { can, useDecideCorrection, useExtractions, usePendingCorrections, useRecompute, useSession } from "@/hooks/queries"
import { ApiError } from "@/lib/api"
import { dateTime, label } from "@/lib/format"
import type { Correction, DecisionResult } from "@/lib/types"

type Mode = "approve" | "reject"

interface Outcome {
  key: number
  tone: "ok" | "info" | "warn"
  text: string
  /** the shipment to record the run for again, when recording failed */
  retry: string | null
}

let counter = 0

/** What the server confirmed, in words: nothing is claimed beyond it. */
export function outcomeOf(result: DecisionResult): Outcome {
  const base = { key: ++counter, retry: null as string | null }
  const name = `${label(result.field_name)} of ${result.filename}`

  if (result.decision === "rejected") return { ...base, tone: "info", text: `Correction #${result.correction_id} (${name}) was rejected. It had no effect on any audit.` }

  const approved = `Correction #${result.correction_id} (${name}) was approved.`
  const recompute = result.recompute

  switch (recompute.status) {
    case "recorded":
      return { ...base, tone: "ok", text: `${approved} Audit run #${recompute.run_id} was recorded for ${(recompute.shipments ?? []).join(", ")}; it supersedes the earlier run, which is unchanged.` }
    case "failed":
      return {
        ...base,
        tone: "warn",
        retry: result.shipment_key,
        text: `${approved} The audit already uses it, but the recalculated run could not be recorded. ${recompute.message ?? "Record it again."}`,
      }
    case "no_recorded_history":
      return { ...base, tone: "info", text: `${approved} Nothing has been recorded for this shipment yet, so no earlier run was superseded.` }
    case "no_calculation_consumes_this_field":
      return { ...base, tone: "info", text: `${approved} No audit calculation reads this field, so no run was recorded.` }
    case "no_shipment":
      return { ...base, tone: "info", text: `${approved} The document is not attributed to a shipment, so no run was recorded.` }
    case "not_authorized":
      return { ...base, tone: "info", text: `${approved} You cannot record audit runs: an administrator who can must record the recalculated run.` }
    default:
      return { ...base, tone: "info", text: approved }
  }
}

function OutcomeAlert({ outcome, onDismiss }: { outcome: Outcome; onDismiss: () => void }) {
  const recompute = useRecompute()
  const busy = useRef(false)
  const [retried, setRetried] = useState<{ tone: "ok" | "info" | "bad"; text: string } | null>(null)

  const retry = async () => {
    if (busy.current || !outcome.retry) return
    busy.current = true
    setRetried(null)

    try {
      const result = await recompute.mutateAsync(outcome.retry)
      setRetried(
        result.recorded
          ? { tone: "ok", text: `Recorded run #${result.run_id} for ${result.shipments.join(", ")}.` }
          : { tone: "info", text: "Nothing needed recording: the run had already been recorded." },
      )
    } catch (error) {
      setRetried({ tone: "bad", text: `The run was not recorded: ${errorMessage(error)}` })
    } finally {
      busy.current = false
    }
  }

  const resolved = retried?.tone === "ok" || retried?.tone === "info"

  return (
    <Alert tone={outcome.tone} className="items-start">
      <div className="flex-1">
        <p>{outcome.text}</p>
        {outcome.retry && !resolved && (
          <Button size="sm" variant="outline" className="mt-2 bg-card text-foreground" onClick={retry} disabled={recompute.isPending}>
            {recompute.isPending ? "Recording…" : "Retry recording the run"}
          </Button>
        )}
        {retried && (
          <p className="mt-2 font-medium" role={retried.tone === "bad" ? "alert" : "status"}>
            {retried.text}
          </p>
        )}
      </div>
      <button type="button" aria-label="Dismiss this message" className="text-xs underline" onClick={onDismiss}>
        Dismiss
      </button>
    </Alert>
  )
}

function DecisionDialog({ item, mode, shipmentLabel, onClose, onDone }: { item: Correction; mode: Mode; shipmentLabel: string; onClose: () => void; onDone: (result: DecisionResult) => void }) {
  const client = useQueryClient()
  const decide = useDecideCorrection()
  const [note, setNote] = useState("")
  const [noteError, setNoteError] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const inFlight = useRef(false)
  const consumer = consumerOf(item.document_type ?? "", item.field_name)
  const approving = mode === "approve"

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current) return

    if (!approving && note.trim().length < MIN_REASON) {
      setNoteError(`Write why it is rejected (at least ${MIN_REASON} characters).`)
      return
    }

    setNoteError(null)
    setProblem(null)
    inFlight.current = true

    try {
      const result = await decide.mutateAsync({ id: item.id, decision: mode, note: note.trim() })
      onDone(result)
      onClose()
    } catch (error) {
      setProblem(errorMessage(error))

      // Someone else decided it, or it changed: show the current queue, not a stale one
      if (error instanceof ApiError && (error.status === 409 || error.status === 404)) client.invalidateQueries({ queryKey: ["audit"] })
    } finally {
      inFlight.current = false
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent aria-describedby="decision-description">
        <DialogHeader>
          <DialogTitle>{approving ? "Approve" : "Reject"} correction: {label(item.field_name)}</DialogTitle>
          <DialogDescription id="decision-description">
            {shipmentLabel} · {item.filename}
          </DialogDescription>
        </DialogHeader>

        <p className="break-words text-sm">
          {valueOrState(item.field_name, item.previous.value, item.previous.state)} <span aria-hidden="true">→</span> <strong>{valueOrState(item.field_name, item.corrected, "EXTRACTED")}</strong>
        </p>

        <p className="text-xs text-muted-foreground">
          {approving
            ? consumer
              ? `Approving makes this the value ${consumer} reads for this shipment, and records a recalculated audit run. The original extracted value is kept.`
              : "No audit calculation reads this field: approving is recorded but changes no result. The original extracted value is kept."
            : "Rejecting keeps the original value. The proposal stays in the history and has no effect on any audit."}
        </p>

        <form onSubmit={submit} className="grid gap-4" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="decision-note">{approving ? "Note (optional)" : "Reason for rejecting (required)"}</Label>
            <Textarea
              id="decision-note"
              value={note}
              maxLength={1000}
              onChange={(event) => setNote(event.target.value)}
              aria-invalid={!!noteError}
              aria-describedby={noteError ? "decision-note-error" : undefined}
              disabled={decide.isPending}
            />
            {noteError && (
              <p id="decision-note-error" role="alert" className="text-xs font-medium text-bad">
                {noteError}
              </p>
            )}
          </div>

          {problem && (
            <Alert tone="bad">
              <p>{problem}</p>
            </Alert>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={onClose} disabled={decide.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant={approving ? "default" : "destructive"} disabled={decide.isPending}>
              {decide.isPending ? "Saving…" : approving ? "Approve correction" : "Reject correction"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function QueueItem({ item, shipmentLabel, own, onDecide }: { item: Correction; shipmentLabel: string; own: boolean; onDecide: (mode: Mode) => void }) {
  const id = `correction-${item.id}`

  return (
    <Card className="p-4" aria-labelledby={`${id}-title`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id={`${id}-title`} className="text-sm font-semibold">
            {item.shipment_key ? (
              <Link className="underline-offset-2 hover:underline" to={`/shipments/${encodeURIComponent(item.shipment_key)}`}>
                {shipmentLabel}
              </Link>
            ) : (
              shipmentLabel
            )}{" "}
            · {label(item.field_name)}
          </h3>
          <p className="break-all text-xs text-muted-foreground">
            {item.filename} · {label(item.document_type)} · extraction #{item.extraction_id}
          </p>
        </div>
        <CorrectionBadge status={item.status} />
      </div>

      <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2">
        <div className="border-l-2 border-border pl-2.5">
          <dt className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Original value</dt>
          <dd className="break-words">{valueOrState(item.field_name, item.previous.value, item.previous.state)}</dd>
          <dd className="text-xs text-muted-foreground">{item.previous.source === "approved_correction" ? "an earlier approved correction" : "read from the document"}</dd>
        </div>
        <div className="border-l-2 border-warn pl-2.5">
          <dt className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Proposed value</dt>
          <dd className="break-words font-medium">{valueOrState(item.field_name, item.corrected, "EXTRACTED")}</dd>
          <dd className="text-xs text-muted-foreground">not applied until approved</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Reason</dt>
          <dd className="whitespace-pre-wrap break-words">{item.reason}</dd>
        </div>
        <div>
          <dt className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Proposed by</dt>
          <dd>
            {item.actor} · {dateTime(item.at)}
            {!item.authenticated && <Badge tone="warn" className="ml-2">Not signed in</Badge>}
          </dd>
        </div>
        <div>
          <dt className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Version</dt>
          <dd className="text-xs text-muted-foreground">
            {item.based_on === null ? "Based on the original extraction (no earlier correction)." : `Based on correction #${item.based_on}.`} Extractor {item.extractor_version ?? "unknown"}.
          </dd>
        </div>
      </dl>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={() => onDecide("approve")} disabled={own} aria-describedby={own ? `${id}-own` : undefined}>
          <Check /> Approve
        </Button>
        <Button size="sm" variant="outline" onClick={() => onDecide("reject")} disabled={own} aria-describedby={own ? `${id}-own` : undefined}>
          <X /> Reject
        </Button>
        {own && (
          <p id={`${id}-own`} className="text-xs text-muted-foreground">
            You proposed this correction. A different administrator must approve or reject it.
          </p>
        )}
      </div>
    </Card>
  )
}

export function Corrections() {
  const session = useSession()
  const canApprove = can(session.data, "extraction.approve")
  const pending = usePendingCorrections(canApprove)
  const extractions = useExtractions(undefined, canApprove)
  const [active, setActive] = useState<{ item: Correction; mode: Mode } | null>(null)
  const [outcomes, setOutcomes] = useState<Outcome[]>([])
  const username = session.data?.user?.username

  const header = <PageHeader title="Correction approvals" description="Proposed corrections wait here for a different administrator. Approving one makes it the value an audit reads; rejecting it keeps the original. Nothing changes until then." />

  if (session.isPending) return <>{header}<LoadingBlock rows={3} label="Loading" /></>

  if (!canApprove) {
    return (
      <>
        {header}
        <EmptyState title="Administrators only" icon={<ShieldAlert className="size-5" />}>
          Approving or rejecting corrections is limited to administrators. You can propose a correction from a shipment's Extractions section, and see its state there.
        </EmptyState>
      </>
    )
  }

  const shipmentOf = (item: Correction) => extractions.data?.extractions.find((row) => row.id === item.extraction_id)?.shipment_id ?? item.shipment_key ?? "No shipment"

  return (
    <>
      {header}

      <div className="mb-6 space-y-4">
        <CorrectionStoreWarning />
        <RecomputeStatus />
      </div>

      <div className="space-y-3" aria-live="polite">
        {outcomes.map((outcome) => (
          <OutcomeAlert key={outcome.key} outcome={outcome} onDismiss={() => setOutcomes((current) => current.filter((item) => item.key !== outcome.key))} />
        ))}
      </div>

      <section aria-label="Pending corrections" className="mt-4 space-y-3">
        {pending.isPending && <LoadingBlock rows={3} label="Loading pending corrections" />}
        {pending.isError && <ErrorState error={pending.error} title="The pending corrections could not be loaded" onRetry={() => pending.refetch()} />}
        {pending.data && pending.data.pending.length === 0 && (
          <EmptyState title="No corrections are waiting for approval">When a reviewer proposes a correction, it appears here.</EmptyState>
        )}
        {pending.data?.pending.map((item) => (
          <QueueItem key={item.id} item={item} shipmentLabel={shipmentOf(item)} own={item.actor === username} onDecide={(mode) => setActive({ item, mode })} />
        ))}
      </section>

      {active && (
        <DecisionDialog
          item={active.item}
          mode={active.mode}
          shipmentLabel={shipmentOf(active.item)}
          onClose={() => setActive(null)}
          onDone={(result) => setOutcomes((current) => [outcomeOf(result), ...current])}
        />
      )}
    </>
  )
}
