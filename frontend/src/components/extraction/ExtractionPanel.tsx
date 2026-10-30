import { ChevronDown, ChevronRight, FileText, Pencil } from "lucide-react"
import { useState, type ReactNode } from "react"
import { Link } from "react-router-dom"
import { Section } from "@/components/common/PageHeader"
import { EmptyState, ErrorState, LoadingBlock, errorMessage } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { CorrectionDialog } from "@/components/extraction/CorrectionDialog"
import { CorrectionBadge, FieldStateBadge, STATE_LOOK, ValidationBadge, consumerOf, pageText, valueText } from "@/components/extraction/fields"
import { CorrectionStoreWarning, RecomputeStatus } from "@/components/extraction/RecomputeStatus"
import { StoreStateNotice, useStoreWriteBlock } from "@/components/extraction/storeState"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { can, useExtraction, useExtractions, useRunExtraction, useSession, useStaleness } from "@/hooks/queries"
import { dateTime, label } from "@/lib/format"
import type { Correction, CrossDocumentIssue, DocumentFile, ExtractionAlias, ExtractionDetail, ExtractionSummary } from "@/lib/types"
import { cn } from "@/lib/utils"

const TONE_BORDER = { plain: "border-border", ok: "border-ok", warn: "border-warn", bad: "border-bad" } as const

/** One labelled value. Original, proposed and approved values are never drawn alike. */
function Block({ tone = "plain", title, children, testId }: { tone?: keyof typeof TONE_BORDER; title: string; children: ReactNode; testId?: string }) {
  return (
    <div className={cn("border-l-2 pl-2.5", TONE_BORDER[tone])} data-testid={testId}>
      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{title}</p>
      <div className="break-words text-sm">{children}</div>
    </div>
  )
}

function who(correction: Correction): string {
  return `${correction.actor} · ${dateTime(correction.at)}`
}

