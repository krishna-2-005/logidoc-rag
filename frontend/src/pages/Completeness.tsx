import { AlertTriangle, FileX2 } from "lucide-react"
import { useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Input, Label } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useDebounced } from "@/hooks/useDebounced"
import { useCompleteness } from "@/hooks/queries"

const TYPES = ["BOL", "INVOICE", "POD"] as const

function Presence({ files }: { files: string[] }) {
  return files.length > 0 ? (
    <span className="inline-flex flex-col gap-0.5">
      <Badge tone="ok">Present</Badge>
      {files.length > 1 && <span className="text-xs text-warn">{files.length} documents</span>}
    </span>
  ) : (
    <Badge tone="warn">Missing</Badge>
  )
}

export function Completeness() {
  // The Overview's readiness cards open this page with ?incomplete=1
  const [params] = useSearchParams()
  const [search, setSearch] = useState("")
  const [incompleteOnly, setIncompleteOnly] = useState(params.get("incomplete") === "1")
  const debounced = useDebounced(search)
  const completeness = useCompleteness(debounced, incompleteOnly)

  const data = completeness.data

  return (
    <>
      <PageHeader title="Document Completeness" description="A freight audit needs a BOL, an invoice and a POD for each shipment. This shows what is present, what is missing, and which files could not be used." />

      <form role="search" aria-label="Filter shipments" className="mb-5 flex flex-wrap items-end gap-4" onSubmit={(event) => event.preventDefault()}>
        <div className="w-64 space-y-1.5">
          <Label htmlFor="comp-search">Search shipment</Label>
          <Input id="comp-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="SHJ-2026-001" />
        </div>
        <label className="flex cursor-pointer items-center gap-2 pb-2 text-sm">
          <input type="checkbox" className="size-4 rounded border-input accent-[hsl(var(--primary))]" checked={incompleteOnly} onChange={(event) => setIncompleteOnly(event.target.checked)} /> Incomplete only
        </label>
      </form>

      {completeness.isPending && <LoadingBlock rows={5} label="Loading completeness" />}
      {completeness.isError && <ErrorState error={completeness.error} onRetry={() => completeness.refetch()} />}

      {data && (
        <div className="space-y-8">
          {data.unusable.length > 0 && (
            <Section title="Files that could not be used" description="They belong to no shipment, and no shipment is reported clean while they stay unresolved.">
              <div className="space-y-2">
                {data.unusable.map((item) => (
                  <Alert key={item.filename} tone="bad" className="items-start">
                    <FileX2 />
                    <div>
                      <p>
                        <span className="font-medium">{item.filename}</span> <StatusBadge status={item.status} />
                      </p>
                      <p className="mt-0.5 text-sm">{item.message}{item.error ? ` (${item.error})` : ""}</p>
                      {item.status === "no_text" && <p className="mt-0.5 text-xs">This is an image-only scan. There is no OCR here: provide a text-based PDF.</p>}
                    </div>
                  </Alert>
                ))}
              </div>
            </Section>
          )}

          {data.unidentified.length > 0 && (
            <Section title="Documents without a shipment ID">
              <div className="space-y-2">
                {data.unidentified.map((item) => (
                  <Alert key={item.filename} tone="warn">
                    <AlertTriangle />
                    <p>
                      <span className="font-medium">{item.filename}</span>: {item.message}
                    </p>
                  </Alert>
                ))}
              </div>
            </Section>
          )}

          <Section title="Shipments">
            {data.shipments.length === 0 ? (
              <EmptyState title="No shipment matches">{search || incompleteOnly ? "Try fewer filters." : "No document names a shipment yet."}</EmptyState>
            ) : (
              <Table>
                <TableHeader>
                  <tr>
                    <TableHead>Shipment</TableHead>
                    <TableHead>Readiness</TableHead>
                    {TYPES.map((type) => (
                      <TableHead key={type}>{type === "INVOICE" ? "Invoice" : type}</TableHead>
                    ))}
                    <TableHead>Next step and warnings</TableHead>
                  </tr>
                </TableHeader>
                <TableBody>
                  {data.shipments.map((row) => (
                    <TableRow key={row.shipment_key}>
                      <TableCell className="font-medium">
                        <Link to={`/shipments/${encodeURIComponent(row.shipment_id)}`} className="text-primary hover:underline">
                          {row.shipment_id}
                        </Link>
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={row.readiness} />
                      </TableCell>
                      {TYPES.map((type) => (
                        <TableCell key={type}>
                          <Presence files={row.by_type[type] ?? []} />
                        </TableCell>
                      ))}
                      <TableCell className="max-w-md text-xs">
                        <p className={row.readiness === "incomplete" ? "text-foreground" : "text-muted-foreground"}>{row.next_step}</p>
                        {row.warnings.map((warning) => (
                          <p key={warning} className="mt-1 text-warn">
                            {warning}
                          </p>
                        ))}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            <p className="mt-3 text-xs text-muted-foreground">Ready means the documents are there. It does not mean the audit will find nothing: values can still be missing, conflicting or unreadable.</p>
          </Section>

          {data.duplicates.length > 0 && (
            <Section title="Identical files">
              {data.duplicates.map((group) => (
                <Alert key={group.join("|")} tone="info">
                  <p>The same content is stored as {group.join(", ")}.</p>
                </Alert>
              ))}
            </Section>
          )}
        </div>
      )}
    </>
  )
}
