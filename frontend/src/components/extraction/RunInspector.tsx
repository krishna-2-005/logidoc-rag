import { Link } from "react-router-dom"
import { StatusBadge } from "@/components/common/StatusBadge"
import { ErrorState, LoadingBlock } from "@/components/common/states"
import { pageText, ValidationBadge, valueText } from "@/components/extraction/fields"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useRunInputs, useShipmentHistory } from "@/hooks/queries"
import { label, money, NA } from "@/lib/format"
import type { AuditRun, RunInput } from "@/lib/types"

/** What the run recorded for the shipment: the snapshot it found, read from the history as it was stored. */
function RecordedResult({ shipmentId, runId }: { shipmentId: string; runId: number }) {
  const history = useShipmentHistory(shipmentId)

  if (history.isPending) return <LoadingBlock rows={1} label="Loading the recorded result" />
  if (history.isError) return <ErrorState error={history.error} title="The recorded result could not be loaded" onRetry={() => history.refetch()} />

  const entry = history.data.find((item) => item.snapshot.runs.some((run) => run.run_id === runId))

  if (!entry) return <p className="text-xs text-muted-foreground">No recorded snapshot of this shipment points to this run.</p>

  const snapshot = entry.snapshot
  const total = typeof snapshot.invoice_total === "number" ? snapshot.invoice_total : null

  return (
    <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2 lg:grid-cols-4" aria-label="Result recorded in this run">
      <div>
        <dt className="text-xs text-muted-foreground">Result</dt>
        <dd>
          <StatusBadge status={snapshot.final_status} />
        </dd>
      </div>
      <div>
        <dt className="text-xs text-muted-foreground">Quantity · Delivery · Invoice</dt>
        <dd className="flex flex-wrap gap-1">
          <StatusBadge status={typeof snapshot.quantity_status === "string" ? snapshot.quantity_status : null} />
          <StatusBadge status={typeof snapshot.delivery_status === "string" ? snapshot.delivery_status : null} />
          <StatusBadge status={typeof snapshot.invoice_status === "string" ? snapshot.invoice_status : null} />
        </dd>
      </div>
      <div>
        <dt className="text-xs text-muted-foreground">BOL / POD pieces</dt>
        <dd>
          {typeof snapshot.bol_pieces === "number" ? snapshot.bol_pieces : NA} / {typeof snapshot.pod_pieces === "number" ? snapshot.pod_pieces : NA}
        </dd>
      </div>
      <div>
        <dt className="text-xs text-muted-foreground">Invoice total</dt>
        <dd>{money(total)}</dd>
      </div>
    </dl>
  )
}

function InputRow({ input }: { input: RunInput }) {
  const approved = input.source === "approved_correction"

  return (
    <TableRow className="align-top">
      <TableCell className="min-w-[9rem] font-medium">{label(input.field_name)}</TableCell>
      <TableCell className="min-w-[8rem]">
        {input.resolved ? (
          <span className={approved ? "font-medium" : undefined}>{valueText(input.field_name, input.value)}</span>
        ) : (
          <span className="inline-flex flex-wrap items-center gap-1.5">
            <Badge tone="bad">Unresolved</Badge>
            <span className="text-xs text-muted-foreground">{input.state}</span>
          </span>
        )}
      </TableCell>
      <TableCell className="min-w-[11rem]">
        {approved ? <Badge tone="ok">Approved correction{input.correction_id !== null ? ` #${input.correction_id}` : ""}</Badge> : <Badge>Document reader</Badge>}
        {input.extraction_id !== null && (
          <p className="mt-1 text-xs text-muted-foreground">
            Extraction #{input.extraction_id}
            {input.extractor_version ? ` · ${input.extractor_version}` : ""}
          </p>
        )}
      </TableCell>
      <TableCell className="min-w-[11rem] max-w-[16rem]">
        <p className="break-all">{input.document ?? NA}</p>
        <p className="text-xs text-muted-foreground">{approved ? "typed by a reviewer: no page" : pageText(input.page)}</p>
      </TableCell>
      <TableCell className="min-w-[10rem] max-w-[18rem]">
        <ValidationBadge validation={input.validation} />
        {input.warnings.length > 0 && (
          <ul className="mt-1 list-disc pl-5 text-xs text-muted-foreground">
            {input.warnings.map((warning) => (
              <li key={warning} className="break-words">
                {warning}
              </li>
            ))}
          </ul>
        )}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">{input.consumed_by.map(label).join(", ")}</TableCell>
    </TableRow>
  )
}

/**
 * The inputs a recorded run consumed, as they were recorded, and the result it
 * recorded. Nothing is recalculated or rewritten: a run recorded before inputs
 * were recorded says so instead of showing a reconstruction.
 */
export function RunInspector({ run, onInspect }: { run: AuditRun; onInspect: (runId: number) => void }) {
  const inputs = useRunInputs(run.id)

  if (inputs.isPending) return <LoadingBlock rows={3} label={`Loading the inputs of run ${run.id}`} />
  if (inputs.isError) return <ErrorState error={inputs.error} title={`The inputs of run #${run.id} could not be loaded`} onRetry={() => inputs.refetch()} />

  const data = inputs.data

  if (!data.recorded) {
    return (
      <Alert tone="info">
        <p>
          Run #{run.id} was recorded before audit inputs were recorded. They are not reconstructed, so none are shown. The snapshots it points to are unchanged and appear in each shipment's history.
        </p>
      </Alert>
    )
  }

  const groups = new Map<string, RunInput[]>()
  data.inputs.forEach((input) => groups.set(input.shipment_key, [...(groups.get(input.shipment_key) ?? []), input]))

  return (
    <div className="space-y-4" aria-label={`Inputs of run ${run.id}`}>
      {[...groups.entries()].map(([key, items]) => {
        const superseded = data.superseded_by[key]

        return (
          <Card key={key} className="space-y-3 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-sm font-semibold">
                <Link className="underline-offset-2 hover:underline" to={`/shipments/${encodeURIComponent(items[0].shipment_id)}`}>
                  {items[0].shipment_id}
                </Link>
                <span className="ml-2 text-xs font-normal text-muted-foreground">calculation version {items[0].calculation_version}</span>
              </h4>
              {superseded ? (
                <Button variant="outline" size="sm" onClick={() => onInspect(superseded)}>
                  Superseded by run #{superseded}
                </Button>
              ) : (
                <Badge>Not superseded</Badge>
              )}
            </div>

            <RecordedResult shipmentId={items[0].shipment_id} runId={run.id} />

            <Table>
              <TableHeader>
                <tr>
                  <TableHead>Field</TableHead>
                  <TableHead>Value consumed</TableHead>
                  <TableHead>Source</TableHead>
                  <TableHead>Document</TableHead>
                  <TableHead>Checks</TableHead>
                  <TableHead>Used by</TableHead>
                </tr>
              </TableHeader>
              <TableBody>
                {items.map((input) => (
                  <InputRow key={input.id} input={input} />
                ))}
              </TableBody>
            </Table>
          </Card>
        )
      })}
    </div>
  )
}
