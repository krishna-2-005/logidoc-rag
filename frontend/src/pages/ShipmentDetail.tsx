import { ArrowLeft, ClipboardCheck, Download, FileText, Scale } from "lucide-react"
import { Link, useParams } from "react-router-dom"
import { AskPanel } from "@/components/common/AskPanel"
import { DeliveryCard, InvoiceCard, QuantityCard } from "@/components/common/AuditComparison"
import { Explanations } from "@/components/common/Explanations"
import { FreightAuditPanel } from "@/components/common/FreightAuditPanel"
import { ExtractionsSection } from "@/components/extraction/ExtractionPanel"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { Timeline } from "@/components/common/Timeline"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { useShipment } from "@/hooks/queries"
import { download } from "@/lib/api"
import { NA } from "@/lib/format"
import { ApiError } from "@/lib/api"

export function DownloadButtons({ shipmentId }: { shipmentId: string }) {
  const toast = useToast()
  const path = `/api/reports/shipments/${encodeURIComponent(shipmentId)}`

  const run = (extension: "csv" | "pdf") => () =>
    download(`${path}.${extension}`, `freight_audit_${shipmentId}.${extension}`).then(
      (name) => toast.success(`Downloaded ${name}`),
      (error: Error) => toast.error(error.message),
    )

  return (
    <>
      <Button variant="outline" size="sm" onClick={run("csv")}>
        <Download /> CSV report
      </Button>
      <Button variant="outline" size="sm" onClick={run("pdf")}>
        <Download /> PDF report
      </Button>
    </>
  )
}

export function ShipmentDetailPage() {
  const { id } = useParams()
  const shipment = useShipment(id)

  const back = (
    <Link to="/shipments" className="mb-3 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
      <ArrowLeft className="size-4" aria-hidden="true" /> All shipments
    </Link>
  )

  if (shipment.isPending) {
    return (
      <>
        {back}
        <LoadingBlock rows={6} label="Loading shipment" />
      </>
    )
  }

  if (shipment.isError) {
    const missing = shipment.error instanceof ApiError && shipment.error.status === 404

    return (
      <>
        {back}
        {missing ? <EmptyState title="Shipment not found">No shipment called {id} is in the documents.</EmptyState> : <ErrorState error={shipment.error} onRetry={() => shipment.refetch()} />}
      </>
    )
  }

  const { result, exceptions, explanations, documents, history } = shipment.data
  const encoded = encodeURIComponent(shipment.data.shipment_id)

  return (
    <>
      {back}
      <PageHeader
        title={shipment.data.shipment_id}
        description={
          result.success ? (
            <span className="flex flex-wrap items-center gap-2">
              <StatusBadge status={result.final_status} />
              <span>
                {result.carrier?.bol ?? NA} · {result.route?.origin && result.route?.destination ? `${result.route.origin} → ${result.route.destination}` : NA}
              </span>
            </span>
          ) : (
            "This shipment could not be reconciled."
          )
        }
        actions={
          <>
            <Button variant="outline" size="sm" asChild>
              <Link to={`/freight-audit/${encoded}`}>
                <Scale /> Freight audit
              </Link>
            </Button>
            {result.success && <DownloadButtons shipmentId={shipment.data.shipment_id} />}
          </>
        }
      />

      {!result.success && <Alert tone="bad">{result.error ?? "Reconciliation failed."}</Alert>}

      {result.success && result.quantity && result.delivery && result.invoice && (
        <section aria-label="Audit comparisons" className="grid items-start gap-4 lg:grid-cols-3">
          <QuantityCard audit={result.quantity} />
          <DeliveryCard audit={result.delivery} />
          <InvoiceCard audit={result.invoice} />
        </section>
      )}

      {result.success && result.review_items && result.review_items.length > 0 && (
        <Section title="Values that could not be verified" description="Missing, conflicting or unreadable values are never treated as a match.">
          <div className="space-y-2">
            {result.review_items.map((item, index) => (
              <Alert key={index} tone="warn">
                <p>
                  <span className="font-medium">{item.category}:</span> {item.reason}
                </p>
              </Alert>
            ))}
          </div>
        </Section>
      )}

      <FreightAuditPanel audit={shipment.data.freight_audit} />

      <Section
        title="Findings and explanations"
        description="Written from the audited values and their source documents; no language model is involved."
        actions={
          <Button variant="outline" size="sm" asChild>
            <Link to={`/exceptions?search=${encoded}`}>
              <ClipboardCheck /> Review in Exception Center
            </Link>
          </Button>
        }
      >
        <Explanations items={explanations} />
        <p className="mt-2 text-xs text-muted-foreground">{exceptions.length} exception{exceptions.length === 1 ? "" : "s"} recorded for this shipment.</p>
      </Section>

      <Section title="Documents" description="Only the documents that name this shipment.">
        {documents.length === 0 ? (
          <EmptyState title="No documents">No usable document names this shipment.</EmptyState>
        ) : (
          <Card className="divide-y">
            {documents.map((file) => (
              <div key={file.filename} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-sm">
                <span className="flex min-w-0 items-center gap-2">
                  <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                  <span className="break-all font-medium">{file.filename}</span>
                  <span className="text-muted-foreground">{file.document_type ?? NA}</span>
                </span>
                <span className="flex items-center gap-3 text-xs text-muted-foreground">
                  {file.text_pages ?? 0} of {file.total_pages ?? 0} page(s) with text
                  <StatusBadge status={file.status} />
                </span>
              </div>
            ))}
          </Card>
        )}
      </Section>

      <ExtractionsSection shipmentId={shipment.data.shipment_id} shipmentKey={shipment.data.shipment_key} documents={documents} />

      <Section title="Audit history" description="Earlier recorded audits of this shipment, with what changed since the one before.">
        {history.length === 0 ? (
          <EmptyState title="No audit recorded yet">An administrator records audit runs from Audit History. Nothing is recorded by viewing a shipment.</EmptyState>
        ) : (
          <Timeline entries={history} />
        )}
      </Section>

      <Section title="Ask about this shipment">
        <AskPanel shipmentId={shipment.data.shipment_id} />
      </Section>
    </>
  )
}
