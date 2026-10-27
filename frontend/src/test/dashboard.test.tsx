import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { Route, Routes, useLocation } from "react-router-dom"
import { afterEach, describe, expect, it, vi } from "vitest"
import { attentionList, checkBreakdown, checkOutcome, documentsByType, invoiceComparison, tally } from "@/lib/insights"
import type { DocumentFile, ExceptionListItem, ShipmentRow } from "@/lib/types"
import { Exceptions } from "@/pages/Exceptions"
import { Overview } from "@/pages/Overview"
import { Shipments } from "@/pages/Shipments"
import { REVIEWER_SESSION, SHIPMENT, mockApi, renderPage } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const CLEAN: ShipmentRow = {
  ...SHIPMENT, shipment_id: "SHJ-2026-002", shipment_key: "SHJ2026002", carrier: "Lakeshore Freight Lines",
  quantity_status: "MATCH", delivery_status: "EARLY", invoice_status: "MATCH", final_status: "NO DISCREPANCIES",
  invoice_total: 2875.5, invoice_difference: 0, discrepancy_count: 0, discrepancy_categories: [],
}

const SHIPMENTS = { shipments: [CLEAN, SHIPMENT], total: 2, carriers: [], statuses: [], categories: [] }

const EXPOSURE = {
  rows: [
    { shipment_id: "SHJ-2026-001", stated_total: 1550, line_item_sum: 1430, signed_difference: 120, currency: null },
    { shipment_id: "SHJ-2026-002", stated_total: 2875.5, line_item_sum: 2875.5, signed_difference: 0, currency: null },
  ],
  summary: {}, by_carrier: [], by_category: [], metric_definitions: {}, note: "",
}

const file = (filename: string, document_type: string | null, status = "parsed") =>
  ({ filename, document_type, status, status_text: status, shipment_id: null, shipment_key: null, total_pages: 1, text_pages: 1, size: 1, sha256: "x", error: null, references: [], ingestion: null }) as DocumentFile

const DOCUMENTS = {
  files: [file("BOL_1.pdf", "BOL"), file("BOL_2.pdf", "BOL"), file("INVOICE_1.pdf", "INVOICE"), file("POD_1.pdf", "POD", "partial"), file("scan.pdf", null, "no_text")],
  completeness: { shipments: [], unidentified: [], unusable: [], duplicates: [] },
}

const OVERVIEW = {
  empty: false,
  kpis: { identified: 2, reconciled: 2, with_discrepancies: 1, needs_review: 0, no_discrepancies: 1, invoice_total: 4425.5, invoice_total_covered: 2, invoice_variance: 120, invoice_variance_net: 120, invoice_variance_covered: 2, discrepancy_rate: 50 },
  metric_definitions: {},
  status_counts: { "DISCREPANCIES DETECTED": 1, "NO DISCREPANCIES": 1 },
  category_counts: { "Delivery Date": 1, Invoice: 1, Quantity: 1 },
  carriers: [],
  exposure: { invoices: 2, verified: 1, mismatches: 1, unverifiable: 0, totals_read: 2, totals_missing: 0, stated_total: 4425.5, net_difference: 120, absolute_variance: 120, amount_under_review: null, unverifiable_without_total: 0, mixed_currency: false, currencies: [] },
  exposure_note: "A mismatch is an arithmetic finding. It does not prove overbilling.",
  completeness: { ready: 2, incomplete: 0, unusable_files: 0, unidentified_files: 0 },
  reviews: { open: 3, in_review: 0, resolved: 0, dismissed: 0 },
  open_ingestion_errors: 0, unreadable_files: [], unidentified_sources: [],
}

const DASHBOARD = { "/api/overview": OVERVIEW, "/api/shipments": SHIPMENTS, "/api/exposure": EXPOSURE, "/api/documents": DOCUMENTS }

function Where() {
  const location = useLocation()

  return <p data-testid="location">{location.pathname + location.search}</p>
}

const withRoutes = (page: JSX.Element) => (
  <Routes>
    <Route path="/" element={page} />
    <Route path="/shipments" element={<Where />} />
  </Routes>
)

