import { CalendarClock, ChevronDown, ChevronRight, Play, RotateCw } from "lucide-react"
import { Fragment, useEffect, useState, type FormEvent } from "react"
import { JsonBlock } from "@/components/common/Facts"
import { PageHeader } from "@/components/common/PageHeader"
import { Pagination } from "@/components/common/Pagination"
import { EmptyState, ErrorState, LoadingBlock, errorMessage } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { EnabledBadge, RunBadge, every } from "@/components/integrations/badges"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input, Label } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useChangeJob, useJobRuns, useRunJob, useSchedulerStatus } from "@/hooks/integrations"
import { dateTime } from "@/lib/format"
import type { ScheduledJob, SchedulerStatus } from "@/lib/types"

function Runs({ name }: { name: string }) {
  const [offset, setOffset] = useState(0)
  const runs = useJobRuns(name, offset)

  if (runs.isPending) return <LoadingBlock rows={2} label="Loading the runs" />
  if (runs.isError) return <ErrorState error={runs.error} onRetry={() => runs.refetch()} />
  if (runs.data.total === 0) return <p className="text-sm text-muted-foreground">This job has not run yet.</p>

  return (
    <>
      <Table aria-label={`Runs of ${name}`}>
        <TableHeader>
          <TableRow>
            <TableHead>Started</TableHead>
            <TableHead>Trigger</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Duration</TableHead>
            <TableHead className="text-right">Started late by</TableHead>
            <TableHead>Result</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.data.runs.map((run) => (
            <TableRow key={run.id}>
              <TableCell className="whitespace-nowrap text-xs">{dateTime(run.started_at)}</TableCell>
              <TableCell className="text-xs">
                {run.trigger}
                {run.requested_by && <span className="text-muted-foreground"> by {run.requested_by}</span>}
              </TableCell>
              <TableCell>
                <RunBadge status={run.status} />
              </TableCell>
              <TableCell className="tabular text-right text-xs">{run.duration_ms === null ? "—" : `${(run.duration_ms / 1000).toFixed(1)} s`}</TableCell>
              <TableCell className="tabular text-right text-xs">{run.delay_seconds ? `${run.delay_seconds} s` : "—"}</TableCell>
              <TableCell className="max-w-96 text-xs">
                {run.error_message ? (
                  <span className="text-bad">
                    {run.error_category}: {run.error_message}
                  </span>
                ) : run.summary && typeof run.summary === "object" && Object.keys(run.summary as object).length > 0 ? (
                  <details>
                    <summary className="cursor-pointer text-primary">Summary</summary>
                    <JsonBlock value={run.summary} label="Run summary" />
                  </details>
                ) : (
                  <span className="text-muted-foreground">Nothing to do</span>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Pagination offset={runs.data.offset} limit={runs.data.limit} total={runs.data.total} onChange={setOffset} label={`Run pages of ${name}`} />
    </>
  )
}

function IntervalDialog({ job, onClose }: { job: ScheduledJob | null; onClose: () => void }) {
  const change = useChangeJob()
  const toast = useToast()
  const [seconds, setSeconds] = useState("")

  useEffect(() => {
    if (job) {
      setSeconds(String(job.interval_seconds))
      change.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!job) return
    change.mutate({ name: job.name, interval_seconds: Number(seconds) }, { onSuccess: () => { toast.success(`${job.name} now runs ${every(Number(seconds))}`); onClose() } })
  }

  return (
    <Dialog open={job !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>Schedule of {job?.name}</DialogTitle>
            <DialogDescription>How often the job runs. A failed run is retried sooner, within this interval.</DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="job-interval">Interval in seconds</Label>
            <Input id="job-interval" type="number" min={10} max={604800} required value={seconds} onChange={(event) => setSeconds(event.target.value)} />
            <p className="text-xs text-muted-foreground">10 seconds to 7 days.</p>
          </div>
          {change.error ? <Alert tone="bad" role="alert">{errorMessage(change.error)}</Alert> : null}
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={change.isPending}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function Owner({ status }: { status: SchedulerStatus }) {
  if (!status.configured) {
    return <Alert tone="info" className="mb-4">Nothing is configured yet: the scheduler starts working once a webhook endpoint or an integration exists.</Alert>
  }

  if (status.owner?.stale) {
    return <Alert tone="bad" className="mb-4">The scheduler has not checked in since {dateTime(status.owner.heartbeat_at)}: background jobs may be stopped. Check the server process.</Alert>
  }

  if (!status.owner) {
    return <Alert tone="warn" className="mb-4">No server process is running the schedule. Jobs run when the API server starts with background jobs on (src/api/serve.py); they can still be run here by hand.</Alert>
  }

  return (
    <Alert tone="ok" className="mb-4">
      The schedule runs {status.owner.this_process ? "in this server process" : "in another server process"}, since {dateTime(status.owner.since)}; last check-in {dateTime(status.owner.heartbeat_at)}.
    </Alert>
  )
}

/**
 * The integration scheduler: every background job, whether it is on, when it
 * runs next, how its last runs went, and its execution history. One process
 * owns the schedule; a job never runs twice at the same time.
 */
export function ScheduledJobs() {
  const status = useSchedulerStatus()
  const change = useChangeJob()
  const run = useRunJob()
  const toast = useToast()
  const [open, setOpen] = useState<string | null>(null)
  const [scheduling, setScheduling] = useState<ScheduledJob | null>(null)

  const start = (job: ScheduledJob, retry = false) =>
    run.mutate(
      { name: job.name, retry },
      {
        onSuccess: () => {
          toast.success(`${job.name} started`)
          setOpen(job.name)
        },
        onError: (error) => toast.error(errorMessage(error)),
      },
    )

  return (
    <>
      <PageHeader
        title="Scheduled jobs"
        description="Background work for webhooks and integrations: reading new events, delivering and retrying webhooks, synchronising external systems and checking their health. Each run is recorded."
      />

      {status.isPending && <LoadingBlock rows={6} label="Loading the jobs" />}
      {status.isError && <ErrorState error={status.error} onRetry={() => status.refetch()} title="The scheduler status could not be loaded" />}

      {status.data && <Owner status={status.data} />}
      {status.data && !status.data.webhook_delivery.enabled && status.data.configured && (
        <Alert tone="warn" className="mb-4">
          Webhook delivery is off: {status.data.webhook_delivery.problems.join(" ")}
        </Alert>
      )}

      {status.data && status.data.jobs.length === 0 && <EmptyState icon={<CalendarClock className="size-5" />} title="No job yet">Jobs appear when the first webhook endpoint or integration is configured.</EmptyState>}

      {status.data && status.data.jobs.length > 0 && (
        <Table aria-label="Scheduled jobs">
          <TableHeader>
            <TableRow>
              <TableHead className="w-8" />
              <TableHead>Job</TableHead>
              <TableHead>State</TableHead>
              <TableHead>Schedule</TableHead>
              <TableHead>Last success</TableHead>
              <TableHead>Last failure</TableHead>
              <TableHead>Next run</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {status.data.jobs.map((job) => (
              <Fragment key={job.name}>
                <TableRow>
                  <TableCell>
                    <Button variant="ghost" size="icon" className="size-7" aria-expanded={open === job.name} aria-label={`${open === job.name ? "Hide" : "Show"} the runs of ${job.name}`} onClick={() => setOpen(open === job.name ? null : job.name)}>
                      {open === job.name ? <ChevronDown /> : <ChevronRight />}
                    </Button>
                  </TableCell>
                  <TableCell className="max-w-80">
                    <p className="font-mono text-xs font-medium">{job.name}</p>
                    <p className="text-xs text-muted-foreground">{job.description}</p>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      <EnabledBadge enabled={job.enabled} />
                      {job.running && <RunBadge status="running" />}
                      {job.delayed && <Badge tone="warn">Late by {Math.round(job.delay_seconds / 60)} min</Badge>}
                      {job.consecutive_failures > 0 && <Badge tone="bad">{job.consecutive_failures} failed in a row</Badge>}
                    </div>
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs">
                    <button type="button" className="text-primary hover:underline" onClick={() => setScheduling(job)} aria-label={`Change the schedule of ${job.name}`}>
                      {every(job.interval_seconds)}
                    </button>
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{job.last_success_at ? dateTime(job.last_success_at) : "Never"}</TableCell>
                  <TableCell className="max-w-64 text-xs">
                    {job.last_failure_at ? (
                      <>
                        <span className="text-muted-foreground">{dateTime(job.last_failure_at)}</span>
                        {job.last_error && job.consecutive_failures > 0 && <p className="text-bad">{job.last_error}</p>}
                      </>
                    ) : (
                      <span className="text-muted-foreground">None</span>
                    )}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{job.enabled ? dateTime(job.next_run_at) : "Disabled"}</TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-1.5">
                      <Button size="sm" variant="outline" disabled={job.running || run.isPending} onClick={() => start(job)} aria-label={`Run ${job.name} now`}>
                        <Play /> Run now
                      </Button>
                      {job.consecutive_failures > 0 && (
                        <Button size="sm" variant="outline" disabled={job.running || run.isPending} onClick={() => start(job, true)} aria-label={`Retry ${job.name}`}>
                          <RotateCw /> Retry
                        </Button>
                      )}
                      <Button size="sm" variant="ghost" disabled={change.isPending} onClick={() => change.mutate({ name: job.name, enabled: !job.enabled }, { onError: (error) => toast.error(errorMessage(error)) })}>
                        {job.enabled ? "Disable" : "Enable"}
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
                {open === job.name && (
                  <TableRow className="hover:bg-transparent">
                    <TableCell />
                    <TableCell colSpan={7} className="bg-muted/30">
                      <Runs name={job.name} />
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            ))}
          </TableBody>
        </Table>
      )}

      <IntervalDialog job={scheduling} onClose={() => setScheduling(null)} />
    </>
  )
}
