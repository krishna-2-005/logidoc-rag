import { CheckCircle2, CircleHelp, Info, Receipt, Search, Sigma } from "lucide-react"
import { useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { KpiCard } from "@/components/common/KpiCard"
import { PageHeader } from "@/components/common/PageHeader"
import { EmptyState, ErrorState, KpiSkeletons, LoadingBlock } from "@/components/common/states"
import { Alert } from "@/components/ui/alert"
import { Badge, type Tone } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Input, Label } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useDebounced } from "@/hooks/useDebounced"
import { useExposure } from "@/hooks/queries"
import { count, money, NA } from "@/lib/format"
import type { InvoiceExposure } from "@/lib/types"
import { cn } from "@/lib/utils"

/** The audit's invoice classifications (financial_exposure.py), as the filter values the API takes */
const CHECKS: { key: string; label: string; tone: Tone }[] = [
  { key: "mismatch", label: "Mismatch", tone: "bad" },
  { key: "verified", label: "Verified", tone: "ok" },
  { key: "unverifiable", label: "Could not be verified", tone: "warn" },
]

function CheckBadge({ classification }: { classification: string }) {
  const look = CHECKS.find((item) => item.key === classification)
  return <Badge tone={look?.tone ?? "neutral"}>{look?.label ?? classification}</Badge>
}

/** Stated total minus the line items: above in red, below in amber, equal in grey */
function Difference({ value, currency }: { value: number | null; currency: string | null }) {
  if (value === null) return <span className="text-muted-foreground">{NA}</span>
  return (
    <span className={cn("tabular font-medium", value > 0 ? "text-bad" : value < 0 ? "text-warn" : "text-muted-foreground")}>
      {value > 0 ? "+" : ""}
      {money(value, currency)}
    </span>
  )
}