/** The rows of a chart's data table (screen readers read it; the numbers are the chart's). */
function rowsOf(caption: string) {
  const table = screen.getByRole("table", { name: caption })

  return within(table).getAllByRole("row").slice(1).map((row) => [...row.querySelectorAll("th, td")].map((cell) => cell.textContent))
}

describe("dashboard data", () => {
  it("never counts an unsettled check as passed", () => {
    expect(["MATCH", "ON TIME", "EARLY"].map(checkOutcome)).toEqual(["passed", "passed", "passed"])
    expect(["MISMATCH", "DELAYED"].map(checkOutcome)).toEqual(["discrepancy", "discrepancy"])
    expect(["NOT AVAILABLE", null, "", "SOMETHING NEW"].map(checkOutcome)).toEqual(["not_verified", "not_verified", "not_verified", "not_verified"])
  })

  it("counts every shipment once per check", () => {
    const unread = { ...CLEAN, shipment_id: "X", quantity_status: "NOT AVAILABLE", delivery_status: null, invoice_status: "MATCH" }

    expect(checkBreakdown([SHIPMENT, CLEAN, unread])).toEqual([
      { check: "Quantity", passed: 1, discrepancy: 1, not_verified: 1 },
      { check: "Delivery", passed: 1, discrepancy: 1, not_verified: 1 },
      { check: "Invoice", passed: 2, discrepancy: 1, not_verified: 0 },
    ])
  })

  it("leaves out an invoice it cannot compare instead of drawing a zero, and reports the currencies", () => {
    const result = invoiceComparison([
      ...EXPOSURE.rows,
      { shipment_id: "C", stated_total: null, line_item_sum: 10, currency: "USD" },
      { shipment_id: "D", stated_total: 5, line_item_sum: 4, currency: "EUR" },
    ])

    expect(result.rows.map((row) => [row.shipment, row.stated, row.lineItems, row.difference])).toEqual([
      ["SHJ-2026-001", 1550, 1430, 120], ["SHJ-2026-002", 2875.5, 2875.5, 0], ["D", 5, 4, 1],
    ])
    expect(result.incomplete).toBe(1)
    expect(result.currencies).toEqual(["USD", "EUR"])
  })

  it("puts the most urgent shipments first and leaves clean ones out", () => {
    const minor = { ...SHIPMENT, shipment_id: "B", discrepancy_count: 1, invoice_difference: 500 }
    const flagged = { ...CLEAN, shipment_id: "A", needs_review: true }

    expect(attentionList([CLEAN, minor, flagged, SHIPMENT]).map((row) => row.shipment_id)).toEqual(["SHJ-2026-001", "B", "A"])
  })

  it("counts documents per type by how well their text could be read", () => {
    expect(documentsByType(DOCUMENTS.files)).toEqual([
      { type: "BOL", usable: 2, partial: 0, unusable: 0 },
      { type: "INVOICE", usable: 1, partial: 0, unusable: 0 },
      { type: "POD", usable: 0, partial: 1, unusable: 0 },
      { type: "UNKNOWN", usable: 0, partial: 0, unusable: 1 },
    ])
  })

  it("tallies in the given order, then by count", () => {
    const items = [{ severity: "MEDIUM" }, { severity: "HIGH" }, { severity: "MEDIUM" }, { severity: null }] as ExceptionListItem[]

    expect(tally(items, "severity", ["HIGH", "MEDIUM", "LOW"])).toEqual([{ key: "HIGH", value: 1 }, { key: "MEDIUM", value: 2 }, { key: "not set", value: 1 }])
  })
})

