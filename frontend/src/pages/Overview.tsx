import type { UseQueryResult } from "@tanstack/react-query"
import { AlertTriangle, ArrowRight, ClipboardCheck, DollarSign, Eye, FileX2, PackageCheck, Percent, RefreshCw, Truck } from "lucide-react"
import type { ReactNode } from "react"
import { Link, useNavigate } from "react-router-dom"
import { BarList, Columns, Donut, Legend, ProportionBar, type Segment } from "@/components/common/Charts"
import { EmptyState, ErrorState, KpiSkeletons, LoadingBlock } from "@/components/common/states"
import { KpiCard } from "@/components/common/KpiCard"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, ChartCard } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useDocuments, useExposure, useOverview, useShipments } from "@/hooks/queries"
import { CHART, REVIEW_COLOURS, REVIEW_ORDER, SERIES, STATUS_COLOURS, STATUS_ORDER, sentence } from "@/lib/chart"
import { count, money, NA } from "@/lib/format"
import { attentionList, CHECKS, checkBreakdown, checkOutcome, documentsByType, invoiceComparison } from "@/lib/insights"

// Every shipment, unfiltered (the Shipment Explorer keeps its own filters)
const ALL_SHIPMENTS = {}

const OUTCOME_SERIES = [
  { key: "discrepancy", label: "Discrepancy found", colour: CHART.bad },
  { key: "passed", label: "Passed", colour: CHART.info },
  { key: "not_verified", label: "Could not be verified", colour: CHART.neutral },
]

const INVOICE_SERIES = [
  { key: "stated", label: "Stated total", colour: SERIES[6] },
  { key: "lineItems", label: "Sum of line items", colour: SERIES[2] },
]

const DOCUMENT_SERIES = [
  { key: "usable", label: "Text read", colour: CHART.info },
  { key: "partial", label: "Some pages without text", colour: CHART.warn },
  { key: "unusable", label: "Not usable", colour: CHART.bad },
]

/** A chart's own data: its loading and error states stay inside its card, so one failed request never blanks the page. */
function Loaded<T>({ query, children, rows = 4 }: { query: UseQueryResult<T>; children: (data: T) => ReactNode; rows?: number }) {
  if (query.isPending) return <LoadingBlock rows={rows} label="Loading chart" />
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} title="This chart could not be loaded" />

  return <>{children(query.data)}</>
}

