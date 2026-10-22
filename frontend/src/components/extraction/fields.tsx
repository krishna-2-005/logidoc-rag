import { AlertTriangle, CheckCircle2, CircleDashed, CircleSlash, Clock, FileWarning, ShieldCheck, XCircle } from "lucide-react"
import { Badge, type Tone } from "@/components/ui/badge"
import { NA } from "@/lib/format"
import type { CorrectionStatus, FieldState } from "@/lib/types"

/**
 * How an extracted field is shown. A value that is not there is never drawn as a
 * number: each state says in words what is known about it.
 */
export const STATE_LOOK: Record<FieldState, { tone: Tone; short: string; long: string }> = {
  EXTRACTED: { tone: "ok", short: "Extracted", long: "Read from the document." },
  MISSING: { tone: "warn", short: "Missing", long: "Not found on the document." },
  UNPARSEABLE: { tone: "bad", short: "Unparseable", long: "Present on the document but not a valid value for this field." },
  UNREADABLE: { tone: "bad", short: "Unreadable", long: "Some pages have no readable text, so it cannot be said whether the field is absent." },
  UNSUPPORTED: { tone: "neutral", short: "Unsupported", long: "The extractor cannot read this field." },
  CONFLICTING: { tone: "bad", short: "Conflicting", long: "The document gives different values; none was chosen." },
}

const STATE_ICON: Record<FieldState, JSX.Element> = {
  EXTRACTED: <CheckCircle2 />,
  MISSING: <CircleDashed />,
  UNPARSEABLE: <XCircle />,
  UNREADABLE: <FileWarning />,
  UNSUPPORTED: <CircleSlash />,
  CONFLICTING: <AlertTriangle />,
}

export function FieldStateBadge({ state }: { state: FieldState | string }) {
  const known = state in STATE_LOOK
  const look = known ? STATE_LOOK[state as FieldState] : { tone: "neutral" as Tone, short: String(state), long: "" }

  return (
    <Badge tone={look.tone}>
      <span aria-hidden="true" className="inline-flex">
        {known ? STATE_ICON[state as FieldState] : <CircleDashed />}
      </span>
      {look.short}
    </Badge>
  )
}

/** A correction's status. A legacy correction predates approvals and is never applied. */
export const CORRECTION_LOOK: Record<CorrectionStatus, { tone: Tone; text: string; icon: JSX.Element }> = {
  pending: { tone: "warn", text: "Awaiting approval", icon: <Clock /> },
  approved: { tone: "ok", text: "Approved", icon: <ShieldCheck /> },
  rejected: { tone: "bad", text: "Rejected", icon: <XCircle /> },
  legacy: { tone: "neutral", text: "Legacy (never applied)", icon: <CircleSlash /> },
}

export function CorrectionBadge({ status }: { status: CorrectionStatus }) {
  const look = CORRECTION_LOOK[status]

  return (
    <Badge tone={look.tone}>
      <span aria-hidden="true" className="inline-flex">
        {look.icon}
      </span>
      {look.text}
    </Badge>
  )
}

const VALIDATION_LOOK: Record<string, { tone: Tone; text: string }> = {
  valid: { tone: "ok", text: "Valid" },
  warning: { tone: "warn", text: "Warning" },
  invalid: { tone: "bad", text: "Invalid" },
  not_checked: { tone: "neutral", text: "Not checked" },
}

export function ValidationBadge({ validation }: { validation: string | null | undefined }) {
  const look = VALIDATION_LOOK[validation ?? "not_checked"] ?? VALIDATION_LOOK.not_checked

  return <Badge tone={look.tone}>{look.text}</Badge>
}

// ---------------------------------------------------------------------------
// What a field holds
// ---------------------------------------------------------------------------

export type Kind = "date" | "count" | "money" | "weight" | "currency" | "party" | "text"

const KINDS: Record<string, Kind> = {
  pickup_date: "date", expected_delivery_date: "date", delivery_date: "date", invoice_date: "date", due_date: "date",
  piece_count: "count", delivered_pieces: "count",
  subtotal: "money", tax: "money", invoice_total: "money", agreed_rate: "money", fuel_surcharge: "money",
  weight: "weight", currency: "currency", shipper: "party", consignee: "party",
}

export const kindOf = (field: string): Kind => KINDS[field] ?? "text"

/** Guidance only: the server parses and validates what is sent. */
export const HINTS: Record<Kind, string> = {
  date: "A date such as October 3, 2026 or 2026-10-03.",
  count: "A whole number, such as 48.",
  money: "An amount such as 1,430.00. The currency is not part of the value.",
  weight: "An amount and unit, such as 12,500 lbs.",
  currency: "An ISO code such as USD or CAD.",
  party: "The name on the first line, then address lines.",
  text: "Text as it should read.",
}

/**
 * The calculation that reads a field once a correction of it is approved. This
 * mirrors audit_inputs.TARGETS on the server, for explanation only: the server
 * decides what an audit reads.
 */
const CONSUMERS: Record<string, string> = {
  "BOL.piece_count": "the quantity audit",
  "POD.delivered_pieces": "the quantity audit",
  "BOL.expected_delivery_date": "the delivery audit",
  "POD.delivery_date": "the delivery audit",
  "INVOICE.invoice_total": "the invoice audit and the financial exposure",
}

export const consumerOf = (documentType: string, field: string): string | null => CONSUMERS[`${documentType}.${field}`] ?? null

const number = (value: number, digits?: number) =>
  value.toLocaleString("en-US", digits === undefined ? { maximumFractionDigits: 6 } : { minimumFractionDigits: digits, maximumFractionDigits: digits })

/** A typed value as text. Money has no currency symbol: the document may not say which currency. */
export function valueText(field: string, value: unknown): string {
  if (value === null || value === undefined) return NA

  if (typeof value === "number") return number(value, kindOf(field) === "money" ? 2 : undefined)
  if (typeof value === "string") return value

  if (typeof value === "object") {
    const item = value as Record<string, unknown>

    if (typeof item.name === "string") {
      const lines = Array.isArray(item.address_lines) ? item.address_lines.filter((line): line is string => typeof line === "string") : []
      return lines.length ? `${item.name} — ${lines.join(", ")}` : item.name
    }

    if (typeof item.amount === "number") return `${number(item.amount)} ${typeof item.unit === "string" ? item.unit : "(no unit written)"}`
  }

  return JSON.stringify(value)
}

/** The value if the field has one, otherwise the state in words: never a blank or a zero. */
export function valueOrState(field: string, value: unknown, state: FieldState | string): string {
  if (state === "EXTRACTED") return valueText(field, value)

  const look = STATE_LOOK[state as FieldState]
  return look ? `${look.short}: ${look.long}` : NA
}

export function pageText(page: number | null | undefined): string {
  return typeof page === "number" && page > 0 ? `page ${page}` : "page not recorded"
}
