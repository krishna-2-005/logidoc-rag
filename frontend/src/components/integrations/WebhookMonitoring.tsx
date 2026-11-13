import { AlertTriangle, CheckCircle2, Clock, RotateCw, Send } from "lucide-react"
import { useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { ChartTable, shortDay } from "@/components/common/ChartTable"
import { BarList, Columns } from "@/components/common/Charts"
import { KpiCard } from "@/components/common/KpiCard"
import { Pagination } from "@/components/common/Pagination"
import { EmptyState, ErrorState, KpiSkeletons, LoadingBlock } from "@/components/common/states"
import { DeliveryBadge, EnabledBadge } from "@/components/integrations/badges"
import { ChartCard } from "@/components/ui/card"
import { Input, Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useDeliveries, useEndpoints, useWebhookMonitoring, useWebhookSettings } from "@/hooks/integrations"
import { OUTCOME } from "@/lib/chart"
import { count, dateTime } from "@/lib/format"

const WINDOWS = [7, 14, 30, 90]

function Filters({ days, setDays, endpoint, setEndpoint, eventType, setEventType }: {
  days: number
  setDays: (days: number) => void
  endpoint: string
  setEndpoint: (value: string) => void
  eventType: string
  setEventType: (value: string) => void
}) {
  const endpoints = useEndpoints(true)
  const settings = useWebhookSettings()

  return (
    <form aria-label="Monitoring filters" className="mb-4 grid gap-3 rounded-lg border bg-card p-4 shadow-card sm:grid-cols-3" onSubmit={(event) => event.preventDefault()}>
      <div className="space-y-1.5">
        <Label htmlFor="monitoring-days">Period</Label>
        <Select id="monitoring-days" value={days} onChange={(event) => setDays(Number(event.target.value))}>
          {WINDOWS.map((value) => (
            <option key={value} value={value}>
              Last {value} days
            </option>
          ))}
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="monitoring-endpoint">Endpoint</Label>
        <Select id="monitoring-endpoint" value={endpoint} onChange={(event) => setEndpoint(event.target.value)}>
          <option value="">All endpoints</option>
          {(endpoints.data?.endpoints ?? []).map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
              {item.archived ? " (archived)" : ""}
            </option>
          ))}
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="monitoring-event">Event type</Label>
        <Select id="monitoring-event" value={eventType} onChange={(event) => setEventType(event.target.value)}>
          <option value="">All event types</option>
          {(settings.data?.event_types ?? []).map((item) => (
            <option key={item.type} value={item.type}>
              {item.type}
            </option>
          ))}
        </Select>
      </div>
    </form>
  )
}

/**
 * Delivery monitoring over a period: outcome counts, retries, the endpoints that
 * failed, the error categories and the attempt latency per day. Every figure comes
 * from the recorded deliveries and attempts (test deliveries are left out).
 */
