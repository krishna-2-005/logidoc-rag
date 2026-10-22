/**
 * Chart colours. Charts always also carry text (axis labels, values, a legend
 * and an accessible summary), so colour is never the only signal.
 */
export const CHART = {
  bad: "#dc2626",
  warn: "#d97706",
  ok: "#059669",
  info: "#2563eb",
  neutral: "#64748b",
  grid: "#e2e8f0",
  axis: "#475569",
} as const

export const STATUS_COLOURS: Record<string, string> = {
  "DISCREPANCIES DETECTED": CHART.bad,
  "NEEDS REVIEW": CHART.warn,
  "NO DISCREPANCIES": CHART.ok,
  "NOT RECONCILED": CHART.neutral,
}

export const REVIEW_COLOURS: Record<string, string> = {
  open: CHART.info,
  in_review: CHART.warn,
  resolved: CHART.ok,
  dismissed: CHART.neutral,
}

/** "3 shipments with discrepancies, 1 with no discrepancies": the text version of a chart. */
export function summarize(entries: [string, number][], unit: string): string {
  return entries.length === 0 ? `No ${unit}.` : entries.map(([name, value]) => `${value} ${name}`).join(", ") + "."
}