function FieldRow({ detail, name, canPropose, onPropose }: { detail: ExtractionDetail; name: string; canPropose: boolean; onPropose: (field: string) => void }) {
  const field = detail.payload.fields[name]
  const effective = detail.effective[name]
  const corrections = detail.corrections.filter((item) => item.field_name === name)
  const rejected = [...corrections].reverse().find((item) => item.status === "rejected")
  const legacy = corrections.filter((item) => item.status === "legacy")
  const consumer = consumerOf(detail.document_type, name)
  const title = label(name)
  const blocked = useStoreWriteBlock()

  let action: ReactNode

  if (name === "shipment_id") action = <span className="text-xs text-muted-foreground">Shipment ID corrections are not available.</span>
  else if (field.state === "UNSUPPORTED") action = <span className="text-xs text-muted-foreground">Not supported by the extractor.</span>
  else if (effective?.proposed) action = <span className="text-xs text-muted-foreground">A correction is awaiting a decision.</span>
  else if (canPropose)
    action = (
      <Button variant="outline" size="sm" onClick={() => onPropose(name)} disabled={blocked !== null} title={blocked ?? undefined} aria-label={`Propose a correction for ${title}`}>
        <Pencil /> Propose correction
      </Button>
    )
  else action = <span className="text-xs text-muted-foreground">Reviewers and administrators can propose corrections.</span>

  const sourceFile = field.source_file ?? detail.filename

  return (
    <TableRow className="align-top">
      <TableCell className="min-w-[9rem]">
        <p className="font-medium">{title}</p>
        {consumer && <p className="mt-0.5 text-xs text-muted-foreground">Read by {consumer} once a correction is approved.</p>}
      </TableCell>

      <TableCell className="min-w-[16rem] max-w-[28rem]">
        <div className="space-y-2">
          <Block title="Extracted (original)" testId={`original-${name}`}>
            {field.state === "EXTRACTED" ? (
              <span>{valueText(name, field.value)}</span>
            ) : (
              <span className="inline-flex flex-wrap items-center gap-1.5">
                <FieldStateBadge state={field.state} />
                <span className="text-xs text-muted-foreground">{STATE_LOOK[field.state].long}</span>
              </span>
            )}
          </Block>

          {field.state === "CONFLICTING" && field.alternatives.length > 0 && (
            <ul className="list-disc pl-5 text-xs text-muted-foreground">
              {field.alternatives.map((item, index) => (
                <li key={index}>
                  {item.problem ? `an occurrence that could not be read (${pageText(item.page)})` : `${valueText(name, item.value)} (${pageText(item.page)})`}
                </li>
              ))}
            </ul>
          )}

          {effective?.approved && (
            <Block tone="ok" title="Approved correction" testId={`approved-${name}`}>
              <p className="font-medium">{valueText(name, effective.approved.corrected)}</p>
              <p className="text-xs text-muted-foreground">
                Approved by {effective.approved.decided_by ?? "an administrator"} · {dateTime(effective.approved.decided_at)} · typed by a reviewer, so it has no page.
              </p>
              <p className="text-xs text-muted-foreground">Proposed by {who(effective.approved)}</p>
            </Block>
          )}

          {effective?.proposed && (
            <Block tone="warn" title="Proposed (not applied)" testId={`proposed-${name}`}>
              <p className="font-medium">{valueText(name, effective.proposed.corrected)}</p>
              <p className="text-xs text-muted-foreground">
                Proposed by {who(effective.proposed)} · <CorrectionBadge status="pending" /> It changes no audit until a different administrator approves it.
              </p>
            </Block>
          )}

          {rejected && (
            <Block tone="bad" title="Rejected correction" testId={`rejected-${name}`}>
              <p>{valueText(name, rejected.corrected)}</p>
              <p className="text-xs text-muted-foreground">
                Proposed by {who(rejected)} · rejected by {rejected.decided_by ?? "an administrator"}. It has no effect.
              </p>
            </Block>
          )}

          {legacy.map((item) => (
            <Block key={item.id} title="Legacy correction" testId={`legacy-${name}`}>
              <p>{valueText(name, item.corrected)}</p>
              <p className="text-xs text-muted-foreground">
                <CorrectionBadge status="legacy" /> Recorded by {who(item)} before approvals existed. It is not applied; propose it again to have it considered.
              </p>
            </Block>
          ))}
        </div>
      </TableCell>

      <TableCell className="min-w-[11rem] max-w-[16rem]">
        <p className="break-all text-sm">{sourceFile}</p>
        <p className="text-xs text-muted-foreground">
          {field.state === "EXTRACTED" ? pageText(field.page) : field.state === "CONFLICTING" ? "pages are listed with each value" : "no page: nothing was read"}
        </p>
        {field.method && <p className="text-xs text-muted-foreground">Method: {field.method}</p>}
      </TableCell>

      <TableCell className="min-w-[11rem] max-w-[20rem]">
        <div className="flex flex-wrap items-center gap-1.5">
          <ValidationBadge validation={field.validation} />
          {field.needs_review && <Badge tone="warn">Needs review</Badge>}
        </div>
        {field.warnings.length > 0 && (
          <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">
            {field.warnings.map((warning, index) => (
              <li key={index} className="break-words">
                {warning}
              </li>
            ))}
          </ul>
        )}
      </TableCell>

      <TableCell className="min-w-[12rem]">{action}</TableCell>
    </TableRow>
  )
}

const ALIAS_LOOK: Record<ExtractionAlias["relation"], { text: string; tone: "neutral" | "ok" | "warn" }> = {
  original: { text: "Original name", tone: "neutral" },
  same_type: { text: "Same type", tone: "ok" },
  type_conflict: { text: "Type conflict", tone: "warn" },
}

/**
 * The file names this content was seen under. Information only: no alias is used by an audit or a correction, and a
 * name that classifies as another type is recorded but not used at all. While the store awaits its migration the
 * records do not exist yet, which is said as "not available", never as "none".
 */
