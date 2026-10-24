import type { ReactNode } from "react"
import { Tooltip } from "@/components/ui/tooltip"
import { CHART } from "@/lib/chart"
import { cn } from "@/lib/utils"

/**
 * Small, quiet charts built from HTML: every value is written next to its mark, so
 * the chart reads without hovering, prints, and is a list to a screen reader.
 * Marks are thin with a 2px surface gap between touching segments; text uses
 * text colours only, the colour lives on the mark (and a swatch in the legend).
 */

export interface Segment {
  key: string
  label: string
  value: number
  colour: string
}

const percent = (value: number, total: number) => (total > 0 ? Math.round((value / total) * 100) : 0)

export function Swatch({ colour, className }: { colour: string; className?: string }) {
  return <span aria-hidden="true" className={cn("inline-block size-2.5 shrink-0 rounded-[3px]", className)} style={{ background: colour }} />
}

/** A legend: swatch, label, and (optionally) the value. Always shown for two or more series. */
export function Legend({ items, className }: { items: { key: string; label: string; colour: string; value?: ReactNode }[]; className?: string }) {
  return (
    <ul className={cn("flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-muted-foreground", className)}>
      {items.map((item) => (
        <li key={item.key} className="flex items-center gap-1.5">
          <Swatch colour={item.colour} />
          <span>{item.label}</span>
          {item.value !== undefined && <span className="tabular font-medium text-foreground">{item.value}</span>}
        </li>
      ))}
    </ul>
  )
}

/**
 * Part of a whole: one bar split by state, then a row per state with its count and
 * share. Zero states are left out of the bar but kept in the list, so "0 open" is said.
 */
export function ProportionBar({ segments, unit, label }: { segments: Segment[]; unit: string; label: string }) {
  const total = segments.reduce((sum, item) => sum + item.value, 0)
  const drawn = segments.filter((item) => item.value > 0)

  return (
    <div>
      <div className="flex items-baseline gap-2">
        <span className="tabular text-3xl font-semibold tracking-tight">{total}</span>
        <span className="text-sm text-muted-foreground">{unit}</span>
      </div>

      <div role="img" aria-label={`${label}: ${segments.map((item) => `${item.value} ${item.label.toLowerCase()}`).join(", ")}`} className="mt-4 flex h-2.5 w-full gap-[2px] overflow-hidden rounded-full" style={{ background: CHART.track }}>
        {drawn.map((item) => (
          <Tooltip key={item.key} content={`${item.label}: ${item.value} (${percent(item.value, total)}%)`}>
            <span className="h-full first:rounded-l-full last:rounded-r-full transition-opacity hover:opacity-80" style={{ width: `${(item.value / total) * 100}%`, background: item.colour }} />
          </Tooltip>
        ))}
      </div>

      <ul className="mt-5 divide-y text-sm">
        {segments.map((item) => (
          <li key={item.key} className="flex items-center justify-between gap-3 py-2">
            <span className="flex min-w-0 items-center gap-2.5">
              <Swatch colour={item.colour} />
              <span className="truncate">{item.label}</span>
            </span>
            <span className="flex items-baseline gap-3">
              <span className="tabular font-medium">{item.value}</span>
              <span className="tabular w-10 text-right text-xs text-muted-foreground">{percent(item.value, total)}%</span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export interface BarRow {
  key: string
  label: ReactNode
  /** The plain-text name, for the tooltip and the accessible label */
  name: string
  /** One segment for a simple bar; several for a stacked one (drawn in order) */
  parts: Segment[]
  /** Already formatted total shown at the end of the row */
  display: string
  /** Shown under the label in muted text */
  note?: ReactNode
}

/**
 * Ranked horizontal bars: label and value on one line, a thin bar under it, scaled
 * to the largest row. Long names wrap instead of being cut. Stacked rows keep a 2px
 * gap between their parts.
 */
export function BarList({ rows, label, max }: { rows: BarRow[]; label: string; max?: number }) {
  const largest = max ?? Math.max(0, ...rows.map((row) => row.parts.reduce((sum, part) => sum + Math.max(part.value, 0), 0)))

  return (
    <ul aria-label={label} className="space-y-4">
      {rows.map((row) => {
        const total = row.parts.reduce((sum, part) => sum + Math.max(part.value, 0), 0)
        const drawn = row.parts.filter((part) => part.value > 0)

        return (
          <li key={row.key}>
            <div className="flex items-baseline justify-between gap-3 text-sm">
              <span className="min-w-0 break-words">{row.label}</span>
              <span className="tabular shrink-0 font-medium">{row.display}</span>
            </div>
            {row.note && <p className="text-xs text-muted-foreground">{row.note}</p>}
            <div className="mt-1.5 h-2 w-full rounded-full" style={{ background: CHART.track }} aria-hidden="true">
              <div className="flex h-full gap-[2px]" style={{ width: largest > 0 ? `${(total / largest) * 100}%` : "0%" }}>
                {drawn.map((part) => (
                  <Tooltip key={part.key} content={`${row.name} · ${part.label}: ${part.value}`}>
                    <span
                      className="h-full first:rounded-l-full last:rounded-r-full transition-opacity hover:opacity-80"
                      style={{ width: `${(part.value / total) * 100}%`, background: part.colour, minWidth: 4 }}
                    />
                  </Tooltip>
                ))}
              </div>
            </div>
          </li>
        )
      })}
    </ul>
  )
}
