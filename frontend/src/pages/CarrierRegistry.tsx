import { ArrowDownAZ, ArrowUpZA, Link2, Plus, Search, Truck } from "lucide-react"
import { useState } from "react"
import { Link, useNavigate, useSearchParams } from "react-router-dom"
import { CarrierForm } from "@/components/carriers/CarrierForm"
import { PageHeader } from "@/components/common/PageHeader"
import { Pagination } from "@/components/common/Pagination"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { RegistrationBadge } from "@/components/integrations/badges"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input, Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { can, useSession } from "@/hooks/queries"
import { useCarrierPage, useLinkQueue } from "@/hooks/registry"
import { useDebounced } from "@/hooks/useDebounced"
import { dateTime } from "@/lib/format"
import type { RegisteredCarrierRecord } from "@/lib/types"

const PAGE = 25

const SORTS: [string, string][] = [
  ["legal_name", "Legal name"],
  ["trading_name", "Trading name"],
  ["scac", "SCAC"],
  ["mc_number", "MC number"],
  ["dot_number", "USDOT number"],
  ["status", "Registration status"],
  ["created_at", "Created"],
  ["updated_at", "Last updated"],
]

function Contact({ carrier }: { carrier: RegisteredCarrierRecord }) {
  const parts = [carrier.contact_name, carrier.contact_email, carrier.contact_phone].filter(Boolean)
  if (parts.length === 0) return <span className="text-muted-foreground">Not recorded</span>
  return (
    <div className="min-w-0 text-xs leading-relaxed">
      {carrier.contact_name && <p className="font-medium text-foreground">{carrier.contact_name}</p>}
      {carrier.contact_email && <p className="truncate text-muted-foreground">{carrier.contact_email}</p>}
      {carrier.contact_phone && <p className="text-muted-foreground">{carrier.contact_phone}</p>}
    </div>
  )
}

function Identifier({ value }: { value: string | null }) {
  return value ? <span className="tabular font-mono text-[13px]">{value}</span> : <span className="text-muted-foreground">—</span>
}

/**
 * The carrier master data: every registered carrier with its identifiers,
 * contact and registration status. Searching, filtering, sorting and paging are
 * done by the server. Administrators register and edit carriers here; the
 * name links that put document names under a carrier are decided on the
 * approvals screen by two administrators.
 */