describe("Overview dashboard", () => {
  it("charts the server's figures, each one checkable as text", async () => {
    mockApi(DASHBOARD)
    renderPage(withRoutes(<Overview />))

    expect(await screen.findByText("Audit outcomes")).toBeInTheDocument()

    await waitFor(() => expect(screen.getByRole("table", { name: "Shipments by check and outcome" })).toBeInTheDocument())
    expect(rowsOf("Shipments by check and outcome")).toEqual([["Quantity", "1", "1", "0"], ["Delivery", "1", "1", "0"], ["Invoice", "1", "1", "0"]])

    await waitFor(() => expect(screen.getByRole("table", { name: "Stated invoice total and sum of line items per shipment" })).toBeInTheDocument())
    expect(rowsOf("Stated invoice total and sum of line items per shipment")).toEqual([
      ["SHJ-2026-001", "$1,550.00", "$1,430.00"],
      ["SHJ-2026-002", "$2,875.50", "$2,875.50"],
    ])
    expect(rowsOf("Invoice difference per shipment")).toEqual([["SHJ-2026-001", "$120.00"], ["SHJ-2026-002", "$0.00"]])

    await waitFor(() => expect(screen.getByRole("table", { name: "Stored documents per type" })).toBeInTheDocument())
    expect(rowsOf("Stored documents per type")).toEqual([["BOL", "2", "0", "0"], ["INVOICE", "1", "0", "0"], ["POD", "0", "1", "0"], ["UNKNOWN", "0", "0", "1"]])
  })

  it("lists the shipments that need attention, with links to their evidence", async () => {
    mockApi(DASHBOARD)
    renderPage(withRoutes(<Overview />))

    const section = await screen.findByRole("region", { name: "Shipments needing attention" })
    await waitFor(() => expect(within(section).getByRole("link", { name: "SHJ-2026-001" })).toHaveAttribute("href", "/shipments/SHJ-2026-001"))

    expect(within(section).getByRole("link", { name: "Open audit" })).toHaveAttribute("href", "/freight-audit/SHJ-2026-001")
    expect(within(section).queryByText("SHJ-2026-002")).not.toBeInTheDocument()
  })

  it("opens the shipment list filtered by the result or the category chosen in a chart", async () => {
    mockApi(DASHBOARD)
    renderPage(withRoutes(<Overview />))

    expect(await screen.findByRole("link", { name: "Invoice" })).toHaveAttribute("href", "/shipments?category=Invoice")

    await userEvent.click(screen.getByRole("button", { name: /Discrepancies detected/ }))
    expect(await screen.findByTestId("location")).toHaveTextContent("/shipments?status=DISCREPANCIES%20DETECTED")
  })

  it("keeps the rest of the page when one chart's data cannot be loaded", async () => {
    mockApi({ ...DASHBOARD, "/api/exposure": () => ({ status: 500, body: { detail: "Something went wrong on the server." } }) })
    renderPage(withRoutes(<Overview />))

    expect(await screen.findByText("With discrepancies")).toBeInTheDocument()
    expect(await screen.findAllByText("This chart could not be loaded")).toHaveLength(2)
    await waitFor(() => expect(screen.getByRole("table", { name: "Shipments by check and outcome" })).toBeInTheDocument())
  })

  it("draws only what was read, says what it left out, and shows an empty state instead of zeros", async () => {
    const unreadable = { shipment_id: "SHJ-2026-003", stated_total: null, line_item_sum: 980, signed_difference: null, currency: null }
    mockApi({
      ...DASHBOARD,
      "/api/shipments": { ...SHIPMENTS, shipments: [], total: 0 },
      "/api/exposure": { ...EXPOSURE, rows: [EXPOSURE.rows[0], unreadable] },
      "/api/documents": { ...DOCUMENTS, files: [] },
    })
    renderPage(withRoutes(<Overview />))

    await waitFor(() => expect(screen.getByRole("table", { name: "Stated invoice total and sum of line items per shipment" })).toBeInTheDocument())
    expect(rowsOf("Stated invoice total and sum of line items per shipment")).toEqual([["SHJ-2026-001", "$1,550.00", "$1,430.00"]])
    expect(screen.getByText(/1 invoice is left out/)).toBeInTheDocument()

    expect(await screen.findByText("No shipment to check")).toBeInTheDocument()
    expect(await screen.findByText("No documents stored")).toBeInTheDocument()
    expect(screen.queryByRole("table", { name: "Shipments by check and outcome" })).not.toBeInTheDocument()
    expect(screen.queryByRole("table", { name: "Stored documents per type" })).not.toBeInTheDocument()
  })

  it("never draws amounts in different currencies on one axis", async () => {
    mockApi({
      ...DASHBOARD,
      "/api/exposure": { ...EXPOSURE, rows: [{ ...EXPOSURE.rows[0], currency: "USD" }, { ...EXPOSURE.rows[1], currency: "EUR" }] },
    })
    renderPage(withRoutes(<Overview />))

    expect(await screen.findByText("Several currencies")).toBeInTheDocument()
    expect(screen.getByText(/USD, EUR/)).toBeInTheDocument()
    expect(screen.queryByRole("table", { name: "Stated invoice total and sum of line items per shipment" })).not.toBeInTheDocument()
    expect(screen.queryByRole("table", { name: "Invoice difference per shipment" })).not.toBeInTheDocument()
  })

  it("shows a chart as loading inside its card while the rest of the page is ready", async () => {
    const { fetchMock } = mockApi(DASHBOARD)
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
      new URL(String(input), "http://localhost").pathname === "/api/documents" ? new Promise<Response>(() => {}) : fetchMock(input, init),
    )
    renderPage(withRoutes(<Overview />))

    await waitFor(() => expect(screen.getByRole("table", { name: "Shipments by check and outcome" })).toBeInTheDocument())
    expect(screen.getByText("Loading chart…")).toBeInTheDocument()
    expect(screen.queryByRole("table", { name: "Stored documents per type" })).not.toBeInTheDocument()
  })
})

