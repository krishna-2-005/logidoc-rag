import { Activity, AlertTriangle, CheckCircle2, Database, RotateCw, XCircle } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router-dom"
import { ChartTable, shortDay } from "@/components/common/ChartTable"
import { BarList, Columns } from "@/components/common/Charts"
import { KpiCard } from "@/components/common/KpiCard"
import { EmptyState, ErrorState, KpiSkeletons } from "@/components/common/states"
import { ConnectionBadge, EnabledBadge, RunBadge } from "@/components/integrations/badges"
import { ChartCard } from "@/components/ui/card"
import { Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useIntegrationDashboard } from "@/hooks/integrations"
import { OUTCOME, SERIES } from "@/lib/chart"
import { count, dateTime } from "@/lib/format"

const WINDOWS = [7, 30, 90]

function words(text: string): string {
  return text.replace(/_/g, " ")
}

/**
 * The integrations' health over a period, from the recorded sync runs only: how
 * runs ended, the records they brought, retries and why runs failed, per day and
 * per integration.
 */
export function IntegrationHealth() {
  const [days, setDays] = useState(30)
  const view = useIntegrationDashboard(days)
  const data = view.data
  const totals = data?.totals
  const finished = totals ? totals.succeeded + totals.partial + totals.failed : 0
  const daily = (data?.daily ?? []).map((day) => ({ ...day, label: shortDay(day.day) }))
  const anyRecord = daily.some((day) => day.imported + day.updated + day.rejected > 0)

  return (
    <>
      <form aria-label="Health period" className="mb-4 flex flex-wrap items-end gap-3 rounded-lg border bg-card p-4 shadow-card" onSubmit={(event) => event.preventDefault()}>
        <div className="w-48 space-y-1.5">
          <Label htmlFor="health-days">Period</Label>
          <Select id="health-days" value={days} onChange={(event) => setDays(Number(event.target.value))}>
            {WINDOWS.map((value) => (
              <option key={value} value={value}>
                Last {value} days
              </option>
            ))}
          </Select>
        </div>
        {data && <p className="pb-2 text-xs text-muted-foreground">From {shortDay(data.window.start)} to {shortDay(data.window.end)} (UTC days), by the day each run started.</p>}
      </form>

      {view.isPending && <KpiSkeletons count={6} />}
      {view.isError && <ErrorState error={view.error} onRetry={() => view.refetch()} title="The integration health could not be loaded" />}

      {data && totals && (
        <div className="space-y-6">
          <section aria-label="Sync figures" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <KpiCard title="Sync runs" value={count(totals.runs)} icon={<Activity />} detail={totals.interrupted ? `${totals.interrupted} interrupted by a stop` : "Scheduled and manual"} />
            <KpiCard
              title="Success rate"
              value={totals.success_rate === null ? "n/a" : `${Math.round(totals.success_rate * 100)}%`}
              icon={<CheckCircle2 />}
              detail={finished ? `${totals.succeeded} succeeded of ${finished} finished` : "No run finished in the period"}
              definition="Runs that succeeded / runs that finished (succeeded, partial or failed). A partial run finished with rejected records or an error after some pages."
              tone={totals.success_rate !== null && totals.success_rate < 0.9 ? "warn" : "default"}
            />
            <KpiCard title="Records imported" value={count(totals.imported)} icon={<Database />} detail={`${count(totals.updated)} updated`} />
            <KpiCard title="Records rejected" value={count(totals.rejected)} icon={<XCircle />} tone={totals.rejected ? "warn" : "default"} detail="Refused by validation, with their reasons in each run" />
            <KpiCard title="Retries" value={count(totals.retries)} icon={<RotateCw />} detail="Pages fetched again after a rate limit or a timeout" />
            <KpiCard title="Failed runs" value={count(totals.failed)} icon={<AlertTriangle />} tone={totals.failed ? "bad" : "default"} detail={`${count(totals.partial)} partial`} />
          </section>

          {totals.runs === 0 ? (
            <EmptyState title="No sync run in this period">Runs appear when an integration syncs on its schedule or an administrator runs one.</EmptyState>
          ) : (
            <>
              <div className="grid gap-4 xl:grid-cols-2">
                <ChartCard title="Sync outcomes per day" description="How each run ended, by the day it started.">
                  <Columns
                    stacked
                    integers
                    data={daily}
                    category="label"
                    label="Succeeded, partial, failed and interrupted sync runs per day"
                    format={(value) => count(value)}
                    series={[
                      { key: "succeeded", label: "Succeeded", colour: OUTCOME.done },
                      { key: "partial", label: "Partial", colour: OUTCOME.partial },
                      { key: "failed", label: "Failed", colour: OUTCOME.failed },
                      { key: "interrupted", label: "Interrupted", colour: OUTCOME.interrupted },
                    ]}
                  />
                  <ChartTable caption="Sync runs per day" columns={["Day", "Succeeded", "Partial", "Failed", "Interrupted"]} rows={daily.map((day) => [day.label, day.succeeded, day.partial, day.failed, day.interrupted])} />
                </ChartCard>
                <ChartCard title="Records per day" description="Records the runs imported, updated and rejected, by the day the run started.">
                  {anyRecord ? (
                    <>
                      <Columns
                        integers
                        data={daily}
                        category="label"
                        label="Records imported, updated and rejected per day"
                        format={(value) => count(value)}
                        series={[
                          { key: "imported", label: "Imported", colour: SERIES[0] },
                          { key: "updated", label: "Updated", colour: SERIES[3] },
                          { key: "rejected", label: "Rejected", colour: OUTCOME.failed },
                        ]}
                      />
                      <ChartTable caption="Records per day" columns={["Day", "Imported", "Updated", "Rejected"]} rows={daily.map((day) => [day.label, day.imported, day.updated, day.rejected])} />
                    </>
                  ) : (
                    <EmptyState title="No record changed">The runs in this period found nothing new.</EmptyState>
                  )}
                </ChartCard>
              </div>

              <ChartCard title="Why runs failed" description={`Error categories of the ${count(totals.runs)} runs in the period (a run has at most one).`}>
                {data.errors.length === 0 ? (
                  <EmptyState title="No run ended with an error">Every run in the period finished without an error category.</EmptyState>
                ) : (
                  <BarList
                    label="Sync runs by error category"
                    rows={data.errors.map((item) => ({
                      key: item.category,
                      name: words(item.category),
                      label: words(item.category),
                      display: `${item.runs} of ${totals.runs}`,
                      parts: [{ key: item.category, label: "Runs", value: item.runs, colour: OUTCOME.failed }],
                    }))}
                  />
                )}
              </ChartCard>
            </>
          )}

          <ChartCard title="Each integration" description="Its state now, and its runs in the period.">
            {data.integrations.length === 0 ? (
              <EmptyState title="No integration configured" />
            ) : (
              <Table aria-label="Integration health">
                <TableHeader>
                  <TableRow>
                    <TableHead>Integration</TableHead>
                    <TableHead>Connection</TableHead>
                    <TableHead>Last success</TableHead>
                    <TableHead>Last failure</TableHead>
                    <TableHead>Next sync</TableHead>
                    <TableHead className="text-right">Runs</TableHead>
                    <TableHead className="text-right">Imported</TableHead>
                    <TableHead className="text-right">Rejected</TableHead>
                    <TableHead className="text-right">Retries</TableHead>
                    <TableHead>Errors</TableHead>
                    <TableHead>Recent runs</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.integrations.map((item) => (
                    <TableRow key={item.id}>
                      <TableCell>
                        <Link to={`/integrations/connections/${item.id}`} className="font-medium text-primary hover:underline">
                          {item.name}
                        </Link>
                        {!item.enabled && (
                          <span className="ml-2 inline-block align-middle">
                            <EnabledBadge enabled={false} />
                          </span>
                        )}
                      </TableCell>
                      <TableCell>
                        <ConnectionBadge status={item.connection_status} />
                        {item.consecutive_failures > 0 && <p className="mt-1 text-xs text-bad">{item.consecutive_failures} failed in a row</p>}
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{item.last_success_at ? dateTime(item.last_success_at) : "Never"}</TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{item.last_failure_at ? dateTime(item.last_failure_at) : "None"}</TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{item.enabled ? dateTime(item.next_sync_at) : "Disabled"}</TableCell>
                      <TableCell className="tabular text-right">{item.window.runs}</TableCell>
                      <TableCell className="tabular text-right">{item.window.imported}</TableCell>
                      <TableCell className={`tabular text-right ${item.window.rejected ? "text-warn" : ""}`}>{item.window.rejected}</TableCell>
                      <TableCell className="tabular text-right">{item.window.retries}</TableCell>
                      <TableCell className="text-xs">
                        {Object.keys(item.window.error_categories).length === 0 ? (
                          <span className="text-muted-foreground">None</span>
                        ) : (
                          Object.entries(item.window.error_categories).map(([category, number]) => (
                            <span key={category} className="block">
                              {words(category)}: {number}
                            </span>
                          ))
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          {item.recent_runs.length === 0 ? <span className="text-xs text-muted-foreground">None</span> : item.recent_runs.map((run) => <RunBadge key={run.id} status={run.status} />)}
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </ChartCard>
        </div>
      )}
    </>
  )
}
