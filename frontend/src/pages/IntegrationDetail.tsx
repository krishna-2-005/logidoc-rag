import { ArrowLeft, PlugZap, Power, RefreshCw, Search, Shuffle } from "lucide-react"
import { useState, type FormEvent } from "react"
import { Link, useParams } from "react-router-dom"
import { Facts, JsonBlock } from "@/components/common/Facts"
import { KpiCard } from "@/components/common/KpiCard"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { Pagination } from "@/components/common/Pagination"
import { EmptyState, ErrorState, LoadingBlock, errorMessage } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { ConnectionBadge, EnabledBadge, RunBadge, every } from "@/components/integrations/badges"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input, Label, Select, Textarea } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs } from "@/components/ui/tabs"
import {
  useChangeIntegration,
  useCorrectRecord,
  useIntegration,
  useRecord,
  useRecords,
  useSimulateChanges,
  useSyncNow,
  useSyncRun,
  useSyncRuns,
  useTestIntegration,
} from "@/hooks/integrations"
import { ApiError } from "@/lib/api"
import { count, dateTime } from "@/lib/format"
import type { ImportedRecordDetail } from "@/lib/types"

const CORRECTABLE: Record<string, string[]> = {
  shipments: ["reference", "status", "carrier_name", "origin", "destination", "pickup_date", "delivery_date", "pieces", "weight_kg", "currency", "freight_charge"],
  carriers: ["legal_name", "scac", "mc_number", "dot_number", "status"],
}

