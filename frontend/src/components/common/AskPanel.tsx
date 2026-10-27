import { MessageSquare, Send, Sparkles } from "lucide-react"
import { useState, type FormEvent } from "react"
import { errorMessage } from "@/components/common/states"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input, Label, Select } from "@/components/ui/input"
import { useChat, useShipments } from "@/hooks/queries"

// Every shipment, for the scope list (the server decides which documents belong to each)
const ALL_SHIPMENTS = {}

/** All shipments or one of them; only shown where no shipment is fixed, so a shipment's own page asks for no list */
function ScopeSelect({ id, value, onChange }: { id: string; value: string; onChange: (value: string) => void }) {
  const shipments = useShipments(ALL_SHIPMENTS)

  return (
    <div className="space-y-1.5 sm:w-60">
      <Label htmlFor={id}>Shipments to search</Label>
      <Select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">All shipments</option>
        {shipments.data?.shipments.map((row) => (
          <option key={row.shipment_key} value={row.shipment_id}>
            {row.shipment_id}
          </option>
        ))}
      </Select>
    </div>
  )
}

/** The scope the server says it searched, e.g. "Shipment SHJ-2026-002 (selected)" */
function scopeLabel(scope: unknown): string | null {
  return scope && typeof scope === "object" && typeof (scope as { label?: unknown }).label === "string" ? (scope as { label: string }).label : null
}

/**
 * Ask a question about the documents (the existing RAG with Ollama). The
 * answer is generated text grounded in the evidence shown beneath it. It is
 * labelled as such and is never mixed into the verified audit findings.
 * Without a fixed shipment, the person chooses all shipments or one of them.
 */
export function AskPanel({ shipmentId }: { shipmentId?: string }) {
  const chat = useChat()
  const [question, setQuestion] = useState("")
  const [chosen, setChosen] = useState("")
  const scope = shipmentId ?? (chosen || undefined)
  const id = `ask-${shipmentId ?? "all"}`

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const text = question.trim()
    if (text) chat.mutate({ question: text, shipment_id: scope })
  }

  const choose = (value: string) => {
    setChosen(value)
    chat.reset()
  }

  const searched = chat.data ? scopeLabel(chat.data.scope) : null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MessageSquare className="size-4 text-muted-foreground" aria-hidden="true" /> Ask the documents
        </CardTitle>
        <CardDescription>{scope ? `Answers use only the documents of ${scope}.` : "Answers search every shipment's documents unless you name one."}</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-3 sm:flex-row sm:items-end">
          {!shipmentId && <ScopeSelect id={`${id}-scope`} value={chosen} onChange={choose} />}
          <div className="flex-1 space-y-1.5">
            <Label htmlFor={id}>Your question</Label>
            <Input id={id} value={question} maxLength={1000} onChange={(event) => setQuestion(event.target.value)} placeholder="What is the invoice total?" />
          </div>
          <Button type="submit" disabled={chat.isPending || !question.trim()}>
            <Send /> {chat.isPending ? "Asking…" : "Ask"}
          </Button>
        </form>

        {chat.isError && (
          <Alert tone="bad" className="mt-4">
            {errorMessage(chat.error)}
          </Alert>
        )}

        {chat.data && (
          <div className="mt-4 space-y-3" aria-live="polite">
            <div className="rounded-lg border border-dashed bg-muted/50 p-4">
              <p className="mb-2 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs font-medium text-muted-foreground">
                <Sparkles className="size-3.5" aria-hidden="true" /> Generated answer
                {searched && <span className="font-normal">· searched: {searched}</span>}
              </p>
              <p className="whitespace-pre-wrap text-sm">{chat.data.answer}</p>
            </div>
            <p className="text-xs text-muted-foreground">{chat.data.notice}</p>

            {chat.data.evidence.length > 0 && (
              <details className="text-xs">
                <summary className="cursor-pointer font-medium text-primary">Evidence behind the answer ({chat.data.evidence.length})</summary>
                <ul className="mt-2 space-y-1.5">
                  {chat.data.evidence.map((item, index) => (
                    <li key={index} className="rounded-md bg-muted/70 px-2.5 py-1.5">
                      <span className="font-medium">{String(item.source ?? "")}{item.page ? ` · p.${item.page}` : ""}</span>
                      {item.text ? <span className="block text-muted-foreground">{String(item.text)}</span> : null}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
