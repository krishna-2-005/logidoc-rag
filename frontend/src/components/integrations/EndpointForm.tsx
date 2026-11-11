import { useEffect, useState, type FormEvent } from "react"
import { errorMessage } from "@/components/common/states"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input, Label } from "@/components/ui/input"
import type { EndpointInput } from "@/hooks/integrations"
import type { WebhookConfiguration, WebhookEndpoint } from "@/lib/types"

/**
 * Registering or editing a webhook endpoint. The server checks the destination
 * (HTTPS, a public address, no credentials in the URL) when it is saved and
 * again before every delivery; its refusal is shown as it is.
 */
export function EndpointForm({
  open,
  onOpenChange,
  settings,
  endpoint,
  pending,
  error,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  settings: WebhookConfiguration | undefined
  endpoint?: WebhookEndpoint | null
  pending: boolean
  error: unknown
  onSubmit: (body: EndpointInput) => void
}) {
  const [name, setName] = useState("")
  const [url, setUrl] = useState("")
  const [types, setTypes] = useState<string[]>([])

  useEffect(() => {
    if (open) {
      setName(endpoint?.name ?? "")
      // A stored URL is shown masked: editing starts empty and keeps the stored one unless a new one is typed
      setUrl(endpoint ? "" : "https://")
      setTypes(endpoint?.event_types ?? [])
    }
  }, [open, endpoint])

  const toggle = (type: string) => setTypes((current) => (current.includes(type) ? current.filter((item) => item !== type) : [...current, type]))

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const body: EndpointInput = { name: name.trim(), event_types: types }
    if (!endpoint || url.trim()) body.url = url.trim()
    onSubmit(body)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <form onSubmit={submit} className="space-y-4" aria-label={endpoint ? "Edit the endpoint" : "Add a webhook endpoint"}>
          <DialogHeader>
            <DialogTitle>{endpoint ? `Edit ${endpoint.name}` : "Add a webhook endpoint"}</DialogTitle>
            <DialogDescription>
              Events are POSTed as signed JSON. {settings?.https_only === false ? "Development: plain http is allowed here." : "The destination must be a public HTTPS address."}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-1.5">
            <Label htmlFor="endpoint-name">Name</Label>
            <Input id="endpoint-name" value={name} maxLength={100} required onChange={(event) => setName(event.target.value)} placeholder="ERP accounts payable" />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="endpoint-url">Destination URL</Label>
            <Input id="endpoint-url" type="url" value={url} maxLength={2000} required={!endpoint} onChange={(event) => setUrl(event.target.value)} placeholder="https://erp.example.net/webhooks/logidoc" />
            {endpoint && <p className="text-xs text-muted-foreground">Now: {endpoint.url}. Leave empty to keep it.</p>}
          </div>

          <fieldset className="space-y-2">
            <legend className="text-xs font-medium">Events</legend>
            {(settings?.event_types ?? []).map((item) => (
              <label key={item.type} className="flex items-start gap-2.5 rounded-md border p-2.5 text-sm">
                <input type="checkbox" className="mt-0.5" checked={types.includes(item.type)} onChange={() => toggle(item.type)} />
                <span>
                  <span className="font-mono text-xs font-medium">{item.type}</span>
                  <span className="block text-xs text-muted-foreground">{item.description}</span>
                </span>
              </label>
            ))}
          </fieldset>

          {error ? <Alert tone="bad" role="alert">{errorMessage(error)}</Alert> : null}

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={pending || !name.trim() || types.length === 0 || (!endpoint && !url.trim())}>
              {endpoint ? "Save changes" : "Add endpoint"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
