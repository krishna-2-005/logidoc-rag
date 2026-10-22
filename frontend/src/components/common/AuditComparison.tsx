import { ArrowRight, CalendarClock, Package, Receipt } from "lucide-react"
import type { ReactNode } from "react"
import { StatusBadge } from "@/components/common/StatusBadge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { days, money, NA, pieces } from "@/lib/format"
import type { DeliveryAudit, Evidence, InvoiceAudit, QuantityAudit } from "@/lib/types"

function Row({ label, children, strong }: { label: string; children: ReactNode; strong?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b py-2 text-sm last:border-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={`tabular text-right ${strong ? "font-semibold" : ""}`}>{children}</dd>
    </div>
  )
}

function Reason({ text }: { text: string }) {
  return text ? <p className="mt-3 rounded-md bg-warn-soft px-3 py-2 text-xs text-warn">{text}</p> : null
}

/** Evidence the audit used, as the documents wrote it. */
export function EvidenceList({ items }: { items: Evidence[] }) {
  if (!items?.length) return <p className="mt-3 text-xs text-muted-foreground">No source text is attached to this check.</p>

  return (
    <details className="mt-3 text-xs">
      <summary className="cursor-pointer font-medium text-primary">Evidence ({items.length})</summary>
      <ul className="mt-2 space-y-1.5">
        {items.map((item, index) => (
          <li key={index} className="rounded-md bg-muted/70 px-2.5 py-1.5">
            <span className="font-medium">
              {item.source ?? NA}
              {item.page ? ` · p.${item.page}` : ""}
              {item.document_type ? ` · ${item.document_type}` : ""}
            </span>
            <span className="block text-muted-foreground">{String(item.text ?? "")}</span>
          </li>
        ))}
      </ul>
    </details>
  )
}

export function QuantityCard({ audit }: { audit: QuantityAudit }) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Package className="size-4 text-muted-foreground" aria-hidden="true" /> Quantity
        </CardTitle>
        <StatusBadge status={audit.status} />
      </CardHeader>
      <CardContent>
        <dl>
          <Row label="Expected on the BOL">{pieces(audit.bol)}</Row>
          <Row label="Delivered on the POD">{pieces(audit.pod)}</Row>
          <Row label="Difference (BOL − POD)" strong>
            {audit.difference === null ? NA : audit.difference > 0 ? `${pieces(audit.difference)} short` : audit.difference < 0 ? `${pieces(Math.abs(audit.difference))} over` : "None"}
          </Row>
        </dl>
        <Reason text={audit.status === "NEEDS REVIEW" ? audit.reason : ""} />
        <EvidenceList items={audit.evidence} />
      </CardContent>
    </Card>
  )
}

export function DeliveryCard({ audit }: { audit: DeliveryAudit }) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <CalendarClock className="size-4 text-muted-foreground" aria-hidden="true" /> Delivery
        </CardTitle>
        <StatusBadge status={audit.status} />
      </CardHeader>
      <CardContent>
        <dl>
          <Row label="Expected (BOL)">{audit.expected ?? NA}</Row>
          <Row label="Actual (POD)">{audit.actual ?? NA}</Row>
          <Row label="Timing" strong>
            {days(audit.days_late)}
          </Row>
        </dl>
        <Reason text={audit.status === "NEEDS REVIEW" ? audit.reason : ""} />
        <EvidenceList items={audit.evidence} />
      </CardContent>
    </Card>
  )
}

export function InvoiceCard({ audit }: { audit: InvoiceAudit }) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Receipt className="size-4 text-muted-foreground" aria-hidden="true" /> Invoice
        </CardTitle>
        <StatusBadge status={audit.status} />
      </CardHeader>
      <CardContent>
        <dl>
          {audit.line_items.map(([name, amount]) => (
            <Row key={name} label={name}>
              {money(amount)}
            </Row>
          ))}
          <Row label="Sum of line items">{money(audit.line_item_sum)}</Row>
          <Row label="Stated total">{money(audit.total)}</Row>
          <Row label="Difference (total − line items)" strong>
            {audit.difference === null ? NA : money(audit.difference)}
          </Row>
        </dl>
        {audit.status === "MISMATCH" && (
          <p className="mt-3 flex gap-2 rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground">
            <ArrowRight className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            This is an arithmetic finding about the invoice itself. It does not show overbilling or an amount that is payable or recoverable.
          </p>
        )}
        <Reason text={audit.status === "NEEDS REVIEW" ? audit.reason : ""} />
        <EvidenceList items={audit.evidence} />
      </CardContent>
    </Card>
  )
}
