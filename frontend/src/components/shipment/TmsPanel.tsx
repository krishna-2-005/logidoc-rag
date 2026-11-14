import { Truck } from "lucide-react"
import { useRef, useState, type FormEvent } from "react"
import { Facts } from "@/components/common/Facts"
import { Section } from "@/components/common/PageHeader"
import { EmptyState, errorMessage } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Badge, type Tone } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label, Textarea } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { can, useResolveTms, useSession, useShipmentTms } from "@/hooks/queries"
import { ApiError } from "@/lib/api"
import { NA, dateTime } from "@/lib/format"
import type { TmsComparison, TmsState } from "@/lib/types"

/** The server refuses a shorter reason. */
export const MIN_TMS_REASON = 10

export const TMS_LOOK: Record<TmsState, { tone: Tone; text: string }> = {
  agrees: { tone: "ok", text: "Agrees" },
  not_compared: { tone: "neutral", text: "Not compared" },
  conflict: { tone: "bad", text: "Differs" },
  document_missing: { tone: "warn", text: "Only in the TMS" },
  resolved_document: { tone: "ok", text: "Documents' value kept" },
  resolved_tms: { tone: "info", text: "TMS value used" },
  protected: { tone: "neutral", text: "Approved correction wins" },
}

function text(value: unknown): string {
  if (value === null || value === undefined) return NA
  if (typeof value === "number") return value.toLocaleString("en-US", { maximumFractionDigits: 2 })
  return String(value)
}

