import { useQuery } from "@tanstack/react-query"
import { Download, History as HistoryIcon, Info } from "lucide-react"
import { Fragment, useState } from "react"
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts"
import { Legend as ChartLegend, Swatch } from "@/components/common/Charts"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { EmptyState, ErrorState, LoadingBlock, errorMessage } from "@/components/common/states"
import { ChangeSummary, Timeline } from "@/components/common/Timeline"
import { RunInspector } from "@/components/extraction/RunInspector"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input, Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs } from "@/components/ui/tabs"
import { can, useRecordRun, useRuns, useSession, useShipmentHistory, useShipments, useStaleness, useTrends } from "@/hooks/queries"
import { download, get } from "@/lib/api"
import { CHART, SERIES as SERIES_COLOURS } from "@/lib/chart"
import { count, dateTime, interval, money, NA, rate } from "@/lib/format"
import type { AuditRun, Proportion, SnapshotChanges, Snapshot, Trends } from "@/lib/types"

// Four identities, so four categorical slots in their fixed order (never status colours)
const SERIES: [string, "discrepancy" | "delay" | "shortage" | "invoice_mismatch", string][] = [
  ["Discrepancy rate", "discrepancy", SERIES_COLOURS[0]],
  ["Delay rate", "delay", SERIES_COLOURS[1]],
  ["Shortage rate", "shortage", SERIES_COLOURS[2]],
  ["Invoice arithmetic mismatch rate", "invoice_mismatch", SERIES_COLOURS[3]],
]

function Rate({ item }: { item: Proportion }) {
  return <span className="tabular whitespace-nowrap">{rate(item)}</span>
}

