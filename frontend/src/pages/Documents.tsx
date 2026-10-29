import { ExternalLink, FileText, RotateCcw, Trash2, Upload as UploadIcon } from "lucide-react"
import { useRef, useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { AskPanel } from "@/components/common/AskPanel"
import { ProcessingTab } from "@/components/documents/ProcessingTrace"
import { PageHeader, Section } from "@/components/common/PageHeader"
import { StatusBadge } from "@/components/common/StatusBadge"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { useToast } from "@/components/common/toast"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input, Label, Select, Textarea } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs } from "@/components/ui/tabs"
import { can, useDocuments, useIngestion, useIngestionErrors, useRemove, useReplace, useReprocess, useResolveError, useRetry, useSession, useUpload } from "@/hooks/queries"
import { errorMessage } from "@/components/common/states"
import { dateTime, NA } from "@/lib/format"
import type { DocumentFile, UploadSummary } from "@/lib/types"

const AUDIT_LABEL: Record<string, string> = { quantity: "Quantity", delivery: "Delivery", invoice: "Invoice" }

/** The Processing tab, following one stored document */
const processingLink = (filename: string) => `/documents?tab=processing&file=${encodeURIComponent(filename)}`

function formatSize(bytes: number): string {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function References({ file }: { file: DocumentFile }) {
  if (file.references.length === 0) return <span className="text-xs text-muted-foreground">Not cited by a finding</span>

  return (
    <details className="text-xs">
      <summary className="cursor-pointer whitespace-nowrap font-medium text-primary">Cited {file.references.length}×</summary>
      <ul className="mt-1.5 max-w-md space-y-1">
        {file.references.map((reference, index) => (
          <li key={index} className="rounded bg-muted/70 px-2 py-1">
            <span className="font-medium">
              {reference.shipment_id} · {AUDIT_LABEL[reference.audit] ?? reference.audit}
              {reference.page ? ` · p.${reference.page}` : ""}
            </span>
            {reference.text && <span className="block text-muted-foreground">{reference.text}</span>}
          </li>
        ))}
      </ul>
    </details>
  )
}

function DocumentsTab() {
  const documents = useDocuments()

  if (documents.isPending) return <LoadingBlock rows={6} label="Loading documents" />
  if (documents.isError) return <ErrorState error={documents.error} onRetry={() => documents.refetch()} />

  const { files, completeness } = documents.data

  if (files.length === 0) return <EmptyState title="No documents stored">Upload PDFs in the Ingestion tab. Each one is checked, indexed and recorded.</EmptyState>

  return (
    <>
      <Table>
        <TableHeader>
          <tr>
            <TableHead>Document</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Shipment</TableHead>
            <TableHead>Extraction</TableHead>
            <TableHead>Pages with text</TableHead>
            <TableHead>Size</TableHead>
            <TableHead>Evidence</TableHead>
            <TableHead>Ingestion</TableHead>
          </tr>
        </TableHeader>
        <TableBody>
          {files.map((file) => (
            <TableRow key={file.filename}>
              <TableCell className="max-w-[16rem]">
                <a href={`/api/documents/file?name=${encodeURIComponent(file.filename)}`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 font-medium text-primary hover:underline">
                  <FileText className="size-4 shrink-0" aria-hidden="true" />
                  <span className="truncate">{file.filename}</span>
                  <ExternalLink className="size-3 shrink-0" aria-label="opens the PDF in a new tab" />
                </a>
                <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">sha256 {file.sha256}…</p>
                <Link to={processingLink(file.filename)} className="mt-0.5 inline-block text-xs font-medium text-primary hover:underline">
                  See how it was processed
                </Link>
              </TableCell>
              <TableCell>{file.document_type ?? NA}</TableCell>
              <TableCell>
                {file.shipment_id ? (
                  <Link to={`/shipments/${encodeURIComponent(file.shipment_id)}`} className="whitespace-nowrap text-primary hover:underline">
                    {file.shipment_id}
                  </Link>
                ) : (
                  <span className="text-muted-foreground">none found</span>
                )}
              </TableCell>
              <TableCell>
                <StatusBadge status={file.status} />
                {(file.error || file.text_pages !== file.total_pages) && <p className="mt-1 max-w-[14rem] text-xs text-muted-foreground">{file.error ?? file.status_text}</p>}
              </TableCell>
              <TableCell className="tabular whitespace-nowrap">
                {file.text_pages ?? NA} of {file.total_pages ?? NA}
              </TableCell>
              <TableCell className="tabular whitespace-nowrap">{formatSize(file.size)}</TableCell>
              <TableCell>
                <References file={file} />
              </TableCell>
              <TableCell>{file.ingestion ? <StatusBadge status={file.ingestion.status} /> : <span className="text-xs text-muted-foreground">added outside the register</span>}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      {(completeness.unusable.length > 0 || completeness.unidentified.length > 0 || completeness.duplicates.length > 0) && (
        <div className="mt-5 space-y-2">
          {completeness.unusable.map((item) => (
            <Alert key={item.filename} tone="bad">
              <p>
                <span className="font-medium">{item.filename}</span> cannot be used: {item.message} It belongs to no shipment, and no shipment is reported clean while it is open.
              </p>
            </Alert>
          ))}
          {completeness.unidentified.map((item) => (
            <Alert key={item.filename} tone="warn">
              <p>
                <span className="font-medium">{item.filename}</span>: {item.message}
              </p>
            </Alert>
          ))}
          {completeness.duplicates.map((group) => (
            <Alert key={group.join("|")} tone="info">
              <p>Identical content stored as: {group.join(", ")}.</p>
            </Alert>
          ))}
        </div>
      )}
    </>
  )
}

function UploadResult({ result }: { result: UploadSummary }) {
  return (
    <div className="mt-4 space-y-2" aria-live="polite">
      <p className="text-sm font-medium">
        {result.indexed} indexed · {result.partial} partial · {result.duplicates} duplicate · {result.failed + result.unsupported} not ingested
      </p>
      <ul className="space-y-1.5">
        {result.files.map((file) => (
          <li key={file.id} className="rounded-md border bg-card p-2.5 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={file.status} />
              <span className="font-medium">{file.filename}</span>
              {file.shipment_id && <span className="text-xs text-muted-foreground">{file.shipment_id}</span>}
            </div>
            {file.message && <p className="mt-1 text-xs text-muted-foreground">{file.message}</p>}
            {file.action && <p className="mt-0.5 text-xs">Next step: {file.action}</p>}
            {(file.status === "indexed" || file.status === "partial") && (
              <Link to={processingLink(file.filename)} className="mt-1 inline-block text-xs font-medium text-primary hover:underline">
                See how it was processed
              </Link>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

function UploadCard() {
  const toast = useToast()
  const upload = useUpload()
  const input = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [result, setResult] = useState<UploadSummary | null>(null)

  const submit = () => {
    upload.mutate(files, {
      onSuccess: (summary) => {
        setResult(summary)
        setFiles([])
        if (input.current) input.current.value = ""
        toast.success(`${summary.indexed + summary.partial} file(s) added; ${summary.failed + summary.unsupported} not ingested.`)
      },
      onError: (error) => toast.error(errorMessage(error)),
    })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <UploadIcon className="size-4 text-muted-foreground" aria-hidden="true" /> Add documents
        </CardTitle>
        <CardDescription>
          PDFs only. Every file is checked, hashed to catch duplicates, indexed and recorded; transient failures are retried. Originals are never changed or deleted. Image-only scans are reported as needing OCR.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-1.5">
          <Label htmlFor="upload-files">PDF files (up to 20)</Label>
          <Input id="upload-files" ref={input} type="file" accept="application/pdf,.pdf" multiple onChange={(event) => setFiles(Array.from(event.target.files ?? []))} className="h-auto cursor-pointer py-2" />
        </div>
        {files.length > 0 && (
          <p className="mt-2 text-xs text-muted-foreground">
            {files.length} file{files.length === 1 ? "" : "s"} selected: {files.map((file) => file.name).join(", ")}
          </p>
        )}
        <Button className="mt-4" onClick={submit} disabled={files.length === 0 || upload.isPending}>
          <UploadIcon /> {upload.isPending ? "Processing…" : "Process documents"}
        </Button>
        {result && <UploadResult result={result} />}
      </CardContent>
    </Card>
  )
}

function ErrorsCard({ writable }: { writable: boolean }) {
  const toast = useToast()
  const errors = useIngestionErrors()
  const retry = useRetry()
  const resolve = useResolveError()
  const [resolving, setResolving] = useState<number | null>(null)
  const [note, setNote] = useState("")

  if (errors.isPending) return <LoadingBlock rows={2} label="Loading errors" />
  if (errors.isError) return <ErrorState error={errors.error} onRetry={() => errors.refetch()} />

  if (errors.data.length === 0) {
    return (
      <Alert tone="ok">
        <p>No open ingestion errors.</p>
      </Alert>
    )
  }

  return (
    <div className="space-y-3">
      <Alert tone="warn">
        <p>Until each error below is corrected or resolved, no shipment is reported clean: a file that could not be ingested might belong to it.</p>
      </Alert>

      {errors.data.map((record) => (
        <Card key={record.id} className="p-4">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={record.status} />
            <span className="font-medium">{record.filename}</span>
            <span className="text-xs text-muted-foreground">
              {record.attempts} attempt{record.attempts === 1 ? "" : "s"} · {dateTime(record.updated_at)}
            </span>
          </div>
          <p className="mt-2 text-sm">{record.message}</p>
          {record.action && <p className="mt-1 text-sm text-muted-foreground">What to do: {record.action}</p>}

          {writable && (
            <div className="mt-3 flex flex-wrap gap-2">
              {record.can_retry && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={retry.isPending}
                  onClick={() => retry.mutate(record.id, { onSuccess: (summary) => toast.success(`Retry: ${summary.files[0]?.status}`), onError: (error) => toast.error(errorMessage(error)) })}
                >
                  <RotateCcw /> Retry
                </Button>
              )}
              <Button size="sm" variant="outline" onClick={() => { setResolving(record.id); setNote("") }}>
                Mark as resolved
              </Button>
            </div>
          )}
        </Card>
      ))}

      <Dialog open={resolving !== null} onOpenChange={(open) => !open && setResolving(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Resolve this ingestion error</DialogTitle>
            <DialogDescription>Say what was done, for example that a corrected copy was uploaded under another name, or why the file does not matter. This is recorded with your name.</DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="resolve-note">What was done (at least 10 characters)</Label>
            <Textarea id="resolve-note" value={note} onChange={(event) => setNote(event.target.value)} />
          </div>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">Cancel</Button>
            </DialogClose>
            <Button
              disabled={note.trim().length < 10 || resolve.isPending}
              onClick={() =>
                resolving !== null &&
                resolve.mutate(
                  { id: resolving, note },
                  { onSuccess: () => { toast.success("Error resolved"); setResolving(null) }, onError: (error) => toast.error(errorMessage(error)) },
                )
              }
            >
              Resolve
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function StoredActions() {
  const toast = useToast()
  const documents = useDocuments()
  const reprocess = useReprocess()
  const replace = useReplace()
  const remove = useRemove()
  const [name, setName] = useState("")
  const [corrected, setCorrected] = useState<File | null>(null)
  const [confirm, setConfirm] = useState<"replace" | "remove" | null>(null)

  const files = documents.data?.files ?? []
  const target = name || files[0]?.filename || ""

  if (files.length === 0) return <p className="text-sm text-muted-foreground">No documents are stored yet.</p>

  const done = (message: string) => () => {
    toast.success(message)
    setConfirm(null)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Change a stored document</CardTitle>
        <CardDescription>Replaced and removed originals are copied to the archive folder first and verified. They are never deleted. Each action is logged with your name.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="max-w-md space-y-1.5">
          <Label htmlFor="stored-target">Stored document</Label>
          <Select id="stored-target" value={target} onChange={(event) => setName(event.target.value)}>
            {files.map((file) => (
              <option key={file.filename} value={file.filename}>
                {file.filename}
              </option>
            ))}
          </Select>
        </div>

        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={reprocess.isPending}
            onClick={() => reprocess.mutate(target, { onSuccess: (result) => toast.success(result.action === "already_indexed" ? "Already indexed; nothing was written." : `Indexed ${result.chunks} chunk(s).`), onError: (error) => toast.error(errorMessage(error)) })}
          >
            <RotateCcw /> Reprocess
          </Button>
          <Button variant="outline" size="sm" onClick={() => setConfirm("replace")}>
            Replace…
          </Button>
          <Button variant="outline" size="sm" className="text-destructive" onClick={() => setConfirm("remove")}>
            <Trash2 /> Remove…
          </Button>
        </div>

        <Dialog open={confirm === "replace"} onOpenChange={(open) => !open && setConfirm(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Replace {target}</DialogTitle>
              <DialogDescription>The current PDF is archived first. The corrected PDF is checked and indexed before the swap; if anything fails, the old document is restored.</DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5">
              <Label htmlFor="replace-file">Corrected PDF</Label>
              <Input id="replace-file" type="file" accept="application/pdf,.pdf" className="h-auto py-2" onChange={(event) => setCorrected(event.target.files?.[0] ?? null)} />
            </div>
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline">Cancel</Button>
              </DialogClose>
              <Button disabled={!corrected || replace.isPending} onClick={() => corrected && replace.mutate({ name: target, file: corrected }, { onSuccess: done(`Replaced ${target}`), onError: (error) => toast.error(errorMessage(error)) })}>
                {replace.isPending ? "Replacing…" : "Replace document"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        <Dialog open={confirm === "remove"} onOpenChange={(open) => !open && setConfirm(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Remove {target}?</DialogTitle>
              <DialogDescription>Its vectors are removed from the search index and the PDF is moved to the archive folder. Shipments that relied on it will show the missing document.</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline">Keep it</Button>
              </DialogClose>
              <Button variant="destructive" disabled={remove.isPending} onClick={() => remove.mutate(target, { onSuccess: done(`Removed ${target}`), onError: (error) => toast.error(errorMessage(error)) })}>
                {remove.isPending ? "Removing…" : "Remove document"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  )
}

function IngestionTab() {
  const session = useSession()
  const writable = can(session.data, "ingestion.write")
  const [status, setStatus] = useState("")
  const [search, setSearch] = useState("")
  const register = useIngestion(status ? [status] : [], search)

  return (
    <div className="space-y-8">
      {writable ? <UploadCard /> : <Alert tone="info"><p>Only administrators can add, replace or remove documents. You can follow the register below.</p></Alert>}

      <Section title="Errors and corrective actions">
        <ErrorsCard writable={writable} />
      </Section>

      {writable && (
        <Section title="Stored documents">
          <StoredActions />
        </Section>
      )}

      <Section title="Register" description="The latest status of every file offered for ingestion.">
        <div className="mb-3 flex flex-wrap gap-3">
          <div className="w-48">
            <Label htmlFor="register-status" className="sr-only">Status</Label>
            <Select id="register-status" value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="">All statuses</option>
              {["queued", "processing", "indexed", "partial", "failed", "unsupported"].map((item) => (
                <option key={item} value={item}>{item.charAt(0).toUpperCase() + item.slice(1)}</option>
              ))}
            </Select>
          </div>
          <div className="w-64">
            <Label htmlFor="register-search" className="sr-only">Search</Label>
            <Input id="register-search" placeholder="Search file or shipment" value={search} onChange={(event) => setSearch(event.target.value)} />
          </div>
          {register.data && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              {Object.entries(register.data.summary).map(([key, value]) => (
                <Badge key={key}>{key.replace("_", " ")}: {value}</Badge>
              ))}
            </div>
          )}
        </div>

        {register.isPending && <LoadingBlock rows={3} label="Loading register" />}
        {register.isError && <ErrorState error={register.error} onRetry={() => register.refetch()} />}
        {register.data && register.data.files.length === 0 && <EmptyState title="Nothing in the register">Files appear here as soon as they are offered for ingestion.</EmptyState>}
        {register.data && register.data.files.length > 0 && (
          <Table>
            <TableHeader>
              <tr>
                <TableHead>File</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Reason</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Shipment</TableHead>
                <TableHead>Attempts</TableHead>
                <TableHead>By</TableHead>
                <TableHead>Updated</TableHead>
              </tr>
            </TableHeader>
            <TableBody>
              {register.data.files.map((record) => (
                <TableRow key={record.id}>
                  <TableCell className="font-medium">{record.filename}</TableCell>
                  <TableCell>
                    <StatusBadge status={record.status} />
                  </TableCell>
                  <TableCell className="max-w-xs text-xs text-muted-foreground">{record.message ?? ""}</TableCell>
                  <TableCell>{record.document_type ?? NA}</TableCell>
                  <TableCell>{record.shipment_id ?? NA}</TableCell>
                  <TableCell className="tabular">{record.attempts}</TableCell>
                  <TableCell>{record.actor ?? NA}</TableCell>
                  <TableCell className="whitespace-nowrap text-xs">{dateTime(record.updated_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>
    </div>
  )
}

const TABS = ["documents", "ingestion", "processing", "ask"]

export function Documents() {
  // The tab (and the document followed in Processing) live in the address, so other screens can link to them
  const [params, setParams] = useSearchParams()
  const tab = TABS.includes(params.get("tab") ?? "") ? (params.get("tab") as string) : "documents"
  const file = params.get("file")

  return (
    <>
      <PageHeader title="Document Intelligence" description="Every stored document with what extraction found, the findings that cite it, and how it entered the system." />

      <Tabs
        label="Document views"
        value={tab}
        onChange={(next) => setParams(next === "documents" ? {} : { tab: next }, { replace: true })}
        items={[
          { id: "documents", label: "Documents" },
          { id: "ingestion", label: "Ingestion" },
          { id: "processing", label: "How it was processed" },
          { id: "ask", label: "Ask the documents" },
        ]}
      >
        {tab === "documents" && <DocumentsTab />}
        {tab === "ingestion" && <IngestionTab />}
        {tab === "processing" && <ProcessingTab file={file} onFile={(name) => setParams({ tab: "processing", file: name }, { replace: true })} />}
        {tab === "ask" && <AskPanel />}
      </Tabs>
    </>
  )
}
