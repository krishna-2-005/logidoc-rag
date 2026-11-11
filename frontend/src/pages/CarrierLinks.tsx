import { ArrowLeft, FileText, Search } from "lucide-react"
import { useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { DecisionButtons, ProposeLinkDialog } from "@/components/carriers/LinkDialogs"
import { PageHeader } from "@/components/common/PageHeader"
import { Pagination } from "@/components/common/Pagination"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { LinkBadge } from "@/components/integrations/badges"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input, Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs } from "@/components/ui/tabs"
import { can, useSession } from "@/hooks/queries"
import { useDocumentNames, useLinkQueue } from "@/hooks/registry"
import { useDebounced } from "@/hooks/useDebounced"
import { dateTime } from "@/lib/format"
import type { DocumentName, LinkRequest } from "@/lib/types"

function RequestTable({ requests, me, manage, decided }: { requests: LinkRequest[]; me?: string; manage: boolean; decided?: boolean }) {
  return (
    <Table aria-label={decided ? "Decided link requests" : "Link requests awaiting approval"}>
      <TableHeader>
        <TableRow>
          <TableHead>Name on the documents</TableHead>
          <TableHead>Proposed change</TableHead>
          <TableHead>Linked now to</TableHead>
          <TableHead>Reason</TableHead>
          <TableHead>Proposed</TableHead>
          <TableHead>{decided ? "Decision" : "Decide"}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {requests.map((request) => (
          <TableRow key={request.id}>
            <TableCell className="font-medium">{request.display_name}</TableCell>
            <TableCell className="text-sm">
              {request.carrier_id === null ? (
                "Unlink the name"
              ) : (
                <Link to={`/carrier-registry/${request.carrier_id}`} className="text-primary hover:underline">
                  {request.carrier_legal_name ?? `Carrier ${request.carrier_id}`}
                </Link>
              )}
            </TableCell>
            <TableCell className="text-sm">{request.current_carrier_legal_name ?? <span className="text-muted-foreground">Not linked</span>}</TableCell>
            <TableCell className="max-w-80 text-sm">{request.reason}</TableCell>
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
                  {request.decision_note && <p className="max-w-60">{request.decision_note}</p>}
                </div>
              ) : manage ? (
                <DecisionButtons request={request} me={me} canDecide={manage} />
              ) : (
                <span className="text-xs text-muted-foreground">An administrator decides</span>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function Queue({ state, me, manage }: { state: "pending" | "decided"; me?: string; manage: boolean }) {
  const [offset, setOffset] = useState(0)
  const queue = useLinkQueue(state, offset)

  if (queue.isPending) return <LoadingBlock rows={4} label="Loading the requests" />
  if (queue.isError) return <ErrorState error={queue.error} onRetry={() => queue.refetch()} title="The requests could not be loaded" />

  if (queue.data.total === 0) {
    return state === "pending" ? (
      <EmptyState title="Nothing awaits approval">Proposals appear here until a second administrator approves or rejects them.</EmptyState>
    ) : (
      <EmptyState title="No decision yet">Approved and rejected requests are listed here, with who decided and why.</EmptyState>
    )
  }

  return (
    <>
      <RequestTable requests={queue.data.requests} me={me} manage={manage} decided={state === "decided"} />
      <Pagination offset={queue.data.offset} limit={queue.data.limit} total={queue.data.total} onChange={setOffset} label="Request pages" />
    </>
  )
}

function Context({ name }: { name: DocumentName }) {
  return (
    <details className="text-xs">
      <summary className="cursor-pointer text-primary">
        {name.shipments} shipment{name.shipments === 1 ? "" : "s"}, {name.documents.length} document{name.documents.length === 1 ? "" : "s"} shown
      </summary>
      <ul className="mt-2 space-y-1" aria-label={`Documents printing ${name.name}`}>
        {name.documents.map((document, index) => (
          <li key={`${document.shipment_id}-${document.file ?? index}`} className="flex items-center gap-1.5">
            <FileText className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <Link to={`/shipments/${encodeURIComponent(document.shipment_id)}`} className="text-primary hover:underline">
              {document.shipment_id}
            </Link>
            <span className="text-muted-foreground">
              {document.document_type ?? "Document"}
              {document.file ? ` · ${document.file}` : ""}
            </span>
          </li>
        ))}
      </ul>
      {name.spellings.length > 1 && <p className="mt-2 text-muted-foreground">Written as: {name.spellings.join(" / ")}</p>}
    </details>
  )
}

function Names({ manage }: { manage: boolean }) {
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get("name") ?? "")
  const query = useDebounced(search.trim(), 300)
  const state = (params.get("state") ?? "unlinked") as "all" | "unlinked" | "pending" | "linked"
  const [offset, setOffset] = useState(0)
  const names = useDocumentNames({ search: query, state, offset, limit: 25 })
  const [proposing, setProposing] = useState<DocumentName | null>(null)

  const choose = (value: string) => {
    const next = new URLSearchParams(params)
    next.set("state", value)
    setParams(next, { replace: true })
    setOffset(0)
  }

  return (
    <>
      <form role="search" aria-label="Find document names" className="mb-4 grid gap-3 rounded-lg border bg-card p-4 shadow-card sm:grid-cols-[2fr_1fr]" onSubmit={(event) => event.preventDefault()}>
        <div className="space-y-1.5">
          <Label htmlFor="names-search">Name on the documents</Label>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <Input id="names-search" className="pl-9" value={search} maxLength={100} onChange={(event) => { setSearch(event.target.value); setOffset(0) }} placeholder="Part of a carrier name" />
          </div>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="names-state">Link state</Label>
          <Select id="names-state" value={state} onChange={(event) => choose(event.target.value)}>
            <option value="unlinked">Not linked{names.data ? ` (${names.data.counts.unlinked})` : ""}</option>
            <option value="pending">Awaiting approval{names.data ? ` (${names.data.counts.pending})` : ""}</option>
            <option value="linked">Linked{names.data ? ` (${names.data.counts.linked})` : ""}</option>
            <option value="all">All</option>
          </Select>
        </div>
      </form>

      {names.isPending && <LoadingBlock rows={4} label="Loading the document names" />}
      {names.isError && <ErrorState error={names.error} onRetry={() => names.refetch()} title="The document names could not be loaded" />}
      {names.data && names.data.total === 0 && (
        <EmptyState title="No name here">{query ? `No carrier name on the documents contains “${query}”.` : "No carrier name on the current documents is in this state."}</EmptyState>
      )}

      {names.data && names.data.total > 0 && (
        <>
          <Table aria-label="Carrier names on the documents">
            <TableHeader>
              <TableRow>
                <TableHead>Name on the documents</TableHead>
                <TableHead>Shipments and documents</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Registered carrier</TableHead>
                {manage && <TableHead className="text-right">Action</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {names.data.names.map((name) => (
                <TableRow key={name.name_key}>
                  <TableCell className="font-medium">{name.name}</TableCell>
                  <TableCell className="min-w-64">
                    <Context name={name} />
                  </TableCell>
                  <TableCell>
                    <LinkBadge state={name.state} />
                  </TableCell>
                  <TableCell className="text-sm">
                    {name.linked_carrier && (
                      <Link to={`/carrier-registry/${name.linked_carrier.id}`} className="text-primary hover:underline">
                        {name.linked_carrier.legal_name}
                      </Link>
                    )}
                    {name.pending_request && (
                      <p className="text-xs text-muted-foreground">
                        Proposed by {name.pending_request.proposed_by}: {name.pending_request.carrier_legal_name ?? "unlink"}
                      </p>
                    )}
                    {!name.linked_carrier && !name.pending_request && name.suggestions.length > 0 && (
                      <p className="text-xs text-muted-foreground">Same name registered: {name.suggestions.map((item) => item.legal_name).join(", ")} (not linked)</p>
                    )}
                    {!name.linked_carrier && !name.pending_request && name.suggestions.length === 0 && <span className="text-muted-foreground">None</span>}
                  </TableCell>
                  {manage && (
                    <TableCell className="text-right">
                      {name.state !== "pending" && (
                        <Button size="sm" variant="outline" onClick={() => setProposing(name)}>
                          {name.linked_carrier ? "Propose a change" : "Propose a link"}
                        </Button>
                      )}
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Pagination offset={names.data.offset} limit={names.data.limit} total={names.data.total} onChange={setOffset} label="Name pages" />
        </>
      )}

      <ProposeLinkDialog
        open={proposing !== null}
        onOpenChange={(open) => !open && setProposing(null)}
        name={proposing?.name ?? null}
        current={proposing?.linked_carrier ?? null}
        suggestions={proposing?.suggestions ?? []}
      />
    </>
  )
}

/**
 * Two-person approval of name links. A carrier name printed on the documents
 * counts as a registered carrier only when one administrator proposes it with
 * a reason and a different administrator approves it; every decision is kept
 * in the carrier history. Similar names are never merged by themselves.
 */
export function CarrierLinks() {
  const session = useSession()
  const manage = can(session.data, "carriers.manage")
  const me = session.data?.user?.username
  const [params, setParams] = useSearchParams()
  const tab = params.get("tab") ?? "pending"
  const pending = useLinkQueue("pending")

  const choose = (id: string) => {
    const next = new URLSearchParams()
    next.set("tab", id)
    setParams(next, { replace: true })
  }

  return (
    <>
      <Link to="/carrier-registry" className="mb-3 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Carrier Registry
      </Link>
      <PageHeader
        title="Name link approvals"
        description="Which carrier names printed on the documents belong to which registered carrier. One administrator proposes a link with a reason; a different administrator approves or rejects it. Nothing is merged because two names look alike."
      />

      {!manage && <Alert tone="info" className="mb-4">You can follow the requests here. Proposing and deciding links is for administrators.</Alert>}

      <Tabs
        label="Name links"
        value={tab}
        onChange={choose}
        items={[
          { id: "pending", label: "Awaiting approval", badge: pending.data && pending.data.total > 0 ? <Badge tone="warn">{pending.data.total}</Badge> : undefined },
          { id: "names", label: "Names on the documents" },
          { id: "decided", label: "Decided" },
        ]}
      >
        {tab === "pending" && <Queue state="pending" me={me} manage={manage} />}
        {tab === "names" && <Names manage={manage} />}
        {tab === "decided" && <Queue state="decided" me={me} manage={manage} />}
      </Tabs>
    </>
  )
}
