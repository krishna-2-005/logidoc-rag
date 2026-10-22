import { FilterX, Search } from "lucide-react"
import { useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { PageHeader } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { Button } from "@/components/ui/button"
import { Input, Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useDebounced } from "@/hooks/useDebounced"
import { useShipments } from "@/hooks/queries"
import { money, NA } from "@/lib/format"

export function Shipments() {
  const navigate = useNavigate()
  const [search, setSearch] = useState("")
  const [carrier, setCarrier] = useState("")
  const [status, setStatus] = useState("")
  const [category, setCategory] = useState("")
  const [invoiceMismatch, setInvoiceMismatch] = useState(false)
  const [delayed, setDelayed] = useState(false)
  const [needsReview, setNeedsReview] = useState(false)

  const debounced = useDebounced(search)

  const shipments = useShipments({
    search: debounced,
    carrier: carrier ? [carrier] : [],
    status: status ? [status] : [],
    category: category ? [category] : [],
    invoice_mismatch: invoiceMismatch,
    delivery_delayed: delayed,
    needs_review: needsReview,
  })

  const filtered = !!(debounced || carrier || status || category || invoiceMismatch || delayed || needsReview)

  const clear = () => {
    setSearch("")
    setCarrier("")
    setStatus("")
    setCategory("")
    setInvoiceMismatch(false)
    setDelayed(false)
    setNeedsReview(false)
  }

  const data = shipments.data

  return (
    <>
      <PageHeader title="Shipment Explorer" description="Every shipment identified in your documents, with what the audit found. Select one to see its documents, comparisons and history." />

      <form role="search" aria-label="Filter shipments" className="mb-4 grid gap-3 rounded-lg border bg-card p-4 shadow-card md:grid-cols-2 xl:grid-cols-4" onSubmit={(event) => event.preventDefault()}>
        <div className="space-y-1.5 xl:col-span-1">
          <Label htmlFor="shipment-search">Search shipment ID</Label>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" aria-hidden="true" />
            <Input id="shipment-search" className="pl-8" placeholder="e.g. SHJ-2026-001" value={search} onChange={(event) => setSearch(event.target.value)} />
          </div>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="filter-status">Audit result</Label>
          <Select id="filter-status" value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">All results</option>
            {data?.statuses.map((item) => (
              <option key={item} value={item}>
                {item.charAt(0) + item.slice(1).toLowerCase()}
              </option>
            ))}
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="filter-carrier">Carrier</Label>
          <Select id="filter-carrier" value={carrier} onChange={(event) => setCarrier(event.target.value)}>
            <option value="">All carriers</option>
            {data?.carriers.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="filter-category">Discrepancy category</Label>
          <Select id="filter-category" value={category} onChange={(event) => setCategory(event.target.value)}>
            <option value="">All categories</option>
            {data?.categories.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </Select>
        </div>

        <fieldset className="flex flex-wrap items-center gap-x-6 gap-y-2 md:col-span-2 xl:col-span-4">
          <legend className="sr-only">Quick filters</legend>
          {[
            ["Invoice mismatch", invoiceMismatch, setInvoiceMismatch],
            ["Delivery delayed", delayed, setDelayed],
            ["Needs review", needsReview, setNeedsReview],
          ].map(([name, value, set]) => (
            <label key={name as string} className="flex cursor-pointer items-center gap-2 text-sm">
              <input type="checkbox" className="size-4 rounded border-input accent-[hsl(var(--primary))]" checked={value as boolean} onChange={(event) => (set as (next: boolean) => void)(event.target.checked)} />
              {name as string}
            </label>
          ))}
          {filtered && (
            <Button type="button" variant="ghost" size="sm" onClick={clear} className="ml-auto">
              <FilterX /> Clear filters
            </Button>
          )}
        </fieldset>
      </form>

      {shipments.isPending && <LoadingBlock rows={6} label="Loading shipments" />}

      {shipments.isError && <ErrorState error={shipments.error} onRetry={() => shipments.refetch()} title="Shipments could not be loaded" />}

      {data && data.total === 0 && (
        <EmptyState title="No shipments yet" action={<Button asChild><Link to="/documents">Add documents</Link></Button>}>
          Upload a BOL, an invoice and a POD that name the same shipment, and it will appear here.
        </EmptyState>
      )}

      {data && data.total > 0 && data.shipments.length === 0 && (
        <EmptyState title="No shipment matches these filters" icon={<FilterX className="size-5" />} action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>
          {data.total} shipment{data.total === 1 ? " is" : "s are"} hidden by the filters.
        </EmptyState>
      )}

      {data && data.shipments.length > 0 && (
        <>
          <p className="mb-2 text-xs text-muted-foreground" aria-live="polite">
            Showing {data.shipments.length} of {data.total} shipment{data.total === 1 ? "" : "s"}
          </p>
          <Table>
            <TableHeader>
              <tr>
                <TableHead>Shipment</TableHead>
                <TableHead>Carrier</TableHead>
                <TableHead>Route</TableHead>
                <TableHead>Quantity</TableHead>
                <TableHead>Delivery</TableHead>
                <TableHead>Invoice</TableHead>
                <TableHead className="text-right">Invoice total</TableHead>
                <TableHead>Result</TableHead>
              </tr>
            </TableHeader>
            <TableBody>
              {data.shipments.map((row) => (
                <TableRow key={row.shipment_key} className="cursor-pointer" onClick={() => navigate(`/shipments/${encodeURIComponent(row.shipment_id)}`)}>
                  <TableCell className="font-medium">
                    <Link to={`/shipments/${encodeURIComponent(row.shipment_id)}`} className="text-primary underline-offset-2 hover:underline" onClick={(event) => event.stopPropagation()}>
                      {row.shipment_id}
                    </Link>
                  </TableCell>
                  <TableCell>{row.carrier ?? NA}</TableCell>
                  <TableCell className="whitespace-nowrap text-muted-foreground">{row.origin && row.destination ? `${row.origin} → ${row.destination}` : NA}</TableCell>
                  <TableCell>{row.quantity_status ? <StatusBadge status={row.quantity_status} /> : NA}</TableCell>
                  <TableCell>{row.delivery_status ? <StatusBadge status={row.delivery_status} /> : NA}</TableCell>
                  <TableCell>{row.invoice_status ? <StatusBadge status={row.invoice_status} /> : NA}</TableCell>
                  <TableCell className="tabular text-right">{money(row.invoice_total)}</TableCell>
                  <TableCell>
                    <StatusBadge status={row.final_status} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </>
  )
}
