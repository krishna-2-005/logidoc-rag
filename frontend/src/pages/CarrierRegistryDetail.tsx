import { ArrowLeft, FileCheck2, Link2, Pencil, Receipt, Scale, ShieldAlert, Truck } from "lucide-react"
import { useState } from "react"
import { Link, useParams } from "react-router-dom"
import { CarrierForm } from "@/components/carriers/CarrierForm"
import { DecisionButtons, ProposeLinkDialog } from "@/components/carriers/LinkDialogs"
import { Facts } from "@/components/common/Facts"
import { KpiCard } from "@/components/common/KpiCard"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { Pagination } from "@/components/common/Pagination"
import { StatusBadge } from "@/components/common/StatusBadge"
import { EmptyState, ErrorState, KpiSkeletons, LoadingBlock } from "@/components/common/states"
import { LinkBadge, RegistrationBadge } from "@/components/integrations/badges"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { can, useCarrierDetail, useSession } from "@/hooks/queries"
import { useCarrierHistory, useRegistryCarrier } from "@/hooks/registry"
import { ApiError } from "@/lib/api"
import { count, dateTime, money, NA, rate } from "@/lib/format"
import type { CarrierDetail, LinkRequest, NameLink, RegisteredCarrierRecord, RegistryEvent } from "@/lib/types"

const ACTIONS: Record<string, string> = {
  create: "Registered",
  update: "Changed",
  propose_link: "Proposed a name link",
  propose_unlink: "Proposed unlinking a name",
  approved_link: "Approved a name link",
  rejected_link: "Rejected a name link",
}

const FIELD_LABELS: Record<string, string> = {
  legal_name: "legal name", trading_name: "trading name", scac: "SCAC", mc_number: "MC number", dot_number: "USDOT number",
  contact_name: "contact person", contact_email: "email", contact_phone: "phone", status: "status", address_line1: "address line 1",
  address_line2: "address line 2", city: "city", region: "state or province", postal_code: "postal code", country: "country",
}

function show(value: unknown): string {
  return value === null || value === undefined || value === "" ? "empty" : String(value)
}

/** What one history event changed, in words */
function describe(event: RegistryEvent): string {
  const before = event.before ?? {}
  const after = event.after ?? {}

  if (event.action === "update") {
    return Object.keys(after)
      .map((field) => `${FIELD_LABELS[field] ?? field}: ${show(before[field])} → ${show(after[field])}`)
      .join("; ")
  }

  if (event.action.startsWith("propose")) return `“${show(after.name)}”. Reason: ${show(after.reason)}`
  if (event.action.endsWith("_link")) return `“${show(after.name)}”${after.note ? `. Note: ${show(after.note)}` : ""}`
  if (event.action === "create") return "Master data recorded"

  return ""
}

function address(carrier: RegisteredCarrierRecord): string | null {
  const lines = [carrier.address_line1, carrier.address_line2, [carrier.city, carrier.region, carrier.postal_code].filter(Boolean).join(", "), carrier.country].filter(Boolean)
  return lines.length ? lines.join(" · ") : null
}

function MasterData({ carrier }: { carrier: RegisteredCarrierRecord }) {
  return (
    <Card>
      <CardContent className="pt-6">
        <Facts
          label="Carrier master data"
          columns={3}
          items={[
            ["Legal name", carrier.legal_name],
            ["Trading name", carrier.trading_name],
            ["Registration status", <RegistrationBadge key="s" status={carrier.status} />],
            ["SCAC", carrier.scac && <span className="font-mono">{carrier.scac}</span>],
            ["MC number", carrier.mc_number && <span className="font-mono">MC-{carrier.mc_number}</span>],
            ["USDOT number", carrier.dot_number && <span className="font-mono">{carrier.dot_number}</span>],
            ["Contact person", carrier.contact_name],
            ["Email", carrier.contact_email],
            ["Phone", carrier.contact_phone],
            ["Address", address(carrier)],
            ["Registered", `${dateTime(carrier.created_at)} by ${carrier.created_by}`],
            ["Last updated", dateTime(carrier.updated_at)],
          ]}
        />
        <p className="mt-4 text-xs text-muted-foreground">Identifiers are checked for their format, not against FMCSA or NMFTA records.</p>
      </CardContent>
    </Card>
  )
}

