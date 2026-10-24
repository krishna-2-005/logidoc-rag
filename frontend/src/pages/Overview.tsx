import { AlertTriangle, ClipboardCheck, DollarSign, Eye, FileX2, PackageCheck, Percent, RefreshCw, Truck } from "lucide-react"
import { Link } from "react-router-dom"
import { BarList, Legend, ProportionBar, type Segment } from "@/components/common/Charts"
import { EmptyState, ErrorState, KpiSkeletons, LoadingBlock } from "@/components/common/states"
import { KpiCard } from "@/components/common/KpiCard"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, ChartCard } from "@/components/ui/card"
import { useOverview } from "@/hooks/queries"
import { CHART, REVIEW_COLOURS, REVIEW_ORDER, STATUS_COLOURS, STATUS_ORDER, sentence } from "@/lib/chart"
import { count, money, NA } from "@/lib/format"

export function Overview() {
  const overview = useOverview()

  const header = (
    <PageHeader
      title="Overview"
      description="Where your shipments stand: what the audit found, what still needs a person, and what the invoices say."
      actions={
        <Button variant="outline" size="sm" onClick={() => overview.refetch()} disabled={overview.isFetching}>
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
        <ChartCard title="Audit results" description="Every identified shipment, by what the audit found.">
          <ProportionBar segments={statuses} unit={statuses.reduce((sum, item) => sum + item.value, 0) === 1 ? "shipment" : "shipments"} label="Shipments by audit result" />
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

        <ChartCard title="Discrepancy categories" description={categories.length === 0 ? "No discrepancy was detected." : "Discrepancies found, by what disagreed across the documents."}>
          {categories.length === 0 ? (
            <EmptyState title="No discrepancies">Every reconciled shipment agrees across its documents.</EmptyState>
          ) : (
            <BarList
              label="Discrepancies by category"
              rows={categories.map((item) => ({
                key: item.name, name: item.name, label: item.name, display: count(item.value),
                parts: [{ key: "value", label: "Discrepancies", value: item.value, colour: CHART.bad }],
              }))}
            />
          )}
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
