import { AlertTriangle, CheckCircle2, CircleDashed, CircleSlash, Clock, Eye, FileWarning, Loader2, PauseCircle, ShieldCheck, XCircle } from "lucide-react"
import type { ReactNode } from "react"
import { Badge, type Tone } from "@/components/ui/badge"
import { label } from "@/lib/format"

interface Look {
  tone: Tone
  icon: ReactNode
  text: string
}

/**
 * One place decides how every status looks. A status is always shown as text
 * with an icon, never by colour alone.
 */
const LOOKS: Record<string, Look> = {
  // Overall audit result
  "DISCREPANCIES DETECTED": { tone: "bad", icon: <AlertTriangle />, text: "Discrepancies" },
  "NEEDS REVIEW": { tone: "warn", icon: <Eye />, text: "Needs review" },
  "NO DISCREPANCIES": { tone: "ok", icon: <CheckCircle2 />, text: "No discrepancies" },
  "NOT RECONCILED": { tone: "neutral", icon: <CircleSlash />, text: "Not reconciled" },

  // Quantity, invoice and carrier checks
  MATCH: { tone: "ok", icon: <CheckCircle2 />, text: "Match" },
  MISMATCH: { tone: "bad", icon: <XCircle />, text: "Mismatch" },

  // Delivery
  "ON TIME": { tone: "ok", icon: <CheckCircle2 />, text: "On time" },
  EARLY: { tone: "info", icon: <Clock />, text: "Early" },
  DELAYED: { tone: "bad", icon: <Clock />, text: "Delayed" },

  // Freight audit (invoice against contract and execution)
  MATCHED: { tone: "ok", icon: <CheckCircle2 />, text: "Matched" },
  MATCHED_WITH_TOLERANCE: { tone: "ok", icon: <CheckCircle2 />, text: "Matched within tolerance" },
  DISCREPANCY: { tone: "bad", icon: <XCircle />, text: "Discrepancy" },
  INSUFFICIENT_EVIDENCE: { tone: "warn", icon: <FileWarning />, text: "Insufficient evidence" },
  DUPLICATE: { tone: "bad", icon: <AlertTriangle />, text: "Duplicate" },
  PENDING_REVIEW: { tone: "warn", icon: <Eye />, text: "Pending review" },

  // Exception workflow
  open: { tone: "info", icon: <CircleDashed />, text: "Open" },
  in_review: { tone: "warn", icon: <Eye />, text: "In review" },
  resolved: { tone: "ok", icon: <CheckCircle2 />, text: "Resolved" },
  dismissed: { tone: "neutral", icon: <PauseCircle />, text: "Dismissed" },
  pending: { tone: "warn", icon: <Clock />, text: "Awaiting approval" },
  approved: { tone: "ok", icon: <ShieldCheck />, text: "Approved" },
  rejected: { tone: "bad", icon: <XCircle />, text: "Rejected" },

  // Ingestion
  queued: { tone: "info", icon: <Clock />, text: "Queued" },
  processing: { tone: "info", icon: <Loader2 className="animate-spin" />, text: "Processing" },
  indexed: { tone: "ok", icon: <CheckCircle2 />, text: "Indexed" },
  partial: { tone: "warn", icon: <AlertTriangle />, text: "Partial" },
  failed: { tone: "bad", icon: <XCircle />, text: "Failed" },
  unsupported: { tone: "bad", icon: <FileWarning />, text: "Unsupported" },

  // Document scan and completeness
  parsed: { tone: "ok", icon: <CheckCircle2 />, text: "Text extracted" },
  no_text: { tone: "bad", icon: <FileWarning />, text: "No text (needs OCR)" },
  empty: { tone: "bad", icon: <FileWarning />, text: "No pages" },
  unreadable: { tone: "bad", icon: <XCircle />, text: "Unreadable" },
  ready: { tone: "ok", icon: <CheckCircle2 />, text: "Ready" },
  incomplete: { tone: "warn", icon: <AlertTriangle />, text: "Incomplete" },

  // Severity
  HIGH: { tone: "bad", icon: <AlertTriangle />, text: "High" },
  MEDIUM: { tone: "warn", icon: <AlertTriangle />, text: "Medium" },
}

export function statusLook(status: string | null | undefined): Look {
  if (status && LOOKS[status]) return LOOKS[status]
  return { tone: "neutral", icon: <CircleDashed />, text: label(status) }
}

export function StatusBadge({ status, className }: { status: string | null | undefined; className?: string }) {
  const look = statusLook(status)

  return (
    <Badge tone={look.tone} className={className}>
      <span aria-hidden="true" className="inline-flex">
        {look.icon}
      </span>
      {look.text}
    </Badge>
  )
}