describe("Exception Center summary", () => {
  const item = (category: string, severity: string, status: string, key: string) => ({
    exception_key: key, shipment_key: "SHJ2026001", shipment_id: "SHJ-2026-001", category, status, assignee: null, assigned_at: null, evidence_refs: [],
    approval_state: null, approval_by: null, resolved_by: null, needs_recheck: 0, absent_since: null, created_at: "2026-10-01T00:00:00+00:00",
    updated_at: "2026-10-01T00:00:00+00:00", note: null, age_days: 1, detected: true, description: null, severity, kind: "Discrepancy", allowed_actions: [],
  })

  const LIST = {
    exceptions: [item("Invoice mismatch", "HIGH", "open", "a"), item("Delivery delay", "HIGH", "in_review", "b"), item("Quantity shortage", "MEDIUM", "open", "c")],
    total: 3, assignees: [], categories: ["Delivery delay", "Invoice mismatch", "Quantity shortage"], statuses: ["open", "in_review", "resolved", "dismissed"],
  }

  it("counts the listed exceptions by severity and state, and filters by a chosen category", async () => {
    const { calls } = mockApi({ "/api/session": REVIEWER_SESSION, "/api/settings": { minimum_note_length: 10 }, "/api/exceptions": LIST })
    renderPage(<Exceptions />, "/exceptions")

    const severity = await screen.findByLabelText("Exceptions by severity: 2 high, 1 medium")
    expect(severity).toBeInTheDocument()
    expect(screen.getByLabelText("Exceptions by review state: 2 open, 1 in review, 0 resolved, 0 dismissed")).toBeInTheDocument()
    expect(screen.getAllByText("High").length).toBeGreaterThan(0)

    await userEvent.click(screen.getByRole("button", { name: "Invoice mismatch" }))

    await waitFor(() => {
      const url = calls.filter((call) => call.url.pathname === "/api/exceptions").pop()!.url
      expect(url.searchParams.getAll("category")).toEqual(["Invoice mismatch"])
    })
  })
})

describe("Shipment Explorer links", () => {
  it("starts with the filters a link carries", async () => {
    const { calls } = mockApi({ "/api/shipments": { shipments: [SHIPMENT], total: 1, carriers: [], statuses: ["DISCREPANCIES DETECTED"], categories: ["Invoice"] } })
    renderPage(<Shipments />, "/shipments?status=DISCREPANCIES%20DETECTED&category=Invoice")

    await screen.findByRole("link", { name: "SHJ-2026-001" })

    const first = calls[0].url
    expect(first.searchParams.getAll("status")).toEqual(["DISCREPANCIES DETECTED"])
    expect(first.searchParams.getAll("category")).toEqual(["Invoice"])
  })
})
