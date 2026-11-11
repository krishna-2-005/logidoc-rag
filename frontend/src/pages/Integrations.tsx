import { Cable, Plus } from "lucide-react"
import { useEffect, useState, type FormEvent } from "react"
import { Link, useNavigate } from "react-router-dom"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { EmptyState, ErrorState, LoadingBlock, errorMessage } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { ConnectionBadge, EnabledBadge, RunBadge, every } from "@/components/integrations/badges"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input, Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useCreateIntegration, useIntegrations } from "@/hooks/integrations"
import { count, dateTime } from "@/lib/format"
import type { ProviderInfo } from "@/lib/types"

const SANDBOX_SWITCHES: [string, string][] = [
  ["simulate_auth_failure", "Refuse the credential (authentication failure)"],
  ["duplicate_records", "Repeat a record in every page (duplicates)"],
  ["unavailable", "Be unavailable (every call fails)"],
]

function AddIntegration({ open, onOpenChange, providers }: { open: boolean; onOpenChange: (open: boolean) => void; providers: ProviderInfo[] }) {
  const create = useCreateIntegration()
  const toast = useToast()
  const navigate = useNavigate()
  const available = providers.filter((provider) => provider.available)
  const [provider, setProvider] = useState("")
  const [name, setName] = useState("")
  const [credential, setCredential] = useState("")
  const [minutes, setMinutes] = useState("60")
  const [pageSize, setPageSize] = useState("10")
  const [rateLimit, setRateLimit] = useState("0")
  const [timeouts, setTimeouts] = useState("0")
  const [switches, setSwitches] = useState<Record<string, boolean>>({})

  useEffect(() => {
    if (open) {
      const first = available[0]
      setProvider(first?.key ?? "")
      setName(first ? first.label.replace(/ \(.*\)$/, "") : "")
      setCredential(first?.default_credential_env ?? "")
      setMinutes("60")
      setPageSize("10")
      setRateLimit("0")
      setTimeouts("0")
      setSwitches({})
      create.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const chosen = providers.find((item) => item.key === provider)

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const settings = chosen?.sandbox
      ? { page_size: Number(pageSize), rate_limit_every: Number(rateLimit), timeout_every: Number(timeouts), ...Object.fromEntries(SANDBOX_SWITCHES.map(([key]) => [key, !!switches[key]])) }
      : {}
    create.mutate(
      { name: name.trim(), provider, settings, credential_env: credential.trim() || null, sync_interval_seconds: Number(minutes) * 60 },
      {
        onSuccess: (integration) => {
          toast.success(`Configured ${integration.name}`)
          onOpenChange(false)
          navigate(`/integrations/connections/${integration.id}`)
        },
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <form onSubmit={submit} className="space-y-4" aria-label="Configure an integration">
          <DialogHeader>
            <DialogTitle>Configure an integration</DialogTitle>
            <DialogDescription>The credential is never typed here: name the environment variable on the server that holds it (LOGIDOC_INTEGRATION_…). Its value is read when the system is called and never stored or shown.</DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="integration-provider">System</Label>
              <Select id="integration-provider" value={provider} onChange={(event) => setProvider(event.target.value)} required>
                {available.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="integration-name">Name</Label>
              <Input id="integration-name" value={name} maxLength={100} required onChange={(event) => setName(event.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="integration-credential">Credential variable</Label>
              <Input id="integration-credential" value={credential} maxLength={80} onChange={(event) => setCredential(event.target.value)} className="font-mono text-xs" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="integration-interval">Sync every (minutes)</Label>
              <Input id="integration-interval" type="number" min={1} max={10080} required value={minutes} onChange={(event) => setMinutes(event.target.value)} />
            </div>
          </div>

          {chosen?.sandbox && (
            <fieldset className="space-y-3 rounded-md border p-3">
              <legend className="px-1 text-xs font-medium">Sandbox behaviour (to test retries and error handling)</legend>
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="space-y-1.5">
                  <Label htmlFor="sandbox-page">Records per page</Label>
                  <Input id="sandbox-page" type="number" min={1} max={200} value={pageSize} onChange={(event) => setPageSize(event.target.value)} />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="sandbox-rate">Rate limit every Nth call</Label>
                  <Input id="sandbox-rate" type="number" min={0} max={100} value={rateLimit} onChange={(event) => setRateLimit(event.target.value)} />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="sandbox-timeout">Time out every Nth call</Label>
                  <Input id="sandbox-timeout" type="number" min={0} max={100} value={timeouts} onChange={(event) => setTimeouts(event.target.value)} />
                </div>
              </div>
              {SANDBOX_SWITCHES.map(([key, text]) => (
                <label key={key} className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={!!switches[key]} onChange={(event) => setSwitches((current) => ({ ...current, [key]: event.target.checked }))} /> {text}
                </label>
              ))}
              <p className="text-xs text-muted-foreground">0 turns a simulated failure off. The sandbox token is the public value “sandbox-token”.</p>
            </fieldset>
          )}

          {create.error ? <Alert tone="bad" role="alert">{errorMessage(create.error)}</Alert> : null}

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || !provider || !name.trim()}>
              Configure
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function Providers({ providers }: { providers: ProviderInfo[] }) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {providers.map((provider) => (
        <Card key={provider.key}>
          <CardContent className="space-y-2 pt-6 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="font-semibold">{provider.label}</h3>
              {provider.sandbox && <Badge tone="info">Synthetic data</Badge>}
              <Badge tone={provider.available ? "ok" : "neutral"}>{provider.available ? "Available" : "Not available here"}</Badge>
            </div>
            <p className="text-muted-foreground">{provider.description}</p>
            <p className="text-xs">
              Reads {provider.entities.join(" and ")}
              {provider.accepts_audit_runs ? "; receives recorded audit runs" : ""}.
            </p>
            {provider.unavailable_reason && <p className="text-xs text-warn">{provider.unavailable_reason}</p>}
          </CardContent>
        </Card>
      ))}
      <Card>
        <CardContent className="space-y-2 pt-6 text-sm">
          <h3 className="font-semibold">Real TMS and ERP systems</h3>
          <p className="text-muted-foreground">No real vendor is connected yet. A vendor adapter is added only after it has been tested against the vendor’s own sandbox with its documentation and credentials (docs/TMS_ERP_INTEGRATION_FRAMEWORK.md).</p>
        </CardContent>
      </Card>
    </div>
  )
}

/**
 * TMS and ERP integrations: each configured system with its connection,
 * last syncs, record counts and cursor. Records imported from a system are
 * reference data with their provenance; they never change a document, an
 * audit or a review.
 */
export function Integrations() {
  const list = useIntegrations()
  const [adding, setAdding] = useState(false)
  const navigate = useNavigate()
  const providers = list.data?.providers ?? []
  const anyAvailable = providers.some((provider) => provider.available)

  return (
    <>
      <PageHeader
        title="TMS and ERP integrations"
        description="Shipments and carriers read from external systems on a schedule, with every record’s source and history, and LogiDoc’s recorded audit runs handed back. Credentials stay on the server."
        actions={
          <Button size="sm" disabled={!anyAvailable} onClick={() => setAdding(true)}>
            <Plus /> Configure integration
          </Button>
        }
      />

      {list.isPending && <LoadingBlock rows={4} label="Loading the integrations" />}
      {list.isError && <ErrorState error={list.error} onRetry={() => list.refetch()} title="The integrations could not be loaded" />}

      {list.data && list.data.integrations.length === 0 && (
        <EmptyState icon={<Cable className="size-5" />} title="No integration configured">
          {anyAvailable ? "Configure one to start synchronising." : "No system can be configured on this server yet: see why below."}
        </EmptyState>
      )}

      {list.data && list.data.integrations.length > 0 && (
        <Table aria-label="Integrations">
          <TableHeader>
            <TableRow>
              <TableHead>Integration</TableHead>
              <TableHead>Connection</TableHead>
              <TableHead>Last sync</TableHead>
              <TableHead>Last success</TableHead>
              <TableHead>Last failure</TableHead>
              <TableHead>Next sync</TableHead>
              <TableHead className="text-right">Imported</TableHead>
              <TableHead className="text-right">Updated</TableHead>
              <TableHead className="text-right">Rejected</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.data.integrations.map((integration) => (
              <TableRow key={integration.id} className="cursor-pointer" onClick={() => navigate(`/integrations/connections/${integration.id}`)}>
                <TableCell>
                  <Link to={`/integrations/connections/${integration.id}`} className="font-medium text-primary hover:underline" onClick={(event) => event.stopPropagation()}>
                    {integration.name}
                  </Link>
                  <p className="text-xs text-muted-foreground">
                    {providers.find((item) => item.key === integration.provider)?.label ?? integration.provider} · {every(integration.sync_interval_seconds)}
                  </p>
                </TableCell>
                <TableCell>
                  <div className="flex flex-wrap gap-1">
                    <ConnectionBadge status={integration.connection_status} />
                    {!integration.enabled && <EnabledBadge enabled={false} />}
                  </div>
                </TableCell>
                <TableCell>{integration.running ? <RunBadge status="running" /> : integration.last_run ? <RunBadge status={integration.last_run.status} /> : <span className="text-xs text-muted-foreground">Never</span>}</TableCell>
                <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{integration.last_success_at ? dateTime(integration.last_success_at) : "Never"}</TableCell>
                <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{integration.last_failure_at ? dateTime(integration.last_failure_at) : "None"}</TableCell>
                <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{integration.enabled ? dateTime(integration.next_sync_at) : "Disabled"}</TableCell>
                <TableCell className="tabular text-right">{count(integration.totals.imported)}</TableCell>
                <TableCell className="tabular text-right">{count(integration.totals.updated)}</TableCell>
                <TableCell className={`tabular text-right ${integration.totals.rejected ? "text-warn" : ""}`}>{count(integration.totals.rejected)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {list.data && (
        <Section title="Systems" description="What this server can connect to.">
          <Providers providers={providers} />
        </Section>
      )}

      <AddIntegration open={adding} onOpenChange={setAdding} providers={providers} />
    </>
  )
}
