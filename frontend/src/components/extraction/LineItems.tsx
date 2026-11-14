import { Pencil, RefreshCw } from "lucide-react"
import { useRef, useState, type FormEvent } from "react"
import { errorMessage } from "@/components/common/states"
import { MIN_REASON } from "@/components/extraction/CorrectionDialog"
import { CorrectionBadge, pageText, valueText } from "@/components/extraction/fields"
import { useStoreWriteBlock } from "@/components/extraction/storeState"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input, Label, Textarea } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useExtraction, useProposeLineItem } from "@/hooks/queries"
import { ApiError } from "@/lib/api"
import { dateTime } from "@/lib/format"
import type { Correction, ExtractionDetail, LineItemView } from "@/lib/types"

const money = (value: unknown) => valueText("invoice_total", value)

const amountOf = (value: unknown): unknown => (value && typeof value === "object" ? (value as { amount?: unknown }).amount : value)

/**
 * Proposes a corrected amount for one charge of an invoice. Like any correction
 * it is a proposal: nothing changes until a different administrator approves
 * it, and its effect on the recorded history is then recorded by a controlled
 * re-audit of the shipment. The version it is based on is fixed when the
 * dialog opens and only changes when the reviewer reloads after a conflict.
 */
export function LineItemDialog({ detail, line, onClose }: { detail: ExtractionDetail; line: LineItemView; onClose: () => void }) {
  const [view, setView] = useState<LineItemView>(line)
  const [amount, setAmount] = useState("")
  const [reason, setReason] = useState("")
  const [errors, setErrors] = useState<{ amount?: string; reason?: string }>({})
  const [problem, setProblem] = useState<{ kind: "stale" | "error"; message: string } | null>(null)
  const [reloaded, setReloaded] = useState(false)
  const [done, setDone] = useState<Correction | null>(null)
  const inFlight = useRef(false)
  const propose = useProposeLineItem()
  const fresh = useExtraction(detail.id)
  const blocked = view.proposed !== null
  const title = `Charge line ${view.index + 1}: ${view.extracted.description}`

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current || done || blocked) return

    const found: { amount?: string; reason?: string } = {}
    if (!amount.trim()) found.amount = "Enter the corrected amount."
    if (reason.trim().length < MIN_REASON) found.reason = `Write why the amount is being corrected (at least ${MIN_REASON} characters).`
    setErrors(found)
    if (found.amount || found.reason) return

    inFlight.current = true
    setProblem(null)
    setReloaded(false)

    try {
      setDone(await propose.mutateAsync({ extractionId: detail.id, index: view.index, amount: amount.trim(), reason: reason.trim(), basedOn: view.latest_correction_id }))
    } catch (error) {
      setProblem({ kind: error instanceof ApiError && error.status === 409 ? "stale" : "error", message: errorMessage(error) })
    } finally {
      inFlight.current = false
    }
  }

  const reload = async () => {
    const result = await fresh.refetch()
    const latest = result.data?.line_items?.find((item) => item.index === view.index)

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
      <DialogContent aria-describedby="line-item-description">
        <DialogHeader>
          <DialogTitle>Propose a corrected amount</DialogTitle>
          <DialogDescription id="line-item-description">
            {title} · {detail.filename}
          </DialogDescription>
        </DialogHeader>

        <dl className="grid gap-3 rounded-md border bg-muted/40 p-3 text-sm">
          <div>
            <dt className="text-xs font-medium text-muted-foreground">Amount read from the invoice</dt>
            <dd className="mt-0.5" data-testid="line-original">
              {money(view.extracted.amount)} <span className="text-xs text-muted-foreground">({pageText(view.extracted.page)})</span>
            </dd>
          </div>
          {view.approved && view.applies && (
            <div>
              <dt className="text-xs font-medium text-muted-foreground">Approved correction in effect</dt>
              <dd className="mt-0.5" data-testid="line-approved">
                {money(view.amount)}
              </dd>
            </div>
          )}
        </dl>

        {done ? (
          <>
            <Alert tone="ok" role="status">
              <div>
                <p className="font-medium">Proposal #{done.id} recorded: awaiting approval.</p>
                <p className="mt-0.5">It has not changed any audit. A different administrator must approve it; its effect is then recorded by a re-audit of the shipment that an administrator previews and confirms.</p>
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
                <p>A correction proposed by {view.proposed.actor} is already awaiting a decision. It must be approved or rejected before another can be proposed.</p>
              </Alert>
            )}

            <p className="text-xs text-muted-foreground">
              Only the amount can be corrected; the charge keeps its description. If approved, the freight audit reads the corrected amount for this line (and the invoice audit, for a charge it reads by label).
            </p>

            <div className="space-y-1.5">
              <Label htmlFor="line-amount">Corrected amount</Label>
              <Input
                id="line-amount"
                value={amount}
                maxLength={60}
                inputMode="decimal"
                onChange={(event) => setAmount(event.target.value)}
                aria-invalid={!!errors.amount}
                aria-describedby={`line-amount-hint${errors.amount ? " line-amount-error" : ""}`}
                disabled={blocked || propose.isPending}
              />
              <p id="line-amount-hint" className="text-xs text-muted-foreground">
                An amount such as 180.00. A credit is written with a minus sign. The currency is not part of the value.
              </p>
              {errors.amount && (
                <p id="line-amount-error" role="alert" className="text-xs font-medium text-bad">
                  {errors.amount}
                </p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="line-reason">Reason (required)</Label>
              <Textarea
                id="line-reason"
                value={reason}
                maxLength={1000}
                onChange={(event) => setReason(event.target.value)}
                aria-invalid={!!errors.reason}
                aria-describedby={errors.reason ? "line-reason-error" : undefined}
                disabled={blocked || propose.isPending}
              />
              {errors.reason && (
                <p id="line-reason-error" role="alert" className="text-xs font-medium text-bad">
                  {errors.reason}
                </p>
              )}
            </div>

            {problem?.kind === "stale" && (
              <Alert tone="warn" role="alert" className="items-start">
                <div className="flex-1">
                  <p className="font-medium">This line changed while you were editing.</p>
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

/**
 * The charges of an invoice: what was read, what is proposed and what is
 * approved for each line, kept apart. A correction is only applied to the line
 * it was made on, while that line still reads as it did.
 */
export function LineItemsTable({ detail, canPropose }: { detail: ExtractionDetail; canPropose: boolean }) {
  const [editing, setEditing] = useState<LineItemView | null>(null)
  const blocked = useStoreWriteBlock()
  const lines = detail.line_items ?? []

  if (lines.length === 0) return null

  return (
    <div className="space-y-2" aria-label="Invoice charges">
      <h3 className="text-sm font-semibold">Charges read from this invoice ({lines.length})</h3>
      <Table aria-label="Invoice charges">
        <TableHeader>
          <TableRow>
            <TableHead>Charge</TableHead>
            <TableHead className="text-right">Read</TableHead>
            <TableHead>Correction</TableHead>
            <TableHead>
              <span className="sr-only">Action</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {lines.map((line) => (
            <TableRow key={line.index} className="align-top" data-testid={`line-${line.index}`}>
              <TableCell className="min-w-[10rem]">
                <p className="font-medium">{line.extracted.description}</p>
                <p className="text-xs text-muted-foreground">
                  Line {line.index + 1} · {pageText(line.extracted.page)}
                </p>
              </TableCell>
              <TableCell className="tabular whitespace-nowrap text-right">{money(line.extracted.amount)}</TableCell>
              <TableCell className="min-w-[14rem] text-sm">
                {line.approved && line.applies && (
                  <p>
                    <Badge tone="ok">Approved</Badge> <span className="tabular font-medium">{money(line.amount)}</span>
                    <span className="block text-xs text-muted-foreground">
                      by {line.approved.decided_by ?? "an administrator"} · {dateTime(line.approved.decided_at ?? null)} · proposed by {line.approved.actor}
                    </span>
                  </p>
                )}
                {line.approved && !line.applies && (
                  <p className="text-xs text-warn">An approved correction ({money(amountOf(line.approved.corrected))}) was made on this file, but this line no longer reads as it did then: it is not applied.</p>
                )}
                {line.proposed && (
                  <p className="mt-1">
                    <CorrectionBadge status="pending" /> <span className="tabular">{money(amountOf(line.proposed.corrected))}</span>
                    <span className="block text-xs text-muted-foreground">Proposed by {line.proposed.actor}: changes nothing until approved.</span>
                  </p>
                )}
                {!line.approved && !line.proposed && <span className="text-xs text-muted-foreground">None</span>}
              </TableCell>
              <TableCell>
                {canPropose && !line.proposed ? (
                  <Button variant="outline" size="sm" onClick={() => setEditing(line)} disabled={blocked !== null} title={blocked ?? undefined} aria-label={`Propose a corrected amount for line ${line.index + 1}, ${line.extracted.description}`}>
                    <Pencil /> Correct amount
                  </Button>
                ) : null}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {editing && <LineItemDialog detail={detail} line={editing} onClose={() => setEditing(null)} />}
    </div>
  )
}