function Aliases({ detail }: { detail: ExtractionDetail }) {
  if (detail.aliases_available === undefined) return null

  if (detail.aliases_available === false) {
    return (
      <div data-testid="aliases-unavailable" className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
        <p className="font-medium text-foreground">File names</p>
        <p className="mt-0.5">Other file names for this content are not available until the extraction store is migrated. This does not mean there are none.</p>
      </div>
    )
  }

  const aliases = detail.aliases ?? []

  if (aliases.length === 0) {
    return (
      <div data-testid="aliases-empty" className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
        <p className="font-medium text-foreground">File names</p>
        <p className="mt-0.5">No file names are recorded for this content.</p>
      </div>
    )
  }

  const conflicts = aliases.filter((item) => item.relation === "type_conflict")

  return (
    <div data-testid="aliases" className="rounded-md border p-3 text-sm">
      <p className="font-medium">File names for this content</p>
      <ul className="mt-2 space-y-1.5">
        {aliases.map((item) => {
          const look = ALIAS_LOOK[item.relation] ?? { text: "Unknown relation", tone: "neutral" as const }

          return (
            <li key={item.id} className="flex flex-wrap items-center gap-2" data-testid={`alias-${item.relation}`}>
              <span className="break-all">{item.filename}</span>
              <span className="text-muted-foreground">read as {label(item.inferred_type)}</span>
              <Badge tone={look.tone}>{look.text}</Badge>
            </li>
          )
        })}
      </ul>
      {conflicts.length > 0 && (
        <p className="mt-2 text-xs text-warn">
          {conflicts.length === 1 ? "A name" : `${conflicts.length} names`} classified as a different document type than this extraction ({label(detail.document_type)}). Type-conflicting names are recorded for review only: they are not used for any audit calculation and have no corrections.
        </p>
      )}
      <p className="mt-2 text-xs text-muted-foreground">File names are information only. They do not change this extraction, its corrections or any audit.</p>
    </div>
  )
}

function ExtractionBody({ detail, canPropose, onPropose }: { detail: ExtractionDetail; canPropose: boolean; onPropose: (field: string) => void }) {
  const { payload } = detail
  const names = Object.keys(payload.fields)

  return (
    <div className="space-y-4 border-t p-4">
      {payload.status !== "extracted" && (
        <Alert tone="warn">
          <p>
            {payload.status === "partial" && `Pages without text: ${payload.pages_without_text.join(", ") || "some"}. A field that was not found may be on those pages.`}
            {payload.status === "needs_ocr" && "No page has readable text (an image-only scan). There is no OCR, so nothing could be read."}
            {payload.status === "unreadable" && "The file could not be opened, so nothing could be read."}
          </p>
        </Alert>
      )}

      {payload.review_reasons.length > 0 && (
        <Alert tone="warn" className="items-start">
          <div>
            <p className="font-medium">This extraction needs review:</p>
            <ul className="mt-1 list-disc pl-5">
              {payload.review_reasons.map((reason) => (
                <li key={reason} className="break-words">
                  {reason}
                </li>
              ))}
            </ul>
          </div>
        </Alert>
      )}

      <Aliases detail={detail} />

      <Table>
        <TableHeader>
          <tr>
            <TableHead>Field</TableHead>
            <TableHead>Value</TableHead>
            <TableHead>Source</TableHead>
            <TableHead>Checks on the original</TableHead>
            <TableHead>Correction</TableHead>
          </tr>
        </TableHeader>
        <TableBody>
          {names.map((name) => (
            <FieldRow key={name} detail={detail} name={name} canPropose={canPropose} onPropose={onPropose} />
          ))}
        </TableBody>
      </Table>

      {(payload.line_items.length > 0 || payload.unclassified_amounts.length > 0) && (
        <details className="rounded-md border bg-card p-3 text-sm">
          <summary className="cursor-pointer font-medium">Charges read from this invoice ({payload.line_items.length})</summary>
          <ul className="mt-2 space-y-1">
            {payload.line_items.map((item, index) => (
              <li key={index} className="flex flex-wrap justify-between gap-2">
                <span className="break-words">
                  {item.description} <span className="text-xs text-muted-foreground">({pageText(item.page)})</span>
                </span>
                <span className="tabular">{valueText("invoice_total", item.amount)}</span>
              </li>
            ))}
          </ul>
          {payload.unclassified_amounts.length > 0 && (
            <p className="mt-2 text-xs text-warn">
              {payload.unclassified_amounts.length} amount(s) could not be classified as charges: {payload.unclassified_amounts.map((item) => `${item.label} (${pageText(item.page)})`).join(", ")}. The invoice total is not checked against the charges.
            </p>
          )}
        </details>
      )}
    </div>
  )
}

