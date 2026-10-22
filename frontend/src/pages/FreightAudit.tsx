import { useNavigate, useParams } from "react-router-dom"
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts"
import { DeliveryCard, InvoiceCard, QuantityCard } from "@/components/common/AuditComparison"
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

          <section aria-label="Comparisons" className="grid gap-4 lg:grid-cols-3">
            <QuantityCard audit={shipment.data.result.quantity} />
            <DeliveryCard audit={shipment.data.result.delivery} />
            <InvoiceCard audit={shipment.data.result.invoice} />
          </section>

          <Section title="Invoice composition" description="Each line item the invoice states. A missing amount is left out, not drawn as zero.">
            <Card>
              <CardContent className="pt-5">
                {(() => {
                  const items = shipment.data!.result.invoice!.line_items.filter(([, amount]) => amount !== null).map(([name, amount]) => ({ name, amount: amount as number }))

                  if (items.length === 0) return <p className="text-sm text-muted-foreground">No line item amount could be read.</p>

                  return (
                    <div role="img" aria-label={`Invoice line items: ${items.map((item) => `${item.name} ${money(item.amount)}`).join(", ")}. Stated total ${money(shipment.data!.result.invoice!.total)}.`} className="h-56">
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart data={items} margin={{ left: 8, right: 8 }}>
                          <CartesianGrid vertical={false} stroke={CHART.grid} />
                          <XAxis dataKey="name" stroke={CHART.axis} fontSize={12} />
                          <YAxis stroke={CHART.axis} fontSize={12} tickFormatter={(value: number) => `$${value}`} />
                          <ChartTooltip formatter={(value) => money(Number(value))} cursor={{ fill: "#f1f5f9" }} />
                          <Bar dataKey="amount" name="Amount" radius={[4, 4, 0, 0]} label={{ position: "top", fontSize: 12, fill: CHART.axis, formatter: (value: unknown) => money(Number(value)) }}>
                            {items.map((item) => (
                              <Cell key={item.name} fill={CHART.info} />
                            ))}
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
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
              <div className="space-y-2">
                {shipment.data.result.discrepancies.map((item, index) => (
                  <Alert key={index} tone="bad">
                    <p>
                      <span className="font-medium">{item.category}:</span> {item.description}
                    </p>
                  </Alert>
                ))}
              </div>
            </Section>
          )}
        </>
      )}
    </>
  )
}