function TrendsTab() {
  const toast = useToast()
  const session = useSession()
  const [period, setPeriod] = useState("month")
  const [carrier, setCarrier] = useState("")
  const [status, setStatus] = useState("")
  const [category, setCategory] = useState("")
  const [from, setFrom] = useState("")
  const [to, setTo] = useState("")
  const [minSample, setMinSample] = useState(5)

  const trends = useTrends({ period, carrier: carrier ? [carrier] : [], status: status ? [status] : [], category: category ? [category] : [], from, to, min_sample: minSample })

  const exportCsv = () =>
    download("/api/trends/export.csv", "audit_trends.csv", { period, carrier: carrier ? [carrier] : [], status: status ? [status] : [], category: category ? [category] : [], from, to, min_sample: minSample }).then(
      (name) => toast.success(`Downloaded ${name}`),
      (error: Error) => toast.error(error.message),
    )

  const data = trends.data

  return (
    <>
      <Alert tone="info" className="mb-5">
        <Info />
        <p>
          These figures come from <strong>recorded audit runs</strong>, not from the documents as they are now. {data?.notes[0] ?? "Invoice variances are arithmetic mismatches, not proof of overbilling."}
        </p>
      </Alert>

      <form aria-label="Filter trends" className="mb-5 grid gap-3 rounded-lg border bg-card p-4 shadow-card sm:grid-cols-2 xl:grid-cols-7" onSubmit={(event) => event.preventDefault()}>
        <div className="space-y-1.5">
          <Label htmlFor="tr-period">Period</Label>
          <Select id="tr-period" value={period} onChange={(event) => setPeriod(event.target.value)}>
            <option value="week">Week</option>
            <option value="month">Month</option>
            <option value="quarter">Quarter</option>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="tr-carrier">Carrier</Label>
          <Select id="tr-carrier" value={carrier} onChange={(event) => setCarrier(event.target.value)}>
            <option value="">All carriers</option>
            {data?.options.carriers.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="tr-status">Result</Label>
          <Select id="tr-status" value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">Any result</option>
            {data?.options.statuses.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="tr-category">Discrepancy category</Label>
          <Select id="tr-category" value={category} onChange={(event) => setCategory(event.target.value)}>
            <option value="">Any category</option>
            {data?.options.categories.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="tr-from">Audited from</Label>
          <Input id="tr-from" type="date" value={from} onChange={(event) => setFrom(event.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="tr-to">Audited to</Label>
          <Input id="tr-to" type="date" value={to} onChange={(event) => setTo(event.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="tr-min">Shortest sample to rank</Label>
          <Input id="tr-min" type="number" min={1} value={minSample} onChange={(event) => setMinSample(Math.max(1, Number(event.target.value) || 1))} />
        </div>
      </form>

      {trends.isPending && <LoadingBlock rows={5} label="Loading trends" />}
      {trends.isError && <ErrorState error={trends.error} onRetry={() => trends.refetch()} title="Trends could not be loaded" />}

      {data?.empty && (
        <EmptyState title="No audit history yet" icon={<HistoryIcon className="size-5" />}>
          Nothing has been recorded, so there is nothing to chart. An administrator records an audit run in the Recorded runs tab; no history is invented.
        </EmptyState>
      )}

      {data && !data.empty && <TrendsBody data={data} canExport={can(session.data, "reports.download")} onExport={exportCsv} />}
    </>
  )
}

/** The hover card of the trend chart: the period, then each rate with its swatch; a missing rate says so. */
function RateTooltip({ active, payload, label }: { active?: boolean; payload?: { name?: string; value?: number | null; color?: string }[]; label?: string }) {
  if (!active || !payload?.length) return null

  return (
    <div className="min-w-48 rounded-lg border bg-card px-3 py-2.5 text-xs shadow-raised">
      <p className="mb-1.5 font-medium">{label}</p>
      <ul className="space-y-1">
        {payload.map((item) => (
          <li key={item.name} className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Swatch colour={item.color ?? CHART.neutral} />
              {item.name}
            </span>
            <span className="tabular font-medium">{item.value === null || item.value === undefined ? "n/a" : `${item.value}%`}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function TrendsBody({ data, canExport, onExport }: { data: Trends; canExport: boolean; onExport: () => void }) {
  const chart = data.series.map((entry) => ({
    period: entry.period,
    ...Object.fromEntries(SERIES.map(([name, key]) => [name, entry[key].rate === null ? null : Math.round(entry[key].rate * 1000) / 10])),
  }))

  return (
    <div className="space-y-8">
      <p className="text-sm text-muted-foreground" aria-live="polite">
        {data.observations} audit observation(s) recorded · {data.shipments} shipment(s) match the filters.
      </p>

      {data.insufficient_reason && (
        <Alert tone="warn">
          <p>{data.insufficient_reason}</p>
        </Alert>
      )}

      {data.series.length >= 2 && (
        <Card className="p-6">
          <h3 className="text-[15px] font-semibold">Rates by period</h3>
          <p className="mb-4 mt-1.5 text-[13px] text-muted-foreground">Percent of the shipments the question could be answered for. A period where a rate cannot be calculated has a gap, not a zero.</p>
          <ChartLegend items={SERIES.map(([name, , colour]) => ({ key: name, label: name, colour }))} className="mb-4" />
          <div role="img" aria-label={`Rates by period for ${data.series.map((entry) => entry.period).join(", ")}. The same figures, with sample sizes, are in the table below.`} className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart} margin={{ left: -8, right: 16, top: 8, bottom: 0 }}>
                <CartesianGrid stroke={CHART.grid} vertical={false} />
                <XAxis dataKey="period" stroke={CHART.axis} fontSize={12} tickLine={false} axisLine={{ stroke: CHART.grid }} tickMargin={8} />
                <YAxis unit="%" domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} stroke={CHART.axis} fontSize={12} tickLine={false} axisLine={false} />
                <ChartTooltip content={<RateTooltip />} cursor={{ stroke: CHART.grid, strokeWidth: 1 }} />
                {SERIES.map(([name, , colour]) => (
                  <Line
                    key={name}
                    dataKey={name}
                    stroke={colour}
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    dot={{ r: 4, fill: colour, stroke: "#ffffff", strokeWidth: 2 }}
                    activeDot={{ r: 5, fill: colour, stroke: "#ffffff", strokeWidth: 2 }}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>
      )}

      {data.series.length > 0 && (
        <Section title="By period" description="Each figure is shown with the number of shipments it is based on (n).">
          <Table>
            <TableHeader>
              <tr>
                <TableHead>Period</TableHead>
                <TableHead>Shipments</TableHead>
                <TableHead>Discrepancy</TableHead>
                <TableHead>Delay</TableHead>
                <TableHead>Shortage</TableHead>
                <TableHead>Invoice mismatch</TableHead>
                <TableHead>Needs review</TableHead>
                <TableHead>Avg days late</TableHead>
              </tr>
            </TableHeader>
            <TableBody>
              {data.series.map((entry) => (
                <TableRow key={entry.period}>
                  <TableCell className="font-medium">{entry.period}</TableCell>
                  <TableCell className="tabular">{entry.shipments}</TableCell>
                  <TableCell><Rate item={entry.discrepancy} /></TableCell>
                  <TableCell><Rate item={entry.delay} /></TableCell>
                  <TableCell><Rate item={entry.shortage} /></TableCell>
                  <TableCell><Rate item={entry.invoice_mismatch} /></TableCell>
                  <TableCell className="tabular">{entry.needs_review.count} of {entry.needs_review.n}</TableCell>
                  <TableCell className="tabular">{entry.average_days_late.value === null ? NA : `${entry.average_days_late.value} (n=${entry.average_days_late.n})`}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Section>
      )}

      {data.series.length > 0 && (
        <Section title="Invoice variance by period" description="An arithmetic mismatch: the stated total differs from its line items. It is not proven overbilling. Currencies are never added together.">
          <Table>
            <TableHeader>
              <tr>
                <TableHead>Period</TableHead>
                <TableHead>Currency</TableHead>
                <TableHead>Invoices checked</TableHead>
                <TableHead>Mismatches</TableHead>
                <TableHead className="text-right">Net difference</TableHead>
                <TableHead className="text-right">Absolute variance</TableHead>
              </tr>
            </TableHeader>
            <TableBody>
              {data.series.flatMap((entry) =>
                entry.variance.map((group) => (
                  <TableRow key={`${entry.period}-${group.currency}`}>
                    <TableCell className="font-medium">{entry.period}</TableCell>
                    <TableCell>{group.currency}</TableCell>
                    <TableCell className="tabular">{group.invoices_checked}</TableCell>
                    <TableCell className="tabular">
                      {group.mismatches}
                      {group.unknown_amount > 0 && <span className="block text-xs text-muted-foreground">{group.unknown_amount} without an amount</span>}
                    </TableCell>
                    <TableCell className="tabular text-right">{money(group.net, group.currency)}</TableCell>
                    <TableCell className="tabular text-right">{money(group.absolute, group.currency)}</TableCell>
                  </TableRow>
                )),
              )}
            </TableBody>
          </Table>
        </Section>
      )}

      {data.carriers.carriers.length > 0 && (
        <Section title="Carrier scorecards" description={data.carriers.notes.join(" ")}>
          <Table>
            <TableHeader>
              <tr>
                <TableHead>Rank</TableHead>
                <TableHead>Carrier</TableHead>
                <TableHead>Shipments</TableHead>
                <TableHead>Discrepancy rate</TableHead>
                <TableHead>95% interval</TableHead>
                <TableHead>Delay</TableHead>
                <TableHead>Shortage</TableHead>
                <TableHead>Invoice mismatch</TableHead>
                <TableHead>Sample</TableHead>
              </tr>
            </TableHeader>
            <TableBody>
              {data.carriers.carriers.map((card) => (
                <TableRow key={card.carrier}>
                  <TableCell className="tabular">{card.rank ?? <span className="text-muted-foreground">not ranked</span>}</TableCell>
                  <TableCell className="font-medium">{card.carrier}</TableCell>
                  <TableCell className="tabular">{card.shipments}</TableCell>
                  <TableCell><Rate item={card.discrepancy} /></TableCell>
                  <TableCell className="tabular text-xs">{interval(card.discrepancy)}</TableCell>
                  <TableCell><Rate item={card.delay} /></TableCell>
                  <TableCell><Rate item={card.shortage} /></TableCell>
                  <TableCell><Rate item={card.invoice_mismatch} /></TableCell>
                  <TableCell>{card.sufficient ? <Badge tone="ok">Enough</Badge> : <Badge tone="warn">Too small to rank</Badge>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Section>
      )}

      {data.categories.length > 0 && (
        <Section title="Repeat discrepancy categories" description="Shipments repeating counts shipments where two or more audit runs saw the category.">
          <Table>
            <TableHeader>
              <tr>
                <TableHead>Category</TableHead>
                <TableHead>Shipments affected</TableHead>
                <TableHead>Audit observations</TableHead>
                <TableHead>Shipments repeating</TableHead>
                <TableHead>Carriers</TableHead>
              </tr>
            </TableHeader>
            <TableBody>
              {data.categories.map((item) => (
                <TableRow key={item.category}>
                  <TableCell className="font-medium">{item.category}</TableCell>
                  <TableCell className="tabular">{item.shipments_affected}</TableCell>
                  <TableCell className="tabular">{item.audit_observations}</TableCell>
                  <TableCell className="tabular">{item.shipments_repeating}</TableCell>
                  <TableCell>{item.carriers.join(", ")}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Section>
      )}

      <details className="rounded-lg border bg-card p-4 text-sm">
        <summary className="cursor-pointer font-medium">Metric definitions and limits</summary>
        <dl className="mt-3 space-y-2">
          {Object.entries(data.definitions).map(([key, text]) => (
            <div key={key}>
              <dt className="font-medium">{key.replace(/_/g, " ")}</dt>
              <dd className="text-muted-foreground">{text}</dd>
            </div>
          ))}
        </dl>
      </details>

      {canExport && data.series.length > 0 && (
        <Button variant="outline" onClick={onExport}>
          <Download /> Download filtered analytics (CSV)
        </Button>
      )}
    </div>
  )
}

interface Comparison {
  earlier: Snapshot
  later: Snapshot
  changes: SnapshotChanges
}

function ShipmentTab() {
  const list = useShipments({})
  const [chosen, setChosen] = useState("")
  const selected = chosen || list.data?.shipments[0]?.shipment_id
  const history = useShipmentHistory(selected)
  const [first, setFirst] = useState<number | null>(null)
  const [second, setSecond] = useState<number | null>(null)

  const entries = history.data ?? []
  const ids = [...entries].map((entry) => entry.snapshot.id).sort((a, b) => a - b)
  const a = first ?? ids[0]
  const b = second ?? ids[ids.length - 1]

  const compare = useQuery({
    queryKey: ["audit", "compare", selected, a, b],
    queryFn: () => get<Comparison>("/api/history/compare", { shipment: selected, first: a, second: b }),
    enabled: !!selected && ids.length >= 2 && a !== undefined && b !== undefined && a !== b,
  })

  if (list.isPending) return <LoadingBlock rows={3} label="Loading shipments" />
  if (list.isError) return <ErrorState error={list.error} onRetry={() => list.refetch()} />
  if (list.data.shipments.length === 0) return <EmptyState title="No shipments">Add documents first.</EmptyState>

  return (
    <div className="space-y-6">
      <div className="max-w-sm space-y-1.5">
        <Label htmlFor="hist-shipment">Shipment</Label>
        <Select id="hist-shipment" value={selected ?? ""} onChange={(event) => { setChosen(event.target.value); setFirst(null); setSecond(null) }}>
          {list.data.shipments.map((row) => (
            <option key={row.shipment_key}>{row.shipment_id}</option>
          ))}
        </Select>
      </div>

      {history.isPending && <LoadingBlock rows={3} label="Loading history" />}
      {history.isError && <ErrorState error={history.error} onRetry={() => history.refetch()} />}

      {history.data && entries.length === 0 && (
        <EmptyState title="Nothing recorded for this shipment">An administrator records audit runs in the Recorded runs tab.</EmptyState>
      )}

      {entries.length >= 2 && (
        <Card className="p-5">
          <h3 className="mb-3 text-sm font-semibold">Compare two recorded audits</h3>
          <div className="mb-4 grid max-w-md gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="cmp-a">Previous</Label>
              <Select id="cmp-a" value={a ?? ""} onChange={(event) => setFirst(Number(event.target.value))}>
                {ids.map((id) => (
                  <option key={id} value={id}>Snapshot #{id}</option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="cmp-b">Current</Label>
              <Select id="cmp-b" value={b ?? ""} onChange={(event) => setSecond(Number(event.target.value))}>
                {ids.map((id) => (
                  <option key={id} value={id}>Snapshot #{id}</option>
                ))}
              </Select>
            </div>
          </div>

          {a === b && <p className="text-sm text-muted-foreground">Pick two different snapshots.</p>}
          {compare.isPending && a !== b && <LoadingBlock rows={2} label="Comparing" />}
          {compare.isError && <Alert tone="bad">{errorMessage(compare.error)}</Alert>}
          {compare.data && (
            <div>
              <p className="mb-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                {dateTime(compare.data.earlier.created_at)} <StatusBadge status={compare.data.earlier.final_status ?? "NOT RECONCILED"} /> → {dateTime(compare.data.later.created_at)} <StatusBadge status={compare.data.later.final_status ?? "NOT RECONCILED"} />
              </p>
              <ChangeSummary changes={compare.data.changes} />
            </div>
          )}
        </Card>
      )}

      {entries.length > 0 && (
        <Section title="Timeline" description="Newest first, each with what changed since the audit before it.">
          <Timeline entries={entries} />
        </Section>
      )}
    </div>
  )
}

/**
 * What the API says about a run: whether a later run superseded it (a recalculation
 * after an approved correction), which it superseded, and whether it is the latest
 * recorded run of a shipment that is missing approved corrections.
 */
function RunStatus({ run, stale, onInspect }: { run: AuditRun; stale: boolean; onInspect: (runId: number) => void }) {
  return (
    <div className="space-y-1">
      {run.superseded_by.map((item) => (
        <button key={`${item.shipment_key}-${item.new_run_id}`} type="button" className="block text-left text-sm font-medium underline underline-offset-2" onClick={() => onInspect(item.new_run_id)}>
          Superseded by run #{item.new_run_id} <span className="text-xs font-normal text-muted-foreground">({item.shipment_key})</span>
        </button>
      ))}
      {run.supersedes.map((item) => (
        <p key={`${item.shipment_key}-${item.old_run_id}`} className="text-xs text-muted-foreground">
          Supersedes run #{item.old_run_id} ({item.shipment_key})
        </p>
      ))}
      {stale && (
        <Badge
          tone="warn"
          title="This is the latest recorded run of the shipment, and it does not include approved corrections the audit now reads. It is not about the extractor version; an outdated cross-check is shown on the shipment and Corrections screens."
        >
          Stale: missing approved corrections
        </Badge>
      )}
      {run.superseded_by.length === 0 && !stale && <span className="text-xs text-muted-foreground">Not superseded</span>}
    </div>
  )
}

export function RunsTab() {
  const toast = useToast()
  const session = useSession()
  const runs = useRuns()
  const staleness = useStaleness(can(session.data, "history.view"))
  const record = useRecordRun()
  const [label, setLabel] = useState("")
  const [inspecting, setInspecting] = useState<number | null>(null)
  const writable = can(session.data, "history.record")

  // A run is stale when it is the latest recorded run of a shipment that lacks approved corrections the audit already reads
  const staleRuns = new Set((staleness.data?.stale ?? []).map((item) => item.recorded_run_id))

  const inspect = (runId: number) => {
    setInspecting(runId)
    window.setTimeout(() => document.getElementById(`run-${runId}`)?.scrollIntoView?.({ block: "center" }), 0)
  }

  return (
    <div className="space-y-6">
      {writable ? (
        <Card className="p-5">
          <h3 className="text-sm font-semibold">Record an audit run</h3>
          <p className="mt-1 text-xs text-muted-foreground">Stores the current result of every shipment in the append-only history. A shipment whose result and documents did not change since its last snapshot is not stored again.</p>
          <form
            className="mt-4 flex flex-wrap items-end gap-3"
            onSubmit={(event) => {
              event.preventDefault()
              record.mutate(label || undefined, {
                onSuccess: (run) => toast.success(`Run #${run.run_id}: ${run.snapshots_written} new snapshot(s) for ${run.shipments_checked} shipment(s).`),
                onError: (error) => toast.error(errorMessage(error)),
              })
            }}
          >
            <div className="w-64 space-y-1.5">
              <Label htmlFor="run-label">Label (optional)</Label>
              <Input id="run-label" value={label} maxLength={100} onChange={(event) => setLabel(event.target.value)} placeholder="Weekly review" />
            </div>
            <Button type="submit" disabled={record.isPending}>
              {record.isPending ? "Recording…" : "Record audit run"}
            </Button>
          </form>
        </Card>
      ) : (
        <Alert tone="info"><p>Only administrators can record audit runs. You can read and compare the history.</p></Alert>
      )}

      {runs.isPending && <LoadingBlock rows={3} label="Loading runs" />}
      {runs.isError && <ErrorState error={runs.error} onRetry={() => runs.refetch()} />}
      {runs.data && runs.data.length === 0 && <EmptyState title="No audit run recorded">Nothing has been recorded yet.</EmptyState>}
      {runs.data && runs.data.length > 0 && (
        <Table>
          <TableHeader>
            <tr>
              <TableHead>Run</TableHead>
              <TableHead>Recorded</TableHead>
              <TableHead>Label</TableHead>
              <TableHead>Calculation</TableHead>
              <TableHead>Shipments checked</TableHead>
              <TableHead>New snapshots</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Inputs</TableHead>
            </tr>
          </TableHeader>
          <TableBody>
            {runs.data.map((run) => (
              <Fragment key={run.id}>
                <TableRow id={`run-${run.id}`} data-selected={inspecting === run.id}>
                  <TableCell className="font-medium">#{run.id}</TableCell>
                  <TableCell>{dateTime(run.created_at)}</TableCell>
                  <TableCell className="max-w-[16rem] break-words">{run.label ?? NA}</TableCell>
                  <TableCell>{run.calculation_version ? `v${run.calculation_version}` : <span className="text-xs text-muted-foreground">not recorded</span>}</TableCell>
                  <TableCell className="tabular">{count(run.shipments_checked)}</TableCell>
                  <TableCell className="tabular">{count(run.snapshots_written)}</TableCell>
                  <TableCell>
                    <RunStatus run={run} stale={staleRuns.has(run.id)} onInspect={inspect} />
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="outline"
                      size="sm"
                      aria-expanded={inspecting === run.id}
                      aria-controls={`run-inputs-${run.id}`}
                      aria-label={`${inspecting === run.id ? "Hide" : "Inspect"} the inputs of run ${run.id}`}
                      onClick={() => setInspecting(inspecting === run.id ? null : run.id)}
                    >
                      {inspecting === run.id ? "Hide inputs" : "Inspect inputs"}
                    </Button>
                  </TableCell>
                </TableRow>
                {inspecting === run.id && (
                  <TableRow>
                    <TableCell colSpan={8} id={`run-inputs-${run.id}`} className="bg-muted/30">
                      <RunInspector run={run} onInspect={inspect} />
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}

export function AuditHistory() {
  const [tab, setTab] = useState("trends")

  return (
    <>
      <PageHeader title="Audit History" description="What past audits recorded: how the rates moved, how carriers compare when the sample is large enough, and what changed for a shipment between two audits." />

      <Tabs
        label="History views"
        value={tab}
        onChange={setTab}
        items={[
          { id: "trends", label: "Trends and scorecards" },
          { id: "shipment", label: "Shipment history" },
          { id: "runs", label: "Recorded runs" },
        ]}
      >
        {tab === "trends" && <TrendsTab />}
        {tab === "shipment" && <ShipmentTab />}
        {tab === "runs" && <RunsTab />}
      </Tabs>
    </>
  )
}