function ExtractionCard({ summary, startOpen, canPropose }: { summary: ExtractionSummary; startOpen: boolean; canPropose: boolean }) {
  const [open, setOpen] = useState(startOpen)
  const [proposing, setProposing] = useState<string | null>(null)
  const detail = useExtraction(open ? summary.id : null)
  const panelId = `extraction-${summary.id}`

  return (
    <Card>
      <button
        type="button"
        className="flex w-full flex-wrap items-center justify-between gap-3 rounded-lg p-4 text-left"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="flex min-w-0 items-center gap-2">
          {open ? <ChevronDown className="size-4 shrink-0" aria-hidden="true" /> : <ChevronRight className="size-4 shrink-0" aria-hidden="true" />}
          <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="break-all font-medium">{summary.filename}</span>
          <span className="text-sm text-muted-foreground">{label(summary.document_type)}</span>
        </span>
        <span className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          {summary.pending_count > 0 && <Badge tone="warn">{summary.pending_count} awaiting approval</Badge>}
          {typeof summary.alias_count === "number" && summary.alias_count > 1 && <Badge tone="neutral">{summary.alias_count} file names</Badge>}
          {summary.has_type_conflict === true && <Badge tone="warn">File name type conflict</Badge>}
          {summary.status !== "extracted" && <Badge tone="warn">{label(summary.status)}</Badge>}
          <span>Extractor {summary.extractor_version}</span>
          <span>{dateTime(summary.created_at)}</span>
        </span>
      </button>

      <div id={panelId} hidden={!open}>
        {open && detail.isPending && (
          <div className="border-t p-4">
            <LoadingBlock rows={4} label="Loading extracted fields" />
          </div>
        )}
        {open && detail.isError && (
          <div className="border-t p-4">
            <ErrorState error={detail.error} title="The extracted fields could not be loaded" onRetry={() => detail.refetch()} />
          </div>
        )}
        {open && detail.data && <ExtractionBody detail={detail.data} canPropose={canPropose} onPropose={setProposing} />}
      </div>

      {proposing && detail.data && <CorrectionDialog detail={detail.data} field={proposing} onClose={() => setProposing(null)} />}
    </Card>
  )
}

function NotExtracted({ documents, heading }: { documents: DocumentFile[]; heading?: boolean }) {
  const session = useSession()
  const toast = useToast()
  const run = useRunExtraction()
  const [working, setWorking] = useState<string | null>(null)
  const canRun = can(session.data, "extraction.run")
  const blocked = useStoreWriteBlock()

  if (documents.length === 0) return null

  const extract = (name: string) => {
    if (working) return
    setWorking(name)
    run.mutate(name, {
      onSuccess: (result) => toast.success(`${name}: ${result.complete ? "extracted" : "extracted, needs review"}.`),
      onError: (error) => toast.error(errorMessage(error)),
      onSettled: () => setWorking(null),
    })
  }

  return (
    <Card className="divide-y">
      {heading && (
        <div className="px-4 py-4">
          <h3 className="text-[15px] font-semibold">No extraction stored for this shipment</h3>
          <p className="mt-1 text-[13px] text-muted-foreground">Extracting a document reads its fields into typed values with their source. Nothing is changed by viewing them.</p>
        </div>
      )}
      {documents.map((file) => (
        <div key={file.filename} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-sm">
          <span className="min-w-0 break-all">
            {file.filename} <span className="text-muted-foreground">has no stored extraction.</span>
          </span>
          {canRun ? (
            <Button variant="outline" size="sm" disabled={working !== null || blocked !== null} title={blocked ?? undefined} onClick={() => extract(file.filename)} aria-label={`Extract fields from ${file.filename}`}>
              {working === file.filename ? "Extracting…" : "Extract fields"}
            </Button>
          ) : (
            <span className="text-xs text-muted-foreground">An administrator can extract it.</span>
          )}
        </div>
      ))}
    </Card>
  )
}

