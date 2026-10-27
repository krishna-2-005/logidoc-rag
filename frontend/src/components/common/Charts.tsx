import type { ReactNode } from "react"
import { Bar, BarChart, CartesianGrid, Cell, LabelList, Pie, PieChart, ReferenceLine, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts"
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

const formatted = (value: unknown, format: (value: number) => string) => (typeof value === "number" ? format(value) : "n/a")

/** The hover card of the SVG charts: the category, then every series with its exact value. */
function ChartTip({ active, payload, label, format }: { active?: boolean; payload?: { name?: string; value?: unknown; color?: string; payload?: { colour?: string } }[]; label?: ReactNode; format: (value: number) => string }) {
  if (!active || !payload?.length) return null

  return (
    <div className="rounded-md border bg-card px-3 py-2 text-xs shadow-md">
      {label !== undefined && label !== "" && <p className="mb-1 font-medium text-foreground">{label}</p>}
      <ul className="space-y-0.5">
        {payload.map((item, index) => (
          <li key={index} className="flex items-center gap-2 text-muted-foreground">
            <Swatch colour={item.payload?.colour ?? item.color ?? CHART.neutral} />
            <span>{item.name}</span>
            <span className="tabular ml-auto pl-3 font-medium text-foreground">{formatted(item.value, format)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * Part of a whole as a ring, the total in its middle and every state listed beside
 * it with its count and share (zero states are listed, not drawn). With onSelect,
 * each listed state is a button (for example to open the matching list).
 */
export function Donut({ segments, unit, label, onSelect, selectHint }: { segments: Segment[]; unit: string; label: string; onSelect?: (key: string) => void; selectHint?: string }) {
  const total = segments.reduce((sum, item) => sum + item.value, 0)
  const drawn = segments.filter((item) => item.value > 0)
  const ring = drawn.length > 0 ? drawn : [{ key: "none", label: "None", value: 1, colour: CHART.track }]

  return (
    <div className="grid items-center gap-6 sm:grid-cols-[176px_minmax(0,1fr)]">
      <div role="img" aria-label={`${label}: ${segments.map((item) => `${item.value} ${item.label.toLowerCase()}`).join(", ")}`} className="relative mx-auto size-[176px]">
        <PieChart width={176} height={176}>
          <Pie data={ring} dataKey="value" nameKey="label" innerRadius={60} outerRadius={84} paddingAngle={drawn.length > 1 ? 2 : 0} stroke="none" isAnimationActive={false}>
            {ring.map((item) => (
              <Cell key={item.key} fill={item.colour} className={onSelect && drawn.length > 0 ? "cursor-pointer" : undefined} onClick={onSelect && drawn.length > 0 ? () => onSelect(item.key) : undefined} />
            ))}
          </Pie>
          {drawn.length > 0 && <ChartTooltip content={<ChartTip format={(value) => `${value} (${percent(value, total)}%)`} />} />}
        </PieChart>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="tabular text-3xl font-semibold tracking-tight">{total}</span>
          <span className="text-xs text-muted-foreground">{unit}</span>
        </div>
      </div>

      <ul className="divide-y text-sm">
        {segments.map((item) => {
          const content = (
            <>
              <span className="flex min-w-0 items-center gap-2.5">
                <Swatch colour={item.colour} />
                <span className="break-words leading-snug">{item.label}</span>
              </span>
              <span className="flex shrink-0 items-baseline gap-3">
                <span className="tabular font-medium">{item.value}</span>
                <span className="tabular w-10 text-right text-xs text-muted-foreground">{percent(item.value, total)}%</span>
              </span>
            </>
          )

          return (
            <li key={item.key}>
              {onSelect && item.value > 0 ? (
                <button type="button" onClick={() => onSelect(item.key)} title={selectHint} className="-mx-2 flex w-[calc(100%+1rem)] items-center justify-between gap-3 rounded-md px-2 py-2 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  {content}
                </button>
              ) : (
                <div className="flex items-center justify-between gap-3 py-2">{content}</div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

export interface Series {
  key: string
  label: string
  colour: string
}

/**
 * Bars from rows of data: grouped (one bar per series), stacked, or horizontal
 * (one row per category). Exact values are in the hover card and in a table for
 * screen readers; with labels, each bar also carries its value. colourOf colours
 * single-series bars one by one (for example by the sign of a difference).
 */
export function Columns<Row extends object>({
  data, category, series, label, format = (value) => String(value), stacked = false, horizontal = false, height = 240,
  integers = false, labels = false, zeroLine = false, colourOf,
}: {
  data: Row[]
  category: keyof Row & string
  series: Series[]
  label: string
  format?: (value: number) => string
  stacked?: boolean
  horizontal?: boolean
  height?: number
  integers?: boolean
  labels?: boolean
  zeroLine?: boolean
  colourOf?: (row: Row) => string
}) {
  const axis = { fill: CHART.axis, fontSize: 12 }
  const field = (row: Row, key: string) => (row as Record<string, unknown>)[key]
  const amounts = (row: Row) => series.map((item) => field(row, item.key)).filter((value): value is number => typeof value === "number")
  const largest = Math.max(0, ...data.map((row) => (stacked ? amounts(row).reduce((sum, value) => sum + value, 0) : Math.max(0, ...amounts(row)))))
  // Small counts get one tick per whole number, so two shipments are not drawn on an axis that runs to four.
  const ticks = integers && largest <= 6 ? Array.from({ length: Math.max(largest, 1) + 1 }, (_, index) => index) : undefined
  const valueAxis = {
    tick: axis, tickLine: false, axisLine: false, tickFormatter: (value: number) => format(value), allowDecimals: !integers,
    ...(ticks && { ticks, domain: [0, ticks[ticks.length - 1]] }),
  }
  const categoryAxis = { dataKey: (row: Row) => String(field(row, category)), tick: axis, tickLine: false, axisLine: { stroke: CHART.grid }, interval: 0 as const }

  return (
    <figure aria-label={label} className="min-w-0">
      {series.length > 1 && <Legend className="mb-3" items={series.map((item) => ({ key: item.key, label: item.label, colour: item.colour }))} />}

      <div style={{ height }} aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} layout={horizontal ? "vertical" : "horizontal"} margin={{ top: labels ? 20 : 8, right: horizontal ? 24 : 8, left: 0, bottom: 0 }} barGap={4} barCategoryGap="30%">
            <CartesianGrid stroke={CHART.grid} horizontal={!horizontal} vertical={horizontal} />
            {horizontal ? (
              <>
                <XAxis type="number" {...valueAxis} />
                <YAxis type="category" {...categoryAxis} width={84} />
              </>
            ) : (
              <>
                <XAxis {...categoryAxis} tickMargin={8} />
                <YAxis {...valueAxis} width={64} />
              </>
            )}
            {zeroLine && (horizontal ? <ReferenceLine x={0} stroke={CHART.axis} /> : <ReferenceLine y={0} stroke={CHART.axis} />)}
            <ChartTooltip cursor={{ fill: CHART.track }} content={<ChartTip format={format} />} />
            {series.map((item) => (
              <Bar key={item.key} dataKey={item.key} name={item.label} fill={item.colour} stackId={stacked ? "stack" : undefined} maxBarSize={horizontal ? 22 : 44} radius={stacked ? 0 : horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]} isAnimationActive={false}>
                {colourOf && data.map((row, index) => <Cell key={index} fill={colourOf(row)} />)}
                {labels && <LabelList dataKey={item.key} position={horizontal ? "right" : "top"} formatter={(value: unknown) => formatted(value, format)} style={{ fill: CHART.ink, fontSize: 12 }} />}
              </Bar>
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>

      <table className="sr-only">
        <caption>{label}</caption>
        <thead>
          <tr>
            <th scope="col">{category}</th>
            {series.map((item) => (
              <th key={item.key} scope="col">{item.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row, index) => (
            <tr key={index}>
              <th scope="row">{String(field(row, category))}</th>
              {series.map((item) => (
                <td key={item.key}>{formatted(field(row, item.key), format)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
