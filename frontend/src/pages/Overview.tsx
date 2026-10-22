import { AlertTriangle, ClipboardCheck, DollarSign, Eye, FileX2, PackageCheck, Percent, RefreshCw, Truck } from "lucide-react"
import { Link } from "react-router-dom"
import { Bar, BarChart, CartesianGrid, Cell, Legend, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts"
import { EmptyState, ErrorState, KpiSkeletons, LoadingBlock } from "@/components/common/states"
import { KpiCard } from "@/components/common/KpiCard"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { useOverview } from "@/hooks/queries"
import { CHART, REVIEW_COLOURS, STATUS_COLOURS, summarize } from "@/lib/chart"
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

  const statuses = Object.entries(data.status_counts).map(([name, value]) => ({ name, value }))
  const categories = Object.entries(data.category_counts)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value)
  const reviews = Object.entries(data.reviews).map(([name, value]) => ({ name, value }))
  const carriers = data.carriers.map((item) => ({ name: item.carrier, "With discrepancies": item.with_discrepancies, "No discrepancy found": item.shipments - item.with_discrepancies }))

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
          title="Invoice arithmetic variance"
          value={money(kpis.invoice_variance)}
          detail={kpis.invoice_variance === null ? "No invoice could be checked" : `Across ${kpis.invoice_variance_covered} invoice${kpis.invoice_variance_covered === 1 ? "" : "s"}`}
          icon={<DollarSign />}
          definition={data.metric_definitions.invoice_variance}
        />
      </section>

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Audit result by shipment</CardTitle>
            <CardDescription>{summarize(statuses.map((item) => [item.name.toLowerCase(), item.value]), "shipments")}</CardDescription>
          </CardHeader>
          <CardContent>
            <div role="img" aria-label={`Shipments by audit result: ${summarize(statuses.map((item) => [item.name.toLowerCase(), item.value]), "shipments")}`} className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={statuses} layout="vertical" margin={{ left: 8, right: 24 }}>
                  <CartesianGrid horizontal={false} stroke={CHART.grid} />
                  <XAxis type="number" allowDecimals={false} stroke={CHART.axis} fontSize={12} />
                  <YAxis type="category" dataKey="name" width={170} stroke={CHART.axis} fontSize={12} tickFormatter={(value: string) => value.charAt(0) + value.slice(1).toLowerCase()} />
                  <ChartTooltip cursor={{ fill: "#f1f5f9" }} />
                  <Bar dataKey="value" name="Shipments" radius={[0, 4, 4, 0]} label={{ position: "right", fontSize: 12, fill: CHART.axis }}>
                    {statuses.map((item) => (
                      <Cell key={item.name} fill={STATUS_COLOURS[item.name] ?? CHART.neutral} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Discrepancy categories</CardTitle>
            <CardDescription>{categories.length === 0 ? "No discrepancy was detected." : summarize(categories.map((item) => [item.name, item.value]), "discrepancies")}</CardDescription>
          </CardHeader>
          <CardContent>
            {categories.length === 0 ? (
              <EmptyState title="No discrepancies">Every reconciled shipment agrees across its documents.</EmptyState>
            ) : (
              <div role="img" aria-label={`Discrepancies by category: ${summarize(categories.map((item) => [item.name, item.value]), "discrepancies")}`} className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={categories} layout="vertical" margin={{ left: 8, right: 24 }}>
                    <CartesianGrid horizontal={false} stroke={CHART.grid} />
                    <XAxis type="number" allowDecimals={false} stroke={CHART.axis} fontSize={12} />
                    <YAxis type="category" dataKey="name" width={120} stroke={CHART.axis} fontSize={12} />
                    <ChartTooltip cursor={{ fill: "#f1f5f9" }} />
                    <Bar dataKey="value" name="Shipments" fill={CHART.bad} radius={[0, 4, 4, 0]} label={{ position: "right", fontSize: 12, fill: CHART.axis }} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Carriers</CardTitle>
            <CardDescription>Shipments per carrier named on the BOL. Small samples say little about a carrier; see Audit History for scorecards with sample sizes.</CardDescription>
          </CardHeader>
          <CardContent>
            <div role="img" aria-label={`Shipments per carrier: ${data.carriers.map((item) => `${item.carrier} ${item.shipments}, of which ${item.with_discrepancies} with discrepancies`).join("; ")}`} className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={carriers} margin={{ left: 0, right: 8 }}>
                  <CartesianGrid vertical={false} stroke={CHART.grid} />
                  <XAxis dataKey="name" stroke={CHART.axis} fontSize={12} />
                  <YAxis allowDecimals={false} stroke={CHART.axis} fontSize={12} />
                  <ChartTooltip cursor={{ fill: "#f1f5f9" }} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="With discrepancies" stackId="a" fill={CHART.bad} />
                  <Bar dataKey="No discrepancy found" stackId="a" fill={CHART.ok} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Exception review queue</CardTitle>
            <CardDescription>
              Review state is kept apart from the audit: resolving an exception never changes what the audit found.{" "}
              <Link to="/exceptions" className="font-medium text-primary underline underline-offset-2">
                Open the Exception Center
              </Link>
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div role="img" aria-label={`Exceptions by review state: ${summarize(reviews.map((item) => [item.name.replace("_", " "), item.value]), "exceptions")}`} className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={reviews} margin={{ left: 0, right: 8 }}>
                  <CartesianGrid vertical={false} stroke={CHART.grid} />
                  <XAxis dataKey="name" stroke={CHART.axis} fontSize={12} tickFormatter={(value: string) => value.replace("_", " ")} />
                  <YAxis allowDecimals={false} stroke={CHART.axis} fontSize={12} />
                  <ChartTooltip cursor={{ fill: "#f1f5f9" }} />
                  <Bar dataKey="value" name="Exceptions" radius={[4, 4, 0, 0]} label={{ position: "top", fontSize: 12, fill: CHART.axis }}>
                    {reviews.map((item) => (
                      <Cell key={item.name} fill={REVIEW_COLOURS[item.name]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>
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
        <Card className="p-5">
          <div className="flex flex-wrap items-center gap-x-8 gap-y-3 text-sm">
            <span className="flex items-center gap-2">
              <StatusBadge status="ready" /> {data.completeness.ready} shipment{data.completeness.ready === 1 ? "" : "s"} with a BOL, invoice and POD
            </span>
            <span className="flex items-center gap-2">
              <StatusBadge status="incomplete" /> {data.completeness.incomplete} missing a document
            </span>
            <span className="text-muted-foreground">
              {data.completeness.unusable_files} unusable and {data.completeness.unidentified_files} unidentified file(s)
            </span>
            <Link to="/completeness" className="font-medium text-primary underline underline-offset-2">
              Open Document Completeness
            </Link>
          </div>
        </Card>
      </Section>
    </>
  )
}