const SEVERITY_TONE = { error: "bad", warning: "warn", info: "info" } as const
const SEVERITY_LABEL = { error: "Error", warning: "Warning", info: "Note" } as const

/** What the stored extractions of the shipment's documents say when compared with each other */
function CrossDocumentFindings({ issues }: { issues: CrossDocumentIssue[] }) {
  if (issues.length === 0) return null

  const order = ["error", "warning", "info"]
  const sorted = [...issues].sort((a, b) => order.indexOf(a.severity) - order.indexOf(b.severity))

  return (
    <div className="space-y-2" aria-label="Checks across documents">
      <h3 className="text-sm font-semibold">Checks across documents</h3>
      <p className="text-xs text-muted-foreground">Findings from comparing the extracted fields of this shipment's documents. They are reports only: no value is chosen or changed.</p>
      <ul className="space-y-2">
        {sorted.map((issue, index) => (
          <li key={`${issue.code}-${index}`}>
            <Alert tone={SEVERITY_TONE[issue.severity] ?? "info"}>
              <p className="text-sm">
                <span className="font-semibold">{SEVERITY_LABEL[issue.severity] ?? issue.severity}: </span>
                {issue.message}
              </p>
              {issue.sources.length > 0 && <p className="mt-1 text-xs">Documents: {issue.sources.join(", ")}</p>}
            </Alert>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * The typed fields read from each document of a shipment, where each came from,
 * and what has been proposed, approved or rejected for it. Nothing here changes
 * an audit: a proposal waits for a different administrator.
 */
export function ExtractionsSection({ shipmentId, shipmentKey, documents }: { shipmentId: string; shipmentKey: string; documents: DocumentFile[] }) {
  const session = useSession()
  const canView = can(session.data, "extraction.view")
  const canPropose = can(session.data, "extraction.correct")
  const list = useExtractions(shipmentId, canView)
  const staleness = useStaleness(can(session.data, "history.view"))
  const unavailable = staleness.data?.corrections_available === false

  let body: ReactNode

  if (session.isPending || (canView && list.isPending)) {
    body = <LoadingBlock rows={3} label="Loading extractions" />
  } else if (!canView) {
    body = <Alert tone="info"><p>Extractions are available to reviewers and administrators.</p></Alert>
  } else if (list.isError) {
    body = unavailable ? null : <ErrorState error={list.error} title="The extractions could not be loaded" onRetry={() => list.refetch()} />
  } else {
    const rows = list.data?.extractions ?? []
    const names = new Set(rows.map((item) => item.filename))
    const missing = documents.filter((file) => !names.has(file.filename))

    body = (
      <div className="space-y-4">
        <CrossDocumentFindings issues={list.data?.cross_document_issues ?? []} />
        {rows.length === 0 && missing.length === 0 && (
          <EmptyState title="No extraction stored for this shipment">Extracting a document reads its fields into typed values with their source. Nothing is changed by viewing them.</EmptyState>
        )}
        {rows.map((summary, index) => (
          <ExtractionCard key={summary.id} summary={summary} startOpen={index === 0} canPropose={canPropose} />
        ))}
        <NotExtracted documents={missing} heading={rows.length === 0} />
      </div>
    )
  }

  return (
    <Section
      title="Extractions"
      description="Typed fields read from each document, with where each came from. A correction is only a proposal until a different administrator approves it."
      actions={
        can(session.data, "extraction.approve") ? (
          <Button variant="outline" size="sm" asChild>
            <Link to="/corrections">Correction approvals</Link>
          </Button>
        ) : undefined
      }
    >
      <div className="space-y-4">
        <CorrectionStoreWarning />
        <StoreStateNotice />
        <RecomputeStatus shipmentKey={shipmentKey} />
        {body}
      </div>
    </Section>
  )
}
