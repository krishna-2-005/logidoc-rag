import { Download, FileSpreadsheet, FileText } from "lucide-react"
import { useState } from "react"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label, Select } from "@/components/ui/input"
import { can, useSession, useShipments } from "@/hooks/queries"
import { download } from "@/lib/api"

interface Report {
  title: string
  description: string
  path: string
  file: string
}

const REPORTS: Report[] = [
  { title: "Shipment register", description: "Every shipment with its statuses, invoice figures and discrepancy categories.", path: "/api/reports/register.csv", file: "shipment_register.csv" },
  { title: "Exceptions", description: "Every exception with its review state, assignee, evidence references and notes.", path: "/api/reports/exceptions.csv", file: "exceptions.csv" },
  { title: "Financial exposure", description: "Invoice totals, arithmetic mismatches and amounts under review, never across currencies.", path: "/api/reports/exposure.csv", file: "financial_exposure.csv" },
  { title: "Document completeness", description: "Which documents each shipment has, what is missing and the next step.", path: "/api/reports/completeness.csv", file: "document_completeness.csv" },
]

export function Reports() {
  const toast = useToast()
  const session = useSession()
  const shipments = useShipments({})
  const [shipment, setShipment] = useState("")
  const [busy, setBusy] = useState<string | null>(null)

  const allowed = can(session.data, "reports.download")
  const selected = shipment || shipments.data?.shipments.find((row) => row.reconciled)?.shipment_id || ""

  const run = (key: string, path: string, file: string) => {
    setBusy(key)
    download(path, file).then(
      (name) => toast.success(`Downloaded ${name}`),
      (error: Error) => toast.error(error.message),
    ).finally(() => setBusy(null))
  }

  return (
    <>
      <PageHeader title="Reports" description="Download the audit as spreadsheets and PDFs. They are produced by the same code as the Streamlit application, from the same audit results." />

      {!allowed && session.data && <Alert tone="warn" className="mb-5"><p>Your role cannot download reports.</p></Alert>}

      <Section title="Freight audit for one shipment" description="The CSV lists every compared value with its source; the PDF is the printable audit.">
        {shipments.isPending && <LoadingBlock rows={2} label="Loading shipments" />}
        {shipments.isError && <ErrorState error={shipments.error} onRetry={() => shipments.refetch()} />}
        {shipments.data && shipments.data.shipments.length === 0 && <EmptyState title="No shipments">Add documents to create reports.</EmptyState>}
        {shipments.data && shipments.data.shipments.length > 0 && (
          <Card>
            <CardContent className="flex flex-wrap items-end gap-4 pt-5">
              <div className="w-72 space-y-1.5">
                <Label htmlFor="report-shipment">Shipment</Label>
                <Select id="report-shipment" value={selected} onChange={(event) => setShipment(event.target.value)}>
                  {shipments.data.shipments.map((row) => (
                    <option key={row.shipment_key} value={row.shipment_id} disabled={!row.reconciled}>
                      {row.shipment_id}{row.reconciled ? "" : " (not reconciled)"}
                    </option>
                  ))}
                </Select>
              </div>
              <Button variant="outline" disabled={!allowed || !selected || busy !== null} onClick={() => run("csv", `/api/reports/shipments/${encodeURIComponent(selected)}.csv`, `freight_audit_${selected}.csv`)}>
                <FileSpreadsheet /> {busy === "csv" ? "Preparing…" : "Download CSV"}
              </Button>
              <Button variant="outline" disabled={!allowed || !selected || busy !== null} onClick={() => run("pdf", `/api/reports/shipments/${encodeURIComponent(selected)}.pdf`, `freight_audit_${selected}.pdf`)}>
                <FileText /> {busy === "pdf" ? "Preparing…" : "Download PDF"}
              </Button>
            </CardContent>
          </Card>
        )}
      </Section>

      <Section title="Portfolio exports" description="Every cell is protected against spreadsheet formula injection. A missing value is exported as NOT AVAILABLE, never 0.">
        <div className="grid gap-4 md:grid-cols-2">
          {REPORTS.map((report) => (
            <Card key={report.path}>
              <CardHeader>
                <CardTitle>{report.title}</CardTitle>
                <CardDescription>{report.description}</CardDescription>
              </CardHeader>
              <CardContent>
                <Button variant="outline" size="sm" disabled={!allowed || busy !== null} onClick={() => run(report.path, report.path, report.file)}>
                  <Download /> {busy === report.path ? "Preparing…" : "Download CSV"}
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
        <p className="mt-4 text-xs text-muted-foreground">Trend analytics are exported from Audit History with the filters you chose.</p>
      </Section>
    </>
  )
}
