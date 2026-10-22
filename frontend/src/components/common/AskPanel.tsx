import { MessageSquare, Send, Sparkles } from "lucide-react"
import { useState, type FormEvent } from "react"
import { errorMessage } from "@/components/common/states"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input, Label } from "@/components/ui/input"
import { useChat } from "@/hooks/queries"

/**
 * Ask a question about the documents (the existing RAG with Ollama). The
 * answer is generated text grounded in the evidence shown beneath it. It is
 * labelled as such and is never mixed into the verified audit findings.
 */
export function AskPanel({ shipmentId }: { shipmentId?: string }) {
  const chat = useChat()
  const [question, setQuestion] = useState("")

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const text = question.trim()
    if (text) chat.mutate({ question: text, shipment_id: shipmentId })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MessageSquare className="size-4 text-muted-foreground" aria-hidden="true" /> Ask the documents
        </CardTitle>
        <CardDescription>{shipmentId ? `Answers use only the documents of ${shipmentId}.` : "Answers search every shipment's documents unless you name one."}</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1 space-y-1.5">
            <Label htmlFor={`ask-${shipmentId ?? "all"}`}>Your question</Label>
            <Input id={`ask-${shipmentId ?? "all"}`} value={question} maxLength={1000} onChange={(event) => setQuestion(event.target.value)} placeholder="What is the invoice total?" />
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
              <p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <Sparkles className="size-3.5" aria-hidden="true" /> Generated answer
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
