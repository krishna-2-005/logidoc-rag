import { useEffect, useState, type FormEvent } from "react"
import { ReasonDialog } from "@/components/common/Dialogs"
import { errorMessage } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label, Select, Textarea } from "@/components/ui/input"
import { useAllCarriers, useDecideLink, useDocumentNames, useProposeLink } from "@/hooks/registry"
import type { CarrierReference, LinkRequest } from "@/lib/types"

export const MIN_REASON = 10

/**
 * Proposing that a carrier name printed on the documents belongs to a
 * registered carrier (or, for a linked name, moving or unlinking it). It has no
 * effect until a different administrator approves it.
 */
export function ProposeLinkDialog({
  name,
  open,
  onOpenChange,
  current,
  suggestions = [],
  fixedCarrier,
}: {
  name: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
  current?: CarrierReference | null
  suggestions?: CarrierReference[]
  /** Proposing from a carrier's own page: the carrier is given */
  fixedCarrier?: CarrierReference
}) {
  const carriers = useAllCarriers(open && !fixedCarrier)
  // No name given (proposing from a carrier's page): choose one of the names the documents print
  const names = useDocumentNames({ limit: 200 }, open && name === null)
  const propose = useProposeLink()
  const toast = useToast()
  const [carrier, setCarrier] = useState("")
  const [picked, setPicked] = useState("")
  const [reason, setReason] = useState("")

  useEffect(() => {
    if (open) {
      setCarrier(fixedCarrier ? String(fixedCarrier.id) : suggestions[0] ? String(suggestions[0].id) : "")
      setPicked("")
      setReason("")
      propose.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, name])

  const chosenName = name ?? (picked || null)
  const unlink = carrier === "unlink"
  const ready = !!chosenName && carrier !== "" && reason.trim().length >= MIN_REASON
  const choices = (names.data?.names ?? []).filter((item) => item.state !== "pending" && item.linked_carrier?.id !== fixedCarrier?.id)

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!ready || !chosenName) return
    propose.mutate(
      { name: chosenName, carrier_id: unlink ? null : Number(carrier), reason: reason.trim() },
      {
        onSuccess: () => {
          toast.success(`Proposed: a second administrator must approve it`)
          onOpenChange(false)
        },
      },
    )
  }

  const options = (carriers.data?.carriers ?? []).filter((item) => item.id !== current?.id)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <form onSubmit={submit} className="space-y-4" aria-label="Propose a name link">
          <DialogHeader>
            <DialogTitle>{current ? "Propose moving or unlinking a name" : "Propose a name link"}</DialogTitle>
            <DialogDescription>
              Shipments whose documents print {chosenName ? <strong className="text-foreground">{chosenName}</strong> : "the chosen name"} would be counted under the chosen carrier. Nothing changes until a different administrator approves the request. Names are never merged because they look alike.
            </DialogDescription>
          </DialogHeader>

          {name === null && (
            <div className="space-y-1.5">
              <Label htmlFor="link-name">Carrier name on the documents</Label>
              <Select id="link-name" value={picked} onChange={(event) => setPicked(event.target.value)} required>
                <option value="">Choose a name…</option>
                {choices.map((item) => (
                  <option key={item.name_key} value={item.name}>
                    {item.name} · {item.shipments} shipment{item.shipments === 1 ? "" : "s"}
                    {item.linked_carrier ? ` (linked to ${item.linked_carrier.legal_name})` : ""}
                  </option>
                ))}
              </Select>
              {names.isSuccess && choices.length === 0 && <p className="text-xs text-muted-foreground">No document name can be linked: none is printed on the documents, or every one is linked here or waiting for a decision.</p>}
              {names.isError && <p className="text-xs text-bad">{errorMessage(names.error)}</p>}
            </div>
          )}

          {current && (
            <p className="text-sm">
              Linked now to <strong>{current.legal_name}</strong>.
            </p>
          )}

          <div className="space-y-1.5">
            <Label htmlFor="link-carrier">Registered carrier</Label>
            {fixedCarrier ? (
              <p id="link-carrier" className="text-sm font-medium">
                {fixedCarrier.legal_name}
              </p>
            ) : (
              <Select id="link-carrier" value={carrier} onChange={(event) => setCarrier(event.target.value)} required>
                <option value="">Choose a carrier…</option>
                {current && <option value="unlink">Unlink the name (no registered carrier)</option>}
                {options.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.legal_name}
                    {item.scac ? ` · SCAC ${item.scac}` : ""}
                    {item.status !== "active" ? ` (${item.status.replace("_", " ")})` : ""}
                  </option>
                ))}
              </Select>
            )}
            {!fixedCarrier && suggestions.length > 0 && (
              <p className="text-xs text-muted-foreground">Same name in the registry: {suggestions.map((item) => item.legal_name).join(", ")}. A hint only: check the documents before proposing.</p>
            )}
            {carriers.isError && <p className="text-xs text-bad">{errorMessage(carriers.error)}</p>}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="link-reason">Reason</Label>
            <Textarea id="link-reason" value={reason} onChange={(event) => setReason(event.target.value)} maxLength={1000} placeholder="What on the documents shows it is this carrier: remit-to address, MC number, SCAC…" />
            <p className="text-xs text-muted-foreground">At least {MIN_REASON} characters. The reason is kept in the carrier history.</p>
          </div>

          {propose.error ? <Alert tone="bad" role="alert">{errorMessage(propose.error)}</Alert> : null}

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!ready || propose.isPending}>
              Propose
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Approving or rejecting a link request. The proposer cannot decide it: the
 * buttons say so, and the server refuses it anyway.
 */