export function CarrierRegistry() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const session = useSession()
  const manage = can(session.data, "carriers.manage")
  const [search, setSearch] = useState(params.get("search") ?? "")
  const [creating, setCreating] = useState(false)
  const query = useDebounced(search.trim(), 300)
  const status = params.get("status") ?? ""
  const sort = params.get("sort") ?? "legal_name"
  const order = (params.get("order") === "desc" ? "desc" : "asc") as "asc" | "desc"
  const offset = Number(params.get("offset") ?? 0) || 0

  const page = useCarrierPage({ search: query, status: status ? [status] : [], sort, order, offset, limit: PAGE })
  const pending = useLinkQueue("pending")

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    if (!("offset" in changes)) next.delete("offset")
    setParams(next, { replace: true })
  }

  const filtered = !!(query || status)
  const data = page.data
  const waiting = pending.data?.total ?? 0

  return (
    <>
      <PageHeader
        title="Carrier Registry"
        description="The carriers your company works with: legal and trading names, validated SCAC, MC and USDOT numbers, contacts and registration status. Shipments are counted under a registered carrier only after two administrators approve linking a document name to it."
        actions={
          <>
            <Button variant="outline" size="sm" asChild>
              <Link to="/carrier-registry/links">
                <Link2 /> Name link approvals
                {waiting > 0 && (
                  <Badge tone="warn" className="ml-1">
                    {waiting}
                  </Badge>
                )}
              </Link>
            </Button>
            {manage && (
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus /> Register carrier
              </Button>
            )}
          </>
        }
      />

      <form role="search" aria-label="Find carriers" className="mb-4 grid gap-3 rounded-lg border bg-card p-4 shadow-card sm:grid-cols-2 lg:grid-cols-[2fr_1fr_1fr_auto]" onSubmit={(event) => event.preventDefault()}>
        <div className="space-y-1.5">
          <Label htmlFor="registry-search">Search</Label>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <Input
              id="registry-search"
              className="pl-9"
              value={search}
              maxLength={100}
              placeholder="Legal or trading name, SCAC, MC or USDOT number"
              onChange={(event) => {
                setSearch(event.target.value)
                update({ search: event.target.value.trim() || null })
              }}
            />
          </div>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="registry-status">Registration status</Label>
          <Select id="registry-status" value={status} onChange={(event) => update({ status: event.target.value || null })}>
            <option value="">All statuses</option>
            <option value="active">Active</option>
            <option value="on_hold">On hold</option>
            <option value="inactive">Inactive</option>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="registry-sort">Sort by</Label>
          <Select id="registry-sort" value={sort} onChange={(event) => update({ sort: event.target.value })}>
            {SORTS.map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex items-end gap-2">
          <Button type="button" variant="outline" size="icon" aria-label={order === "asc" ? "Sorted ascending: sort descending" : "Sorted descending: sort ascending"} onClick={() => update({ order: order === "asc" ? "desc" : null })}>
            {order === "asc" ? <ArrowDownAZ /> : <ArrowUpZA />}
          </Button>
          {filtered && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                setSearch("")
                update({ search: null, status: null })
              }}
            >
              Clear
            </Button>
          )}
        </div>
      </form>

      {page.isPending && <LoadingBlock rows={6} label="Loading the carriers" />}
      {page.isError && <ErrorState error={page.error} onRetry={() => page.refetch()} title="The carrier registry could not be loaded" />}

      {data && data.total === 0 && !filtered && (
        <EmptyState
          icon={<Truck className="size-5" />}
          title="No carrier is registered yet"
          action={
            manage ? (
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus /> Register the first carrier
              </Button>
            ) : undefined
          }
        >
          {manage ? "Register a carrier with its identifiers, then link the carrier names printed on the documents to it." : "An administrator registers carriers and their identifiers."}
        </EmptyState>
      )}

      {data && data.total === 0 && filtered && (
        <EmptyState title="No carrier matches">Nothing matches {query ? `“${query}”` : "these filters"}. Search looks at legal and trading names, SCAC, MC and USDOT numbers.</EmptyState>
      )}

      {data && data.total > 0 && (
        <>
          <Table aria-label="Registered carriers" aria-busy={page.isFetching}>
            <TableHeader>
              <TableRow>
                <TableHead>Carrier</TableHead>
                <TableHead>SCAC</TableHead>
                <TableHead>MC number</TableHead>
                <TableHead>USDOT number</TableHead>
                <TableHead>Contact</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Document names</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Last updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.carriers.map((carrier) => (
                <TableRow key={carrier.id} className="cursor-pointer" onClick={() => navigate(`/carrier-registry/${carrier.id}`)}>
                  <TableCell className="min-w-56">
                    <Link to={`/carrier-registry/${carrier.id}`} className="font-medium text-primary hover:underline" onClick={(event) => event.stopPropagation()}>
                      {carrier.legal_name}
                    </Link>
                    {carrier.trading_name && <p className="text-xs text-muted-foreground">Trading as {carrier.trading_name}</p>}
                  </TableCell>
                  <TableCell>
                    <Identifier value={carrier.scac} />
                  </TableCell>
                  <TableCell>
                    <Identifier value={carrier.mc_number && `MC-${carrier.mc_number}`} />
                  </TableCell>
                  <TableCell>
                    <Identifier value={carrier.dot_number} />
                  </TableCell>
                  <TableCell className="max-w-56">
                    <Contact carrier={carrier} />
                  </TableCell>
                  <TableCell>
                    <RegistrationBadge status={carrier.status} />
                  </TableCell>
                  <TableCell className="text-xs">
                    {carrier.document_names.length > 0 ? `${carrier.document_names.length} linked` : <span className="text-muted-foreground">None linked</span>}
                    {(carrier.pending_requests ?? 0) > 0 && <p className="text-warn">{carrier.pending_requests} awaiting approval</p>}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{dateTime(carrier.created_at)}</TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{dateTime(carrier.updated_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Pagination offset={data.offset} limit={data.limit} total={data.total} onChange={(next) => update({ offset: next ? String(next) : null })} label="Carrier pages" />
        </>
      )}

      <CarrierForm open={creating} onOpenChange={setCreating} onSaved={(carrier) => navigate(`/carrier-registry/${carrier.id}`)} />
    </>
  )
}
