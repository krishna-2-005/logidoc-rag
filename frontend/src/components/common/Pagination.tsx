import { ChevronLeft, ChevronRight } from "lucide-react"
import { Button } from "@/components/ui/button"

/** "Showing 26–50 of 112" with previous and next; the server pages, this only asks for another offset. */
export function Pagination({ offset, limit, total, onChange, label = "Pages" }: { offset: number; limit: number; total: number; onChange: (offset: number) => void; label?: string }) {
  if (total <= limit && offset === 0) return null

  const first = total === 0 ? 0 : offset + 1
  const last = Math.min(offset + limit, total)

  return (
    <nav aria-label={label} className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm">
      <p className="text-muted-foreground" aria-live="polite">
        Showing <span className="tabular font-medium text-foreground">{first}–{last}</span> of <span className="tabular font-medium text-foreground">{total.toLocaleString("en-US")}</span>
      </p>
      <div className="flex gap-2">
        <Button variant="outline" size="sm" onClick={() => onChange(Math.max(0, offset - limit))} disabled={offset === 0}>
          <ChevronLeft /> Previous
        </Button>
        <Button variant="outline" size="sm" onClick={() => onChange(offset + limit)} disabled={offset + limit >= total}>
          Next <ChevronRight />
        </Button>
      </div>
    </nav>
  )
}
