import { useNavigate, useParams } from "react-router-dom"
import { DeliveryCard, InvoiceCard, QuantityCard } from "@/components/common/AuditComparison"
import { BarList } from "@/components/common/Charts"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { Alert } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useShipment, useShipments } from "@/hooks/queries"
import { CHART } from "@/lib/chart"
import { money, NA } from "@/lib/format"
import type { ReconciliationResult } from "@/lib/types"
import { DownloadButtons } from "@/pages/ShipmentDetail"

function Consistency({ result }: { result: ReconciliationResult }) {
  const carrier = result.carrier
  const route = result.route

  if (!carrier || !route) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Consistency across documents</CardTitle>
        <CardDescription>The same shipment should name the same carrier and route on every document.</CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <tr>
              <TableHead>Field</TableHead>
              <TableHead>BOL</TableHead>
              <TableHead>Invoice</TableHead>
              <TableHead>POD</TableHead>
              <TableHead>Check</TableHead>
            </tr>
          </TableHeader>
          <TableBody>
            <TableRow>
              <TableCell className="font-medium">Carrier</TableCell>
              <TableCell>{carrier.bol ?? NA}</TableCell>
              <TableCell>{carrier.invoice ?? NA}</TableCell>
              <TableCell>{carrier.pod ?? NA}</TableCell>
              <TableCell className="space-x-1">
                <StatusBadge status={carrier.bol_invoice_status} />
                <StatusBadge status={carrier.bol_pod_status} />
              </TableCell>
            </TableRow>
            <TableRow>
              <TableCell className="font-medium">Origin</TableCell>
              <TableCell>{route.origin ?? NA}</TableCell>
              <TableCell>{route.invoice_origin ?? NA}</TableCell>
              <TableCell className="text-muted-foreground">—</TableCell>
              <TableCell>
                <StatusBadge status={route.origin_status} />
              </TableCell>
            </TableRow>
            <TableRow>
              <TableCell className="font-medium">Destination</TableCell>
              <TableCell>{route.destination ?? NA}</TableCell>
              <TableCell>{route.invoice_destination ?? NA}</TableCell>
              <TableCell className="text-muted-foreground">—</TableCell>
              <TableCell>
                <StatusBadge status={route.destination_status} />
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

export function FreightAudit() {
  const { id } = useParams()
  const navigate = useNavigate()
  const list = useShipments({})

  const selected = id ?? list.data?.shipments[0]?.shipment_id
  const shipment = useShipment(selected)

  const chooser = (
    <div className="mb-6 max-w-sm space-y-1.5">
      <Label htmlFor="audit-shipment">Shipment</Label>
      <Select id="audit-shipment" value={selected ?? ""} onChange={(event) => navigate(`/freight-audit/${encodeURIComponent(event.target.value)}`)} disabled={!list.data?.shipments.length}>
        {list.data?.shipments.map((row) => (
          <option key={row.shipment_key} value={row.shipment_id}>
            {row.shipment_id} · {row.final_status.charAt(0) + row.final_status.slice(1).toLowerCase()}
          </option>
        ))}
      </Select>
    </div>
  )

  const header = <PageHeader title="Freight Audit" description="Quantity, delivery and invoice, compared across the BOL, POD and invoice of one shipment. The figures are the audit's; this screen only displays them." actions={selected && shipment.data?.result.success ? <DownloadButtons shipmentId={selected} /> : undefined} />

  if (list.isPending) {
    return (
      <>
        {header}
        <LoadingBlock rows={5} label="Loading shipments" />
      </>
    )
  }

  if (list.isError) {
    return (
      <>
        {header}
        <ErrorState error={list.error} onRetry={() => list.refetch()} />
      </>
    )
  }

  if (list.data.shipments.length === 0) {
    return (
      <>
        {header}
        <EmptyState title="No shipments to audit">Add documents in Document Intelligence first.</EmptyState>
      </>
    )
  }

  return (
    <>
      {header}
      {chooser}

      {shipment.isPending && <LoadingBlock rows={5} label="Loading audit" />}
      {shipment.isError && <ErrorState error={shipment.error} onRetry={() => shipment.refetch()} />}

      {shipment.data && !shipment.data.result.success && <Alert tone="bad">{shipment.data.result.error ?? "This shipment could not be reconciled."}</Alert>}

      {shipment.data?.result.success && shipment.data.result.quantity && shipment.data.result.delivery && shipment.data.result.invoice && (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <StatusBadge status={shipment.data.result.final_status} />
            <span className="text-sm text-muted-foreground">
              {shipment.data.result.discrepancies?.length ?? 0} discrepanc{shipment.data.result.discrepancies?.length === 1 ? "y" : "ies"} · {shipment.data.result.review_items?.length ?? 0} value(s) that could not be verified
            </span>
          </div>

          <section aria-label="Comparisons" className="grid items-start gap-4 lg:grid-cols-3">
            <QuantityCard audit={shipment.data.result.quantity} />
            <DeliveryCard audit={shipment.data.result.delivery} />
            <InvoiceCard audit={shipment.data.result.invoice} />
          </section>

          <Section title="Invoice composition" description="Each line item the invoice states. A missing amount is left out, not drawn as zero.">
            <Card>
              <CardContent className="pt-6">
                {(() => {
                  const invoice = shipment.data!.result.invoice!
                  const items = invoice.line_items.filter(([, amount]) => amount !== null).map(([name, amount]) => ({ name, amount: amount as number }))

                  if (items.length === 0) return <p className="text-sm text-muted-foreground">No line item amount could be read.</p>

                  const sum = items.reduce((total, item) => total + item.amount, 0)

                  return (
                    <div className="grid gap-8 lg:grid-cols-[1fr_16rem]">
                      <BarList
                        label="Invoice line items"
                        rows={[...items]
                          .sort((a, b) => b.amount - a.amount)
                          .map((item) => ({
                            key: item.name, name: item.name, label: item.name, display: money(item.amount),
                            parts: [{ key: "amount", label: "Amount", value: Math.max(item.amount, 0), colour: CHART.info }],
                          }))}
                      />
                      <dl className="space-y-3 rounded-lg bg-muted/60 p-4 text-sm" aria-label="Invoice totals">
                        <div className="flex items-baseline justify-between gap-3">
                          <dt className="text-muted-foreground">Sum of line items</dt>
                          <dd className="tabular font-medium">{money(sum)}</dd>
                        </div>
                        <div className="flex items-baseline justify-between gap-3">
                          <dt className="text-muted-foreground">Stated total</dt>
                          <dd className="tabular font-medium">{money(invoice.total)}</dd>
                        </div>
                        {invoice.total !== null && invoice.total !== undefined && Math.abs(invoice.total - sum) >= 0.005 && (
                          <div className="flex items-baseline justify-between gap-3 border-t pt-3">
                            <dt className="text-muted-foreground">Not explained by line items</dt>
                            <dd className="tabular font-semibold text-bad">{money(invoice.total - sum)}</dd>
                          </div>
                        )}
                      </dl>
                    </div>
                  )
                })()}
              </CardContent>
            </Card>
          </Section>

          <Section title="Cross-document checks">
            <Consistency result={shipment.data.result} />
          </Section>

          {shipment.data.result.discrepancies && shipment.data.result.discrepancies.length > 0 && (
            <Section title="Discrepancies">
              <Card>
                <ul className="divide-y">
                  {shipment.data.result.discrepancies.map((item, index) => (
                    <li key={index} className="flex gap-3 px-5 py-4 text-sm">
                      <span aria-hidden="true" className="mt-1.5 size-2 shrink-0 rounded-full bg-bad" />
                      <p>
                        <span className="font-medium">{item.category}:</span> <span className="text-muted-foreground">{item.description}</span>
                      </p>
                    </li>
                  ))}
                </ul>
              </Card>
            </Section>
          )}
        </>
      )}
    </>
  )
}
