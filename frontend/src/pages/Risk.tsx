import { AlertTriangle, Gauge, ShieldAlert, ShieldQuestion } from "lucide-react"
import { useState, type FormEvent } from "react"
import { Link } from "react-router-dom"
import { ChartTable } from "@/components/common/ChartTable"
import { BarList, Columns, Legend } from "@/components/common/Charts"
import { KpiCard } from "@/components/common/KpiCard"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { Pagination } from "@/components/common/Pagination"
import { EmptyState, ErrorState, KpiSkeletons, LoadingBlock } from "@/components/common/states"
import { Badge, type Tone } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, ChartCard } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input, Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { type RiskFilters, useRiskOverview, useRiskShipment, useRiskShipments, useRiskTrends } from "@/hooks/queries"
import { REVIEW_COLOURS, REVIEW_ORDER, RISK, RISK_ORDER, sentence } from "@/lib/chart"
import { NA, count } from "@/lib/format"
import type { RiskCategory, RiskFactor } from "@/lib/types"

const PAGE = 25

export const CATEGORY_LOOK: Record<RiskCategory, { tone: Tone; text: string }> = {
  low: { tone: "info", text: "Low" },
  medium: { tone: "warn", text: "Medium" },
  high: { tone: "bad", text: "High" },
  critical: { tone: "bad", text: "Critical" },
}

export function RiskBadge({ category }: { category: RiskCategory }) {
  const look = CATEGORY_LOOK[category]

  return (
    <Badge tone={look.tone}>
      <span aria-hidden="true" className="inline-block size-2 rounded-full" style={{ background: RISK[category] }} />
      {look.text}
    </Badge>
  )
}

const FACTOR_TONE: Record<RiskFactor["status"], Tone> = { scored: "bad", clear: "ok", unavailable: "neutral" }
const FACTOR_TEXT: Record<RiskFactor["status"], string> = { scored: "Scored", clear: "Nothing found", unavailable: "Not available" }

function evidenceText(item: RiskFactor["evidence"][number]): string {
  if (item.type === "amount") return `${item.currency ?? ""} ${typeof item.amount === "number" ? item.amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : NA} (${item.reference})`.trim()
  if (item.type === "exception") return `${item.reference}${item.category ? ` · ${item.category}` : ""}${item.severity ? ` · ${item.severity.toLowerCase()}` : ""}${item.status ? ` · ${item.status.replace(/_/g, " ")}` : ""}`
  if (item.type === "document") return `${item.reference} missing`
  return `Freight audit: ${sentence(item.reference)}`
}