function RunDetail({ id, onClose }: { id: number | null; onClose: () => void }) {
  const run = useSyncRun(id)

  return (
    <Dialog open={id !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Sync run #{id}</DialogTitle>
          <DialogDescription>What was read, what changed, and every record that was refused with its reason.</DialogDescription>
        </DialogHeader>
        {run.isPending && <LoadingBlock rows={3} label="Loading the run" />}
        {run.isError && <ErrorState error={run.error} />}
        {run.data && (
          <div className="space-y-4">
            <Facts
              columns={4}
              label="Sync run"
              items={[
                ["Status", <RunBadge key="s" status={run.data.status} />],
                ["Started", dateTime(run.data.started_at)],
                ["Trigger", `${run.data.trigger}${run.data.requested_by ? ` by ${run.data.requested_by}` : ""}`],
                ["Pages read", count(run.data.pages)],
                ["Fetched", count(run.data.fetched)],
                ["Imported / updated", `${run.data.imported} / ${run.data.updated}`],
                ["Unchanged / duplicates", `${run.data.unchanged} / ${run.data.duplicates}`],
                ["Retries", count(run.data.retries)],
                ["Corrections kept", count(run.data.conflicts)],
                ["Error", run.data.error_message && `${run.data.error_category}: ${run.data.error_message}`],
              ]}
            />
            <div>
              <h3 className="mb-2 text-sm font-semibold">Rejected records ({run.data.rejections.length})</h3>
              {run.data.rejections.length === 0 ? (
                <p className="text-sm text-muted-foreground">None.</p>
              ) : (
                <Table aria-label="Rejected records">
                  <TableHeader>
                    <TableRow>
                      <TableHead>Entity</TableHead>
                      <TableHead>External id</TableHead>
                      <TableHead>Reason</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {run.data.rejections.map((item) => (
                      <TableRow key={item.id}>
                        <TableCell className="text-xs">{item.entity}</TableCell>
                        <TableCell className="font-mono text-xs">{item.external_id ?? "—"}</TableCell>
                        <TableCell className="text-sm">{item.reason}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <JsonBlock value={run.data.cursor_before} label="Cursor before" />
              <JsonBlock value={run.data.cursor_after} label="Cursor after" />
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function Correction({ record, integrationId }: { record: ImportedRecordDetail; integrationId: number }) {
  const correct = useCorrectRecord()
  const toast = useToast()
  const fields = CORRECTABLE[record.entity] ?? []
  const [field, setField] = useState(fields[0] ?? "")
  const [value, setValue] = useState("")
  const [reason, setReason] = useState("")
  const corrected = field in record.corrections

  const submit = (clear: boolean) => (event?: FormEvent) => {
    event?.preventDefault()
    const numeric = ["pieces", "weight_kg", "freight_charge"].includes(field)
    correct.mutate(
      { integrationId, recordId: record.id, field, value: clear ? undefined : numeric ? Number(value) : value, reason, clear },
      { onSuccess: () => { toast.success(clear ? `Correction of ${field} removed` : `${field} corrected`); setValue(""); setReason("") } },
    )
  }

  return (
    <form onSubmit={submit(false)} className="space-y-3 rounded-md border p-3" aria-label="Correct a field">
      <p className="text-sm font-medium">Correct a field</p>
      <p className="text-xs text-muted-foreground">A corrected value wins over every later import; what the system sends is still kept, and the difference is counted.</p>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor="correct-field">Field</Label>
          <Select id="correct-field" value={field} onChange={(event) => setField(event.target.value)}>
            {fields.map((name) => (
              <option key={name} value={name}>
                {name}
                {name in record.corrections ? " (corrected)" : ""}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="correct-value">Correct value</Label>
          <Input id="correct-value" value={value} onChange={(event) => setValue(event.target.value)} placeholder={String(record.data[field] ?? "")} />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="correct-reason">Reason</Label>
        <Textarea id="correct-reason" value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} placeholder="What shows the system’s value is wrong" />
      </div>
      {correct.error ? <Alert tone="bad" role="alert">{errorMessage(correct.error)}</Alert> : null}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={correct.isPending || !value.trim() || reason.trim().length < 10}>
          Save correction
        </Button>
        {corrected && (
          <Button size="sm" variant="outline" disabled={correct.isPending || reason.trim().length < 10} onClick={() => submit(true)()}>
            Remove the correction
          </Button>
        )}
      </div>
    </form>
  )
}

function RecordDialog({ integrationId, recordId, onClose }: { integrationId: number; recordId: number | null; onClose: () => void }) {
  const record = useRecord(integrationId, recordId)

  return (
    <Dialog open={recordId !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{record.data ? `${record.data.entity === "shipments" ? "Shipment" : "Carrier"} ${record.data.external_id}` : "Imported record"}</DialogTitle>
          <DialogDescription>As imported, with where it came from, every version and any manual correction.</DialogDescription>
        </DialogHeader>
        {record.isPending && <LoadingBlock rows={3} label="Loading the record" />}
        {record.isError && <ErrorState error={record.error} />}
        {record.data && (
          <div className="space-y-4">
            <Facts
              columns={3}
              label="Provenance"
              items={[
                ["Source system", record.data.provider],
                ["External id", <code key="x">{record.data.external_id}</code>],
                ["Changed at the source", dateTime(record.data.source_updated_at)],
                ["First imported by run", `#${record.data.first_run_id}`],
                ["Last seen in run", `#${record.data.last_run_id}`],
                ["Corrected fields", record.data.corrected_fields.join(", ") || "None"],
              ]}
            />
            <div className="grid gap-3 lg:grid-cols-2">
              <div>
                <p className="mb-1 text-xs font-medium">In use (corrections applied)</p>
                <JsonBlock value={record.data.data} label="Record in use" />
              </div>
              <div>
                <p className="mb-1 text-xs font-medium">Last received from the system</p>
                <JsonBlock value={record.data.received} label="Record as received" />
              </div>
            </div>
            <Correction record={record.data} integrationId={integrationId} />
            <div>
              <p className="mb-2 text-sm font-medium">Versions</p>
              <ul className="space-y-1 text-xs">
                {record.data.versions.map((version) => (
                  <li key={version.id}>
                    {dateTime(version.at)}: {version.change.replace(/_/g, " ")}
                    {version.run_id ? ` (run #${version.run_id})` : ""}
                    {version.actor ? ` by ${version.actor}` : ""}
                    {version.kept_fields.length > 0 && <span className="text-warn"> · corrected value kept for {version.kept_fields.join(", ")}</span>}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function Records({ integrationId }: { integrationId: number }) {
  const [entity, setEntity] = useState("")
  const [search, setSearch] = useState("")
  const [offset, setOffset] = useState(0)
  const records = useRecords(integrationId, { entity: entity || undefined, search: search.trim(), offset, limit: 25 })
  const [open, setOpen] = useState<number | null>(null)

  return (
    <>
      <form role="search" aria-label="Find imported records" className="mb-3 flex flex-wrap gap-2" onSubmit={(event) => event.preventDefault()}>
        <Select aria-label="Entity" className="w-40" value={entity} onChange={(event) => { setEntity(event.target.value); setOffset(0) }}>
          <option value="">All records</option>
          <option value="shipments">Shipments</option>
          <option value="carriers">Carriers</option>
        </Select>
        <div className="relative w-72">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <Input aria-label="Search records" className="pl-9" value={search} maxLength={100} onChange={(event) => { setSearch(event.target.value); setOffset(0) }} placeholder="External id or any value" />
        </div>
      </form>
      {records.isPending && <LoadingBlock rows={3} label="Loading the records" />}
      {records.isError && <ErrorState error={records.error} onRetry={() => records.refetch()} />}
      {records.data && records.data.total === 0 && <EmptyState title="No imported record">Records appear after a sync.</EmptyState>}
      {records.data && records.data.total > 0 && (
        <>
          <Table aria-label="Imported records">
            <TableHeader>
              <TableRow>
                <TableHead>External id</TableHead>
                <TableHead>Entity</TableHead>
                <TableHead>Summary</TableHead>
                <TableHead>Changed at the source</TableHead>
                <TableHead>Runs</TableHead>
                <TableHead>Corrected</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {records.data.records.map((record) => (
                <TableRow key={record.id} className="cursor-pointer" onClick={() => setOpen(record.id)}>
                  <TableCell>
                    <button type="button" className="font-mono text-xs text-primary hover:underline" onClick={() => setOpen(record.id)}>
                      {record.external_id}
                    </button>
                  </TableCell>
                  <TableCell className="text-xs">{record.entity}</TableCell>
                  <TableCell className="text-xs">
                    {record.entity === "shipments"
                      ? `${String(record.data.reference ?? "")} · ${String(record.data.status ?? "")} · ${String(record.data.carrier_name ?? "")}`
                      : `${String(record.data.legal_name ?? "")}${record.data.scac ? ` · SCAC ${String(record.data.scac)}` : ""}`}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{dateTime(record.source_updated_at)}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    #{record.first_run_id}
                    {record.last_run_id !== record.first_run_id ? `–#${record.last_run_id}` : ""}
                  </TableCell>
                  <TableCell className="text-xs">{record.corrected_fields.length ? <span className="text-warn">{record.corrected_fields.join(", ")}</span> : <span className="text-muted-foreground">—</span>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Pagination offset={records.data.offset} limit={records.data.limit} total={records.data.total} onChange={setOffset} label="Record pages" />
        </>
      )}
      <RecordDialog integrationId={integrationId} recordId={open} onClose={() => setOpen(null)} />
    </>
  )
}

function Runs({ integrationId }: { integrationId: number }) {
  const [offset, setOffset] = useState(0)
  const runs = useSyncRuns(integrationId, offset)
  const [open, setOpen] = useState<number | null>(null)

  if (runs.isPending) return <LoadingBlock rows={3} label="Loading the runs" />
  if (runs.isError) return <ErrorState error={runs.error} onRetry={() => runs.refetch()} />
  if (runs.data.total === 0) return <EmptyState title="No sync yet">Run a sync now, or wait for the schedule.</EmptyState>

  return (
    <>
      <Table aria-label="Sync runs">
        <TableHeader>
          <TableRow>
            <TableHead>Run</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Trigger</TableHead>
            <TableHead className="text-right">Fetched</TableHead>
            <TableHead className="text-right">Imported</TableHead>
            <TableHead className="text-right">Updated</TableHead>
            <TableHead className="text-right">Unchanged</TableHead>
            <TableHead className="text-right">Duplicates</TableHead>
            <TableHead className="text-right">Rejected</TableHead>
            <TableHead className="text-right">Retries</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.data.runs.map((run) => (
            <TableRow key={run.id} className="cursor-pointer" onClick={() => setOpen(run.id)}>
              <TableCell>
                <button type="button" className="text-primary hover:underline" onClick={() => setOpen(run.id)}>
                  #{run.id}
                </button>
                <p className="whitespace-nowrap text-xs text-muted-foreground">{dateTime(run.started_at)}</p>
              </TableCell>
              <TableCell>
                <RunBadge status={run.status} />
                {run.error_category && <p className="mt-1 text-xs text-bad">{run.error_category.replace(/_/g, " ")}</p>}
              </TableCell>
              <TableCell className="text-xs">
                {run.trigger}
                {run.requested_by ? ` by ${run.requested_by}` : ""}
              </TableCell>
              {[run.fetched, run.imported, run.updated, run.unchanged, run.duplicates, run.rejected, run.retries].map((value, index) => (
                <TableCell key={index} className="tabular text-right">
                  {value}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Pagination offset={runs.data.offset} limit={runs.data.limit} total={runs.data.total} onChange={setOffset} label="Run pages" />
      <RunDetail id={open} onClose={() => setOpen(null)} />
    </>
  )
}

/** One integration: connection, sync status and cursor, run history, imported records with provenance. */
export function IntegrationDetail() {
  const { id } = useParams()
  const integrationId = Number(id)
  const valid = Number.isInteger(integrationId) && integrationId > 0
  const integration = useIntegration(valid ? integrationId : null)
  const test = useTestIntegration()
  const sync = useSyncNow()
  const change = useChangeIntegration()
  const simulate = useSimulateChanges()
  const toast = useToast()
  const [tab, setTab] = useState("runs")

  if (!valid || (integration.error instanceof ApiError && integration.error.status === 404)) {
    return <EmptyState title="No such integration" action={<Button variant="outline" size="sm" asChild><Link to="/integrations">Back to the integrations</Link></Button>} />
  }
  if (integration.isPending) return <LoadingBlock rows={6} label="Loading the integration" />
  if (integration.isError) return <ErrorState error={integration.error} onRetry={() => integration.refetch()} title="This integration could not be loaded" />

  const data = integration.data
  const sandbox = data.provider === "sandbox_tms"

  return (
    <>
      <Link to="/integrations" className="mb-3 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> TMS and ERP integrations
      </Link>
      <PageHeader
        title={data.name}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <ConnectionBadge status={data.connection_status} />
            <EnabledBadge enabled={data.enabled} />
            <span className="text-xs">Syncs {every(data.sync_interval_seconds)}</span>
          </span>
        }
        actions={
          <>
            <Button
              variant="outline"
              size="sm"
              disabled={test.isPending}
              onClick={() => test.mutate(data.id, { onSuccess: (result) => (result.ok ? toast.success(`Connected (${result.latency_ms} ms)`) : toast.error(`${result.category}: ${result.detail}`)), onError: (error) => toast.error(errorMessage(error)) })}
            >
              <PlugZap /> Test connection
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={sync.isPending || data.running}
              onClick={() => sync.mutate(data.id, { onSuccess: (result) => toast.success(`Sync #${result.run_id} started`), onError: (error) => toast.error(errorMessage(error)) })}
            >
              <RefreshCw /> Sync now
            </Button>
            <Button variant="outline" size="sm" disabled={change.isPending} onClick={() => change.mutate({ id: data.id, enabled: !data.enabled }, { onError: (error) => toast.error(errorMessage(error)) })}>
              <Power /> {data.enabled ? "Disable" : "Enable"}
            </Button>
            {sandbox && (
              <Button variant="outline" size="sm" disabled={simulate.isPending} onClick={() => simulate.mutate({ id: data.id, count: 3 }, { onSuccess: (result) => toast.success(`The sandbox changed ${result.changed.length} shipments`), onError: (error) => toast.error(errorMessage(error)) })}>
                <Shuffle /> Simulate partner changes
              </Button>
            )}
          </>
        }
      />

      {data.running && <Alert tone="info" className="mb-4">A sync is running now.</Alert>}
      {!data.credential_configured && <Alert tone="warn" className="mb-4">The credential variable {data.credential_env ?? "(none)"} is not set on the server: the system will refuse the connection.</Alert>}
      {data.connection_error && data.connection_status !== "connected" && <Alert tone="bad" className="mb-4">Last connection problem: {data.connection_error}</Alert>}

      <section aria-label="Sync figures" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <KpiCard title="Records held" value={count(Object.values(data.records).reduce((sum, value) => sum + value, 0))} detail={Object.entries(data.records).map(([entity, number]) => `${number} ${entity}`).join(", ") || "None yet"} />
        <KpiCard title="Imported, all runs" value={count(data.totals.imported)} detail={`${count(data.totals.runs)} runs`} />
        <KpiCard title="Updated, all runs" value={count(data.totals.updated)} />
        <KpiCard title="Rejected, all runs" value={count(data.totals.rejected)} tone={data.totals.rejected ? "warn" : "default"} detail="Records refused by validation" />
      </section>

      <Section title="Status">
        <Card>
          <CardContent className="pt-6">
            <Facts
              columns={4}
              label="Integration status"
              items={[
                ["Last successful sync", data.last_success_at ? dateTime(data.last_success_at) : "Never"],
                ["Last failed sync", data.last_failure_at ? dateTime(data.last_failure_at) : "None"],
                ["Next sync", data.enabled ? dateTime(data.next_sync_at) : "Disabled"],
                ["Failures in a row", count(data.consecutive_failures)],
                ["Connection checked", data.connection_checked_at ? dateTime(data.connection_checked_at) : "Never"],
                ["Credential variable", data.credential_env && <code className="text-xs">{data.credential_env}</code>],
                ["Credential set on the server", data.credential_configured ? "Yes" : "No"],
                ["Last error", data.last_error],
              ]}
            />
            <div className="mt-4 grid gap-3 lg:grid-cols-2">
              <div>
                <p className="mb-1 text-xs font-medium">Current cursor</p>
                <JsonBlock value={data.cursor} label="Current cursor" />
              </div>
              <div>
                <p className="mb-1 text-xs font-medium">Settings</p>
                <JsonBlock value={data.settings} label="Settings" />
              </div>
            </div>
          </CardContent>
        </Card>
      </Section>

      <Section title="Activity">
        <Tabs label="Integration activity" value={tab} onChange={setTab} items={[{ id: "runs", label: "Sync runs" }, { id: "records", label: "Imported records" }, { id: "changes", label: "Configuration history" }]}>
          {tab === "runs" && <Runs integrationId={data.id} />}
          {tab === "records" && <Records integrationId={data.id} />}
          {tab === "changes" && (
            <ol className="rounded-lg border bg-card shadow-card" aria-label="Configuration history">
              {data.changes.map((item) => (
                <li key={item.id} className="flex flex-col gap-1 border-b px-4 py-3 text-sm last:border-0 sm:flex-row sm:gap-4">
                  <span className="w-40 shrink-0 text-xs text-muted-foreground">{dateTime(item.at)}</span>
                  <span className="w-28 shrink-0 font-medium">{item.actor}</span>
                  <span>{item.action}</span>
                </li>
              ))}
            </ol>
          )}
        </Tabs>
      </Section>
    </>
  )
}
