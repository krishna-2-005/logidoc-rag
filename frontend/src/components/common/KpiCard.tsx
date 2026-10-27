import { ArrowRight, Info } from "lucide-react"
import type { ReactNode } from "react"
import { Link } from "react-router-dom"
import { Card } from "@/components/ui/card"
import { Tooltip } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

interface KpiCardProps {
  title: string
  /** Already formatted: a missing figure is shown as "n/a", never 0. */
  value: string
  detail?: ReactNode
  /** What the figure means, shown on hover and focus of the info icon. */
  definition?: string
  icon?: ReactNode
  tone?: "default" | "bad" | "warn" | "ok"
  /** Where the records behind the figure are listed; the whole card then opens that page. */
  to?: string
  /** What the card opens, shown under the figure ("View invoices") */
  cta?: string
  className?: string
}

const accent = { default: "text-primary bg-accent", bad: "text-bad bg-bad-soft", warn: "text-warn bg-warn-soft", ok: "text-ok bg-ok-soft" }

export function KpiCard({ title, value, detail, definition, icon, tone = "default", to, cta = "View details", className }: KpiCardProps) {
  return (
    <Card
      className={cn(
        "group relative flex flex-col p-5 transition duration-150",
        to && "hover:-translate-y-0.5 hover:border-primary/35 hover:shadow-raised has-[a:focus-visible]:ring-2 has-[a:focus-visible]:ring-ring has-[a:focus-visible]:ring-offset-2",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-1.5 pt-1 text-[13px] font-medium text-muted-foreground">
          {to ? (
            // The title is the link; its ::after covers the card, so the whole card opens the page
            <Link to={to} className="truncate after:absolute after:inset-0 after:rounded-lg focus-visible:ring-0 focus-visible:ring-offset-0">
              {title}
            </Link>
          ) : (
            <span className="truncate">{title}</span>
          )}
          {definition && (
            <Tooltip content={definition}>
              <button type="button" aria-label={`What "${title}" means`} className="relative z-10 rounded-full text-muted-foreground hover:text-foreground">
                <Info className="size-3.5" />
              </button>
            </Tooltip>
          )}
        </div>
        {icon && (
          <span aria-hidden="true" className={cn("grid size-9 shrink-0 place-items-center rounded-xl [&_svg]:size-[18px]", accent[tone])}>
            {icon}
          </span>
        )}
      </div>
      <p className="tabular mt-2 text-[30px] font-semibold leading-none tracking-tight" data-testid="kpi-value">
        {value}
      </p>
      {detail && <p className="mt-2.5 text-[13px] leading-snug text-muted-foreground">{detail}</p>}
      {to && (
        <p aria-hidden="true" className="mt-auto flex items-center gap-1 pt-4 text-[12.5px] font-medium text-primary/80 transition-colors group-hover:text-primary">
          {cta}
          <ArrowRight className="size-3.5 transition-transform group-hover:translate-x-0.5" />
        </p>
      )}
    </Card>
  )
}