function Links({ links, manage, onUnlink }: { links: NameLink[]; manage: boolean; onUnlink: (link: NameLink) => void }) {
  if (links.length === 0) {
    return <EmptyState title="No document name is linked yet">Until a carrier name printed on the documents is linked (and approved by a second administrator), this carrier has no shipments, audit or exception figures.</EmptyState>
  }

  return (
    <Table aria-label="Linked document names">
      <TableHeader>
        <TableRow>
          <TableHead>Name on the documents</TableHead>
          <TableHead>Proposed by</TableHead>
          <TableHead>Approved by</TableHead>
          <TableHead>Linked</TableHead>
          {manage && <TableHead className="text-right">Action</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {links.map((link) => (
          <TableRow key={link.name_key}>
            <TableCell className="font-medium">{link.display_name}</TableCell>
            <TableCell>{link.proposed_by ?? NA}</TableCell>
            <TableCell>{link.decided_by ?? NA}</TableCell>
            <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{dateTime(link.linked_at)}</TableCell>
            {manage && (
              <TableCell className="text-right">
                <Button variant="outline" size="sm" onClick={() => onUnlink(link)}>
                  Propose a change
                </Button>
              </TableCell>
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function Requests({ requests, me, manage, decided }: { requests: LinkRequest[]; me?: string; manage: boolean; decided?: boolean }) {
  return (
    <Table aria-label={decided ? "Recent decisions" : "Requests awaiting approval"}>
      <TableHeader>
        <TableRow>
          <TableHead>Name on the documents</TableHead>
          <TableHead>Request</TableHead>
          <TableHead>Reason</TableHead>
          <TableHead>Proposed</TableHead>
          <TableHead>{decided ? "Decision" : "Decide"}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {requests.map((request) => (
          <TableRow key={request.id}>
            <TableCell className="font-medium">{request.display_name}</TableCell>
            <TableCell className="text-sm">{request.carrier_id === null ? "Unlink" : `Link to ${request.carrier_legal_name ?? `carrier ${request.carrier_id}`}`}</TableCell>
            <TableCell className="max-w-72 text-sm">{request.reason}</TableCell>
            <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
              {request.proposed_by}
              <br />
              {dateTime(request.proposed_at)}
            </TableCell>
            <TableCell>
              {decided ? (
                <div className="space-y-1 text-xs">
                  <LinkBadge state={request.decision ?? "pending"} />
                  <p className="text-muted-foreground">
                    {request.decided_by}, {dateTime(request.decided_at)}
                  </p>
                  {request.decision_note && <p>{request.decision_note}</p>}
                </div>
              ) : (
                <DecisionButtons request={request} me={me} canDecide={manage} />
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function Performance({ detail }: { detail: CarrierDetail }) {
  const audit = detail.audit
  const exceptions = detail.exceptions

  return (
    <div className="space-y-6">
      <section aria-label="Carrier figures" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <KpiCard title="Shipments" value={count(detail.shipments)} icon={<Truck />} detail={`${count(detail.delivery.completed)} delivered`} />
        <KpiCard title="On-time deliveries" value={rate(detail.delivery.on_time_rate)} icon={<FileCheck2 />} definition="On time or early / deliveries measured." />
        <KpiCard title="Invoice accuracy" value={rate(audit.invoice_accuracy_rate)} icon={<Receipt />} definition="Invoices whose line items add up to their total / invoices whose arithmetic could be checked." />
        <KpiCard title="Audit pass rate" value={rate(audit.audit_pass_rate)} icon={<Scale />} definition="Invoices matched against the rate confirmation / invoices compared with one." detail={`${audit.discrepant} with a discrepancy`} />
        <KpiCard title="Open exceptions" value={count(exceptions.open)} icon={<ShieldAlert />} tone={exceptions.open ? "warn" : "default"} detail={`${count(exceptions.total)} detected in all`} />
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardContent className="pt-6">
            <h3 className="mb-3 text-sm font-semibold">Freight audit</h3>
            <Facts
              columns={2}
              label="Freight audit statistics"
              items={[
                ["Invoices", count(audit.invoices)],
                ["Audited against a contract", count(audit.audited_against_contract)],
                ["Matched", count(audit.matched)],
                ["Discrepancies", count(audit.discrepant)],
                ["Pending review", count(audit.pending_review)],
                ["Insufficient evidence", count(audit.insufficient_evidence)],
              ]}
            />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <h3 className="mb-3 text-sm font-semibold">Exceptions</h3>
            <Facts
              columns={2}
              label="Exception statistics"
              items={[
                ["Detected", count(exceptions.total)],
                ["Open", count(exceptions.open)],
                ...Object.entries(exceptions.by_status)
                  .filter(([status]) => status !== "open")
                  .map(([status, number]) => [status.replace(/_/g, " ").replace(/^\w/, (letter) => letter.toUpperCase()), count(number)] as [string, string]),
                ["Per shipment", exceptions.exceptions_per_shipment === null ? NA : exceptions.exceptions_per_shipment.toFixed(2)],
              ]}
            />
            {exceptions.repeated.length > 0 && <p className="mt-3 text-xs text-warn">Repeated: {exceptions.repeated.map((item) => `${item.category} (${item.shipments.length} shipments)`).join(", ")}</p>}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function Shipments({ detail, onMore }: { detail: CarrierDetail; onMore: () => void }) {
  return (
    <>
      <Table aria-label="Shipments of the carrier">
        <TableHeader>
          <TableRow>
            <TableHead>Shipment</TableHead>
            <TableHead>Date</TableHead>
            <TableHead>Delivery</TableHead>
            <TableHead className="text-right">Invoice</TableHead>
            <TableHead>Against contract</TableHead>
            <TableHead className="text-right">Exceptions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {detail.shipment_rows.map((row) => (
            <TableRow key={row.shipment_key}>
              <TableCell>
                <Link className="text-primary hover:underline" to={`/shipments/${encodeURIComponent(row.shipment_id)}`}>
                  {row.shipment_id}
                </Link>
              </TableCell>
              <TableCell>{row.date ?? NA}</TableCell>
              <TableCell>{row.measured && row.delivery_status ? <StatusBadge status={row.delivery_status} /> : <span className="text-xs text-muted-foreground">{row.not_measured_reason}</span>}</TableCell>
              <TableCell className="tabular text-right">{money(row.invoice_total, row.currency)}</TableCell>
              <TableCell>{row.freight_outcome ? <StatusBadge status={row.freight_outcome} /> : <span className="text-xs text-muted-foreground">no rate confirmation</span>}</TableCell>
              <TableCell className="tabular text-right">{row.exceptions.length}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {detail.shipment_rows.length < detail.shipment_count && (
        <Button variant="outline" size="sm" className="mt-3" onClick={onMore}>
          Show more shipments ({detail.shipment_rows.length} of {detail.shipment_count})
        </Button>
      )}
    </>
  )
}

function History({ id, enabled }: { id: number; enabled: boolean }) {
  const [offset, setOffset] = useState(0)
  const history = useCarrierHistory(id, offset, enabled)

  if (!enabled) return <p className="text-sm text-muted-foreground">The carrier history is shown to administrators.</p>
  if (history.isPending) return <LoadingBlock rows={3} label="Loading the history" />
  if (history.isError) return <ErrorState error={history.error} onRetry={() => history.refetch()} title="The history could not be loaded" />

  const page = history.data

  return (
    <>
      <ol aria-label="Carrier history" className="space-y-0 rounded-lg border bg-card shadow-card">
        {page.events.map((event) => (
          <li key={event.id} className="flex flex-col gap-1 border-b px-4 py-3 text-sm last:border-0 sm:flex-row sm:items-baseline sm:gap-4">
            <span className="w-40 shrink-0 text-xs text-muted-foreground">{dateTime(event.at)}</span>
            <span className="w-28 shrink-0 font-medium">{event.actor}</span>
            <span className="shrink-0">{ACTIONS[event.action] ?? event.action}</span>
            <span className="min-w-0 text-muted-foreground">{describe(event)}</span>
          </li>
        ))}
      </ol>
      <Pagination offset={page.offset} limit={page.limit} total={page.total} onChange={setOffset} label="History pages" />
    </>
  )
}

/**
 * One registered carrier: master data, the document names linked to it, the
 * requests waiting for a second administrator, its delivery, audit and
 * exception figures (Carrier Intelligence, once a name is linked) and its
 * history. Nothing here changes a recorded audit.
 */
export function CarrierRegistryDetail() {
  const { id } = useParams()
  const carrierId = Number(id)
  const valid = Number.isInteger(carrierId) && carrierId > 0
  const session = useSession()
  const manage = can(session.data, "carriers.manage")
  const me = session.data?.user?.username
  const record = useRegistryCarrier(valid ? carrierId : null)
  const [limit, setLimit] = useState(25)
  const figures = useCarrierDetail(record.data?.intelligence_key ?? null, { limit })
  const [editing, setEditing] = useState(false)
  const [linking, setLinking] = useState<{ name: string | null; current?: NameLink } | null>(null)

  if (!valid || (record.error instanceof ApiError && record.error.status === 404)) {
    return (
      <EmptyState title="No such carrier" action={<Button variant="outline" size="sm" asChild><Link to="/carrier-registry">Back to the registry</Link></Button>}>
        The carrier may have been registered under another number.
      </EmptyState>
    )
  }

  if (record.isPending) return <LoadingBlock rows={6} label="Loading the carrier" />
  if (record.isError) return <ErrorState error={record.error} onRetry={() => record.refetch()} title="This carrier could not be loaded" />

  const { carrier, name_links: links, pending_requests: pending, recent_decisions: decided, intelligence_key: key } = record.data

  return (
    <>
      <Link to="/carrier-registry" className="mb-3 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Carrier Registry
      </Link>
      <PageHeader
        title={carrier.legal_name}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <RegistrationBadge status={carrier.status} />
            {carrier.trading_name && <span>Trading as {carrier.trading_name}</span>}
            {carrier.scac && <span className="font-mono">SCAC {carrier.scac}</span>}
          </span>
        }
        actions={
          manage ? (
            <>
              <Button variant="outline" size="sm" onClick={() => setLinking({ name: null })}>
                <Link2 /> Propose a name link
              </Button>
              <Button size="sm" onClick={() => setEditing(true)}>
                <Pencil /> Edit
              </Button>
            </>
          ) : undefined
        }
      />

      <MasterData carrier={carrier} />

      <Section title="Document names" description="Carrier names printed on the documents that count as this carrier, each approved by a second administrator.">
        <Links links={links} manage={manage} onUnlink={(link) => setLinking({ name: link.display_name, current: link })} />
      </Section>

      {pending.length > 0 && (
        <Section title="Awaiting approval" description="Requests concerning this carrier. The administrator who proposed one cannot decide it.">
          <Requests requests={pending} me={me} manage={manage} />
        </Section>
      )}

      <Section title="Shipments and performance" description="Carrier Intelligence figures for the shipments whose documents print a linked name, as the documents are now.">
        {!key && <EmptyState title="No figures yet">Link a document name to this carrier to see its shipments, deliveries, audit results and exceptions.</EmptyState>}
        {key && figures.isPending && <KpiSkeletons count={5} />}
        {key && figures.isError && (figures.error instanceof ApiError && figures.error.status === 404 ? <EmptyState title="No shipment yet">No current document prints a name linked to this carrier.</EmptyState> : <ErrorState error={figures.error} onRetry={() => figures.refetch()} title="The figures could not be loaded" />)}
        {key && figures.data && (
          <div className="space-y-6">
            <Performance detail={figures.data} />
            <Shipments detail={figures.data} onMore={() => setLimit((value) => value + 25)} />
            <Link to={`/carriers?carrier=${encodeURIComponent(key)}`} className="inline-block text-sm text-primary hover:underline">
              Open in Carrier Intelligence
            </Link>
          </div>
        )}
      </Section>

      {decided.length > 0 && (
        <Section title="Recent link decisions">
          <Requests requests={decided} me={me} manage={manage} decided />
        </Section>
      )}

      <Section title="History" description="Every change to this carrier and every link decision about it. The history cannot be edited.">
        <History id={carrier.id} enabled={record.data.history !== null} />
      </Section>

      <CarrierForm open={editing} onOpenChange={setEditing} carrier={carrier} />
      <ProposeLinkDialog
        open={linking !== null}
        onOpenChange={(open) => !open && setLinking(null)}
        name={linking?.name ?? null}
        current={linking?.current ? { id: carrier.id, legal_name: carrier.legal_name } : null}
        fixedCarrier={linking?.current ? undefined : { id: carrier.id, legal_name: carrier.legal_name }}
      />
    </>
  )
}
