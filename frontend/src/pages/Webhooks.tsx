import { Plus, Webhook } from "lucide-react"
import { useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { SecretDialog } from "@/components/common/Dialogs"
import { Facts } from "@/components/common/Facts"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { EnabledBadge, successRate } from "@/components/integrations/badges"
import { EndpointForm } from "@/components/integrations/EndpointForm"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useCreateEndpoint, useEndpoints, useWebhookSettings } from "@/hooks/integrations"
import { dateTime } from "@/lib/format"
import type { WebhookConfiguration } from "@/lib/types"

function Signing({ settings }: { settings: WebhookConfiguration }) {
  return (
    <Card>
      <CardContent className="pt-6">
        <Facts
          label="How receivers verify deliveries"
          columns={2}
          items={[
            ["Signature header", <code key="s">{settings.signature.signature_header}</code>],
            ["Signed content", <code key="c" className="text-xs">{settings.signature.scheme}</code>],
            ["Timestamp header", <code key="t">{settings.signature.timestamp_header}</code>],
            ["Event id header", <code key="e">{settings.signature.event_id_header}</code>],
            ["Accepted clock difference", `${settings.signature.tolerance_seconds / 60} minutes`],
            ["Attempts per delivery", `${settings.max_attempts}, each with a ${settings.timeout_seconds} s timeout`],
          ]}
        />
        <p className="mt-4 text-xs text-muted-foreground">
          Delivery is at least once: a receiver must refuse an event id it has already processed. After a secret rotation, the previous secret also signs for {settings.rotation_grace_hours} hours. Redirects are never followed.
        </p>
      </CardContent>
    </Card>
  )
}

/**
 * Webhook endpoints: where committed events (freight audits, exceptions,
 * carrier changes) are delivered, with each one's success over the last seven
 * days. Administrators only.
 */
export function Webhooks() {
  const settings = useWebhookSettings()
  const [archived, setArchived] = useState(false)
  const endpoints = useEndpoints(archived)
  const create = useCreateEndpoint()
  const [adding, setAdding] = useState(false)
  const [secret, setSecret] = useState<{ secret: string; note: string } | null>(null)
  const navigate = useNavigate()
  const toast = useToast()
  const enabled = settings.data?.delivery_enabled

  return (
    <>
      <PageHeader
        title="Webhooks"
        description="Signed notifications to external systems when a freight audit is recorded, an exception is created or changes status, or the carrier master data changes. Only committed changes are sent."
        actions={
          <Button
            size="sm"
            disabled={!enabled}
            onClick={() => {
              create.reset()
              setAdding(true)
            }}
          >
            <Plus /> Add endpoint
          </Button>
        }
      />

      {settings.data && !settings.data.delivery_enabled && (
        <Alert tone="warn" className="mb-4 flex-col">
          <p className="font-medium">Webhook delivery is off</p>
          {settings.data.problems.map((problem) => (
            <p key={problem}>{problem}</p>
          ))}
        </Alert>
      )}
      {settings.data?.private_destinations_allowed && <Alert tone="warn" className="mb-4">Development setting: loopback and private-network destinations are allowed. Production refuses this setting.</Alert>}
      {settings.isError && <ErrorState error={settings.error} onRetry={() => settings.refetch()} title="The webhook settings could not be loaded" />}

      {endpoints.isPending && <LoadingBlock rows={4} label="Loading the endpoints" />}
      {endpoints.isError && <ErrorState error={endpoints.error} onRetry={() => endpoints.refetch()} title="The endpoints could not be loaded" />}

      {endpoints.data && endpoints.data.endpoints.length === 0 && (
        <EmptyState icon={<Webhook className="size-5" />} title={archived ? "No endpoint" : "No webhook endpoint yet"}>
          {enabled ? "Add an endpoint to send events to an ERP, a TMS or an accounts payable system." : "Set the signing key first (see above)."}
        </EmptyState>
      )}

      {endpoints.data && endpoints.data.endpoints.length > 0 && (
        <Table aria-label="Webhook endpoints">
          <TableHeader>
            <TableRow>
              <TableHead>Endpoint</TableHead>
              <TableHead>Events</TableHead>
              <TableHead>State</TableHead>
              <TableHead>Success, last 7 days</TableHead>
              <TableHead className="text-right">Failed</TableHead>
              <TableHead className="text-right">Waiting</TableHead>
              <TableHead>Last attempt</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {endpoints.data.endpoints.map((endpoint) => (
              <TableRow key={endpoint.id} className="cursor-pointer" onClick={() => navigate(`/integrations/webhooks/${endpoint.id}`)}>
                <TableCell className="max-w-80">
                  <Link to={`/integrations/webhooks/${endpoint.id}`} className="font-medium text-primary hover:underline" onClick={(event) => event.stopPropagation()}>
                    {endpoint.name}
                  </Link>
                  <p className="truncate font-mono text-xs text-muted-foreground">{endpoint.url}</p>
                </TableCell>
                <TableCell className="text-xs">{endpoint.event_types.length} type{endpoint.event_types.length === 1 ? "" : "s"}</TableCell>
                <TableCell>
                  <EnabledBadge enabled={endpoint.enabled} archived={endpoint.archived} />
                </TableCell>
                <TableCell className="tabular text-sm">{successRate(endpoint.stats)}</TableCell>
                <TableCell className={`tabular text-right ${endpoint.stats.failed ? "font-medium text-bad" : ""}`}>{endpoint.stats.failed}</TableCell>
                <TableCell className="tabular text-right">{endpoint.stats.waiting}</TableCell>
                <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{endpoint.stats.last_attempt_at ? dateTime(endpoint.stats.last_attempt_at) : "Never"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <label className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
        <input type="checkbox" checked={archived} onChange={(event) => setArchived(event.target.checked)} /> Show archived endpoints
      </label>

      {settings.data && (
        <Section title="Verifying deliveries" description="What the receiving system checks before it trusts a delivery.">
          <Signing settings={settings.data} />
        </Section>
      )}

      <EndpointForm
        open={adding}
        onOpenChange={setAdding}
        settings={settings.data}
        pending={create.isPending}
        error={create.error}
        onSubmit={(body) =>
          create.mutate(body, {
            onSuccess: (issued) => {
              setAdding(false)
              setSecret({ secret: issued.secret, note: issued.note })
              toast.success(`Added ${issued.endpoint.name}`)
            },
          })
        }
      />
      <SecretDialog secret={secret?.secret ?? null} note={secret?.note ?? ""} onClose={() => setSecret(null)} />
    </>
  )
}
