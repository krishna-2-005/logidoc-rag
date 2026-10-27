import { FilterX, History as HistoryIcon, UserCheck } from "lucide-react"
import { useEffect, useMemo, useState, type FormEvent } from "react"
import { useSearchParams } from "react-router-dom"
import { BarList, ProportionBar } from "@/components/common/Charts"
import { PageHeader } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { EmptyState, ErrorState, LoadingBlock, errorMessage } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle, ChartCard } from "@/components/ui/card"
import { Input, Label, Select, Textarea } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useDebounced } from "@/hooks/useDebounced"
import { useExceptionAction, useExceptionDetail, useExceptions, useSession, useSettings } from "@/hooks/queries"
import { CHART, REVIEW_COLOURS, REVIEW_ORDER, sentence } from "@/lib/chart"
import { count, dateTime, label, NA } from "@/lib/format"
import { SEVERITY_ORDER, tally } from "@/lib/insights"
import type { ExceptionDetail, ExceptionListItem, ReviewEvent, WorkflowAction } from "@/lib/types"

export const ACTION_LABELS: Record<WorkflowAction, string> = {
  assign: "Assign to a reviewer",
  start: "Start review",
  note: "Add a note or evidence",
  resolve: "Resolve",
  dismiss: "Dismiss",
  reopen: "Reopen",
  approve: "Approve",
  reject: "Reject",
}

const EVENT_LABELS: Record<string, string> = {
  detected: "Detected by the audit",
  audit_reopened: "Reopened by the audit",
  status: "Status changed",
  assign: "Assigned",
  start: "Review started",
  note: "Note added",
  resolve: "Resolved",
  dismiss: "Dismissed",
  reopen: "Reopened",
  approve: "Approved",
  reject: "Rejected",
}

/**
 * The same rules the server enforces (src/review_store.py), shown early so a
 * person does not have to submit to find out. The server remains the authority:
 * a refusal from it is displayed as it was given.
 */
export function validateAction(action: WorkflowAction, note: string, evidence: string[], hasEvidenceOnRecord: boolean, minNote: number, needsEvidence: boolean, assignee: string): string | null {
  const text = note.trim()

  switch (action) {
    case "assign":
      return assignee.trim() ? null : "Say who the exception is assigned to."
    case "note":
      return text || evidence.length ? null : "Write a note or add an evidence reference."
    case "resolve":
      if (text.length < minNote) return `Resolving needs a note of at least ${minNote} characters.`
      if (needsEvidence && evidence.length === 0 && !hasEvidenceOnRecord) return "Resolving needs at least one evidence reference."
      return null
    case "dismiss":
      return text.length < minNote ? `Dismissing needs a reason of at least ${minNote} characters.` : null
    case "reopen":
      return text.length < minNote ? `Reopening needs a note of at least ${minNote} characters.` : null
    case "reject":
      return text.length < minNote ? `Rejecting needs a note of at least ${minNote} characters.` : null
    default:
      return null
  }
}

