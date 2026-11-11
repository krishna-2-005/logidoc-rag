import { Check, Copy, KeyRound } from "lucide-react"
import { useEffect, useState, type FormEvent, type ReactNode } from "react"
import { errorMessage } from "@/components/common/states"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label, Textarea } from "@/components/ui/input"

/**
 * A decision that needs words: a reason, a note. The server checks the length
 * again; the button stays off until the text is long enough.
 */
export function ReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  label,
  minLength = 0,
  confirm,
  tone = "default",
  pending,
  error,
  onSubmit,
  children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: ReactNode
  label: string
  minLength?: number
  confirm: string
  tone?: "default" | "destructive"
  pending?: boolean
  error?: unknown
  onSubmit: (text: string) => void
  children?: ReactNode
}) {
  const [text, setText] = useState("")

  useEffect(() => {
    if (open) setText("")
  }, [open])

  const short = text.trim().length < minLength

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!short) onSubmit(text.trim())
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {description && <DialogDescription>{description}</DialogDescription>}
          </DialogHeader>
          {children}
          <div className="space-y-1.5">
            <Label htmlFor="reason-text">{label}</Label>
            <Textarea id="reason-text" value={text} onChange={(event) => setText(event.target.value)} maxLength={1000} />
            {minLength > 0 && <p className="text-xs text-muted-foreground">At least {minLength} characters ({text.trim().length} so far).</p>}
          </div>
          {error ? <Alert tone="bad" role="alert">{errorMessage(error)}</Alert> : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant={tone === "destructive" ? "destructive" : "default"} disabled={short || pending}>
              {confirm}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** A yes-or-no confirmation for an action that cannot be undone from this screen. */
export function ConfirmDialog({ open, onOpenChange, title, description, confirm, onConfirm, pending, error }: { open: boolean; onOpenChange: (open: boolean) => void; title: string; description: ReactNode; confirm: string; onConfirm: () => void; pending?: boolean; error?: unknown }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {error ? <Alert tone="bad" role="alert">{errorMessage(error)}</Alert> : null}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={onConfirm} disabled={pending}>
            {confirm}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * A signing secret, shown once: the server never returns it again. Copying is
 * offered; closing the dialog forgets it.
 */
export function SecretDialog({ secret, note, onClose }: { secret: string | null; note: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false)

  useEffect(() => setCopied(false), [secret])

  const copy = () =>
    navigator.clipboard?.writeText(secret ?? "").then(
      () => setCopied(true),
      () => setCopied(false),
    )

  return (
    <Dialog open={secret !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRound className="size-5 text-primary" aria-hidden="true" /> Signing secret
          </DialogTitle>
          <DialogDescription>{note}</DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2 rounded-md border bg-muted/50 p-3">
          <code aria-label="Signing secret" className="min-w-0 flex-1 break-all font-mono text-xs">
            {secret}
          </code>
          <Button variant="outline" size="sm" onClick={copy}>
            {copied ? <Check /> : <Copy />} {copied ? "Copied" : "Copy"}
          </Button>
        </div>
        <DialogFooter>
          <Button onClick={onClose}>I have stored it</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
