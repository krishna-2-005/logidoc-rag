import { Archive, ArrowLeft, KeyRound, Pencil, Power, RotateCw, Send } from "lucide-react"
import { useState } from "react"
import { Link, useNavigate, useParams } from "react-router-dom"
import { ConfirmDialog, SecretDialog } from "@/components/common/Dialogs"
import { Facts, JsonBlock } from "@/components/common/Facts"
import { KpiCard } from "@/components/common/KpiCard"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { Pagination } from "@/components/common/Pagination"
import { EmptyState, ErrorState, LoadingBlock, errorMessage } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { DeliveryBadge, EnabledBadge, successRate } from "@/components/integrations/badges"
import { EndpointForm } from "@/components/integrations/EndpointForm"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import {
  useArchiveEndpoint,
  useChangeEndpoint,
  useDeliveries,
  useDelivery,
  useEndpoint,
  useRetryDelivery,
  useRotateSecret,
  useTestEndpoint,
  useWebhookSettings,
} from "@/hooks/integrations"
import { ApiError } from "@/lib/api"
import { count, dateTime } from "@/lib/format"
import type { WebhookDelivery } from "@/lib/types"

const PAGE = 25

function Deliveries({ endpointId }: { endpointId: number }) {
  const [status, setStatus] = useState("")
  const [offset, setOffset] = useState(0)
  const deliveries = useDeliveries({ endpoint_id: endpointId, status: status ? [status] : [], offset, limit: PAGE })
  const retry = useRetryDelivery()
  const toast = useToast()
  const navigate = useNavigate()

  const again = (delivery: WebhookDelivery) =>
    retry.mutate(delivery.id, {
      onSuccess: (result) => (result.status === "succeeded" ? toast.success(`Delivery ${delivery.id} delivered`) : toast.error(`Delivery ${delivery.id}: ${result.last_error ?? result.status}`)),
      onError: (error) => toast.error(errorMessage(error)),
    })

  return (
    <>
      <div className="mb-3 flex items-center gap-2">
        <label htmlFor="delivery-status" className="text-sm text-muted-foreground">
          Show
        </label>
        <Select id="delivery-status" className="w-48" value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0) }}>
          <option value="">All deliveries</option>
          <option value="failed">Failed</option>
          <option value="retrying">Retrying</option>
          <option value="pending">Queued</option>
          <option value="succeeded">Delivered</option>
          <option value="cancelled">Cancelled</option>
        </Select>
      </div>
      {deliveries.isPending && <LoadingBlock rows={3} label="Loading the deliveries" />}
      {deliveries.isError && <ErrorState error={deliveries.error} onRetry={() => deliveries.refetch()} />}
      {deliveries.data && deliveries.data.total === 0 && <EmptyState title="No delivery here">Deliveries appear once an event this endpoint subscribes to is committed.</EmptyState>}
      {deliveries.data && deliveries.data.total > 0 && (
        <>
          <Table aria-label="Deliveries">
            <TableHeader>
              <TableRow>
                <TableHead>Delivery</TableHead>
                <TableHead>Event</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Attempts</TableHead>
                <TableHead>Last answer</TableHead>
                <TableHead>Next attempt</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {deliveries.data.deliveries.map((delivery) => (
                <TableRow key={delivery.id} className="cursor-pointer" onClick={() => navigate(`/integrations/deliveries/${delivery.id}`)}>
                  <TableCell>
                    <Link to={`/integrations/deliveries/${delivery.id}`} className="text-primary hover:underline" onClick={(event) => event.stopPropagation()}>
                      #{delivery.id}
                    </Link>
                    <p className="text-xs text-muted-foreground">{dateTime(delivery.created_at)}</p>
                  </TableCell>
                  <TableCell>
                    <span className="font-mono text-xs">{delivery.event_type}</span>
                    {delivery.test && <span className="ml-1 text-xs text-muted-foreground">(test)</span>}
                  </TableCell>
                  <TableCell>
                    <DeliveryBadge status={delivery.status} />
                  </TableCell>
                  <TableCell className="tabular text-right">{delivery.attempts}</TableCell>
                  <TableCell className="text-xs">
                    {delivery.last_status_code ? `HTTP ${delivery.last_status_code}` : ""}
                    {delivery.last_error && <span className="block text-muted-foreground">{delivery.last_error.replace(/_/g, " ")}</span>}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{delivery.status === "retrying" || delivery.status === "pending" ? dateTime(delivery.next_attempt_at) : "—"}</TableCell>
                  <TableCell className="text-right">
                    {(delivery.status === "failed" || delivery.status === "retrying") && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={retry.isPending}
                        onClick={(event) => {
                          event.stopPropagation()
                          again(delivery)
                        }}
                      >
                        <RotateCw /> Retry
                      </Button>
                    )}
                  </TableCell>
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

const CHANGE_LABELS: Record<string, string> = {
  create: "Registered", update: "Changed", enable: "Enabled", disable: "Disabled", archive: "Archived", rotate_secret: "Rotated the secret",
  test: "Sent a test event", retry_delivery: "Retried a delivery",
}

/** One webhook endpoint: its deliveries, statistics, actions and change history. Never its secret. */
export function WebhookDetail() {
  const { id } = useParams()
  const endpointId = Number(id)
  const valid = Number.isInteger(endpointId) && endpointId > 0
  const endpoint = useEndpoint(valid ? endpointId : null)
  const settings = useWebhookSettings()
  const change = useChangeEndpoint()
  const rotate = useRotateSecret()
  const archive = useArchiveEndpoint()
  const test = useTestEndpoint()
  const toast = useToast()
  const [editing, setEditing] = useState(false)
  const [confirm, setConfirm] = useState<"rotate" | "archive" | null>(null)
  const [secret, setSecret] = useState<{ secret: string; note: string } | null>(null)

  if (!valid || (endpoint.error instanceof ApiError && endpoint.error.status === 404)) {
    return <EmptyState title="No such endpoint" action={<Button variant="outline" size="sm" asChild><Link to="/integrations/webhooks">Back to the webhooks</Link></Button>} />
  }
  if (endpoint.isPending) return <LoadingBlock rows={6} label="Loading the endpoint" />
  if (endpoint.isError) return <ErrorState error={endpoint.error} onRetry={() => endpoint.refetch()} title="This endpoint could not be loaded" />

  const data = endpoint.data
  const stats = data.stats
  const live = !data.archived

  const sendTest = () =>
    test.mutate(data.id, {
      onSuccess: (result) => (result.status === "succeeded" ? toast.success(`Test delivered (HTTP ${result.last_status_code})`) : toast.error(`Test failed: ${result.attempt_log[result.attempt_log.length - 1]?.detail ?? result.last_error ?? result.status}`)),
      onError: (error) => toast.error(errorMessage(error)),
    })

  return (
    <>
      <Link to="/integrations/webhooks" className="mb-3 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Webhooks
      </Link>
      <PageHeader
        title={data.name}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <EnabledBadge enabled={data.enabled} archived={data.archived} />
            <span className="font-mono text-xs">{data.url}</span>
          </span>
        }
        actions={
          live ? (
            <>
              <Button variant="outline" size="sm" onClick={sendTest} disabled={test.isPending || !settings.data?.delivery_enabled}>
                <Send /> Send test
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={change.isPending}
                onClick={() => change.mutate({ id: data.id, enabled: !data.enabled }, { onError: (error) => toast.error(errorMessage(error)) })}
              >
                <Power /> {data.enabled ? "Disable" : "Enable"}
              </Button>
              <Button variant="outline" size="sm" onClick={() => { change.reset(); setEditing(true) }}>
                <Pencil /> Edit
              </Button>
              <Button variant="outline" size="sm" onClick={() => setConfirm("rotate")} disabled={!settings.data?.delivery_enabled}>
                <KeyRound /> Rotate secret
              </Button>
              <Button variant="outline" size="sm" onClick={() => setConfirm("archive")}>
                <Archive /> Archive
              </Button>
            </>
          ) : undefined
        }
      />

      {data.archived && <Alert tone="info" className="mb-4">Archived {dateTime(data.archived_at)} by {data.archived_by}. Nothing more is delivered; the history stays.</Alert>}
      {!data.enabled && live && <Alert tone="warn" className="mb-4">Disabled: new events create no delivery for this endpoint, and waiting deliveries are held until it is enabled again.</Alert>}

      <section aria-label="Delivery statistics" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <KpiCard title={`Deliveries, last ${stats.window_days} days`} value={count(stats.deliveries)} detail="Test deliveries are not counted" />
        <KpiCard title="Success rate" value={successRate(stats)} tone={stats.success_rate !== null && stats.success_rate < 0.95 ? "warn" : "default"} definition="Delivered / delivered and failed, over deliveries created in the window. Waiting ones are not counted." />
        <KpiCard title="Failed" value={count(stats.failed)} tone={stats.failed ? "bad" : "default"} detail={stats.last_failure_at ? `Last ${dateTime(stats.last_failure_at)}` : "None"} />
        <KpiCard title="Waiting or retrying" value={count(stats.waiting)} detail={stats.last_success_at ? `Last delivered ${dateTime(stats.last_success_at)}` : "Nothing delivered yet"} />
      </section>

      <Section title="Deliveries" description="Every event sent to this endpoint, newest first. A failed delivery can be retried after the receiver is fixed.">
        <Deliveries endpointId={data.id} />
      </Section>

      <Section title="Configuration">
        <Card>
          <CardContent className="pt-6">
            <Facts
              columns={3}
              label="Endpoint configuration"
              items={[
                ["Events", <ul key="e" className="space-y-0.5 font-mono text-xs">{data.event_types.map((type) => <li key={type}>{type}</li>)}</ul>],
                ["Signing secret", `Version ${data.secret_version}, issued ${dateTime(data.secret_rotated_at)}`],
                ["Previous secret", data.rotation_in_progress ? `Still signs until ${dateTime(data.previous_secret_expires_at)}` : "None"],
                ["Registered", `${dateTime(data.created_at)} by ${data.created_by}`],
                ["Last changed", dateTime(data.updated_at)],
              ]}
            />
          </CardContent>
        </Card>
      </Section>

      <Section title="Change history" description="Who changed this endpoint, and how. Secrets are never recorded.">
        <ol className="rounded-lg border bg-card shadow-card" aria-label="Endpoint changes">
          {data.changes.map((item) => (
            <li key={item.id} className="flex flex-col gap-1 border-b px-4 py-3 text-sm last:border-0 sm:flex-row sm:gap-4">
              <span className="w-40 shrink-0 text-xs text-muted-foreground">{dateTime(item.at)}</span>
              <span className="w-28 shrink-0 font-medium">{item.actor}</span>
              <span>{CHANGE_LABELS[item.action] ?? item.action}</span>
            </li>
          ))}
        </ol>
      </Section>

      <EndpointForm
        open={editing}
        onOpenChange={setEditing}
        settings={settings.data}
        endpoint={data}
        pending={change.isPending}
        error={change.error}
        onSubmit={(body) => change.mutate({ id: data.id, ...body }, { onSuccess: () => { setEditing(false); toast.success("Saved") } })}
      />
      <ConfirmDialog
        open={confirm === "rotate"}
        onOpenChange={(open) => !open && setConfirm(null)}
        title="Rotate the signing secret?"
        description={`A new secret is issued and shown once. The current one keeps signing beside it for ${settings.data?.rotation_grace_hours ?? 24} hours, so the receiver can switch without missing an event.`}
        confirm="Rotate"
        pending={rotate.isPending}
        error={rotate.error}
        onConfirm={() => rotate.mutate(data.id, { onSuccess: (issued) => { setConfirm(null); setSecret({ secret: issued.secret, note: issued.note }) } })}
      />
      <ConfirmDialog
        open={confirm === "archive"}
        onOpenChange={(open) => !open && setConfirm(null)}
        title="Archive this endpoint?"
        description="Nothing more is delivered to it, and its waiting deliveries are cancelled. Its delivery history stays readable. An archived endpoint cannot be enabled again: register a new one instead."
        confirm="Archive"
        pending={archive.isPending}
        error={archive.error}
        onConfirm={() => archive.mutate(data.id, { onSuccess: () => { setConfirm(null); toast.success("Archived") } })}
      />
      <SecretDialog secret={secret?.secret ?? null} note={secret?.note ?? ""} onClose={() => setSecret(null)} />
    </>
  )
}

/** One delivery: its event (sensitive values masked), every attempt, and a retry when it failed. */
export function DeliveryDetail() {
  const { id } = useParams()
  const deliveryId = Number(id)
  const valid = Number.isInteger(deliveryId) && deliveryId > 0
  const delivery = useDelivery(valid ? deliveryId : null)
  const retry = useRetryDelivery()
  const toast = useToast()

  if (!valid || (delivery.error instanceof ApiError && delivery.error.status === 404)) {
    return <EmptyState title="No such delivery" action={<Button variant="outline" size="sm" asChild><Link to="/integrations/webhooks">Back to the webhooks</Link></Button>} />
  }
  if (delivery.isPending) return <LoadingBlock rows={6} label="Loading the delivery" />
  if (delivery.isError) return <ErrorState error={delivery.error} onRetry={() => delivery.refetch()} title="This delivery could not be loaded" />

  const data = delivery.data

  return (
    <>
      <Link to={`/integrations/webhooks/${data.endpoint_id}`} className="mb-3 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> {data.endpoint_name}
      </Link>
      <PageHeader
        title={`Delivery #${data.id}`}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <DeliveryBadge status={data.status} />
            <span className="font-mono text-xs">{data.event_type}</span>
            {data.test && <span className="text-xs">(test event)</span>}
          </span>
        }
        actions={
          data.retryable ? (
            <Button
              size="sm"
              disabled={retry.isPending}
              onClick={() =>
                retry.mutate(data.id, {
                  onSuccess: (result) => (result.status === "succeeded" ? toast.success("Delivered") : toast.error(`Not delivered: ${result.last_error ?? result.status}`)),
                  onError: (error) => toast.error(errorMessage(error)),
                })
              }
            >
              <RotateCw /> Retry now
            </Button>
          ) : undefined
        }
      />

      <Card>
        <CardContent className="pt-6">
          <Facts
            columns={4}
            label="Delivery"
            items={[
              ["Event id", <code key="e" className="break-all text-xs">{data.event_id}</code>],
              ["Event occurred", dateTime(data.occurred_at)],
              ["Queued", dateTime(data.created_at)],
              ["Finished", data.completed_at ? dateTime(data.completed_at) : "Not yet"],
              ["Attempts", `${data.attempts}${data.manual_retries ? ` (${data.manual_retries} manual retr${data.manual_retries === 1 ? "y" : "ies"}, last by ${data.last_retry_by})` : ""}`],
              ["Last answer", data.last_status_code ? `HTTP ${data.last_status_code}` : "No HTTP answer"],
              ["Last error", data.last_error?.replace(/_/g, " ")],
              ["Next attempt", data.status === "retrying" || data.status === "pending" ? dateTime(data.next_attempt_at) : "None"],
            ]}
          />
        </CardContent>
      </Card>

      <Section title="Attempts" description="Each POST to the receiver: when, how long it took, and what came back.">
        {data.attempt_log.length === 0 ? (
          <EmptyState title="Not attempted yet">The scheduler sends queued deliveries within seconds while the server runs.</EmptyState>
        ) : (
          <Table aria-label="Delivery attempts">
            <TableHeader>
              <TableRow>
                <TableHead>Attempt</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>Trigger</TableHead>
                <TableHead className="text-right">Duration</TableHead>
                <TableHead>Answer</TableHead>
                <TableHead>Outcome</TableHead>
                <TableHead>Detail</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.attempt_log.map((attempt) => (
                <TableRow key={attempt.id}>
                  <TableCell className="tabular">{attempt.attempt}</TableCell>
                  <TableCell className="whitespace-nowrap text-xs">{dateTime(attempt.started_at)}</TableCell>
                  <TableCell className="text-xs">{attempt.trigger}</TableCell>
                  <TableCell className="tabular text-right text-xs">{attempt.duration_ms === null ? "—" : `${attempt.duration_ms} ms`}</TableCell>
                  <TableCell className="text-xs">{attempt.status_code ? `HTTP ${attempt.status_code}` : "—"}</TableCell>
                  <TableCell>
                    <DeliveryBadge status={attempt.outcome === "retry" ? "retrying" : attempt.outcome} />
                  </TableCell>
                  <TableCell className="max-w-96 text-xs text-muted-foreground">{attempt.detail}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>

      <Section title="Payload" description="What was sent, with contact details and secret-like values masked here (the receiver gets the full payload).">
        <JsonBlock value={data.payload_preview} label="Payload preview" />
      </Section>
    </>
  )
}