export function History({ events }: { events: ReviewEvent[] }) {
  if (events.length === 0) return <p className="text-sm text-muted-foreground">No history yet.</p>

  return (
    <Table>
      <TableHeader>
        <tr>
          <TableHead>When</TableHead>
          <TableHead>What</TableHead>
          <TableHead>By</TableHead>
          <TableHead>Change</TableHead>
          <TableHead>Note and evidence</TableHead>
        </tr>
      </TableHeader>
      <TableBody>
        {events.map((event) => (
          <TableRow key={event.id}>
            <TableCell className="whitespace-nowrap text-xs">{dateTime(event.created_at)}</TableCell>
            <TableCell className="whitespace-nowrap">{EVENT_LABELS[event.event_type] ?? label(event.event_type)}</TableCell>
            <TableCell className="whitespace-nowrap">
              {event.actor ?? NA}
              <span className="block text-xs text-muted-foreground">
                {event.actor_role ?? ""}
                {event.authenticated ? "" : " · not authenticated"}
              </span>
            </TableCell>
            <TableCell className="whitespace-nowrap text-xs">
              {event.from_status ? `${label(event.from_status)} → ` : ""}
              {label(event.to_status)}
              {event.assignee ? <span className="block text-muted-foreground">assignee {event.assignee}</span> : null}
            </TableCell>
            <TableCell className="max-w-sm text-xs">
              {event.note}
              {event.evidence_refs?.length ? <span className="block text-muted-foreground">Evidence: {event.evidence_refs.join("; ")}</span> : null}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function ActionForm({ detail, assignees }: { detail: ExceptionDetail; assignees: string[] }) {
  const toast = useToast()
  const settings = useSettings()
  const session = useSession()
  const mutation = useExceptionAction()
  const actions = detail.allowed_actions
  const [action, setAction] = useState<WorkflowAction | "">(actions[0] ?? "")
  const [note, setNote] = useState("")
  const [evidence, setEvidence] = useState("")
  const [assignee, setAssignee] = useState("")
  const [refused, setRefused] = useState<string | null>(null)

  // The set of actions changes with the state: keep the selection valid
  useEffect(() => {
    if (!actions.includes(action as WorkflowAction)) setAction(actions[0] ?? "")
  }, [actions, action])

  const evidenceList = useMemo(() => evidence.split("\n").map((line) => line.trim()).filter(Boolean), [evidence])

  if (actions.length === 0) {
    return <p className="text-sm text-muted-foreground">No action is available to you on this exception in its current state.</p>
  }

  const minNote = settings.data?.minimum_note_length ?? 10
  const problem = action ? validateAction(action, note, evidenceList, detail.review.evidence_refs.length > 0, minNote, settings.data?.resolution_needs_evidence ?? true, assignee) : null
  const me = session.data?.user?.username

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!action || problem) return

    setRefused(null)

    mutation.mutate(
      { key: detail.exception_key, action, note, evidence: evidenceList, assignee: assignee.trim() || undefined },
      {
        onSuccess: () => {
          toast.success(`${ACTION_LABELS[action]}: done`)
          setNote("")
          setEvidence("")
          setAssignee("")
        },
        onError: (error) => setRefused(errorMessage(error)),
      },
    )
  }

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <div className="max-w-sm space-y-1.5">
        <Label htmlFor="action">What do you want to do?</Label>
        <Select id="action" value={action} onChange={(event) => setAction(event.target.value as WorkflowAction)}>
          {actions.map((item) => (
            <option key={item} value={item}>
              {ACTION_LABELS[item]}
            </option>
          ))}
        </Select>
      </div>

      {(action === "assign" || action === "start") && (
        <div className="max-w-sm space-y-1.5">
          <Label htmlFor="assignee">{action === "start" ? "Assign to (leave empty for yourself)" : "Assign to"}</Label>
          <div className="flex gap-2">
            <Input id="assignee" list="assignees" value={assignee} onChange={(event) => setAssignee(event.target.value)} autoComplete="off" />
            {me && (
              <Button type="button" variant="outline" size="sm" className="h-9" onClick={() => setAssignee(me)}>
                <UserCheck /> Me
              </Button>
            )}
          </div>
          <datalist id="assignees">
            {assignees.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        </div>
      )}

      {action !== "start" && action !== "approve" && action !== "assign" ? (
        <div className="space-y-1.5">
          <Label htmlFor="note">{action === "dismiss" ? "Reason for dismissing" : action === "resolve" ? "Resolution note (what was done)" : action === "reject" ? "Why it is rejected" : action === "reopen" ? "Why it is reopened" : "Note"}</Label>
          <Textarea id="note" value={note} onChange={(event) => setNote(event.target.value)} aria-describedby="note-help" />
          <p id="note-help" className="text-xs text-muted-foreground">
            {["resolve", "dismiss", "reopen", "reject"].includes(action) ? `At least ${minNote} characters. Recorded with your name and the time.` : "Optional when you add evidence."}
          </p>
        </div>
      ) : (
        <div className="space-y-1.5">
          <Label htmlFor="note">Note (optional)</Label>
          <Textarea id="note" value={note} onChange={(event) => setNote(event.target.value)} className="min-h-[60px]" />
        </div>
      )}

      {(action === "resolve" || action === "note" || action === "dismiss") && (
        <div className="space-y-1.5">
          <Label htmlFor="evidence">Evidence references (one per line)</Label>
          <Textarea id="evidence" value={evidence} onChange={(event) => setEvidence(event.target.value)} className="min-h-[64px]" placeholder={"INVOICE_SHJ-2026-001.pdf p.1\ncredit memo CM-77"} />
          <p className="text-xs text-muted-foreground">A document and page, a credit memo, a ticket: whatever the decision rests on.{action === "resolve" && detail.review.evidence_refs.length > 0 ? " Evidence already on record counts." : ""}</p>
        </div>
      )}

      {problem && (
        <p className="text-sm text-muted-foreground" role="status">
          {problem}
        </p>
      )}
      {refused && <Alert tone="bad">{refused}</Alert>}

      <Button type="submit" disabled={!!problem || mutation.isPending}>
        {mutation.isPending ? "Saving…" : action ? ACTION_LABELS[action] : "Choose an action"}
      </Button>
    </form>
  )
}

function Panel({ item, assignees }: { item: ExceptionListItem; assignees: string[] }) {
  const detail = useExceptionDetail(item.exception_key)

  return (
    <Card aria-label={`Exception ${item.category} of ${item.shipment_id}`}>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-base">
            {item.shipment_id} · {item.category}
          </CardTitle>
          {item.severity && <StatusBadge status={item.severity} />}
          <StatusBadge status={item.status} />
          {item.approval_state && <StatusBadge status={item.approval_state} />}
        </div>
        <CardDescription>
          Assignee: {item.assignee ?? "nobody"} · opened {dateTime(item.created_at)}
          {item.needs_recheck ? " · reopened by the audit: the audited values changed" : ""}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {item.description && <p className="text-sm">{item.description}</p>}

        {detail.isPending && <LoadingBlock rows={3} label="Loading exception" />}
        {detail.isError && <ErrorState error={detail.error} onRetry={() => detail.refetch()} title="This exception could not be loaded" />}

        {detail.data && (
          <>
            {detail.data.evidence.length > 0 && (
              <details className="text-xs">
                <summary className="cursor-pointer font-medium text-primary">Audit evidence ({detail.data.evidence.length})</summary>
                <ul className="mt-2 space-y-1">
                  {detail.data.evidence.map((line, index) => (
                    <li key={index} className="rounded bg-muted/70 px-2 py-1 text-muted-foreground">
                      {line}
                    </li>
                  ))}
                </ul>
              </details>
            )}

            {detail.data.review.evidence_refs.length > 0 && (
              <div>
                <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Evidence on record</h4>
                <ul className="mt-1 list-disc pl-5 text-sm">
                  {detail.data.review.evidence_refs.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </div>
            )}

            <div>
              <h4 className="mb-3 text-sm font-semibold">Take action</h4>
              <ActionForm key={`${detail.data.exception_key}:${detail.data.review.status}:${detail.data.review.approval_state}`} detail={detail.data} assignees={assignees} />
            </div>

            <div>
              <h4 className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <HistoryIcon className="size-4 text-muted-foreground" aria-hidden="true" /> Who changed what and when
              </h4>
              <History events={detail.data.history} />
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

const SEVERITY_COLOURS: Record<string, string> = { HIGH: CHART.bad, MEDIUM: CHART.warn, LOW: CHART.info }

/**
 * The listed exceptions at a glance, counted from the same list the table shows (so
 * it follows the filters). Selecting a category filters the list by it.
 */
function ExceptionSummary({ items, category, onCategory }: { items: ExceptionListItem[]; category: string; onCategory: (value: string) => void }) {
  const categories = tally(items, "category")
  const severities = tally(items, "severity", SEVERITY_ORDER)
  const states = tally(items, "status", REVIEW_ORDER)
  const stateCounts = new Map(states.map((item) => [item.key, item.value]))
  const unit = (n: number) => (n === 1 ? "exception" : "exceptions")

  return (
    <div className="mb-4 grid gap-4 lg:grid-cols-3">
      <ChartCard title="By category" description={category ? "Showing one category: select it again to show all." : "Select a category to list only its exceptions."}>
        <BarList
          label="Exceptions by category"
          rows={categories.map((item) => ({
            key: item.key,
            name: item.key,
            label: (
              <button type="button" aria-pressed={category === item.key} onClick={() => onCategory(category === item.key ? "" : item.key)} className="rounded-sm text-left hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                {item.key}
              </button>
            ),
            display: count(item.value),
            parts: [{ key: "value", label: "Exceptions", value: item.value, colour: CHART.bad }],
          }))}
        />
      </ChartCard>

      <ChartCard title="By severity" description="As the audit rates each finding.">
        <ProportionBar
          segments={severities.map((item) => ({ key: item.key, label: sentence(item.key), value: item.value, colour: SEVERITY_COLOURS[item.key] ?? CHART.neutral }))}
          unit={unit(items.length)}
          label="Exceptions by severity"
        />
      </ChartCard>

      <ChartCard title="By review state" description="What reviewers have done so far. It never changes what the audit found.">
        <ProportionBar
          segments={[...REVIEW_ORDER, ...states.map((item) => item.key).filter((key) => !REVIEW_ORDER.includes(key))].map((key) => ({
            key, label: sentence(key), value: stateCounts.get(key) ?? 0, colour: REVIEW_COLOURS[key] ?? CHART.neutral,
          }))}
          unit={unit(items.length)}
          label="Exceptions by review state"
        />
      </ChartCard>
    </div>
  )
}

export function Exceptions() {
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get("search") ?? "")
  const [assignee, setAssignee] = useState("")
  const [status, setStatus] = useState("")
  const [category, setCategory] = useState("")
  const [older, setOlder] = useState("")
  const [newer, setNewer] = useState("")
  const [waiting, setWaiting] = useState(false)
  const session = useSession()

  const debounced = useDebounced(search)

  const filters = {
    search: debounced,
    assignee: assignee && assignee !== "__none__" && assignee !== "__me__" ? assignee : assignee === "__me__" ? session.data?.user?.username : undefined,
    unassigned: assignee === "__none__",
    status: status ? [status] : [],
    category: category ? [category] : [],
    min_age_days: older ? Number(older) : undefined,
    max_age_days: newer ? Number(newer) : undefined,
    pending_approval: waiting,
  }

  const list = useExceptions(filters)
  const selectedKey = params.get("key")
  const selected = list.data?.exceptions.find((item) => item.exception_key === selectedKey) ?? null

  const filtered = !!(debounced || assignee || status || category || older || newer || waiting)

  const clear = () => {
    setSearch("")
    setAssignee("")
    setStatus("")
    setCategory("")
    setOlder("")
    setNewer("")
    setWaiting(false)
  }

  const choose = (key: string) => {
    const next = new URLSearchParams(params)
    next.set("key", key)
    setParams(next, { replace: true })
  }

  return (
    <>
      <PageHeader title="Exception Center" description="Everything the audit flagged, with who is working on it. Reviewing records what people did; it never changes what the audit found." />

      <form role="search" aria-label="Filter exceptions" className="mb-4 grid gap-3 rounded-lg border bg-card p-4 shadow-card sm:grid-cols-2 xl:grid-cols-6" onSubmit={(event) => event.preventDefault()}>
        <div className="space-y-1.5">
          <Label htmlFor="ex-search">Search shipment</Label>
          <Input id="ex-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="SHJ-2026-001" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ex-assignee">Assignee</Label>
          <Select id="ex-assignee" value={assignee} onChange={(event) => setAssignee(event.target.value)}>
            <option value="">Anyone</option>
            <option value="__none__">Unassigned</option>
            {session.data?.user && <option value="__me__">Me</option>}
            {list.data?.assignees.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ex-status">State</Label>
          <Select id="ex-status" value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">Any state</option>
            {(list.data?.statuses ?? ["open", "in_review", "resolved", "dismissed"]).map((item) => (
              <option key={item} value={item}>
                {label(item)}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ex-category">Category</Label>
          <Select id="ex-category" value={category} onChange={(event) => setCategory(event.target.value)}>
            <option value="">Any category</option>
            {list.data?.categories.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ex-older">At least (days old)</Label>
          <Input id="ex-older" type="number" min={0} value={older} onChange={(event) => setOlder(event.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ex-newer">At most (days old)</Label>
          <Input id="ex-newer" type="number" min={0} value={newer} onChange={(event) => setNewer(event.target.value)} />
        </div>
        <div className="flex items-center gap-4 sm:col-span-2 xl:col-span-6">
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <input type="checkbox" className="size-4 rounded border-input accent-[hsl(var(--primary))]" checked={waiting} onChange={(event) => setWaiting(event.target.checked)} /> Waiting for approval
          </label>
          {filtered && (
            <Button type="button" variant="ghost" size="sm" onClick={clear} className="ml-auto">
              <FilterX /> Clear filters
            </Button>
          )}
        </div>
      </form>

      {list.data && list.data.exceptions.length > 0 && <ExceptionSummary items={list.data.exceptions} category={category} onCategory={setCategory} />}

      {list.isPending && <LoadingBlock rows={5} label="Loading exceptions" />}
      {list.isError && <ErrorState error={list.error} onRetry={() => list.refetch()} title="Exceptions could not be loaded" />}

      {list.data && list.data.exceptions.length === 0 && (
        <EmptyState title={filtered ? "No exception matches these filters" : "No exceptions"} action={filtered ? <Button variant="outline" onClick={clear}>Clear filters</Button> : undefined}>
          {filtered ? "Try fewer filters." : "The audit has not flagged anything, or no documents have been added yet."}
        </EmptyState>
      )}

      {list.data && list.data.exceptions.length > 0 && (
        <div className="grid gap-6 2xl:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
          <div className="min-w-0">
            <p className="mb-2 text-xs text-muted-foreground" aria-live="polite">
              {list.data.total} exception{list.data.total === 1 ? "" : "s"}
            </p>
            <Table>
              <TableHeader>
                <tr>
                  <TableHead>Shipment</TableHead>
                  <TableHead>Category</TableHead>
                  <TableHead>Severity</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Assignee</TableHead>
                  <TableHead className="text-right">Age</TableHead>
                </tr>
              </TableHeader>
              <TableBody>
                {list.data.exceptions.map((item) => (
                  <TableRow key={item.exception_key} data-selected={item.exception_key === selectedKey} className="cursor-pointer" onClick={() => choose(item.exception_key)}>
                    <TableCell className="font-medium">
                      <button type="button" className="text-left text-primary hover:underline" onClick={(event) => { event.stopPropagation(); choose(item.exception_key) }} aria-label={`Open ${item.category} of ${item.shipment_id}`}>
                        {item.shipment_id}
                      </button>
                    </TableCell>
                    <TableCell>
                      {item.category}
                      {!item.detected && <span className="block text-xs text-muted-foreground">no longer detected</span>}
                    </TableCell>
                    <TableCell>{item.severity ? <StatusBadge status={item.severity} /> : <span className="text-muted-foreground">{NA}</span>}</TableCell>
                    <TableCell className="space-x-1">
                      <StatusBadge status={item.status} />
                      {item.approval_state === "pending" && <StatusBadge status="pending" />}
                    </TableCell>
                    <TableCell>{item.assignee ?? <span className="text-muted-foreground">unassigned</span>}</TableCell>
                    <TableCell className="tabular text-right">{item.age_days ?? 0} d</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <div className="min-w-0">
            {selected ? (
              <Panel key={selected.exception_key} item={selected} assignees={list.data.assignees} />
            ) : (
              <EmptyState title="Select an exception">Choose one from the list to see its evidence, take action and read its history.</EmptyState>
            )}
          </div>
        </div>
      )}
    </>
  )
}