function ResolveDialog({ shipmentId, row, recordId, choice, onClose }: { shipmentId: string; row: TmsComparison; recordId: number; choice: "document" | "tms"; onClose: () => void }) {
  const resolve = useResolveTms()
  const toast = useToast()
  const [reason, setReason] = useState("")
  const [reasonError, setReasonError] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const inFlight = useRef(false)
  const usingTms = choice === "tms"

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current) return

    if (reason.trim().length < MIN_TMS_REASON) {
      setReasonError(`Write why (at least ${MIN_TMS_REASON} characters).`)
      return
    }

    setReasonError(null)
    setProblem(null)
    inFlight.current = true

    try {
      const result = await resolve.mutateAsync({
        shipmentId, field: row.field, choice, reason: reason.trim(), recordId, tmsValue: row.tms_value, documentValue: row.document_value,
      })
      toast.success(
        result.re_audit === "required"
          ? `${row.label}: the audit now reads the TMS value. Record it with the re-audit of this shipment.`
          : `${row.label}: the documents' value is kept.`,
      )
      onClose()
    } catch (error) {
      setProblem(error instanceof ApiError && error.status === 409 ? `${errorMessage(error)} Close this dialog to see the current comparison.` : errorMessage(error))
    } finally {
      inFlight.current = false
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent aria-describedby="tms-resolve-description">
        <DialogHeader>
          <DialogTitle>{usingTms ? "Use the TMS value" : "Keep the documents' value"}: {row.label}</DialogTitle>
          <DialogDescription id="tms-resolve-description">{shipmentId}</DialogDescription>
        </DialogHeader>

        <dl className="grid gap-3 rounded-md border bg-muted/40 p-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-xs font-medium text-muted-foreground">Documents</dt>
            <dd className="mt-0.5 font-medium">{row.document_state === "extracted" ? text(row.document_text) : `Not readable (${row.document_state})`}</dd>
          </div>
          <div>
            <dt className="text-xs font-medium text-muted-foreground">TMS</dt>
            <dd className="mt-0.5 font-medium">{text(row.tms_text)}</dd>
          </div>
        </dl>

        <p className="text-xs text-muted-foreground">
          {usingTms
            ? "The audit will read the TMS value for this field instead of the documents'. It applies only while both values stay as they are; the recorded history changes only through a re-audit."
            : "The audit keeps reading the documents. The difference is marked as decided, with your reason, while both values stay as they are."}
        </p>

        <form onSubmit={submit} className="grid gap-4" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="tms-reason">Reason (required)</Label>
            <Textarea
              id="tms-reason"
              value={reason}
              maxLength={1000}
              onChange={(event) => setReason(event.target.value)}
              aria-invalid={!!reasonError}
              aria-describedby={reasonError ? "tms-reason-error" : undefined}
              disabled={resolve.isPending}
            />
            {reasonError && (
              <p id="tms-reason-error" role="alert" className="text-xs font-medium text-bad">
                {reasonError}
              </p>
            )}
          </div>
          {problem && (
            <Alert tone="bad" role="alert">
              <p>{problem}</p>
            </Alert>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={onClose} disabled={resolve.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={resolve.isPending}>
              {resolve.isPending ? "Saving…" : usingTms ? "Use the TMS value" : "Keep the documents' value"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The TMS shipment record that names this shipment, compared field by field
 * with what the audit read. A difference changes no result until an
 * administrator decides it; an approved correction always wins over the TMS.
 */
export function TmsPanel({ shipmentId }: { shipmentId: string }) {
  const session = useSession()
  const canView = can(session.data, "exceptions.view")
  const canDecide = can(session.data, "extraction.approve")
  const tms = useShipmentTms(shipmentId, canView)
  const [deciding, setDeciding] = useState<{ row: TmsComparison; choice: "document" | "tms" } | null>(null)

  if (!canView || !tms.data || !tms.data.configured) {
    return tms.isError ? (
      <Section title="TMS shipment record">
        <Alert tone="warn">
          <p>The TMS comparison could not be loaded: {errorMessage(tms.error)}</p>
        </Alert>
      </Section>
    ) : null
  }

  const data = tms.data
  const match = data.match

  return (
    <Section title="TMS shipment record" description="The record a TMS integration imported whose reference names this shipment, compared field by field with what the audit read.">
      <div className="space-y-4">
        {data.unavailable.map((item, index) => (
          <Alert key={index} tone="warn">
            <p>An integration's records could not be read: {item.reason} Its records are not compared.</p>
          </Alert>
        ))}

        {!match && !data.ambiguous && <EmptyState icon={<Truck className="size-5" />} title="No TMS record names this shipment">A record is matched when its reference is this shipment's ID.</EmptyState>}

        {data.ambiguous && (
          <Alert tone="warn">
            <p>
              Several TMS records name this shipment ({data.matches.map((item) => `${item.external_id} from ${item.integration_name}`).join(", ")}). Nothing is compared or used until the duplicate is resolved in the TMS.
            </p>
          </Alert>
        )}

        {match && (
          <Card className="space-y-4 p-4">
            <Facts
              label="Where the TMS record comes from"
              columns={3}
              items={[
                ["Integration", match.integration_name],
                ["TMS record", match.external_id],
                ["TMS status", match.status ?? NA],
                ["Changed in the TMS", dateTime(match.source_updated_at)],
                ["Imported", `${dateTime(match.imported_at)} (sync run #${match.last_run_id})`],
                ["Corrected by hand in LogiDoc", match.corrected_fields.length ? match.corrected_fields.join(", ") : "Nothing"],
              ]}
            />

            <Table aria-label="TMS comparison">
              <TableHeader>
                <TableRow>
                  <TableHead>Field</TableHead>
                  <TableHead>Documents</TableHead>
                  <TableHead>TMS</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Decision</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.comparisons.map((row) => {
                  const look = TMS_LOOK[row.state]

                  return (
                    <TableRow key={row.field} className="align-top" data-testid={`tms-${row.field}`}>
                      <TableCell>
                        <p className="font-medium">{row.label}</p>
                        <p className="text-xs text-muted-foreground">{row.consumed ? "Read by a calculation" : "Cross-check only"}</p>
                      </TableCell>
                      <TableCell className="tabular">{row.document_state === "extracted" ? text(row.document_text) : <span className="text-muted-foreground">Not readable ({row.document_state})</span>}</TableCell>
                      <TableCell className="tabular">
                        {text(row.tms_text)}
                        {row.tms_corrected_by_hand && <span className="block text-xs text-warn">Corrected by hand in LogiDoc: can be noted, never used.</span>}
                        {row.currency && row.currency.tms && row.currency.documents && row.currency.tms !== row.currency.documents && (
                          <span className="block text-xs text-warn">
                            Currencies differ: {row.currency.tms} in the TMS, {row.currency.documents} in the documents.
                          </span>
                        )}
                      </TableCell>
                      <TableCell>
                        <Badge tone={look.tone}>{look.text}</Badge>
                        {row.state === "protected" && row.protected_by !== null && <span className="block text-xs text-muted-foreground">Correction #{row.protected_by}</span>}
                      </TableCell>
                      <TableCell className="min-w-[12rem] text-sm">
                        {row.resolution && (
                          <p className="text-xs text-muted-foreground">
                            {row.resolution.actor} · {dateTime(row.resolution.at)}: {row.resolution.reason}
                          </p>
                        )}
                        {row.open && canDecide && (
                          <div className="flex flex-wrap gap-2">
                            <Button size="sm" variant="outline" onClick={() => setDeciding({ row, choice: "document" })} aria-label={`Keep the documents' value for ${row.label}`}>
                              Keep documents' value
                            </Button>
                            {row.choices.includes("tms") && (
                              <Button size="sm" variant="outline" onClick={() => setDeciding({ row, choice: "tms" })} aria-label={`Use the TMS value for ${row.label}`}>
                                Use TMS value
                              </Button>
                            )}
                          </div>
                        )}
                        {row.open && !canDecide && <span className="text-xs text-muted-foreground">An administrator decides.</span>}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
            <p className="text-xs text-muted-foreground">
              An open difference changes no result: the audit reads the documents until an administrator decides. A decision applies only while both values stay as they are; a later sync with another value opens the difference again.
            </p>
          </Card>
        )}

        {data.history.length > 0 && (
          <details className="rounded-md border bg-card p-3 text-sm">
            <summary className="cursor-pointer font-medium">Decisions on TMS differences ({data.history.length})</summary>
            <ul className="mt-2 space-y-1.5">
              {data.history.map((item) => (
                <li key={item.id}>
                  #{item.id} · {item.field}: {item.choice === "tms" ? "TMS value used" : "documents' value kept"} ({text(item.document_value)} / TMS {text(item.tms_value)}) by {item.actor}, {dateTime(item.at)}. {item.reason}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>

      {deciding && match && <ResolveDialog shipmentId={shipmentId} row={deciding.row} recordId={match.record_id} choice={deciding.choice} onClose={() => setDeciding(null)} />}
    </Section>
  )
}
