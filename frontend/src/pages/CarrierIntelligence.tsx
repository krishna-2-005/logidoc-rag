import { Download, FileCheck2, PackageCheck, Receipt, Scale, ShieldAlert, Truck } from "lucide-react"
import { useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { Columns } from "@/components/common/Charts"
import { KpiCard } from "@/components/common/KpiCard"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { EmptyState, ErrorState, KpiSkeletons, LoadingBlock } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { ChartCard } from "@/components/ui/card"
import { Input, Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useCarrierDetail, useCarriers, type CarrierFilters } from "@/hooks/queries"
import { download } from "@/lib/api"
import { CHART } from "@/lib/chart"
import { count, money, NA, percent, rate } from "@/lib/format"
import type { CarrierCard, CarrierDetail, CarrierFigures, CarrierMoney, CarrierMonth } from "@/lib/types"

const PAGE = 25

/** Money of one figure, one line per currency (amounts are never added across currencies) */
function MoneyLines({ rows, value }: { rows: CarrierMoney[]; value: (row: CarrierMoney) => number | null }) {
  const shown = rows.filter((row) => value(row) !== null)
  if (shown.length === 0) return <span className="text-muted-foreground">{NA}</span>
  return (
    <>
      {shown.map((row) => (
        <span key={row.currency} className="block tabular">
          {money(value(row), row.currency)}
        </span>
      ))}
    </>
  )
}

function percentOf(figure: { rate: number | null }) {
  return figure.rate === null ? null : Math.round(figure.rate * 1000) / 10
}

function Totals({ totals }: { totals: CarrierFigures }) {
  const d = totals.delivery
  const a = totals.audit
  return (
    <section aria-label="Totals" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
      <KpiCard title="Shipments" value={count(totals.shipments)} icon={<Truck />} detail={`${count(d.completed)} delivered (POD date stated)`} />
      <KpiCard title="On-time deliveries" value={rate(d.on_time_rate)} icon={<PackageCheck />} definition="On time or early / deliveries measured (BOL pickup date stated and the expected and actual delivery dates compared)." detail={d.average_delay_days === null ? "No late delivery" : `Late by ${d.average_delay_days} days on average`} />
      <KpiCard title="Complete documents" value={rate(totals.documents.completeness_rate)} icon={<FileCheck2 />} definition="Shipments with a usable BOL, invoice and POD / shipments." detail={`${totals.documents.missing_pod} missing a POD`} />
      <KpiCard title="Invoice accuracy" value={rate(a.invoice_accuracy_rate)} icon={<Receipt />} definition="Invoices whose line items add up to their total / invoices whose arithmetic could be checked." detail={`${a.arithmetic_mismatch} do not add up`} />
      <KpiCard title="Audit pass rate (against contract)" value={rate(a.audit_pass_rate)} icon={<Scale />} definition="Invoices matched against the rate confirmation / invoices compared with one." detail={`${a.without_contract} invoices have no rate confirmation`} />
      <KpiCard title="Open exceptions" value={count(totals.exceptions.open)} icon={<ShieldAlert />} tone={totals.exceptions.open ? "warn" : "default"} detail={`${count(totals.exceptions.total)} detected in all`} to="/exceptions" cta="Exception Center" />
    </section>
  )
}

function Comparison({ carriers, selected, onSelect }: { carriers: CarrierCard[]; selected: string | null; onSelect: (key: string) => void }) {
  return (
    <Table aria-label="Carrier comparison">
      <TableHeader>
        <TableRow>
          <TableHead>Carrier</TableHead>
          <TableHead className="text-right">Shipments</TableHead>
          <TableHead>On time</TableHead>
          <TableHead className="text-right">Average delay</TableHead>
          <TableHead>Complete documents</TableHead>
          <TableHead>Invoice accuracy</TableHead>
          <TableHead>Audit pass rate</TableHead>
          <TableHead className="text-right">Invoiced</TableHead>
          <TableHead className="text-right">Variance vs contract</TableHead>
          <TableHead className="text-right">Exceptions (open)</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {carriers.map((card) => (
          <TableRow key={card.carrier_key} data-selected={card.carrier_key === selected}>
            <TableCell className="font-medium">
              <button type="button" className="text-left text-primary hover:underline" onClick={() => onSelect(card.carrier_key)} aria-label={`Open carrier ${card.carrier}`}>
                {card.carrier}
              </button>
              {card.registered && <span className="block text-xs text-muted-foreground">Registered{card.registered.scac ? ` · SCAC ${card.registered.scac}` : ""}</span>}
              {card.exceptions.repeated.length > 0 && <span className="block text-xs text-warn">Repeated: {card.exceptions.repeated.map((item) => item.category).join(", ")}</span>}
            </TableCell>
            <TableCell className="tabular text-right">{count(card.shipments)}</TableCell>
            <TableCell>{rate(card.delivery.on_time_rate)}</TableCell>
            <TableCell className="tabular text-right">{card.delivery.average_delay_days === null ? NA : `${card.delivery.average_delay_days} d`}</TableCell>
            <TableCell>{rate(card.documents.completeness_rate)}</TableCell>
            <TableCell>{rate(card.audit.invoice_accuracy_rate)}</TableCell>
            <TableCell>{rate(card.audit.audit_pass_rate)}</TableCell>
            <TableCell className="text-right">
              <MoneyLines rows={card.financial.by_currency} value={(row) => row.invoiced_total} />
            </TableCell>
            <TableCell className="text-right">
              <MoneyLines rows={card.financial.by_currency} value={(row) => row.verified_variance} />
            </TableCell>
            <TableCell className="tabular text-right">
              {count(card.exceptions.total)} ({count(card.exceptions.open)})
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function CarrierCharts({ carriers }: { carriers: CarrierCard[] }) {
  const onTime = carriers.filter((card) => card.delivery.measured > 0).map((card) => ({ carrier: card.carrier, onTime: percentOf(card.delivery.on_time_rate) }))
  const documents = carriers.map((card) => ({ carrier: card.carrier, complete: card.documents.complete, incomplete: card.documents.shipments - card.documents.complete }))
  const discrepancies = carriers.map((card) => ({ carrier: card.carrier, arithmetic: card.audit.arithmetic_mismatch, contract: card.audit.discrepant }))

  return (
    <div className="grid gap-4 xl:grid-cols-3">
      <ChartCard title="On-time delivery by carrier" description="Carriers with at least one measured delivery. The table shows each rate's count and denominator.">
        {onTime.length === 0 ? (
          <EmptyState title="No delivery could be measured">A delivery is measured when the BOL states a pickup date and the expected and actual delivery dates were compared.</EmptyState>
        ) : (
          <Columns horizontal data={onTime} category="carrier" series={[{ key: "onTime", label: "On time (%)", colour: CHART.ok }]} label="On-time delivery percentage by carrier" format={(value) => `${value}%`} height={Math.max(160, onTime.length * 40)} />
        )}
      </ChartCard>
      <ChartCard title="Document compliance by carrier" description="Shipments with a usable BOL, invoice and POD, and shipments missing at least one.">
        <Columns horizontal stacked integers data={documents} category="carrier" series={[{ key: "complete", label: "Complete", colour: CHART.ok }, { key: "incomplete", label: "Incomplete", colour: CHART.warn }]} label="Complete and incomplete shipments by carrier" format={(value) => count(value)} height={Math.max(160, documents.length * 40)} />
      </ChartCard>
      <ChartCard title="Invoice discrepancies by carrier" description="Invoices whose line items do not add up, and invoices above or below their rate confirmation.">
        <Columns horizontal integers data={discrepancies} category="carrier" series={[{ key: "arithmetic", label: "Line items do not add up", colour: CHART.warn }, { key: "contract", label: "Discrepancy against contract", colour: CHART.bad }]} label="Invoice discrepancies by carrier" format={(value) => count(value)} height={Math.max(160, discrepancies.length * 48)} />
      </ChartCard>
    </div>
  )
}

function MonthlyTrend({ months, title }: { months: CarrierMonth[]; title: string }) {
  return (
    <ChartCard title={title} description="By the month of each shipment's pickup date (or delivery or invoice date when there is none). Undated shipments are not shown.">
      {months.length === 0 ? (
        <EmptyState title="No dated shipment">No shipment in the period states a pickup, delivery or invoice date.</EmptyState>
      ) : (
        <Columns
          integers
          data={months}
          category="month"
          series={[
            { key: "on_time", label: "On time", colour: CHART.ok },
            { key: "late", label: "Late", colour: CHART.bad },
            { key: "exceptions", label: "Exceptions", colour: CHART.warn },
          ]}
          label={title}
          format={(value) => count(value)}
        />
      )}
    </ChartCard>
  )
}

function Detail({ detail, onMore }: { detail: CarrierDetail; onMore: () => void }) {
  const amounts = detail.exceptions.amounts
  return (
    <Section title={`Carrier: ${detail.carrier}`} description={detail.identity_note}>
      <div className="space-y-6" aria-label={`Carrier ${detail.carrier}`}>
        {(detail.identifier || detail.contact) && (
          <dl className="grid gap-3 text-sm sm:grid-cols-2" aria-label="Carrier master data">
            {detail.identifier && (
              <div>
                <dt className="text-xs text-muted-foreground">Identifiers</dt>
                <dd>{detail.identifier}</dd>
              </div>
            )}
            {detail.contact && (
              <div>
                <dt className="text-xs text-muted-foreground">Contact</dt>
                <dd>{detail.contact}</dd>
              </div>
            )}
          </dl>
        )}
        {detail.names.length > 1 && <p className="text-sm">Written on the documents as: {detail.names.join(", ")}</p>}

        <Table aria-label="Money by currency">
          <TableHeader>
            <TableRow>
              <TableHead>Currency</TableHead>
              <TableHead className="text-right">Invoiced</TableHead>
              <TableHead className="text-right">Contracted</TableHead>
              <TableHead className="text-right">Billed against contract</TableHead>
              <TableHead className="text-right">Variance</TableHead>
              <TableHead className="text-right">Average variance</TableHead>
              <TableHead className="text-right">Cost per shipment</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {detail.financial.by_currency.map((row) => (
              <TableRow key={row.currency}>
                <TableCell>{row.currency}</TableCell>
                <TableCell className="tabular text-right">{money(row.invoiced_total, row.currency)} <span className="text-xs text-muted-foreground">({row.invoices_with_total})</span></TableCell>
                <TableCell className="tabular text-right">{money(row.contracted_total, row.currency)} <span className="text-xs text-muted-foreground">({row.invoices_compared_with_contract})</span></TableCell>
                <TableCell className="tabular text-right">{money(row.audited_billed_total, row.currency)}</TableCell>
                <TableCell className="tabular text-right">{money(row.verified_variance, row.currency)}</TableCell>
                <TableCell className="tabular text-right">{money(row.average_invoice_variance, row.currency)}</TableCell>
                <TableCell className="tabular text-right">{money(row.cost_per_shipment, row.currency)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {detail.financial.invoices_total_not_read > 0 && <p className="text-xs text-muted-foreground">{detail.financial.invoices_total_not_read} invoice total(s) could not be read and are not counted (never as 0).</p>}

        <dl className="grid gap-3 text-sm sm:grid-cols-4" aria-label="Exception amounts">
          {(["open", "disputed", "waived", "resolved"] as const).map((state) => (
            <div key={state}>
              <dt className="text-xs text-muted-foreground">Exceptions {state}</dt>
              <dd>
                {amounts[state].length === 0 ? (
                  <span className="text-muted-foreground">no amount</span>
                ) : (
                  amounts[state].map((item) => (
                    <span key={item.currency} className="block tabular">
                      {money(item.amount, item.currency)} ({item.exceptions})
                    </span>
                  ))
                )}
              </dd>
            </div>
          ))}
        </dl>

        {detail.exceptions.repeated.length > 0 && (
          <Alert tone="warn">
            {detail.exceptions.repeated.map((item) => (
              <p key={item.category} className="text-sm">
                Repeated: {item.category} on {item.shipments.length} shipments ({item.shipments.join(", ")})
              </p>
            ))}
          </Alert>
        )}

        <MonthlyTrend months={detail.monthly} title={`Monthly trend: ${detail.carrier}`} />

        <div>
          <h3 className="mb-2 text-sm font-semibold">Shipments ({detail.shipment_count})</h3>
          <Table aria-label="Shipments of the carrier">
            <TableHeader>
              <TableRow>
                <TableHead>Shipment</TableHead>
                <TableHead>Date</TableHead>
                <TableHead>Delivery</TableHead>
                <TableHead className="text-right">Invoice</TableHead>
                <TableHead>Against contract</TableHead>
                <TableHead>Documents</TableHead>
                <TableHead className="text-right">Exceptions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {detail.shipment_rows.map((row) => (
                <TableRow key={row.shipment_key}>
                  <TableCell>
                    <Link className="text-primary hover:underline" to={`/shipments/${encodeURIComponent(row.shipment_id)}`}>
                      {row.shipment_id}
                    </Link>
                  </TableCell>
                  <TableCell>
                    {row.date ?? NA}
                    {row.date_basis && row.date_basis !== "pickup" && <span className="block text-xs text-muted-foreground">by {row.date_basis} date</span>}
                  </TableCell>
                  <TableCell>{row.measured && row.delivery_status ? <StatusBadge status={row.delivery_status} /> : <span className="text-xs text-muted-foreground">{row.not_measured_reason}</span>}</TableCell>
                  <TableCell className="tabular text-right">{money(row.invoice_total, row.currency)}</TableCell>
                  <TableCell>{row.freight_outcome ? <StatusBadge status={row.freight_outcome} /> : <span className="text-xs text-muted-foreground">no rate confirmation</span>}</TableCell>
                  <TableCell>{row.complete ? "Complete" : `Missing ${row.missing.join(", ")}`}</TableCell>
                  <TableCell className="tabular text-right">{row.exceptions.length}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {detail.shipment_rows.length < detail.shipment_count && (
            <Button variant="outline" size="sm" className="mt-3" onClick={onMore}>
              Show more shipments
            </Button>
          )}
        </div>
      </div>
    </Section>
  )
}

export function CarrierIntelligence() {
  const [params, setParams] = useSearchParams()
  const [start, setStart] = useState(params.get("start") ?? "")
  const [end, setEnd] = useState(params.get("end") ?? "")
  const [limit, setLimit] = useState(PAGE)
  const selected = params.get("carrier")
  const toast = useToast()

  const filters: CarrierFilters = { ...(start && { start }), ...(end && { end }) }
  const invalid = !!(start && end && start > end)
  const list = useCarriers(invalid ? {} : filters)
  const detail = useCarrierDetail(invalid ? null : selected, { ...filters, limit })

  const choose = (key: string | null) => {
    const next = new URLSearchParams(params)
    if (key) next.set("carrier", key)
    else next.delete("carrier")
    setParams(next, { replace: true })
    setLimit(PAGE)
  }

  const exportCsv = () =>
    download("/api/reports/carriers.csv", "carrier_performance.csv", { ...filters }).then(
      (name) => toast.success(`Downloaded ${name}`),
      (error: Error) => toast.error(error.message),
    )

  const data = list.data

  return (
    <>
      <PageHeader
        title="Carrier Intelligence"
        description="Each carrier's deliveries, invoices, documents, audit results and exceptions as the documents are now. Every rate shows what it is counted out of; a missing value is never counted as 0."
        actions={
          <Button variant="outline" size="sm" onClick={exportCsv} disabled={invalid}>
            <Download /> Export CSV
          </Button>
        }
      />

      <form role="search" aria-label="Filter carriers" className="mb-4 grid gap-3 rounded-lg border bg-card p-4 shadow-card sm:grid-cols-4" onSubmit={(event) => event.preventDefault()}>
        <div className="space-y-1.5">
          <Label htmlFor="carrier-start">From</Label>
          <Input id="carrier-start" type="date" value={start} onChange={(event) => setStart(event.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="carrier-end">To</Label>
          <Input id="carrier-end" type="date" value={end} onChange={(event) => setEnd(event.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="carrier-pick">Carrier</Label>
          <Select id="carrier-pick" value={selected ?? ""} onChange={(event) => choose(event.target.value || null)}>
            <option value="">All carriers</option>
            {data?.carriers.map((card) => (
              <option key={card.carrier_key} value={card.carrier_key}>
                {card.carrier}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex items-end">
          <Button type="button" variant="ghost" size="sm" onClick={() => { setStart(""); setEnd(""); choose(null) }}>
            Clear filters
          </Button>
        </div>
        {invalid && <p className="text-sm text-bad sm:col-span-4" role="alert">The start date is after the end date.</p>}
      </form>

      {list.isPending && <KpiSkeletons count={6} />}
      {list.isError && <ErrorState error={list.error} onRetry={() => list.refetch()} title="Carrier figures could not be loaded" />}

      {data && (
        <div className="space-y-6">
          {data.totals.shipments === 0 ? (
            <EmptyState title="No shipment in this period">Upload documents, or choose a wider date range. Shipments without a pickup, delivery or invoice date are left out when a range is chosen.</EmptyState>
          ) : (
            <>
              <Totals totals={data.totals} />

              {(data.possible_duplicates.length > 0 || data.unattributed.conflicting.length > 0 || data.unattributed.not_stated.length > 0 || data.unattributed.undated_excluded.length > 0) && (
                <Alert tone="info">
                  {data.possible_duplicates.map((names) => (
                    <p key={names.join("|")} className="text-sm">
                      Possibly the same carrier, kept apart until a person decides: {names.join(" / ")}.
                    </p>
                  ))}
                  {data.unattributed.conflicting.map((item) => (
                    <p key={item.shipment_id} className="text-sm">
                      {item.shipment_id} is not counted for any carrier: its documents name {item.names.join(" and ")}.
                    </p>
                  ))}
                  {data.unattributed.not_stated.length > 0 && <p className="text-sm">No carrier stated on {data.unattributed.not_stated.length} shipment(s): {data.unattributed.not_stated.join(", ")}.</p>}
                  {data.unattributed.undated_excluded.length > 0 && <p className="text-sm">Left out of the date range (no date): {data.unattributed.undated_excluded.join(", ")}.</p>}
                </Alert>
              )}

              {data.carriers.length === 0 ? (
                <EmptyState title="No carrier named">No shipment in the period has documents that name one carrier.</EmptyState>
              ) : (
                <>
                  <Section title="Compare carriers" description={`${data.carriers.length} carrier(s). Select one to see its money, exceptions and shipments.`}>
                    <Comparison carriers={data.carriers} selected={selected} onSelect={(key) => choose(key)} />
                  </Section>
                  <CarrierCharts carriers={data.carriers} />
                </>
              )}

              {!selected && <MonthlyTrend months={data.monthly} title="Monthly trend: all carriers" />}
            </>
          )}

          {selected && detail.isPending && <LoadingBlock rows={4} label="Loading the carrier" />}
          {selected && detail.isError && <ErrorState error={detail.error} onRetry={() => detail.refetch()} title="This carrier could not be loaded" />}
          {selected && detail.data && <Detail detail={detail.data} onMore={() => setLimit((value) => value + PAGE)} />}

          <details className="text-xs text-muted-foreground">
            <summary className="cursor-pointer font-medium text-primary">How each figure is counted</summary>
            <dl className="mt-2 space-y-1">
              {Object.entries(data.definitions).map(([name, text]) => (
                <div key={name}>
                  <dt className="inline font-medium">{name.replace(/_/g, " ")}: </dt>
                  <dd className="inline">{text}</dd>
                </div>
              ))}
            </dl>
          </details>
          <p className="text-xs text-muted-foreground">Percentages are shown with their counts; {percent(null)} means nothing could be counted.</p>
        </div>
      )}
    </>
  )
}