export function Invoices() {
  const [params, setParams] = useSearchParams()
  const check = params.get("check") ?? ""
  const [search, setSearch] = useState(params.get("search") ?? "")
  const debounced = useDebounced(search)

  // The figures of every invoice for the cards and tabs, and the server-filtered list for the table
  const overall = useExposure()
  const listed = useExposure({ search: debounced, classification: check ? [check] : [] })

  const choose = (next: string) => setParams(next ? { check: next } : {}, { replace: true })
  const summary = overall.data?.summary
  const rows = ((listed.data?.rows ?? []) as unknown as InvoiceExposure[])
    .slice()
    .sort((a, b) => (b.absolute_difference ?? -1) - (a.absolute_difference ?? -1) || a.shipment_id.localeCompare(b.shipment_id))

  const tabs = [
    { key: "", label: "All invoices", value: summary?.invoices },
    { key: "mismatch", label: "Mismatch", value: summary?.mismatches },
    { key: "verified", label: "Verified", value: summary?.verified },
    { key: "unverifiable", label: "Could not be verified", value: summary?.unverifiable },
  ]

  return (
    <>
      <PageHeader
        title="Invoice Register"
        description="Every freight invoice: the total it states against the sum of its own line items. A mismatch is an arithmetic finding about the invoice; it does not prove overbilling."
      />

      {overall.isPending && <KpiSkeletons count={4} />}
      {overall.isError && <ErrorState error={overall.error} onRetry={() => overall.refetch()} title="The invoices could not be loaded" />}

      {summary && (
        <section aria-label="Invoice figures" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <KpiCard
            title="Stated invoice total"
            value={summary.mixed_currency ? NA : money(summary.stated_total)}
            detail={summary.mixed_currency ? "Amounts in different currencies are never added" : `${summary.totals_read} of ${summary.invoices} invoices have a readable total`}
            icon={<Receipt />}
            to="/invoices"
            cta="All invoices"
          />
          <KpiCard
            title="Invoice variance"
            value={money(summary.absolute_variance)}
            detail={summary.absolute_variance === null ? "No invoice could be checked" : `Net ${money(summary.net_difference)} across ${count(summary.mismatches)} mismatched invoice${summary.mismatches === 1 ? "" : "s"}`}
            icon={<Sigma />}
            tone={summary.mismatches > 0 ? "bad" : "ok"}
            to="/invoices?check=mismatch"
            cta="Show the mismatches"
          />
          <KpiCard title="Verified invoices" value={count(summary.verified)} detail="Line items add up to the stated total" icon={<CheckCircle2 />} tone="ok" to="/invoices?check=verified" cta="Show verified" />
          <KpiCard
            title="Could not be verified"
            value={count(summary.unverifiable)}
            detail={summary.amount_under_review === null ? "A total or a line item could not be read" : `${money(summary.amount_under_review)} under review`}
            icon={<CircleHelp />}
            tone={summary.unverifiable > 0 ? "warn" : "ok"}
            to="/invoices?check=unverifiable"
            cta="Show these invoices"
          />
        </section>
      )}

      <Card className="mt-8">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
          <div role="tablist" aria-label="Invoice check" className="flex flex-wrap gap-1 rounded-lg bg-secondary p-1">
            {tabs.map((tab) => (
              <button
                key={tab.key || "all"}
                type="button"
                role="tab"
                aria-selected={check === tab.key}
                onClick={() => choose(tab.key)}
                className={cn(
                  "flex items-center gap-2 rounded-md px-3 py-1.5 text-[13px] font-medium text-muted-foreground transition-colors hover:text-foreground",
                  check === tab.key && "bg-card text-foreground shadow-card",
                )}
              >
                {tab.label}
                {tab.value !== undefined && <span className="tabular rounded-full bg-background px-1.5 text-[11px] text-muted-foreground">{tab.value}</span>}
              </button>
            ))}
          </div>
          <div className="relative w-full max-w-xs">
            <Label htmlFor="invoice-search" className="sr-only">
              Search shipment
            </Label>
            <Search className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" aria-hidden="true" />
            <Input id="invoice-search" className="pl-8" placeholder="Search shipment ID" value={search} onChange={(event) => setSearch(event.target.value)} />
          </div>
        </div>

        {listed.isPending && (
          <div className="p-4">
            <LoadingBlock rows={5} label="Loading invoices" />
          </div>
        )}
        {listed.isError && (
          <div className="p-4">
            <ErrorState error={listed.error} onRetry={() => listed.refetch()} title="The invoices could not be loaded" />
          </div>
        )}

        {listed.data && rows.length === 0 && (
          <div className="p-4">
            <EmptyState title={summary?.invoices ? "No invoice matches" : "No invoices yet"} icon={<Receipt className="size-5" />}>
              {summary?.invoices ? "Try another view or clear the search." : "Upload a shipment's BOL, invoice and POD in Document Intelligence."}
            </EmptyState>
          </div>
        )}

        {rows.length > 0 && (
          <>
            <Table>
              <TableHeader>
                <tr>
                  <TableHead>Shipment</TableHead>
                  <TableHead>Carrier</TableHead>
                  <TableHead className="text-right">Stated total</TableHead>
                  <TableHead className="text-right">Sum of line items</TableHead>
                  <TableHead className="text-right">Difference</TableHead>
                  <TableHead>Check</TableHead>
                  <TableHead className="text-right">Evidence</TableHead>
                </tr>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.shipment_key}>
                    <TableCell className="whitespace-nowrap font-medium">
                      <Link to={`/shipments/${encodeURIComponent(row.shipment_id)}`} className="text-primary underline-offset-2 hover:underline">
                        {row.shipment_id}
                      </Link>
                    </TableCell>
                    <TableCell>{row.carrier ?? NA}</TableCell>
                    <TableCell className="tabular text-right">{money(row.stated_total, row.currency)}</TableCell>
                    <TableCell className="tabular text-right">{money(row.line_item_sum, row.currency)}</TableCell>
                    <TableCell className="text-right">
                      <Difference value={row.signed_difference} currency={row.currency} />
                    </TableCell>
                    <TableCell>
                      <CheckBadge classification={row.classification} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Link to={`/freight-audit/${encodeURIComponent(row.shipment_id)}`} className="text-[13px] font-medium text-primary hover:underline">
                        Open audit
                      </Link>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {listed.data && (
              <p className="flex flex-wrap gap-x-4 gap-y-1 border-t px-4 py-3 text-xs text-muted-foreground" aria-live="polite">
                <span>
                  {rows.length} invoice{rows.length === 1 ? "" : "s"} shown
                </span>
                <span>Stated total {listed.data.summary.mixed_currency ? NA : money(listed.data.summary.stated_total)}</span>
                <span>Variance {money(listed.data.summary.absolute_variance)}</span>
              </p>
            )}
          </>
        )}
      </Card>

      {overall.data?.note && (
        <Alert tone="info" className="mt-6 items-start">
          <Info />
          <p>{overall.data.note}</p>
        </Alert>
      )}
    </>
  )
}
