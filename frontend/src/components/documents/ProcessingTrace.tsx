import { Binary, Database, FileCheck2, MessageSquareText, ScanText, Scissors, Tags, Terminal } from "lucide-react"
import type { ReactNode } from "react"
import { Link } from "react-router-dom"
import { StatusBadge } from "@/components/common/StatusBadge"
import { EmptyState, ErrorState, LoadingBlock } from "@/components/common/states"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Label, Select } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useDocuments, usePipeline } from "@/hooks/queries"
import { count, NA } from "@/lib/format"
import type { CutText, PipelineTrace } from "@/lib/types"
import { cn } from "@/lib/utils"

const ms = (value: number) => (value < 1 ? "under 1 ms" : `${value.toLocaleString("en-US", { maximumFractionDigits: 1 })} ms`)

function Step({ number, icon, title, summary, timing, children, last = false }: { number: number; icon: ReactNode; title: string; summary: ReactNode; timing?: string; children?: ReactNode; last?: boolean }) {
  return (
    <li className="relative flex gap-4 pb-9 last:pb-0">
      {!last && <span aria-hidden="true" className="absolute bottom-0 left-5 top-11 w-px bg-border" />}
      <span aria-hidden="true" className="relative z-10 grid size-10 shrink-0 place-items-center rounded-full bg-primary text-primary-foreground shadow-sm [&_svg]:size-[18px]">
        {icon}
      </span>
      <div className="min-w-0 flex-1 pt-1.5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-[15px] font-semibold">
            {number}. {title}
          </h3>
          {timing && <span className="tabular rounded-full bg-secondary px-2.5 py-0.5 text-xs text-muted-foreground">{timing}</span>}
        </div>
        <p className="mt-1 text-[13px] leading-relaxed text-muted-foreground">{summary}</p>
        {children && <div className="mt-3 space-y-3">{children}</div>}
      </div>
    </li>
  )
}

function TextBox({ label, value }: { label: string; value: CutText }) {
  return (
    <figure>
      <figcaption className="mb-1 flex justify-between text-xs text-muted-foreground">
        <span className="font-medium text-foreground">{label}</span>
        <span className="tabular">
          {count(value.characters)} characters{value.truncated ? " (first part shown)" : ""}
        </span>
      </figcaption>
      <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-lg border bg-muted/50 p-3 font-mono text-[12px] leading-relaxed text-foreground">{value.text}</pre>
    </figure>
  )
}

/** The first numbers of a stored embedding as a barcode (above zero up, below zero down), with the first eight written out */
function VectorPreview({ values, dimensions }: { values: number[]; dimensions: number }) {
  const largest = Math.max(...values.map(Math.abs), 1e-9)

  return (
    <div>
      <div role="img" aria-label={`First ${values.length} of ${dimensions} numbers: ${values.join(", ")}`} className="flex h-24 items-stretch gap-[3px] rounded-lg border bg-muted/40 px-3 py-2">
        {values.map((value, index) => (
          <div key={index} className="flex flex-1 flex-col">
            <div className="flex flex-1 items-end">{value > 0 && <span className="w-full rounded-t-sm bg-primary" style={{ height: `${Math.max((value / largest) * 100, 4)}%` }} />}</div>
            <div className="flex flex-1 items-start border-t border-foreground/20">
              {value < 0 && <span className="w-full rounded-b-sm bg-amber-500" style={{ height: `${Math.max((-value / largest) * 100, 4)}%` }} />}
            </div>
          </div>
        ))}
        <span className="self-center pl-1 text-lg text-muted-foreground" aria-hidden="true">
          …
        </span>
      </div>
      <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm bg-primary" aria-hidden="true" /> above zero
        </span>
        <span className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm bg-amber-500" aria-hidden="true" /> below zero
        </span>
        <span>
          first {values.length} of {count(dimensions)} numbers
        </span>
      </p>
      <p className="mt-1 break-all font-mono text-[12px] text-muted-foreground">
        [{values.slice(0, 8).map((value) => value.toFixed(4)).join(", ")}, … ] · {count(dimensions)} numbers
      </p>
    </div>
  )
}

