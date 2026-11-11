import { useEffect, useState, type FormEvent } from "react"
import { errorMessage } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input, Label, Select } from "@/components/ui/input"
import { useChangeCarrier, useRegisterCarrier, type CarrierInput } from "@/hooks/registry"
import { ApiError } from "@/lib/api"
import type { RegisteredCarrierRecord } from "@/lib/types"

const FIELDS = [
  "legal_name", "trading_name", "scac", "mc_number", "dot_number", "contact_name", "contact_email", "contact_phone",
  "address_line1", "address_line2", "city", "region", "postal_code", "country",
] as const

type Field = (typeof FIELDS)[number]
type Values = Record<Field, string> & { status: string }

const EMPTY: Values = { ...(Object.fromEntries(FIELDS.map((field) => [field, ""])) as Record<Field, string>), status: "active" }

function valuesOf(carrier: RegisteredCarrierRecord | null | undefined): Values {
  if (!carrier) return EMPTY
  return { ...(Object.fromEntries(FIELDS.map((field) => [field, carrier[field] ?? ""])) as Record<Field, string>), status: carrier.status }
}

function TextField({ id, label, value, onChange, hint, required, maxLength = 200, type = "text", autoComplete = "off" }: { id: Field; label: string; value: string; onChange: (field: Field, value: string) => void; hint?: string; required?: boolean; maxLength?: number; type?: string; autoComplete?: string }) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={`carrier-${id}`}>
        {label}
        {required && <span className="text-bad"> *</span>}
      </Label>
      <Input id={`carrier-${id}`} type={type} value={value} required={required} maxLength={maxLength} autoComplete={autoComplete} aria-describedby={hint ? `carrier-${id}-hint` : undefined} onChange={(event) => onChange(id, event.target.value)} />
      {hint && (
        <p id={`carrier-${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  )
}

/**
 * Registering or editing a carrier. The server validates every value (it never
 * cleans a malformed identifier into something else) and refuses an identifier
 * another carrier holds; its message is shown as it is. A name equal to an
 * existing carrier's is refused as a possible duplicate, and can be registered
 * only after saying it is a different company.
 */
export function CarrierForm({ open, onOpenChange, carrier, onSaved }: { open: boolean; onOpenChange: (open: boolean) => void; carrier?: RegisteredCarrierRecord | null; onSaved?: (carrier: RegisteredCarrierRecord) => void }) {
  const [values, setValues] = useState<Values>(EMPTY)
  const [distinct, setDistinct] = useState(false)
  const register = useRegisterCarrier()
  const change = useChangeCarrier()
  const toast = useToast()
  const editing = !!carrier
  const mutation = editing ? change : register

  useEffect(() => {
    if (open) {
      setValues(valuesOf(carrier))
      setDistinct(false)
      register.reset()
      change.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, carrier])

  const set = (field: Field | "status", value: string) => setValues((current) => ({ ...current, [field]: value }))

  const error = mutation.error
  const possibleDuplicate = error instanceof ApiError && error.status === 409 && error.message.startsWith("Possibly the same company")

  const submit = (event: FormEvent) => {
    event.preventDefault()

    const fields: Record<string, string | null> = {}
    for (const field of FIELDS) {
      const text = values[field].trim()
      const before = carrier ? (carrier[field] ?? "") : ""
      // Editing sends only what changed; an emptied field is sent as null (removed). The legal name is
      // always required: an empty one is sent as it is, and the server refuses it
      if (!editing || text !== before) fields[field] = text === "" && field !== "legal_name" ? null : text
    }
    const body = fields as CarrierInput
    if (!editing || values.status !== carrier?.status) body.status = values.status

    const done = (saved: RegisteredCarrierRecord) => {
      toast.success(editing ? `Saved ${saved.legal_name}` : `Registered ${saved.legal_name}`)
      onOpenChange(false)
      onSaved?.(saved)
    }

    if (editing && carrier) {
      if (Object.keys(body).length === 0) {
        onOpenChange(false)
        return
      }
      change.mutate({ id: carrier.id, ...body }, { onSuccess: done })
    } else {
      register.mutate({ ...body, confirm_distinct: distinct }, { onSuccess: done })
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <form onSubmit={submit} className="space-y-5" aria-label={editing ? "Edit carrier" : "Register a carrier"}>
          <DialogHeader>
            <DialogTitle>{editing ? `Edit ${carrier?.legal_name}` : "Register a carrier"}</DialogTitle>
            <DialogDescription>
              Identifiers are checked as typed and refused when malformed. An identifier belongs to one carrier only. Every change is recorded in the carrier history.
            </DialogDescription>
          </DialogHeader>

          <fieldset className="grid gap-4 sm:grid-cols-2">
            <legend className="mb-2 text-sm font-semibold">Company</legend>
            <TextField id="legal_name" label="Legal name" value={values.legal_name} onChange={set} required />
            <TextField id="trading_name" label="Trading name" value={values.trading_name} onChange={set} hint="The name used on documents, when it differs." />
            <div className="space-y-1.5">
              <Label htmlFor="carrier-status">Registration status</Label>
              <Select id="carrier-status" value={values.status} onChange={(event) => set("status", event.target.value)}>
                <option value="active">Active</option>
                <option value="on_hold">On hold</option>
                <option value="inactive">Inactive</option>
              </Select>
            </div>
          </fieldset>

          <fieldset className="grid gap-4 sm:grid-cols-3">
            <legend className="mb-2 text-sm font-semibold">Identifiers</legend>
            <TextField id="scac" label="SCAC" value={values.scac} onChange={set} maxLength={10} hint="2 to 4 letters." />
            <TextField id="mc_number" label="MC number" value={values.mc_number} onChange={set} maxLength={20} hint="Up to 8 digits; “MC-” may come first." />
            <TextField id="dot_number" label="USDOT number" value={values.dot_number} onChange={set} maxLength={20} hint="Up to 8 digits; “USDOT” may come first." />
          </fieldset>

          <fieldset className="grid gap-4 sm:grid-cols-3">
            <legend className="mb-2 text-sm font-semibold">Contact</legend>
            <TextField id="contact_name" label="Contact person" value={values.contact_name} onChange={set} />
            <TextField id="contact_email" label="Email" type="email" value={values.contact_email} onChange={set} maxLength={254} />
            <TextField id="contact_phone" label="Phone" type="tel" value={values.contact_phone} onChange={set} maxLength={30} />
          </fieldset>

          <fieldset className="grid gap-4 sm:grid-cols-2">
            <legend className="mb-2 text-sm font-semibold">Address</legend>
            <TextField id="address_line1" label="Address line 1" value={values.address_line1} onChange={set} />
            <TextField id="address_line2" label="Address line 2" value={values.address_line2} onChange={set} />
            <TextField id="city" label="City" value={values.city} onChange={set} maxLength={100} />
            <TextField id="region" label="State or province" value={values.region} onChange={set} maxLength={100} />
            <TextField id="postal_code" label="Postal code" value={values.postal_code} onChange={set} maxLength={20} />
            <TextField id="country" label="Country" value={values.country} onChange={set} maxLength={10} hint="Two-letter code, such as US or CA." />
          </fieldset>

          {error ? (
            <Alert tone="bad" role="alert" className="flex-col gap-2">
              <p>{errorMessage(error)}</p>
              {possibleDuplicate && !editing && (
                <label className="flex items-start gap-2 text-foreground">
                  <input type="checkbox" className="mt-1" checked={distinct} onChange={(event) => setDistinct(event.target.checked)} />
                  <span>I have checked: this is a different company, not the carrier named above. Register it separately.</span>
                </label>
              )}
            </Alert>
          ) : null}

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={mutation.isPending || !values.legal_name.trim()}>
              {editing ? "Save changes" : "Register carrier"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
