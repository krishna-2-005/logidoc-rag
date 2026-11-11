import { AlertTriangle, Ban, CheckCircle2, CircleDashed, Clock, KeyRound, Loader2, PauseCircle, PlugZap, RotateCw, XCircle } from "lucide-react"
import type { ReactNode } from "react"
import { Badge, type Tone } from "@/components/ui/badge"

/**
 * Status badges of the registry and integration screens: always text with an
 * icon, never colour alone. Kept apart from StatusBadge because the same words
 * ("pending") mean different things there.
 */
type Look = [Tone, ReactNode, string]

function Pill({ look }: { look: Look }) {
  const [tone, icon, text] = look
  return (
    <Badge tone={tone}>
      <span aria-hidden="true" className="inline-flex">
        {icon}
      </span>
      {text}
    </Badge>
  )
}

const REGISTRATION: Record<string, Look> = {
  active: ["ok", <CheckCircle2 key="i" />, "Active"],
  on_hold: ["warn", <PauseCircle key="i" />, "On hold"],
  inactive: ["neutral", <Ban key="i" />, "Inactive"],
}

export const RegistrationBadge = ({ status }: { status: string }) => <Pill look={REGISTRATION[status] ?? ["neutral", <CircleDashed key="i" />, status]} />

const LINK: Record<string, Look> = {
  linked: ["ok", <CheckCircle2 key="i" />, "Linked"],
  pending: ["warn", <Clock key="i" />, "Awaiting second approval"],
  unlinked: ["neutral", <CircleDashed key="i" />, "Not linked"],
  approved: ["ok", <CheckCircle2 key="i" />, "Approved"],
  rejected: ["bad", <XCircle key="i" />, "Rejected"],
}

export const LinkBadge = ({ state }: { state: string }) => <Pill look={LINK[state] ?? ["neutral", <CircleDashed key="i" />, state]} />

const DELIVERY: Record<string, Look> = {
  pending: ["info", <Clock key="i" />, "Queued"],
  sending: ["info", <Loader2 key="i" className="animate-spin" />, "Sending"],
  retrying: ["warn", <RotateCw key="i" />, "Retrying"],
  succeeded: ["ok", <CheckCircle2 key="i" />, "Delivered"],
  failed: ["bad", <XCircle key="i" />, "Failed"],
  cancelled: ["neutral", <Ban key="i" />, "Cancelled"],
}

export const DeliveryBadge = ({ status }: { status: string }) => <Pill look={DELIVERY[status] ?? ["neutral", <CircleDashed key="i" />, status]} />

const RUN: Record<string, Look> = {
  running: ["info", <Loader2 key="i" className="animate-spin" />, "Running"],
  succeeded: ["ok", <CheckCircle2 key="i" />, "Succeeded"],
  partial: ["warn", <AlertTriangle key="i" />, "Partial"],
  failed: ["bad", <XCircle key="i" />, "Failed"],
  interrupted: ["warn", <PauseCircle key="i" />, "Interrupted"],
}

export const RunBadge = ({ status }: { status: string }) => <Pill look={RUN[status] ?? ["neutral", <CircleDashed key="i" />, status]} />

const CONNECTION: Record<string, Look> = {
  untested: ["neutral", <CircleDashed key="i" />, "Not tested"],
  connected: ["ok", <PlugZap key="i" />, "Connected"],
  auth_failed: ["bad", <KeyRound key="i" />, "Credential refused"],
  error: ["bad", <XCircle key="i" />, "Error"],
}

export const ConnectionBadge = ({ status }: { status: string }) => <Pill look={CONNECTION[status] ?? ["neutral", <CircleDashed key="i" />, status]} />

export const EnabledBadge = ({ enabled, archived }: { enabled: boolean; archived?: boolean }) =>
  archived ? <Pill look={["neutral", <Ban key="i" />, "Archived"]} /> : enabled ? <Pill look={["ok", <CheckCircle2 key="i" />, "Enabled"]} /> : <Pill look={["neutral", <PauseCircle key="i" />, "Disabled"]} />

/** "every 15 s", "every 5 min", "every 2 h" */
export function every(seconds: number): string {
  if (seconds % 3600 === 0) return `every ${seconds / 3600} h`
  if (seconds % 60 === 0) return `every ${seconds / 60} min`
  return `every ${seconds} s`
}

/** A success rate with its counts: "96% (48 of 50)"; no completed delivery: "No completed delivery" */
export function successRate(stats: { success_rate: number | null; succeeded: number; failed: number }): string {
  if (stats.success_rate === null) return "No completed delivery"
  return `${Math.round(stats.success_rate * 100)}% (${stats.succeeded} of ${stats.succeeded + stats.failed})`
}