export function Overview() {
  const overview = useOverview()
  const shipments = useShipments(ALL_SHIPMENTS)
  const exposureRows = useExposure()
  const documents = useDocuments()
  const navigate = useNavigate()

  const header = (
    <PageHeader
      title="Overview"
      description="Where your shipments stand: what the audit found, what still needs a person, and what the invoices say."
      actions={
        <Button
          variant="outline"
          size="sm"
          onClick={() => [overview, shipments, exposureRows, documents].forEach((query) => query.refetch())}
          disabled={overview.isFetching}
        >
          <RefreshCw className={overview.isFetching ? "animate-spin" : ""} /> Refresh
        </Button>
      }
    />
  )

  if (overview.isPending) {
    return (
      <>
        {header}
        <KpiSkeletons count={4} />
        <div className="mt-6 grid gap-4 lg:grid-cols-2">
          <LoadingBlock rows={5} label="Loading charts" />
          <LoadingBlock rows={5} label="Loading charts" />
        </div>
      </>
    )
  }

  if (overview.isError) {
    return (
      <>
        {header}
        <ErrorState error={overview.error} onRetry={() => overview.refetch()} title="The overview could not be loaded" />
      </>
    )
  }

  const data = overview.data

  if (data.empty) {
    return (
      <>
        {header}
        <EmptyState
          title="No documents yet"
          action={
            <Button asChild>
              <Link to="/documents">Add documents</Link>
            </Button>
          }
        >
          {data.message ?? "Upload a BOL, an invoice and a POD to start auditing shipments."}
        </EmptyState>
      </>
    )
  }

  const { kpis, exposure } = data

  const ordered = (names: string[], counts: Record<string, number>) => [...names.filter((name) => name in counts), ...Object.keys(counts).filter((name) => !names.includes(name))]
  const statuses: Segment[] = ordered(STATUS_ORDER, data.status_counts).map((name) => ({ key: name, label: sentence(name), value: data.status_counts[name], colour: STATUS_COLOURS[name] ?? CHART.neutral }))
  const reviewCounts = data.reviews as unknown as Record<string, number>
  const reviews: Segment[] = ordered(REVIEW_ORDER, reviewCounts).map((name) => ({ key: name, label: sentence(name), value: reviewCounts[name], colour: REVIEW_COLOURS[name] ?? CHART.neutral }))
  const categories = Object.entries(data.category_counts)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value)
  const carriers = [...data.carriers].sort((a, b) => b.shipments - a.shipments)
  const shipmentTotal = statuses.reduce((sum, item) => sum + item.value, 0)

  return (
    <>
      {header}

      <div className="mb-6 space-y-3">
        {data.open_ingestion_errors > 0 && (
          <Alert tone="warn" className="items-start">
            <AlertTriangle />
            <p>
              {data.open_ingestion_errors} document{data.open_ingestion_errors === 1 ? "" : "s"} could not be ingested. Until each is corrected or resolved, no shipment is shown as clean.{" "}
              <Link to="/documents" className="font-medium underline underline-offset-2">
                See Document Intelligence
              </Link>
            </p>
          </Alert>
        )}
        {data.unreadable_files.length > 0 && (
          <Alert tone="warn" className="items-start">
            <FileX2 />
            <p>
              {data.unreadable_files.length} file{data.unreadable_files.length === 1 ? "" : "s"} could not be used and belong to no shipment: {data.unreadable_files.map((file) => file.filename).join(", ")}. No shipment is reported clean while they cannot be ruled out.
            </p>
          </Alert>
        )}
      </div>

      <section aria-label="Key figures" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <KpiCard title="Shipments" value={count(kpis.identified)} detail={`${count(kpis.reconciled)} reconciled`} icon={<Truck />} definition={data.metric_definitions.identified} />
        <KpiCard
          title="With discrepancies"
          value={count(kpis.with_discrepancies)}
          detail={kpis.discrepancy_rate === null ? "No shipment could be reconciled" : `${kpis.discrepancy_rate}% of reconciled shipments`}
          icon={<Percent />}
          tone={kpis.with_discrepancies > 0 ? "bad" : "ok"}
          definition={data.metric_definitions.with_discrepancies}
        />
        <KpiCard title="Needs review" value={count(kpis.needs_review)} detail={`${count(kpis.no_discrepancies)} with no discrepancies`} icon={<Eye />} tone={kpis.needs_review > 0 ? "warn" : "ok"} definition={data.metric_definitions.needs_review} />
        <KpiCard
          title="Invoice variance"
          value={money(kpis.invoice_variance)}
          detail={kpis.invoice_variance === null ? "No invoice could be checked" : `Across ${kpis.invoice_variance_covered} invoice${kpis.invoice_variance_covered === 1 ? "" : "s"}`}
          icon={<DollarSign />}
          definition={data.metric_definitions.invoice_variance}
        />
      </section>

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <ChartCard title="Audit outcomes" description="Every identified shipment by what the audit found. Select a result to list those shipments.">
          <Donut
            segments={statuses}
            unit={shipmentTotal === 1 ? "shipment" : "shipments"}
            label="Shipments by audit result"
            selectHint="List these shipments"
            onSelect={(status) => navigate(`/shipments?status=${encodeURIComponent(status)}`)}
          />
        </ChartCard>

        <ChartCard title="Where discrepancies come from" description="The audit compares each shipment's BOL, POD and invoice three ways. For each check: how many shipments passed it, failed it, or could not be checked.">
          <Loaded query={shipments}>
            {(list) =>
              list.shipments.length === 0 ? (
                <EmptyState title="No shipment to check">The checks appear once a shipment has its documents.</EmptyState>
              ) : (
                <Columns
                  horizontal
                  stacked
                  integers
                  height={200}
                  data={checkBreakdown(list.shipments)}
                  category="check"
                  series={OUTCOME_SERIES}
                  label="Shipments by check and outcome"
                  format={(value) => count(value)}
                />
              )
            }
          </Loaded>
        </ChartCard>
      </div>

      <Section
        title="Shipments needing attention"
        description="Most discrepancies first, then the largest invoice difference. Open a shipment to see the documents and evidence behind each finding."
        actions={
          <Button variant="outline" size="sm" asChild>
            <Link to="/shipments">
              All shipments <ArrowRight />
            </Link>
          </Button>
        }
      >
        <Loaded query={shipments} rows={3}>
          {(list) => {
            const urgent = attentionList(list.shipments)

            return urgent.length === 0 ? (
              <EmptyState title="Nothing needs attention">Every reconciled shipment agrees across its documents.</EmptyState>
            ) : (
              <Table>
                <TableHeader>
                  <tr>
                    <TableHead>Shipment</TableHead>
                    <TableHead>Carrier and route</TableHead>
                    <TableHead>Failed checks</TableHead>
                    <TableHead className="text-right">Discrepancies</TableHead>
                    <TableHead className="text-right">Invoice difference</TableHead>
                    <TableHead className="text-right">Evidence</TableHead>
                  </tr>
                </TableHeader>
                <TableBody>
                  {urgent.map((row) => (
                    <TableRow key={row.shipment_key}>
                      <TableCell className="font-medium">
                        <Link to={`/shipments/${encodeURIComponent(row.shipment_id)}`} className="break-all text-primary hover:underline">
                          {row.shipment_id}
                        </Link>
                        <span className="mt-1 block">
                          <StatusBadge status={row.final_status} />
                        </span>
                      </TableCell>
                      <TableCell className="max-w-[16rem]">
                        <span className="block truncate">{row.carrier ?? NA}</span>
                        <span className="block truncate text-xs text-muted-foreground">
                          {row.origin ?? NA} → {row.destination ?? NA}
                        </span>
                      </TableCell>
                      <TableCell>
                        <span className="flex flex-wrap gap-1">
                          {CHECKS.filter((check) => checkOutcome(row[check.field]) === "discrepancy").map((check) => (
                            <span key={check.key} className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                              {check.label}
                              <StatusBadge status={row[check.field]} />
                            </span>
                          ))}
                        </span>
                      </TableCell>
                      <TableCell className="tabular text-right">{count(row.discrepancy_count)}</TableCell>
                      <TableCell className="tabular text-right">{row.invoice_difference ? money(row.invoice_difference) : NA}</TableCell>
                      <TableCell className="text-right">
                        <Link to={`/freight-audit/${encodeURIComponent(row.shipment_id)}`} className="whitespace-nowrap text-xs font-medium text-primary hover:underline">
                          Open audit
                        </Link>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )
          }}
        </Loaded>
      </Section>

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <ChartCard title="Invoice totals against their line items" description="For each shipment's invoice: the total it states and the sum of its line items, as printed. Amounts are never converted between currencies.">
          <Loaded query={exposureRows}>
            {(result) => {
              const invoices = invoiceComparison(result.rows)

              if (invoices.currencies.length > 1) {
                return <EmptyState title="Several currencies">The invoices state {invoices.currencies.join(", ")}; amounts in different currencies are not drawn on one axis.</EmptyState>
              }

              return invoices.rows.length === 0 ? (
                <EmptyState title="No invoice could be compared">No invoice had both a readable total and readable line items.</EmptyState>
              ) : (
                <>
                  <Columns data={invoices.rows} category="shipment" series={INVOICE_SERIES} label="Stated invoice total and sum of line items per shipment" format={(value) => money(value)} height={230} />
                  {invoices.incomplete > 0 && (
                    <p className="mt-3 text-xs text-muted-foreground">
                      {invoices.incomplete} invoice{invoices.incomplete === 1 ? " is" : "s are"} left out: a total or a line item could not be read.
                    </p>
                  )}
                </>
              )
            }}
          </Loaded>
        </ChartCard>

        <ChartCard title="Invoice differences" description="Stated total minus the line items, per shipment. An arithmetic check of the invoice itself, not a finding of overbilling.">
          <Loaded query={exposureRows}>
            {(result) => {
              const invoices = invoiceComparison(result.rows)

              return invoices.rows.length === 0 || invoices.currencies.length > 1 ? (
                <EmptyState title="No difference to show">No comparable invoice.</EmptyState>
              ) : (
                <>
                  <Legend
                    className="mb-3"
                    items={[
                      { key: "over", label: "Total above its line items", colour: CHART.bad },
                      { key: "under", label: "Total below its line items", colour: CHART.warn },
                      { key: "equal", label: "Adds up", colour: CHART.info },
                    ]}
                  />
                  <Columns
                    data={invoices.rows}
                    category="shipment"
                    series={[{ key: "difference", label: "Difference", colour: CHART.info }]}
                    label="Invoice difference per shipment"
                    format={(value) => money(value)}
                    labels
                    zeroLine
                    height={230}
                    colourOf={(row) => (row.difference > 0 ? CHART.bad : row.difference < 0 ? CHART.warn : CHART.info)}
                  />
                </>
              )
            }}
          </Loaded>
        </ChartCard>

        <ChartCard title="Discrepancy categories" description={categories.length === 0 ? "No discrepancy was detected." : "Discrepancies found, by what disagreed across the documents. Select one to list its shipments."}>
          {categories.length === 0 ? (
            <EmptyState title="No discrepancies">Every reconciled shipment agrees across its documents.</EmptyState>
          ) : (
            <BarList
              label="Discrepancies by category"
              rows={categories.map((item) => ({
                key: item.name,
                name: item.name,
                label: (
                  <Link to={`/shipments?category=${encodeURIComponent(item.name)}`} className="hover:text-primary hover:underline">
                    {item.name}
                  </Link>
                ),
                display: count(item.value),
                parts: [{ key: "value", label: "Discrepancies", value: item.value, colour: CHART.bad }],
              }))}
            />
          )}
        </ChartCard>

        <ChartCard
          title="Exception review queue"
          description="Review state is kept apart from the audit: resolving an exception never changes what the audit found."
          action={
            <Link to="/exceptions" className="text-xs font-medium text-primary hover:underline">
              Open queue
            </Link>
          }
        >
          <ProportionBar segments={reviews} unit={reviews.reduce((sum, item) => sum + item.value, 0) === 1 ? "exception" : "exceptions"} label="Exceptions by review state" />
        </ChartCard>

        <ChartCard title="Carriers" description="Shipments per carrier named on the BOL. Small samples say little about a carrier; Audit History has scorecards with sample sizes.">
          {carriers.length === 0 ? (
            <EmptyState title="No carrier named">No reconciled shipment names a carrier on its BOL.</EmptyState>
          ) : (
            <>
              <Legend
                className="mb-4"
                items={[
                  { key: "bad", label: "Discrepancies found", colour: CHART.bad },
                  { key: "ok", label: "No discrepancy found", colour: CHART.info },
                ]}
              />
              <BarList
                label="Shipments per carrier"
                rows={carriers.map((item) => ({
                  key: item.carrier, name: item.carrier, label: item.carrier,
                  display: `${count(item.shipments)} shipment${item.shipments === 1 ? "" : "s"}`,
                  note: item.with_discrepancies > 0 ? `${count(item.with_discrepancies)} with discrepancies` : "No discrepancy found",
                  parts: [
                    { key: "bad", label: "Discrepancies found", value: item.with_discrepancies, colour: CHART.bad },
                    { key: "ok", label: "No discrepancy found", value: item.shipments - item.with_discrepancies, colour: CHART.info },
                  ],
                }))}
              />
            </>
          )}
        </ChartCard>

        <ChartCard
          title="Documents by type"
          description="Every stored document by type, and whether its text could be read."
          action={
            <Link to="/documents" className="text-xs font-medium text-primary hover:underline">
              Open documents
            </Link>
          }
        >
          <Loaded query={documents}>
            {(payload) =>
              payload.files.length === 0 ? (
                <EmptyState title="No documents stored">Upload PDFs in Document Intelligence.</EmptyState>
              ) : (
                <Columns stacked integers data={documentsByType(payload.files)} category="type" series={DOCUMENT_SERIES} label="Stored documents per type" format={(value) => count(value)} height={210} />
              )
            }
          </Loaded>
        </ChartCard>
      </div>

      <Section title="Financial exposure" description={data.exposure_note}>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <KpiCard title="Stated invoice total" value={exposure.mixed_currency ? NA : money(exposure.stated_total)} detail={exposure.mixed_currency ? "Amounts in different currencies are never added" : `${exposure.totals_read} of ${exposure.invoices} invoices have a readable total`} />
          <KpiCard title="Arithmetic mismatches" value={count(exposure.mismatches)} detail={exposure.absolute_variance === null ? "None confirmed" : `${money(exposure.absolute_variance)} absolute variance`} tone={exposure.mismatches > 0 ? "bad" : "ok"} icon={<PackageCheck />} />
          <KpiCard title="Verified invoices" value={count(exposure.verified)} detail="Line items add up to the stated total" tone="ok" />
          <KpiCard title="Could not be verified" value={count(exposure.unverifiable)} detail={exposure.amount_under_review === null ? "No total to show" : `${money(exposure.amount_under_review)} under review`} icon={<ClipboardCheck />} tone={exposure.unverifiable > 0 ? "warn" : "ok"} />
        </div>
      </Section>

      <Section title="Document readiness">
        <Card className="grid divide-y sm:grid-cols-[1fr_1fr_1fr_auto] sm:divide-x sm:divide-y-0">
          <div className="p-5">
            <StatusBadge status="ready" />
            <p className="tabular mt-3 text-2xl font-semibold">{count(data.completeness.ready)}</p>
            <p className="mt-1 text-[13px] text-muted-foreground">shipment{data.completeness.ready === 1 ? "" : "s"} with a BOL, invoice and POD</p>
          </div>
          <div className="p-5">
            <StatusBadge status="incomplete" />
            <p className="tabular mt-3 text-2xl font-semibold">{count(data.completeness.incomplete)}</p>
            <p className="mt-1 text-[13px] text-muted-foreground">missing a document</p>
          </div>
          <div className="p-5">
            <span className="text-[13px] font-medium text-muted-foreground">Files not used</span>
            <p className="tabular mt-3 text-2xl font-semibold">{count(data.completeness.unusable_files + data.completeness.unidentified_files)}</p>
            <p className="mt-1 text-[13px] text-muted-foreground">
              {data.completeness.unusable_files} unusable, {data.completeness.unidentified_files} unidentified
            </p>
          </div>
          <div className="flex items-center p-5">
            <Button variant="outline" size="sm" asChild>
              <Link to="/completeness">Open completeness</Link>
            </Button>
          </div>
        </Card>
      </Section>
    </>
  )
}
