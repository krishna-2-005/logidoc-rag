import { RefreshCw } from "lucide-react"
import { useRef, useState, type FormEvent } from "react"
import { errorMessage } from "@/components/common/states"
import { HINTS, consumerOf, kindOf, pageText, valueOrState } from "@/components/extraction/fields"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input, Label, Textarea } from "@/components/ui/input"
import { useExtraction, useProposeCorrection } from "@/hooks/queries"
import { ApiError } from "@/lib/api"
import { label } from "@/lib/format"
import type { Correction, EffectiveField, ExtractionDetail } from "@/lib/types"

/** The server refuses a shorter reason (extraction_store.MIN_REASON). */
export const MIN_REASON = 10

type Problem = { kind: "stale" | "error"; message: string }

/**
 * Proposes a correction of one field. A proposal is not an approval: it has no
 * effect on any audit until a different administrator approves it, and this
 * dialog never says otherwise.
 *
 * The version the proposal is based on (the latest correction of the field the
 * reviewer was shown) is fixed when the dialog opens and only changes when the
 * reviewer reloads after a conflict, so a request never silently builds on a
 * state its author has not seen.
 */
export function CorrectionDialog({ detail, field, onClose }: { detail: ExtractionDetail; field: string; onClose: () => void }) {
  const [view, setView] = useState<EffectiveField>(detail.effective[field])
  const [value, setValue] = useState("")
  const [reason, setReason] = useState("")
  const [errors, setErrors] = useState<{ value?: string; reason?: string }>({})
  const [problem, setProblem] = useState<Problem | null>(null)
  const [reloaded, setReloaded] = useState(false)
  const [done, setDone] = useState<Correction | null>(null)
  const inFlight = useRef(false)

  const propose = useProposeCorrection()
  const fresh = useExtraction(detail.id)
  const kind = kindOf(field)
  const consumer = consumerOf(detail.document_type, field)
  const blocked = view.proposed !== null
  const name = label(field)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current || done || blocked) return

    const found: { value?: string; reason?: string } = {}
    if (!value.trim()) found.value = "Enter the corrected value."
    if (reason.trim().length < MIN_REASON) found.reason = `Write why the value is being corrected (at least ${MIN_REASON} characters).`

    setErrors(found)
    if (found.value || found.reason) return

    inFlight.current = true
    setProblem(null)
    setReloaded(false)

    try {
      setDone(await propose.mutateAsync({ extractionId: detail.id, field, value: value.trim(), reason: reason.trim(), basedOn: view.latest_correction_id }))
    } catch (error) {
      setProblem({ kind: error instanceof ApiError && error.status === 409 ? "stale" : "error", message: errorMessage(error) })
    } finally {
      inFlight.current = false
    }
  }

  const reload = async () => {
    const result = await fresh.refetch()
    const latest = result.data?.effective[field]

    if (latest) {
      setView(latest)
      setProblem(null)
      setReloaded(true)
    } else {
      setProblem({ kind: "error", message: "The latest state could not be loaded. Close this dialog and try again." })
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent aria-describedby="correction-description">
        <DialogHeader>
          <DialogTitle>Propose a correction: {name}</DialogTitle>
          <DialogDescription id="correction-description">
            {detail.filename} · {label(detail.document_type)}
          </DialogDescription>
        </DialogHeader>

        <dl className="grid gap-3 rounded-md border bg-muted/40 p-3 text-sm">
          <div>
            <dt className="text-xs font-medium text-muted-foreground">Original value (read from the document)</dt>
            <dd className="mt-0.5 break-words" data-testid="original-value">
              {valueOrState(field, view.extracted.value, view.extracted.state)} <span className="text-xs text-muted-foreground">({pageText(view.extracted.page)})</span>
            </dd>
          </div>
          {view.approved && (
            <div>
              <dt className="text-xs font-medium text-muted-foreground">Approved correction in effect</dt>
              <dd className="mt-0.5 break-words" data-testid="approved-value">
                {valueOrState(field, view.approved.corrected, "EXTRACTED")}
              </dd>
            </div>
          )}
        </dl>

        {done ? (
          <>
            <Alert tone="ok" role="status">
              <div>
                <p className="font-medium">Proposal #{done.id} recorded: awaiting approval.</p>
                <p className="mt-0.5">It has not been approved and has not changed any audit. Another administrator must approve it first; until then the audit keeps using the value it used before.</p>
              </div>
            </Alert>
            <DialogFooter>
              <Button onClick={onClose}>Close</Button>
            </DialogFooter>
          </>
        ) : (
          <form onSubmit={submit} className="grid gap-4" noValidate>
            {blocked && view.proposed && (
              <Alert tone="warn">
                <p>
                  A correction proposed by {view.proposed.actor} is already awaiting a decision. It must be approved or rejected before another can be proposed.
                </p>
              </Alert>
            )}

            <p className="text-xs text-muted-foreground">
              {consumer
                ? `If approved by a different administrator, the approved value replaces the document's value in ${consumer}. Until then nothing changes.`
                : "No audit calculation reads this field yet: an approval is recorded but changes no result."}
            </p>

            <div className="space-y-1.5">
              <Label htmlFor="correction-value">Proposed value</Label>
              <Input
                id="correction-value"
                value={value}
                maxLength={500}
                onChange={(event) => setValue(event.target.value)}
                aria-invalid={!!errors.value}
                aria-describedby={`correction-value-hint${errors.value ? " correction-value-error" : ""}`}
                disabled={blocked || propose.isPending}
              />
              <p id="correction-value-hint" className="text-xs text-muted-foreground">
                {HINTS[kind]}
              </p>
              {errors.value && (
                <p id="correction-value-error" role="alert" className="text-xs font-medium text-bad">
                  {errors.value}
                </p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="correction-reason">Reason (required)</Label>
              <Textarea
                id="correction-reason"
                value={reason}
                maxLength={1000}
                onChange={(event) => setReason(event.target.value)}
                aria-invalid={!!errors.reason}
                aria-describedby={errors.reason ? "correction-reason-error" : undefined}
                disabled={blocked || propose.isPending}
              />
              {errors.reason && (
                <p id="correction-reason-error" role="alert" className="text-xs font-medium text-bad">
                  {errors.reason}
                </p>
              )}
            </div>

            {problem?.kind === "stale" && (
              <Alert tone="warn" role="alert" className="items-start">
                <div className="flex-1">
                  <p className="font-medium">This field changed while you were editing.</p>
                  <p className="mt-0.5">{problem.message}</p>
                  <Button variant="outline" size="sm" className="mt-2 bg-card text-foreground" onClick={reload} disabled={fresh.isFetching}>
                    <RefreshCw /> {fresh.isFetching ? "Loading…" : "Load the latest state"}
                  </Button>
                </div>
              </Alert>
            )}
            {problem?.kind === "error" && (
              <Alert tone="bad">
                <p>{problem.message}</p>
              </Alert>
            )}
            {reloaded && (
              <Alert tone="info" role="status">
                <p>The latest state is loaded and shown above. Check it, then submit again; nothing was sent yet.</p>
              </Alert>
            )}

            <DialogFooter>
              <Button variant="outline" onClick={onClose} disabled={propose.isPending}>
                Cancel
              </Button>
              <Button type="submit" disabled={propose.isPending || blocked}>
                {propose.isPending ? "Submitting…" : "Propose correction"}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