const clock = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : null)

function Trace({ trace }: { trace: PipelineTrace }) {
  const { validation, extraction, classification, chunking, index, ingestion } = trace
  const vectors = index?.vectors ?? []
  const first = vectors[0]

  return (
    <ol className="mt-2">
      <Step
        number={1}
        icon={<FileCheck2 />}
        title="Upload and checks"
        timing={ms(validation.ms)}
        summary={
          <>
            {count(trace.size)} bytes received. The SHA-256 fingerprint identifies the exact content, so the same file is never indexed twice. The PDF is{" "}
            {validation.ok ? "valid" : "not usable"}: {validation.pages ?? NA} page(s), {validation.text_pages ?? NA} with text.
          </>
        }
      >
        <div className="flex flex-wrap items-center gap-2 text-[13px]">
          <code className="rounded bg-muted px-2 py-0.5 font-mono text-[12px]">SHA-256 {trace.sha256.slice(0, 16)}…</code>
          {ingestion?.status && <StatusBadge status={ingestion.status} />}
          {clock(ingestion?.received_at) && (
            <span className="text-muted-foreground">
              uploaded {clock(ingestion?.received_at)} · {ingestion?.status ?? "finished"} {clock(ingestion?.finished_at)}
            </span>
          )}
          {ingestion?.attempts ? <span className="text-muted-foreground">· {ingestion.attempts} attempt{ingestion.attempts === 1 ? "" : "s"}</span> : null}
        </div>
        {!validation.ok && <Alert tone="bad">This file was not processed further ({validation.code ?? "not usable"}).</Alert>}
      </Step>

      {extraction && (
        <Step
          number={2}
          icon={<ScanText />}
          title="Text extraction"
          timing={ms(extraction.ms)}
          summary={`${extraction.engine} reads the text layer of every page: ${count(extraction.characters)} characters from ${extraction.pages.length} page(s). This is the text everything else works on.`}
        >
          {extraction.pages.map((page) => (
            <TextBox key={page.page} label={`Page ${page.page}`} value={page} />
          ))}
        </Step>
      )}

      {classification && (
        <Step number={3} icon={<Tags />} title="Classification and identifiers" summary="The words of the text decide the document type, and the shipment number is read from the text, not from the file name.">
          <div className="flex flex-wrap gap-3">
            <div className="rounded-lg border bg-card px-4 py-2.5">
              <p className="text-xs text-muted-foreground">Document type</p>
              <p className="mt-0.5 text-lg font-semibold">{classification.document_type ?? NA}</p>
            </div>
            <div className="rounded-lg border bg-card px-4 py-2.5">
              <p className="text-xs text-muted-foreground">Shipment</p>
              <p className="mt-0.5 text-lg font-semibold">{classification.shipment_id ?? "not found"}</p>
            </div>
          </div>
          {Object.keys(classification.identifiers).length > 1 && (
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(classification.identifiers).map(([key, value]) => (
                <Badge key={key}>
                  {key.replace(/_/g, " ")}: {value}
                </Badge>
              ))}
            </div>
          )}
        </Step>
      )}

      {chunking && (
        <Step
          number={4}
          icon={<Scissors />}
          title="Chunking"
          timing={ms(chunking.ms)}
          summary={`The text is cut into pieces of up to ${count(chunking.chunk_size)} characters, overlapping by ${count(chunking.overlap)} so a sentence at a boundary is never lost. Each piece keeps its file, page and shipment: ${chunking.chunks.length} chunk(s) here.`}
        >
          {chunking.chunks.map((chunk) => (
            <TextBox key={`${chunk.page}-${chunk.chunk}`} label={`Chunk ${chunk.chunk + 1} · page ${chunk.page}`} value={chunk} />
          ))}
        </Step>
      )}

      {chunking && (
        <Step
          number={5}
          icon={<Binary />}
          title="Embedding"
          summary={
            first
              ? `${trace.embedding.model} turns each chunk into ${count(first.dimensions)} numbers that capture its meaning: texts that mean similar things get similar numbers. These are the numbers stored for chunk 1.`
              : `${trace.embedding.model} turns each chunk into a list of numbers that capture its meaning. No stored vector was found for this document.`
          }
        >
          {first && <VectorPreview values={first.preview} dimensions={first.dimensions} />}
        </Step>
      )}

      {chunking && (
        <Step
          number={6}
          icon={<Database />}
          title="Vector index"
          summary={
            index?.available
              ? `Stored in ChromaDB (collection "${index.collection}"): ${vectors.length} vector(s), each with the chunk text and its labels. Written, then read back to check.`
              : "The vector index could not be opened."
          }
        >
          {vectors.length > 0 && (
            <Card className="overflow-hidden">
              <Table>
                <TableHeader>
                  <tr>
                    <TableHead>Vector ID</TableHead>
                    <TableHead>Page</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Shipment</TableHead>
                    <TableHead>Source</TableHead>
                  </tr>
                </TableHeader>
                <TableBody>
                  {vectors.map((vector) => (
                    <TableRow key={vector.id}>
                      <TableCell className="max-w-[14rem] truncate font-mono text-[12px]" title={vector.id}>
                        {vector.id}
                      </TableCell>
                      <TableCell className="tabular">{vector.metadata.page ?? NA}</TableCell>
                      <TableCell>{vector.metadata.document_type ?? NA}</TableCell>
                      <TableCell>{vector.metadata.shipment_id ?? NA}</TableCell>
                      <TableCell className="max-w-[12rem] truncate">{vector.metadata.source ?? NA}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Card>
          )}
        </Step>
      )}

      {chunking && (
        <Step
          last
          number={7}
          icon={<MessageSquareText />}
          title="Ready for questions"
          summary={
            <>
              When someone asks about {classification?.shipment_id ? <span className="font-medium text-foreground">{classification.shipment_id}</span> : "this shipment"}, the question is embedded with the same model, the closest chunks of that
              shipment are found in the index, and the answer cites them.{" "}
              <Link to="/documents?tab=ask" className="font-medium text-primary hover:underline">
                Ask the documents
              </Link>
            </>
          }
        />
      )}
    </ol>
  )
}

/** Document Intelligence > Processing: every step one stored document went through, with what each step produced */
export function ProcessingTab({ file, onFile }: { file: string | null; onFile: (name: string) => void }) {
  const documents = useDocuments()
  const files = documents.data?.files ?? []
  const selected = file ?? files[0]?.filename
  const trace = usePipeline(selected)

  if (documents.isPending) return <LoadingBlock rows={4} label="Loading documents" />
  if (documents.isError) return <ErrorState error={documents.error} onRetry={() => documents.refetch()} />
  if (files.length === 0) return <EmptyState title="No documents stored">Upload a PDF in the Ingestion tab, then follow it here step by step.</EmptyState>

  return (
    <div className="space-y-6">
      <Card className="flex flex-wrap items-end justify-between gap-4 p-5">
        <div className="w-full max-w-md space-y-1.5">
          <Label htmlFor="pipeline-document">Document</Label>
          <Select id="pipeline-document" value={selected} onChange={(event) => onFile(event.target.value)}>
            {files.map((item) => (
              <option key={item.filename} value={item.filename}>
                {item.filename}
                {item.shipment_id ? ` · ${item.shipment_id}` : ""}
              </option>
            ))}
          </Select>
        </div>
        <p className="flex max-w-md items-start gap-2 text-xs leading-relaxed text-muted-foreground">
          <Terminal className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          Checks, text and chunks are produced again here by the same code that ran at upload; the vectors are the ones stored in the index. During an upload, the server window prints each step as it happens.
        </p>
      </Card>

      {trace.isPending && <LoadingBlock rows={6} label="Loading the processing steps" />}
      {trace.isError && <ErrorState error={trace.error} onRetry={() => trace.refetch()} title="The processing steps could not be loaded" />}
      {trace.data && (
        <Card className={cn("p-6")}>
          <Trace trace={trace.data} />
        </Card>
      )}
    </div>
  )
}