export function DecisionButtons({ request, me, canDecide }: { request: LinkRequest; me: string | undefined; canDecide: boolean }) {
  const decide = useDecideLink()
  const toast = useToast()
  const [mode, setMode] = useState<"approve" | "reject" | null>(null)
  const own = !!me && request.proposed_by === me

  if (!canDecide) return null

  if (own) {
    return <p className="text-xs text-muted-foreground">You proposed this: another administrator decides it.</p>
  }

  const run = (decision: "approve" | "reject", note: string) =>
    decide.mutate(
      { id: request.id, decision, note },
      {
        onSuccess: () => {
          toast.success(decision === "approve" ? `Approved: ${request.display_name} is linked` : `Rejected the request for ${request.display_name}`)
          setMode(null)
        },
      },
    )

  const target = request.carrier_id === null ? "unlinked" : `linked to ${request.carrier_legal_name ?? `carrier ${request.carrier_id}`}`
  const open = (next: "approve" | "reject") => {
    decide.reset()
    setMode(next)
  }

  return (
    <div className="flex flex-wrap gap-2">
      <Button size="sm" onClick={() => open("approve")} aria-label={`Approve the request for ${request.display_name}`}>
        Approve
      </Button>
      <Button size="sm" variant="outline" onClick={() => open("reject")} aria-label={`Reject the request for ${request.display_name}`}>
        Reject
      </Button>
      <ReasonDialog
        open={mode !== null}
        onOpenChange={(open) => !open && setMode(null)}
        title={mode === "approve" ? "Approve the link" : "Reject the link"}
        description={
          <>
            “{request.display_name}” would be {target}. Proposed by {request.proposed_by}: {request.reason}
          </>
        }
        label={mode === "approve" ? "Note (optional)" : "Why it is rejected"}
        minLength={mode === "reject" ? 1 : 0}
        confirm={mode === "approve" ? "Approve" : "Reject"}
        tone={mode === "reject" ? "destructive" : "default"}
        pending={decide.isPending}
        error={decide.error}
        onSubmit={(note) => mode && run(mode, note)}
      />
    </div>
  )
}
