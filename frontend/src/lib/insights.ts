/**
 * Chart rows derived from what the API returned. Nothing here is calculated
 * from documents: the audit already did that on the server. These functions only
 * count and arrange the server's results so a chart can show them, and they say
 * what they had to leave out instead of guessing.
 */
import type { DocumentFile, ExceptionListItem, ShipmentRow } from "@/lib/types"

export type CheckOutcome = "passed" | "discrepancy" | "not_verified"

/** The audit's own words for each outcome (see reconciliation.py and shipment_calculations.py). */
const PASSED = new Set(["MATCH", "ON TIME", "EARLY"])
const DISCREPANCY = new Set(["MISMATCH", "DELAYED"])

/** A status the audit did not settle (NOT AVAILABLE, missing, or a word this screen does not know) is never counted as passed. */
export function checkOutcome(status: string | null | undefined): CheckOutcome {
  const word = (status ?? "").trim().toUpperCase()

  return PASSED.has(word) ? "passed" : DISCREPANCY.has(word) ? "discrepancy" : "not_verified"
}

export const CHECKS = [
  { key: "quantity", label: "Quantity", field: "quantity_status" },
  { key: "delivery", label: "Delivery", field: "delivery_status" },
  { key: "invoice", label: "Invoice", field: "invoice_status" },
] as const

export interface CheckRow {
  check: string
  passed: number
  discrepancy: number
  not_verified: number
}

/** For each of the three cross-document checks: how many shipments passed it, failed it, or could not be checked. */
export function checkBreakdown(shipments: ShipmentRow[]): CheckRow[] {
  return CHECKS.map((check) => {
    const row: CheckRow = { check: check.label, passed: 0, discrepancy: 0, not_verified: 0 }

    for (const shipment of shipments) row[checkOutcome(shipment[check.field])] += 1

    return row
  })
}

export interface InvoiceRow {
  shipment: string
  stated: number
  lineItems: number
  difference: number
}

export interface InvoiceComparison {
  rows: InvoiceRow[]
  /** Shipments left out because a total or the line items could not be read */
  incomplete: number
  /** The currencies the invoices state; more than one means the amounts are not drawn on one axis */
  currencies: string[]
}

/**
 * Stated total against the sum of the line items, per shipment, from the exposure
 * rows. A row with either amount missing is counted as incomplete, never drawn as zero.
 */
export function invoiceComparison(rows: Record<string, unknown>[]): InvoiceComparison {
  const currencies = [...new Set(rows.map((row) => row.currency).filter((value): value is string => typeof value === "string" && value !== ""))]
  const usable: InvoiceRow[] = []
  let incomplete = 0

  for (const row of rows) {
    const stated = row.stated_total
    const lineItems = row.line_item_sum

    if (typeof stated !== "number" || typeof lineItems !== "number") {
      incomplete += 1
      continue
    }

    usable.push({
      shipment: String(row.shipment_id ?? ""),
      stated,
      lineItems,
      difference: typeof row.signed_difference === "number" ? row.signed_difference : Math.round((stated - lineItems) * 100) / 100,
    })
  }

  return { rows: usable, incomplete, currencies }
}

/**
 * Shipments that need a person, most urgent first: the most discrepancies, then the
 * largest invoice difference, then those flagged for review.
 */
export function attentionList(shipments: ShipmentRow[]): ShipmentRow[] {
  return shipments
    .filter((row) => row.discrepancy_count > 0 || row.needs_review || row.final_status === "NEEDS REVIEW")
    .sort(
      (a, b) =>
        b.discrepancy_count - a.discrepancy_count ||
        Math.abs(b.invoice_difference ?? 0) - Math.abs(a.invoice_difference ?? 0) ||
        a.shipment_id.localeCompare(b.shipment_id),
    )
}

export const DOCUMENT_TYPES = ["BOL", "INVOICE", "POD", "RATE_CONFIRMATION"]

export interface DocumentTypeRow {
  type: string
  usable: number
  partial: number
  unusable: number
}

/** Stored documents per type, split by whether extraction could read them (parsed, some pages without text, or not usable). */
export function documentsByType(files: DocumentFile[]): DocumentTypeRow[] {
  const rows = new Map<string, DocumentTypeRow>()

  for (const file of files) {
    const type = (file.document_type ?? "").toUpperCase() || "UNKNOWN"
    const row = rows.get(type) ?? { type, usable: 0, partial: 0, unusable: 0 }

    if (file.status === "parsed") row.usable += 1
    else if (file.status === "partial") row.partial += 1
    else row.unusable += 1

    rows.set(type, row)
  }

  const known = DOCUMENT_TYPES.filter((type) => rows.has(type)).map((type) => rows.get(type) as DocumentTypeRow)
  const other = [...rows.values()].filter((row) => !DOCUMENT_TYPES.includes(row.type)).sort((a, b) => a.type.localeCompare(b.type))

  return [...known, ...other]
}

export const SEVERITY_ORDER = ["HIGH", "MEDIUM", "LOW"]

/** How many of the listed exceptions fall in each value of one field, in a fixed order with unknown values last. */
export function tally(items: ExceptionListItem[], field: "category" | "severity" | "status", order: string[] = []): { key: string; value: number }[] {
  const counts = new Map<string, number>()

  for (const item of items) {
    const key = String(item[field] ?? "not set")
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }

  const ranked = [...counts.entries()].sort((a, b) => {
    const left = order.indexOf(a[0])
    const right = order.indexOf(b[0])

    if (left !== -1 || right !== -1) return (left === -1 ? order.length : left) - (right === -1 ? order.length : right)

    return b[1] - a[1] || a[0].localeCompare(b[0])
  })

  return ranked.map(([key, value]) => ({ key, value }))
}
