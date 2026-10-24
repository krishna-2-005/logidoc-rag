/**
 * Chart colours. Charts always also carry text (labels, values, a legend and an
 * accessible summary), so colour is never the only signal.
 *
 * Status colours mean a state and are never reused for a series. Problems are red,
 * a clean result is blue (red beside green fails for deuteranopia: checked with the
 * dataviz palette validator, red/blue/amber/grey pass adjacent CVD and normal-vision
 * separation), and "nothing to say" is neutral grey. Text never wears these colours.
 */
export const CHART = {
  bad: "#d03b3b",
  warn: "#f2a50c",
  ok: "#0ca30c",
  info: "#2a78d6",
  neutral: "#a1a1aa",
  track: "#eef0f4",
  grid: "#e5e7eb",
  axis: "#71717a",
  ink: "#18181b",
} as const

export const STATUS_COLOURS: Record<string, string> = {
  "DISCREPANCIES DETECTED": CHART.bad,
  "NEEDS REVIEW": CHART.warn,
  "NO DISCREPANCIES": CHART.info,
  "NOT RECONCILED": CHART.neutral,
}

/** The order shipments' results are drawn in: problems first, then clean, then not checked. */
export const STATUS_ORDER = ["DISCREPANCIES DETECTED", "NEEDS REVIEW", "NO DISCREPANCIES", "NOT RECONCILED"]

export const REVIEW_COLOURS: Record<string, string> = {
  open: CHART.info,
  in_review: CHART.warn,
  resolved: CHART.ok,
  dismissed: CHART.neutral,
}

export const REVIEW_ORDER = ["open", "in_review", "resolved", "dismissed"]

/** Categorical slots for series that are identities (carriers, line series), in a fixed order. */
export const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"] as const

/** "3 shipments with discrepancies, 1 with no discrepancies": the text version of a chart. */
export function summarize(entries: [string, number][], unit: string): string {
  return entries.length === 0 ? `No ${unit}.` : entries.map(([name, value]) => `${value} ${name}`).join(", ") + "."
}

/** "Discrepancies detected" from "DISCREPANCIES DETECTED"; "in review" from "in_review". */
export function sentence(text: string): string {
  const words = text.replace(/_/g, " ").toLowerCase()

  return words.charAt(0).toUpperCase() + words.slice(1)
}