/** One shipment's score, factor by factor, with the evidence each rests on and what could not be assessed */
function RiskDetail({ shipmentId, onClose }: { shipmentId: string; onClose: () => void }) {
  const detail = useRiskShipment(shipmentId)
  const data = detail.data

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent aria-describedby="risk-detail-description" className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Risk of {shipmentId}</DialogTitle>
          <DialogDescription id="risk-detail-description">
            {data ? `Score ${data.score} of 100 under model ${data.model.version}. Every point comes from a factor below.` : "Loading the score."}
          </DialogDescription>
        </DialogHeader>

        {detail.isPending && <LoadingBlock rows={4} label="Loading the risk explanation" />}
        {detail.isError && <ErrorState error={detail.error} onRetry={() => detail.refetch()} title="The risk explanation could not be loaded" />}

        {data && (
          <div className="max-h-[65vh] space-y-4 overflow-y-auto pr-1" data-testid="risk-detail">
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <RiskBadge category={data.category} />
              <span className="tabular font-semibold">{data.score} / 100</span>
              <span className="text-muted-foreground">
                Thresholds: medium from {data.thresholds.medium}, high from {data.thresholds.high}, critical from {data.thresholds.critical}.
              </span>
              <Link className="text-primary underline-offset-2 hover:underline" to={`/shipments/${encodeURIComponent(data.shipment_id)}`}>
                Open the shipment
              </Link>
            </div>
            {data.partial && (
              <p className="rounded-md border border-dashed p-2 text-xs text-muted-foreground">
                Partial: {data.unavailable.length} signal(s) could not be assessed and add nothing. Missing evidence is never read as no risk.
              </p>
            )}
            <Table aria-label="Risk factors">
              <TableHeader>
                <TableRow>
                  <TableHead>Factor</TableHead>
                  <TableHead className="text-right">Points</TableHead>
                  <TableHead>Why</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.factors.map((factor) => (
                  <TableRow key={factor.signal} className="align-top" data-testid={`factor-${factor.signal}`}>
                    <TableCell className="min-w-[9rem]">
                      <p className="font-medium">{factor.label}</p>
                      <Badge tone={FACTOR_TONE[factor.status]}>{FACTOR_TEXT[factor.status]}</Badge>
                    </TableCell>
                    <TableCell className="tabular whitespace-nowrap text-right">
                      {factor.status === "unavailable" ? NA : `${factor.points} of ${factor.max}`}
                      {factor.uncapped_points > factor.points && <span className="block text-xs text-muted-foreground">capped from {factor.uncapped_points}</span>}
                    </TableCell>
                    <TableCell className="text-sm">
                      <p>{factor.detail}</p>
                      {factor.evidence.length > 0 && (
                        <ul className="mt-1 list-disc pl-5 text-xs text-muted-foreground">
                          {factor.evidence.map((item, index) => (
                            <li key={index} className="break-all">
                              {evidenceText(item)}
                            </li>
                          ))}
                        </ul>
                      )}
                      {factor.not_counted.length > 0 && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          Not counted (resolved, dismissed or waived): {factor.not_counted.map((item) => item.reference).join(", ")}
                        </p>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <p className="text-xs text-muted-foreground">
              Model {data.model.version} (digest {data.model.digest}). A duplicate invoice number is an indicator to check, not a finding of fraud.
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

/**
 * Shipment risk: an explainable score from 0 to 100 per shipment, made only of the
 * audit's own evidence under a versioned model. No language model is involved.
 */
export function Risk() {
  const [draft, setDraft] = useState<RiskFilters>({})
  const [filters, setFilters] = useState<RiskFilters>({})
  const [offset, setOffset] = useState(0)
  const [sort, setSort] = useState<"score" | "shipment" | "carrier" | "date">("score")
  const [period, setPeriod] = useState<"month" | "week">("month")
  const [selected, setSelected] = useState<string | null>(null)

  const overview = useRiskOverview(filters)
  const table = useRiskShipments({ ...filters, sort, order: sort === "score" ? "desc" : "asc", offset, limit: PAGE })
  const trends = useRiskTrends(period)

  const apply = (event: FormEvent) => {
    event.preventDefault()
    setOffset(0)
    setFilters({ ...draft, search: draft.search?.trim() || undefined })
  }

  const data = overview.data
  const totals = data?.totals
  const distribution = RISK_ORDER.map((category) => ({ category: CATEGORY_LOOK[category].text, key: category, count: totals?.by_category[category] ?? 0 }))
  const trendRows = (trends.data?.points ?? []).map((point) => ({ period: point.period, ...point.by_category, mean: point.mean_score }))

  return (
    <>
      <PageHeader
        title="Shipment risk"
        description={
          <>
            An explainable score from 0 to 100 for each shipment, made only of the audit's own evidence: unresolved discrepancies, values not verified, duplicate invoice indicators, amounts at stake, missing documents and the freight audit outcome.
            {data && ` Model ${data.model.version}.`}
          </>
        }
      />

      <form aria-label="Risk filters" onSubmit={apply} className="mb-6 flex flex-wrap items-end gap-3 rounded-lg border bg-card p-4 shadow-card">
        <div className="w-56 space-y-1.5">
          <Label htmlFor="risk-search">Shipment or carrier</Label>
          <Input id="risk-search" value={draft.search ?? ""} maxLength={100} onChange={(event) => setDraft({ ...draft, search: event.target.value })} />
        </div>
        <div className="w-40 space-y-1.5">
          <Label htmlFor="risk-category">Risk category</Label>
          <Select id="risk-category" value={draft.category?.[0] ?? ""} onChange={(event) => setDraft({ ...draft, category: event.target.value ? [event.target.value] : undefined })}>
            <option value="">All</option>
            {RISK_ORDER.map((category) => (
              <option key={category} value={category}>
                {CATEGORY_LOOK[category].text}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-52 space-y-1.5">
          <Label htmlFor="risk-carrier">Carrier</Label>
          <Select id="risk-carrier" value={draft.carrier ?? ""} onChange={(event) => setDraft({ ...draft, carrier: event.target.value || undefined })}>
            <option value="">All carriers</option>
            {(data?.carriers ?? [])
              .filter((item) => item.carrier_key)
              .map((item) => (
                <option key={item.carrier_key} value={item.carrier_key ?? ""}>
                  {item.carrier}
                </option>
              ))}
          </Select>
        </div>
        <div className="w-40 space-y-1.5">
          <Label htmlFor="risk-start">Shipment date from</Label>
          <Input id="risk-start" type="date" value={draft.start ?? ""} onChange={(event) => setDraft({ ...draft, start: event.target.value || undefined })} />
        </div>
        <div className="w-40 space-y-1.5">
          <Label htmlFor="risk-end">to</Label>
          <Input id="risk-end" type="date" value={draft.end ?? ""} onChange={(event) => setDraft({ ...draft, end: event.target.value || undefined })} />
        </div>
        <Button type="submit">Apply</Button>
      </form>

      {overview.isPending && <KpiSkeletons count={4} />}
      {overview.isError && <ErrorState error={overview.error} onRetry={() => overview.refetch()} title="The risk figures could not be loaded" />}

      {data && totals && (
        <div className="space-y-6">
          <section aria-label="Risk figures" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <KpiCard title="Shipments scored" value={count(totals.shipments)} icon={<Gauge />} detail="In the selection" />
            <KpiCard
              title="High or critical"
              value={count(totals.high_risk)}
              icon={<ShieldAlert />}
              tone={totals.high_risk ? "bad" : "default"}
              detail={totals.shipments ? `${totals.high_risk} of ${totals.shipments} shipments` : "No shipment selected"}
              definition={`Score of ${data.model.thresholds.high} or more (critical from ${data.model.thresholds.critical}).`}
            />
            <KpiCard title="Mean score" value={totals.mean_score === null ? "n/a" : String(totals.mean_score)} icon={<AlertTriangle />} detail="Out of 100" definition="The average of the selected shipments' scores; n/a when none is selected." />
            <KpiCard title="Partly assessed" value={count(totals.partial)} icon={<ShieldQuestion />} detail="A signal's evidence was not available" definition="Shipments for which at least one signal could not be assessed. It adds nothing to their score and is never read as no risk." />
          </section>

          {totals.shipments === 0 ? (
            <EmptyState title="No shipment matches">Change the filters, or add documents: every shipment of the audit is scored.</EmptyState>
          ) : (
            <div className="grid gap-4 xl:grid-cols-2">
              <ChartCard title="Risk distribution" description="Shipments in each risk category.">
                <Columns
                  integers
                  labels
                  data={distribution}
                  category="category"
                  label={`Shipments by risk category: ${distribution.map((item) => `${item.count} ${item.category.toLowerCase()}`).join(", ")}`}
                  format={(value) => count(value)}
                  series={[{ key: "count", label: "Shipments", colour: RISK.low }]}
                  colourOf={(row) => RISK[row.key]}
                />
                <ChartTable caption="Shipments by risk category" columns={["Category", "Shipments"]} rows={distribution.map((item) => [item.category, item.count])} />
              </ChartCard>

              <ChartCard
                title="Risk over time"
                description="What recorded audit runs found, each shipment once per period at its latest run."
                action={
                  <Select aria-label="Period" value={period} onChange={(event) => setPeriod(event.target.value as "month" | "week")} className="w-32">
                    <option value="month">By month</option>
                    <option value="week">By week</option>
                  </Select>
                }
              >
                {trends.isError && <ErrorState error={trends.error} onRetry={() => trends.refetch()} title="The risk trend could not be loaded" />}
                {trends.data && trendRows.length === 0 && <EmptyState title="No recorded audit run">Trends appear once an administrator records audit runs (Audit History).</EmptyState>}
                {trendRows.length > 0 && (
                  <>
                    <Columns
                      stacked
                      integers
                      data={trendRows}
                      category="period"
                      label="Recorded shipments per period, stacked by risk category"
                      format={(value) => count(value)}
                      series={RISK_ORDER.map((category) => ({ key: category, label: CATEGORY_LOOK[category].text, colour: RISK[category] }))}
                    />
                    <ChartTable
                      caption="Recorded shipments by risk category per period"
                      columns={["Period", "Low", "Medium", "High", "Critical", "Mean score"]}
                      rows={trendRows.map((row) => [row.period, row.low, row.medium, row.high, row.critical, row.mean])}
                    />
                    <p className="mt-2 text-xs text-muted-foreground">{trends.data?.note}</p>
                  </>
                )}
              </ChartCard>

              <ChartCard title="Exceptions" description="The selected shipments' exceptions by category and review state.">
                {data.exceptions.length === 0 ? (
                  <EmptyState title="No exception">The selected shipments have no exception.</EmptyState>
                ) : (
                  <>
                    <Legend items={REVIEW_ORDER.map((status) => ({ key: status, label: sentence(status), colour: REVIEW_COLOURS[status] }))} />
                    <BarList
                      label="Exceptions by category and review state"
                      rows={data.exceptions.map((group) => ({
                        key: group.category,
                        name: group.category,
                        label: group.category,
                        display: count(group.total),
                        parts: REVIEW_ORDER.filter((status) => group.by_status[status]).map((status) => ({
                          key: status, label: sentence(status), value: group.by_status[status], colour: REVIEW_COLOURS[status],
                        })),
                      }))}
                    />
                    <ChartTable
                      caption="Exceptions by category and review state"
                      columns={["Category", "Total", ...REVIEW_ORDER.map(sentence)]}
                      rows={data.exceptions.map((group) => [group.category, group.total, ...REVIEW_ORDER.map((status) => group.by_status[status] ?? 0)])}
                    />
                  </>
                )}
              </ChartCard>

              <ChartCard title="Carrier risk patterns" description="Each carrier's shipments, mean score and what scored most often.">
                <Table aria-label="Carrier risk patterns">
                  <TableHeader>
                    <TableRow>
                      <TableHead>Carrier</TableHead>
                      <TableHead className="text-right">Shipments</TableHead>
                      <TableHead className="text-right">Mean</TableHead>
                      <TableHead className="text-right">High or critical</TableHead>
                      <TableHead>Most frequent</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.carriers.map((item) => (
                      <TableRow key={`${item.carrier_key}-${item.carrier}`}>
                        <TableCell className="font-medium">{item.carrier}</TableCell>
                        <TableCell className="tabular text-right">{item.shipments}</TableCell>
                        <TableCell className="tabular text-right">{item.mean_score}</TableCell>
                        <TableCell className={`tabular text-right ${item.high_risk ? "font-medium text-bad" : ""}`}>{item.high_risk}</TableCell>
                        <TableCell className="text-xs">{item.top_signals.length ? item.top_signals.map((signal) => `${signal.label} (${signal.shipments})`).join(", ") : "Nothing scored"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </ChartCard>
            </div>
          )}
        </div>
      )}

      <Section title="Shipments by risk" description="Select a shipment to see each factor, the evidence it rests on and what could not be assessed.">
        <div className="mb-3 flex items-center gap-2">
          <Label htmlFor="risk-sort">Sort by</Label>
          <Select id="risk-sort" className="w-44" value={sort} onChange={(event) => { setSort(event.target.value as typeof sort); setOffset(0) }}>
            <option value="score">Highest score</option>
            <option value="shipment">Shipment</option>
            <option value="carrier">Carrier</option>
            <option value="date">Date</option>
          </Select>
        </div>
        {table.isPending && <LoadingBlock rows={5} label="Loading shipments" />}
        {table.isError && <ErrorState error={table.error} onRetry={() => table.refetch()} title="The shipments could not be loaded" />}
        {table.data && table.data.rows.length === 0 && <EmptyState title="No shipment matches">Change the filters above.</EmptyState>}
        {table.data && table.data.rows.length > 0 && (
          <Card>
            <Table aria-label="Shipments by risk">
              <TableHeader>
                <TableRow>
                  <TableHead>Shipment</TableHead>
                  <TableHead>Carrier</TableHead>
                  <TableHead>Date</TableHead>
                  <TableHead className="text-right">Score</TableHead>
                  <TableHead>Category</TableHead>
                  <TableHead>Main factors</TableHead>
                  <TableHead className="text-right">Open exceptions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {table.data.rows.map((row) => (
                  <TableRow key={row.shipment_key} className="cursor-pointer" onClick={() => setSelected(row.shipment_id)} data-testid={`risk-row-${row.shipment_key}`}>
                    <TableCell>
                      <button type="button" className="font-medium text-primary hover:underline" onClick={(event) => { event.stopPropagation(); setSelected(row.shipment_id) }} aria-label={`Explain the risk of ${row.shipment_id}`}>
                        {row.shipment_id}
                      </button>
                    </TableCell>
                    <TableCell>{row.carrier}</TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{row.date ?? "Not dated"}</TableCell>
                    <TableCell className="tabular text-right font-semibold">
                      {row.score}
                      {row.partial && <span className="block text-[11px] font-normal text-muted-foreground">partial</span>}
                    </TableCell>
                    <TableCell>
                      <RiskBadge category={row.category} />
                    </TableCell>
                    <TableCell className="text-xs">{row.top_factors.length ? row.top_factors.map((item) => `${item.label} (${item.points})`).join(", ") : "Nothing scored"}</TableCell>
                    <TableCell className="tabular text-right">{row.open_exceptions}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        )}
        {table.data && <Pagination offset={offset} limit={PAGE} total={table.data.total} onChange={setOffset} label="Risk table pages" />}
      </Section>

      {selected && <RiskDetail shipmentId={selected} onClose={() => setSelected(null)} />}
    </>
  )
}
