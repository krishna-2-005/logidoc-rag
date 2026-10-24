import { Info } from "lucide-react"
import type { ReactNode } from "react"
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
  className?: string
}

const accent = { default: "text-primary bg-accent", bad: "text-bad bg-bad-soft", warn: "text-warn bg-warn-soft", ok: "text-ok bg-ok-soft" }

export function KpiCard({ title, value, detail, definition, icon, tone = "default", className }: KpiCardProps) {
  return (
    <Card className={cn("p-5", className)}>
      <div className="flex min-h-8 items-center justify-between gap-3">
        <div className="flex items-center gap-1.5 text-[13px] font-medium text-muted-foreground">
          {title}
          {definition && (
            <Tooltip content={definition}>
              <button type="button" aria-label={`What "${title}" means`} className="rounded-full text-muted-foreground hover:text-foreground">
                <Info className="size-3.5" />
              </button>
            </Tooltip>
          )}
        </div>
        {icon && (
          <span aria-hidden="true" className={cn("grid size-8 place-items-center rounded-lg [&_svg]:size-4", accent[tone])}>
            {icon}
          </span>
        )}
      </div>
      <p className="tabular mt-3 text-[28px] font-semibold leading-none tracking-tight" data-testid="kpi-value">
        {value}
      </p>
      {detail && <p className="mt-2.5 text-[13px] text-muted-foreground">{detail}</p>}
    </Card>
  )
}