export function DeliveryMonitoring() {
  const [days, setDays] = useState(14)
  const [endpoint, setEndpoint] = useState("")
  const [eventType, setEventType] = useState("")
  const view = useWebhookMonitoring(days, endpoint ? Number(endpoint) : undefined, eventType || undefined)
  const data = view.data
  const totals = data?.totals
  const finished = totals ? totals.succeeded + totals.failed : 0
  const outcomeDays = (data?.daily ?? []).map((day) => ({ ...day, label: shortDay(day.day) }))
  const anyAttempt = outcomeDays.some((day) => day.attempts > 0)

  return (
    <>
      <Filters days={days} setDays={setDays} endpoint={endpoint} setEndpoint={setEndpoint} eventType={eventType} setEventType={setEventType} />

      {view.isPending && <KpiSkeletons count={6} />}
      {view.isError && <ErrorState error={view.error} onRetry={() => view.refetch()} title="The delivery figures could not be loaded" />}

      {data && totals && (
        <div className="space-y-6">
          <section aria-label="Delivery figures" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <KpiCard title="Deliveries" value={count(totals.deliveries)} icon={<Send />} detail={`Created in the last ${data.window.days} days, test deliveries left out`} />
            <KpiCard
              title="Success rate"
              value={totals.success_rate === null ? "n/a" : `${Math.round(totals.success_rate * 100)}%`}
              icon={<CheckCircle2 />}
              detail={finished ? `${totals.succeeded} delivered of ${finished} finished` : "No delivery finished in the period"}
              definition="Delivered / (delivered + failed). Deliveries still waiting are not counted."
              tone={totals.success_rate !== null && totals.success_rate < 0.95 ? "warn" : "default"}
            />
            <KpiCard title="Failed" value={count(totals.failed)} icon={<AlertTriangle />} tone={totals.failed ? "bad" : "default"} detail={data.last_failure_at ? `Last failure ${dateTime(data.last_failure_at)}` : "None in the period"} />
            <KpiCard title="Waiting or retrying" value={count(totals.waiting)} icon={<Clock />} detail={totals.cancelled ? `${totals.cancelled} cancelled (archived endpoint)` : "Queued for a first attempt or a retry"} />
            <KpiCard title="Retry attempts" value={count(totals.retry_attempts)} icon={<RotateCw />} detail={`Of ${count(totals.attempts)} attempts in the period`} definition="Attempts after the first attempt of a delivery." />
            <KpiCard title="Last successful delivery" value={data.last_success_at ? dateTime(data.last_success_at) : "None"} icon={<CheckCircle2 />} detail="In the period and filters chosen" />
          </section>

          {totals.deliveries === 0 ? (
            <EmptyState title="No delivery in this period">No committed event reached an endpoint with these filters. Deliveries appear once a subscribed event is committed.</EmptyState>
          ) : (
            <>
              <div className="grid gap-4 xl:grid-cols-2">
                <ChartCard title="Delivery outcomes per day" description="By the day each delivery was created; a delivery still waiting may succeed or fail later.">
                  <Columns
                    stacked
                    integers
                    data={outcomeDays}
                    category="label"
                    label="Delivered, waiting and failed deliveries per day"
                    format={(value) => count(value)}
                    series={[
                      { key: "succeeded", label: "Delivered", colour: OUTCOME.done },
                      { key: "waiting", label: "Waiting", colour: OUTCOME.partial },
                      { key: "failed", label: "Failed", colour: OUTCOME.failed },
                    ]}
                  />
                  <ChartTable caption="Deliveries per day" columns={["Day", "Delivered", "Waiting", "Failed"]} rows={outcomeDays.map((day) => [day.label, day.succeeded, day.waiting, day.failed])} />
                </ChartCard>
                <ChartCard title="Attempt latency per day" description="Median and 95th percentile of the time each attempt took, including failed ones.">
                  {anyAttempt ? (
                    <>
                      <Columns
                        data={outcomeDays}
                        category="label"
                        label="Median and 95th percentile attempt latency per day, in milliseconds"
                        format={(value) => `${value} ms`}
                        series={[
                          { key: "latency_p50_ms", label: "Median", colour: "#2a78d6" },
                          { key: "latency_p95_ms", label: "95th percentile", colour: "#eb6834" },
                        ]}
                      />
                      <ChartTable caption="Attempt latency per day" columns={["Day", "Attempts", "Median (ms)", "95th percentile (ms)"]} rows={outcomeDays.map((day) => [day.label, day.attempts, day.latency_p50_ms ?? "—", day.latency_p95_ms ?? "—"])} />
                    </>
                  ) : (
                    <EmptyState title="No attempt in this period">Latency is measured on each attempt to send a delivery.</EmptyState>
                  )}
                </ChartCard>
              </div>

              <div className="grid gap-4 xl:grid-cols-2">
                <ChartCard title="Endpoints" description="Endpoints with failed deliveries first.">
                  <Table aria-label="Deliveries by endpoint">
                    <TableHeader>
                      <TableRow>
                        <TableHead>Endpoint</TableHead>
                        <TableHead className="text-right">Delivered</TableHead>
                        <TableHead className="text-right">Failed</TableHead>
                        <TableHead className="text-right">Waiting</TableHead>
                        <TableHead>Last success</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {data.endpoints.map((item) => (
                        <TableRow key={item.endpoint_id}>
                          <TableCell>
                            <Link to={`/integrations/webhooks/${item.endpoint_id}`} className="font-medium text-primary hover:underline">
                              {item.name}
                            </Link>
                            {(!item.enabled || item.archived) && (
                              <span className="ml-2 inline-block align-middle">
                                <EnabledBadge enabled={item.enabled} archived={item.archived} />
                              </span>
                            )}
                          </TableCell>
                          <TableCell className="tabular text-right">{item.succeeded}</TableCell>
                          <TableCell className={`tabular text-right ${item.failed ? "font-medium text-bad" : ""}`}>{item.failed}</TableCell>
                          <TableCell className="tabular text-right">{item.waiting}</TableCell>
                          <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{item.last_success_at ? dateTime(item.last_success_at) : "None"}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </ChartCard>
                <ChartCard title="Why attempts failed" description={`Error categories of the ${count(totals.attempts)} attempts in the period.`}>
                  {data.errors.length === 0 ? (
                    <EmptyState title="No failed attempt">Every attempt in the period was accepted.</EmptyState>
                  ) : (
                    <BarList
                      label="Failed attempts by error category"
                      rows={data.errors.map((item) => ({
                        key: item.category,
                        name: item.category.replace(/_/g, " "),
                        label: item.category.replace(/_/g, " "),
                        display: `${item.attempts} of ${totals.attempts}`,
                        parts: [{ key: item.category, label: "Attempts", value: item.attempts, colour: OUTCOME.failed }],
                      }))}
                    />
                  )}
                </ChartCard>
              </div>

              {data.event_types.length > 0 && (
                <ChartCard title="Event types" description="Deliveries in the period by the type of event.">
                  <Table aria-label="Deliveries by event type">
                    <TableHeader>
                      <TableRow>
                        <TableHead>Event type</TableHead>
                        <TableHead className="text-right">Deliveries</TableHead>
                        <TableHead className="text-right">Delivered</TableHead>
                        <TableHead className="text-right">Failed</TableHead>
                        <TableHead className="text-right">Waiting</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {data.event_types.map((item) => (
                        <TableRow key={item.event_type}>
                          <TableCell className="font-mono text-xs">{item.event_type}</TableCell>
                          <TableCell className="tabular text-right">{item.deliveries}</TableCell>
                          <TableCell className="tabular text-right">{item.succeeded}</TableCell>
                          <TableCell className="tabular text-right">{item.failed}</TableCell>
                          <TableCell className="tabular text-right">{item.waiting}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </ChartCard>
              )}
            </>
          )}
        </div>
      )}
    </>
  )
}

const STATUSES: [string, string][] = [
  ["failed", "Failed"],
  ["retrying", "Retrying"],
  ["pending", "Queued"],
  ["succeeded", "Delivered"],
  ["cancelled", "Cancelled"],
]

/** Every delivery, filtered by endpoint, status, event type and day; each opens its detail with every attempt. */
export function DeliveryHistory() {
  const navigate = useNavigate()
  const endpoints = useEndpoints(true)
  const settings = useWebhookSettings()
  const [endpoint, setEndpoint] = useState("")
  const [status, setStatus] = useState("")
  const [eventType, setEventType] = useState("")
  const [from, setFrom] = useState("")
  const [to, setTo] = useState("")
  const [offset, setOffset] = useState(0)
  const invalid = !!(from && to && from > to)
  const deliveries = useDeliveries({
    endpoint_id: endpoint ? Number(endpoint) : undefined, status: status ? [status] : [], event_type: eventType ? [eventType] : [],
    from: invalid ? undefined : from || undefined, to: invalid ? undefined : to || undefined, offset, limit: 25,
  })
  const change = (setter: (value: string) => void) => (value: string) => {
    setter(value)
    setOffset(0)
  }

  return (
    <>
      <form aria-label="Delivery filters" className="mb-4 grid gap-3 rounded-lg border bg-card p-4 shadow-card sm:grid-cols-2 lg:grid-cols-5" onSubmit={(event) => event.preventDefault()}>
        <div className="space-y-1.5">
          <Label htmlFor="history-endpoint">Endpoint</Label>
          <Select id="history-endpoint" value={endpoint} onChange={(event) => change(setEndpoint)(event.target.value)}>
            <option value="">All endpoints</option>
            {(endpoints.data?.endpoints ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="history-status">Status</Label>
          <Select id="history-status" value={status} onChange={(event) => change(setStatus)(event.target.value)}>
            <option value="">All statuses</option>
            {STATUSES.map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="history-event">Event type</Label>
          <Select id="history-event" value={eventType} onChange={(event) => change(setEventType)(event.target.value)}>
            <option value="">All event types</option>
            {(settings.data?.event_types ?? []).map((item) => (
              <option key={item.type} value={item.type}>
                {item.type}
              </option>
            ))}
            <option value="webhook.test">webhook.test</option>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="history-from">Created from</Label>
          <Input id="history-from" type="date" value={from} onChange={(event) => change(setFrom)(event.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="history-to">Created to</Label>
          <Input id="history-to" type="date" value={to} onChange={(event) => change(setTo)(event.target.value)} />
        </div>
        {invalid && <p role="alert" className="text-sm text-bad sm:col-span-2 lg:col-span-5">The start day is after the end day.</p>}
      </form>

      {deliveries.isPending && <LoadingBlock rows={4} label="Loading the deliveries" />}
      {deliveries.isError && <ErrorState error={deliveries.error} onRetry={() => deliveries.refetch()} title="The deliveries could not be loaded" />}
      {deliveries.data && deliveries.data.total === 0 && <EmptyState title="No delivery matches">Change the filters, or wait for a subscribed event to be committed.</EmptyState>}
      {deliveries.data && deliveries.data.total > 0 && (
        <>
          <Table aria-label="Delivery history">
            <TableHeader>
              <TableRow>
                <TableHead>Delivery</TableHead>
                <TableHead>Endpoint</TableHead>
                <TableHead>Event</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Attempts</TableHead>
                <TableHead>Last answer</TableHead>
                <TableHead>Finished</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {deliveries.data.deliveries.map((item) => (
                <TableRow key={item.id} className="cursor-pointer" onClick={() => navigate(`/integrations/deliveries/${item.id}`)}>
                  <TableCell>
                    <Link to={`/integrations/deliveries/${item.id}`} className="text-primary hover:underline" onClick={(event) => event.stopPropagation()}>
                      #{item.id}
                    </Link>
                    <p className="whitespace-nowrap text-xs text-muted-foreground">{dateTime(item.created_at)}</p>
                  </TableCell>
                  <TableCell className="text-sm">{item.endpoint_name}</TableCell>
                  <TableCell>
                    <span className="font-mono text-xs">{item.event_type}</span>
                    {item.test && <span className="ml-1 text-xs text-muted-foreground">(test)</span>}
                  </TableCell>
                  <TableCell>
                    <DeliveryBadge status={item.status} />
                  </TableCell>
                  <TableCell className="tabular text-right">
                    {item.attempts}
                    {item.manual_retries > 0 && <span className="block text-xs text-muted-foreground">{item.manual_retries} by hand</span>}
                  </TableCell>
                  <TableCell className="text-xs">
                    {item.last_status_code ? `HTTP ${item.last_status_code}` : "—"}
                    {item.last_error && <span className="block text-muted-foreground">{item.last_error.replace(/_/g, " ")}</span>}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{item.completed_at ? dateTime(item.completed_at) : "Not yet"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Pagination offset={deliveries.data.offset} limit={deliveries.data.limit} total={deliveries.data.total} onChange={setOffset} label="Delivery pages" />
        </>
      )}
    </>
  )
}
