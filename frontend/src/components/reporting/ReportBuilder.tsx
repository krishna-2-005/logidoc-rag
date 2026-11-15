import { FileSpreadsheet, FileText } from "lucide-react"
import { useState, type FormEvent } from "react"
import { Pagination } from "@/components/common/Pagination"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input, Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { type ReportFilters, useReportCatalog, useReportPreview } from "@/hooks/queries"
import { download, qs } from "@/lib/api"
import { NA, dateTime } from "@/lib/format"
import type { ReportName, ReportPreview } from "@/lib/types"
import { cn } from "@/lib/utils"

const PAGE = 25

const words = (text: string) => text.replace(/_/g, " ").toLowerCase()

function cellText(value: unknown, kind: ReportPreview["columns"][number]["kind"]): string {
  if (value === null || value === undefined || value === "") return NA
  if (kind === "money" && typeof value === "number") return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  if (typeof value === "number") return value.toLocaleString("en-US", { maximumFractionDigits: 2 })
  return String(value)
}

/** The filters a report accepts, as a form; the server refuses any other. */
function FilterFields({ accepts, draft, setDraft, options }: { accepts: string[]; draft: ReportFilters; setDraft: (value: ReportFilters) => void; options: NonNullable<ReturnType<typeof useReportCatalog>["data"]>["options"] }) {
  const one = (key: keyof ReportFilters, value: string) => setDraft({ ...draft, [key]: value ? [value] : undefined })

  return (
    <>
      {accepts.includes("dates") && (
        <>
          <div className="w-40 space-y-1.5">
            <Label htmlFor="report-start">From</Label>
            <Input id="report-start" type="date" value={draft.start ?? ""} onChange={(event) => setDraft({ ...draft, start: event.target.value || undefined })} />
          </div>
          <div className="w-40 space-y-1.5">
            <Label htmlFor="report-end">To</Label>
            <Input id="report-end" type="date" value={draft.end ?? ""} onChange={(event) => setDraft({ ...draft, end: event.target.value || undefined })} />
          </div>
        </>
      )}
      {accepts.includes("carrier") && (
        <div className="w-52 space-y-1.5">
          <Label htmlFor="report-carrier">Carrier</Label>
          <Select id="report-carrier" value={draft.carrier ?? ""} onChange={(event) => setDraft({ ...draft, carrier: event.target.value || undefined })}>
            <option value="">All carriers</option>
            {options.carriers.map((item) => (
              <option key={item.key} value={item.key}>
                {item.name}
              </option>
            ))}
          </Select>
        </div>
      )}
      {accepts.includes("shipment") && (
        <div className="w-44 space-y-1.5">
          <Label htmlFor="report-shipment-filter">Shipment ID contains</Label>
          <Input id="report-shipment-filter" maxLength={100} value={draft.shipment ?? ""} onChange={(event) => setDraft({ ...draft, shipment: event.target.value || undefined })} />
        </div>
      )}
      {accepts.includes("outcome") && (
        <div className="w-48 space-y-1.5">
          <Label htmlFor="report-outcome">Freight audit outcome</Label>
          <Select id="report-outcome" value={draft.outcome?.[0] ?? ""} onChange={(event) => one("outcome", event.target.value)}>
            <option value="">All outcomes</option>
            {options.outcomes.map((item) => (
              <option key={item} value={item}>
                {item === "NOT_RUN" ? "not run" : words(item)}
              </option>
            ))}
          </Select>
        </div>
      )}
      {accepts.includes("exception_status") && (
        <div className="w-40 space-y-1.5">
          <Label htmlFor="report-status">Exception status</Label>
          <Select id="report-status" value={draft.exception_status?.[0] ?? ""} onChange={(event) => one("exception_status", event.target.value)}>
            <option value="">All statuses</option>
            {options.statuses.map((item) => (
              <option key={item} value={item}>
                {words(item)}
              </option>
            ))}
          </Select>
        </div>
      )}
      {accepts.includes("risk_category") && (
        <div className="w-36 space-y-1.5">
          <Label htmlFor="report-risk">Risk category</Label>
          <Select id="report-risk" value={draft.risk_category?.[0] ?? ""} onChange={(event) => one("risk_category", event.target.value)}>
            <option value="">All</option>
            {options.categories.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </Select>
        </div>
      )}
    </>
  )
}

/**
 * Portfolio reports: choose a report and its filters, preview it, then download
 * it as CSV or PDF. The preview and the files come from the same build on the
 * server, so they never disagree; the totals always cover every row.
 */
export function ReportBuilder({ allowed }: { allowed: boolean }) {
  const toast = useToast()
  const catalog = useReportCatalog(allowed)
  const [report, setReport] = useState<ReportName>("freight-audit-summary")
  const [draft, setDraft] = useState<ReportFilters>({})
  const [filters, setFilters] = useState<ReportFilters>({})
  const [offset, setOffset] = useState(0)
  const [busy, setBusy] = useState<string | null>(null)
  const preview = useReportPreview(allowed && catalog.data ? report : null, filters, offset, PAGE)

  if (!allowed) return null
  if (catalog.isPending) return <LoadingBlock rows={3} label="Loading the reports" />
  if (catalog.isError) return <ErrorState error={catalog.error} onRetry={() => catalog.refetch()} title="The reports could not be loaded" />

  const definitions = catalog.data.reports.filter((item) => item.report !== "corrections" || catalog.data.can_view_history)
  const definition = definitions.find((item) => item.report === report) ?? definitions[0]

  const choose = (name: ReportName) => {
    setReport(name)
    setDraft({})
    setFilters({})
    setOffset(0)
  }

  const apply = (event: FormEvent) => {
    event.preventDefault()
    setOffset(0)
    setFilters({ ...draft, shipment: draft.shipment?.trim() || undefined })
  }

  const fetchFile = (extension: "csv" | "pdf") => {
    setBusy(extension)
    download(`/api/reporting/${report}.${extension}${qs({ ...filters })}`, `${report}.${extension}`).then(
      (name) => toast.success(`Downloaded ${name}`),
      (error: Error) => toast.error(error.message),
    ).finally(() => setBusy(null))
  }

  const data = preview.data

  return (
    <div className="space-y-4">
      <div role="radiogroup" aria-label="Report" className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {definitions.map((item) => (
          <button
            key={item.report}
            type="button"
            role="radio"
            aria-checked={item.report === definition.report}
            onClick={() => choose(item.report)}
            className={cn("rounded-lg border bg-card p-4 text-left shadow-card transition hover:border-primary/60", item.report === definition.report && "border-primary ring-1 ring-primary")}
          >
            <p className="text-sm font-semibold">{item.title}</p>
            <p className="mt-1 text-xs text-muted-foreground">{item.description}</p>
          </button>
        ))}
      </div>

      <form aria-label="Report filters" onSubmit={apply} className="flex flex-wrap items-end gap-3 rounded-lg border bg-card p-4 shadow-card">
        <FilterFields accepts={definition.filters} draft={draft} setDraft={setDraft} options={catalog.data.options} />
        <Button type="submit">Preview</Button>
        <div className="ml-auto flex gap-2">
          <Button type="button" variant="outline" disabled={busy !== null || !data} onClick={() => fetchFile("csv")}>
            <FileSpreadsheet /> {busy === "csv" ? "Preparing…" : "Download CSV"}
          </Button>
          <Button type="button" variant="outline" disabled={busy !== null || !data} onClick={() => fetchFile("pdf")}>
            <FileText /> {busy === "pdf" ? "Preparing…" : "Download PDF"}
          </Button>
        </div>
      </form>

      {preview.isPending && <LoadingBlock rows={4} label="Preparing the preview" />}
      {preview.isError && <ErrorState error={preview.error} onRetry={() => preview.refetch()} title="The report could not be prepared" />}

      {data && (
        <Card data-testid="report-preview">
          <CardContent className="space-y-4 pt-5">
            <div className="text-sm">
              <p className="font-semibold">{data.title}</p>
              <p className="text-xs text-muted-foreground">
                Generated {dateTime(data.generated_at)} ({data.generated_at} UTC). Dates are {data.date_basis}.
              </p>
              <ul className="mt-2 list-disc pl-5 text-xs" aria-label="Filters applied">
                {data.filters.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
              <details className="mt-2 text-xs text-muted-foreground">
                <summary className="cursor-pointer">Where the figures come from</summary>
                <ul className="mt-1 list-disc pl-5">
                  {data.provenance.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </details>
            </div>

            {data.notes.map((note) => (
              <Alert key={note} tone="info">
                <p>{note}</p>
              </Alert>
            ))}

            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-label="Totals">
              {data.totals.map((section) => (
                <div key={section.title} className="rounded-md border p-3">
                  <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{section.title}</p>
                  <dl className="mt-1 space-y-0.5 text-sm">
                    {section.rows.map((row) => (
                      <div key={row.label} className="flex justify-between gap-3">
                        <dt>{row.label}</dt>
                        <dd className="tabular text-right">
                          {row.display}
                          {row.note && <span className="block text-[11px] text-muted-foreground">{row.note}</span>}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
              ))}
            </div>

            {data.total_rows === 0 ? (
              <EmptyState title="No row matches the filters">Change the filters; the downloads would be empty too.</EmptyState>
            ) : (
              <div className="overflow-x-auto">
                <Table aria-label={`${data.title} rows`}>
                  <TableHeader>
                    <TableRow>
                      {data.columns.map((column) => (
                        <TableHead key={column.key} className={column.kind === "money" || column.kind === "count" || column.kind === "number" ? "text-right" : undefined}>
                          {column.label}
                        </TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.rows.map((row, index) => (
                      <TableRow key={index}>
                        {data.columns.map((column) => (
                          <TableCell key={column.key} className={cn("text-xs", (column.kind === "money" || column.kind === "count" || column.kind === "number") && "tabular text-right")}>
                            {cellText(row[column.key], column.kind)}
                          </TableCell>
                        ))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
            <Pagination offset={offset} limit={PAGE} total={data.total_rows} onChange={setOffset} label="Report rows" />
          </CardContent>
        </Card>
      )}
    </div>
  )
}
