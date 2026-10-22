/**
 * Formatting that never invents a value. A missing number is "n/a", never 0:
 * the audit leaves a value empty when the documents do not give it.
 */

export const NA = "n/a"

export function money(value: number | null | undefined, currency?: string | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NA

  const sign = value < 0 ? "-" : ""
  const text = `${sign}$${Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

  return currency && currency !== "Not stated" ? `${text} ${currency}` : text
}

export function count(value: number | null | undefined): string {
  return value === null || value === undefined ? NA : value.toLocaleString("en-US")
}

export function percent(value: number | null | undefined, digits = 0): string {
  return value === null || value === undefined ? NA : `${(value * 100).toFixed(digits)}%`
}

/** A rate with the number it is based on: "50% (2 of 4)". */
export function rate(item: { rate: number | null; count: number; n: number }): string {
  return item.rate === null ? `${NA} (0 of 0)` : `${percent(item.rate)} (${item.count} of ${item.n})`
}

export function interval(item: { low: number | null; high: number | null }): string {
  return item.low === null || item.high === null ? "" : `${Math.round(item.low * 100)}–${Math.round(item.high * 100)}%`
}

export function pieces(value: number | null | undefined): string {
  return value === null || value === undefined ? NA : `${value.toLocaleString("en-US")} ${Math.abs(value) === 1 ? "piece" : "pieces"}`
}

export function days(value: number | null | undefined): string {
  if (value === null || value === undefined) return NA
  if (value === 0) return "on the expected date"
  return `${Math.abs(value)} day${Math.abs(value) === 1 ? "" : "s"} ${value > 0 ? "late" : "early"}`
}

export function dateTime(text: string | null | undefined): string {
  if (!text) return NA

  const moment = new Date(text)
  if (Number.isNaN(moment.getTime())) return text

  return moment.toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" })
}

export function label(text: string | null | undefined): string {
  if (!text) return NA
  return text.replace(/_/g, " ").replace(/^\w/, (letter) => letter.toUpperCase())
}
