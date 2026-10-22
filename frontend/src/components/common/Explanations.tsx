import { ChevronDown } from "lucide-react"
import { StatusBadge } from "@/components/common/StatusBadge"
import { Card } from "@/components/ui/card"
import type { Explanation } from "@/lib/types"

function Fact({ term, children }: { term: string; children: string | null }) {
  if (!children) return null

  return (
    <div className="grid gap-0.5 sm:grid-cols-[9rem_1fr] sm:gap-3">
      <dt className="text-xs font-medium text-muted-foreground">{term}</dt>
      <dd className="text-sm">{children}</dd>
    </div>
  )
}

/**
 * What the audit found and why, written from the audited values and their
 * source documents. These explanations are deterministic: no language model
 * wrote any of this text.
 */
export function Explanations({ items }: { items: Explanation[] }) {
  if (items.length === 0) return <p className="text-sm text-muted-foreground">Nothing to explain: no finding for this shipment.</p>

  return (
    <div className="space-y-3">
      {items.map((item) => (
        <Card key={item.exception_key} className="overflow-hidden">
          <details className="group">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-3 p-4 hover:bg-muted/40">
              <span className="flex flex-wrap items-center gap-2">
                <StatusBadge status={item.severity} />
                <span className="text-sm font-medium">{item.category}</span>
                {item.difference_text && <span className="tabular text-sm text-muted-foreground">{item.difference_text}</span>}
              </span>
              <ChevronDown className="size-4 text-muted-foreground transition-transform group-open:rotate-180" aria-hidden="true" />
            </summary>

            <div className="space-y-4 border-t p-4">
              <p className="text-sm">{item.finding}</p>

              <dl className="space-y-2">
                <Fact term="Expected">{item.expected}</Fact>
                <Fact term="Observed">{item.observed}</Fact>
                <Fact term="Why it was flagged">{item.reason ?? item.rule}</Fact>
                <Fact term="Next step">{item.next_step}</Fact>
              </dl>

              {item.caveat && <p className="rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground">{item.caveat}</p>}

              <div>
                <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Source evidence</h4>
                {item.sources.length === 0 ? (
                  <p className="mt-1.5 text-xs text-muted-foreground">{item.evidence_note ?? "No source text is attached."}</p>
                ) : (
                  <ul className="mt-1.5 space-y-1.5">
                    {item.sources.map((source, index) => (
                      <li key={index} className="rounded-md bg-muted/70 px-2.5 py-1.5 text-xs">
                        <span className="font-medium">
                          {source.source}
                          {source.page ? ` · p.${source.page}` : ""} · {source.document_type} · {source.shipment_id}
                        </span>
                        <span className="block text-muted-foreground">{source.text}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {item.excluded_foreign_evidence > 0 && item.evidence_note && <p className="mt-1.5 text-xs text-muted-foreground">{item.evidence_note}</p>}
              </div>
            </div>
          </details>
        </Card>
      ))}
    </div>
  )
}
