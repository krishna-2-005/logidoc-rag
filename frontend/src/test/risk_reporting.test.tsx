import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ReportBuilder } from "@/components/reporting/ReportBuilder"
import { Risk } from "@/pages/Risk"
import type { ReportCatalog, ReportPreview, RiskOverview, RiskShipment, RiskShipmentPage, RiskTrends } from "@/lib/types"
import { ADMIN_SESSION, mockApi, renderPage } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const user = () => userEvent.setup({ pointerEventsCheck: 0 })

const MODEL = { version: "risk-2026.11-1", digest: "abcd1234abcd1234" }

// ---------------------------------------------------------------------------
// Fixtures that follow the real response schemas (src/api/schemas.py)
// ---------------------------------------------------------------------------

const OVERVIEW: RiskOverview = {
  model: { ...MODEL, thresholds: { medium: 25, high: 50, critical: 75 } },
  totals: { shipments: 4, high_risk: 1, partial: 1, mean_score: 41.5, by_category: { low: 2, medium: 1, high: 0, critical: 1 } },
  histogram: [],
  exceptions: [{ category: "Invoice mismatch", total: 3, by_status: { open: 2, resolved: 1 } }],
  carriers: [
    { carrier_key: "swift lines", carrier: "Swift Lines", shipments: 3, mean_score: 50, high_risk: 1, by_category: { low: 1, medium: 1, high: 0, critical: 1 }, partial: 0,
      top_signals: [{ signal: "audit_discrepancies", label: "Audit discrepancies", shipments: 2 }] },
  ],
  signals: [],
}

const PAGE: RiskShipmentPage = {
  rows: [
    { shipment_id: "SYN-001", shipment_key: "SYN001", carrier_key: "swift lines", carrier: "Swift Lines", date: "2026-10-01", final_status: "DISCREPANCIES DETECTED",
      freight_outcome: "DISCREPANCY", open_exceptions: 3, score: 80, category: "critical", partial: false,
      top_factors: [{ signal: "duplicate_invoice", label: "Duplicate invoice indicator", points: 20 }] },
    { shipment_id: "SYN-002", shipment_key: "SYN002", carrier_key: null, carrier: "Not stated", date: null, final_status: "NO DISCREPANCIES",
      freight_outcome: null, open_exceptions: 0, score: 4, category: "low", partial: true, top_factors: [] },
  ],
  total: 2, offset: 0, limit: 25, model: MODEL,
}

const DETAIL: RiskShipment = {
  ...PAGE.rows[0], date_basis: "pickup", currency: "USD", basis: "current", model: MODEL, thresholds: { medium: 25, high: 50, critical: 75 }, unavailable: [],
  factors: [
    { signal: "duplicate_invoice", label: "Duplicate invoice indicator", status: "scored", points: 20, max: 20, uncapped_points: 20,
      detail: "The invoice number also appears on another shipment's invoice. Check it before paying; this is an indicator, not a finding of fraud.",
      evidence: [{ type: "exception", reference: "SYN001|freight-duplicate-invoice", category: "Freight audit: duplicate invoice", severity: "HIGH", status: "open" }], not_counted: [] },
    { signal: "audit_discrepancies", label: "Audit discrepancies", status: "scored", points: 30, max: 30, uncapped_points: 36, detail: "3 unresolved discrepancies.",
      evidence: [], not_counted: [{ type: "exception", reference: "SYN001|route", status: "resolved" }] },
    { signal: "missing_documents", label: "Missing documents", status: "unavailable", points: 0, max: 15, uncapped_points: 0, detail: "The document completeness register could not be read.", evidence: [], not_counted: [] },
  ],
}

const TRENDS: RiskTrends = {
  model: MODEL, period: "month", signals_used: [], signals_unavailable: ["missing_documents"], note: "Recorded history has no review states.",
  points: [{ period: "2026-10", shipments: 4, mean_score: 30, high_risk: 1, by_category: { low: 2, medium: 1, high: 1, critical: 0 } }],
}

function riskApi(overrides: Record<string, unknown> = {}) {
  return mockApi({
    "/api/session": ADMIN_SESSION,
    "/api/risk/overview": OVERVIEW,
    "/api/risk/shipments": PAGE,
    "/api/risk/trends": TRENDS,
    "/api/risk/shipments/SYN-001": DETAIL,
    ...overrides,
  })
}

describe("Shipment risk dashboard", () => {
  it("shows the figures, the distribution with its numbers, the trend and the carriers", async () => {
    riskApi()
    renderPage(<Risk />)

    expect(await screen.findByText("Shipments scored")).toBeInTheDocument()
    expect(screen.getByText("41.5")).toBeInTheDocument()
    expect(screen.getByText("1 of 4 shipments")).toBeInTheDocument()

    // The chart's own numbers, as a table, are the API's counts
    const distribution = screen.getByText("Shipments by risk category", { selector: "caption" }).closest("table") as HTMLElement
    const cells = within(distribution).getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))
    expect(cells).toEqual([["Low", "2"], ["Medium", "1"], ["High", "0"], ["Critical", "1"]])

    const trend = screen.getByText("Recorded shipments by risk category per period", { selector: "caption" }).closest("table") as HTMLElement
    expect(within(trend).getAllByRole("row")[1]).toHaveTextContent("2026-1021103")

    const carriers = screen.getByRole("table", { name: "Carrier risk patterns" })
    expect(within(carriers).getByText("Swift Lines")).toBeInTheDocument()
    expect(within(carriers).getByText("Audit discrepancies (2)")).toBeInTheDocument()
  })

  it("explains a shipment factor by factor, with its evidence and what was not available", async () => {
    riskApi()
    renderPage(<Risk />)

    await user().click(await screen.findByRole("button", { name: "Explain the risk of SYN-001" }))
    const detail = await screen.findByTestId("risk-detail")

    expect(within(detail).getByText("80 / 100")).toBeInTheDocument()
    const duplicate = within(detail).getByTestId("factor-duplicate_invoice")
    expect(within(duplicate).getByText("20 of 20")).toBeInTheDocument()
    expect(within(duplicate).getByText(/not a finding of fraud/)).toBeInTheDocument()
    expect(within(duplicate).getByText(/SYN001\|freight-duplicate-invoice/)).toBeInTheDocument()
    expect(within(within(detail).getByTestId("factor-audit_discrepancies")).getByText("capped from 36")).toBeInTheDocument()
    expect(within(within(detail).getByTestId("factor-audit_discrepancies")).getByText(/Not counted.*SYN001\|route/)).toBeInTheDocument()
    expect(within(within(detail).getByTestId("factor-missing_documents")).getByText("Not available")).toBeInTheDocument()
  })

  it("sends the chosen filters to the server", async () => {
    const { calls } = riskApi()
    renderPage(<Risk />)

    await screen.findByText("Shipments scored")
    await user().selectOptions(screen.getByLabelText("Risk category"), "critical")
    await user().selectOptions(screen.getByLabelText("Carrier"), "swift lines")
    await user().click(screen.getByRole("button", { name: "Apply" }))

    await waitFor(() => {
      const last = calls.filter((call) => call.url.pathname === "/api/risk/shipments").slice(-1)[0]
      expect(last?.url.searchParams.getAll("category")).toEqual(["critical"])
      expect(last?.url.searchParams.get("carrier")).toBe("swift lines")
    })
  })

  it("says so when nothing is selected or nothing was recorded", async () => {
    riskApi({
      "/api/risk/overview": { ...OVERVIEW, totals: { ...OVERVIEW.totals, shipments: 0, high_risk: 0, partial: 0, mean_score: null, by_category: { low: 0, medium: 0, high: 0, critical: 0 } }, carriers: [], exceptions: [] },
      "/api/risk/shipments": { ...PAGE, rows: [], total: 0 },
      "/api/risk/trends": { ...TRENDS, points: [] },
    })
    renderPage(<Risk />)

    expect(await screen.findAllByText("No shipment matches")).toHaveLength(2)
    expect(screen.getByText("n/a")).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Reporting
// ---------------------------------------------------------------------------

const CATALOG: ReportCatalog = {
  reports: [
    { report: "freight-audit-summary", title: "Freight audit summary", description: "Every shipment.", filters: ["dates", "carrier", "shipment", "outcome", "exception_status", "risk_category"], date_basis: "the shipment date" },
    { report: "discrepancies", title: "Discrepancies and exceptions", description: "Every exception.", filters: ["dates", "carrier", "shipment", "outcome", "exception_status", "risk_category"], date_basis: "the shipment date" },
    { report: "carrier-performance", title: "Carrier performance", description: "Each carrier.", filters: ["dates", "carrier", "risk_category"], date_basis: "the shipment date" },
    { report: "corrections", title: "Corrections and re-audits", description: "Every correction.", filters: ["dates", "shipment"], date_basis: "the date of each event (UTC)" },
  ],
  options: { carriers: [{ key: "swift lines", name: "Swift Lines" }], outcomes: ["MATCHED", "DISCREPANCY", "NOT_RUN"], statuses: ["open", "resolved"], categories: ["low", "high"] },
  can_view_history: true,
}

const PREVIEW: ReportPreview = {
  report: "freight-audit-summary", title: "Freight audit summary", description: "Every shipment.", generated_at: "2026-11-15T09:30:00+00:00",
  date_basis: "the shipment date", filters: ["No filter: every shipment"], provenance: ["The current audit of 2 shipment(s)."],
  columns: [{ key: "shipment_id", label: "Shipment", kind: "text" }, { key: "invoice_total", label: "Invoice total", kind: "money" }],
  rows: [{ shipment_id: "SYN-001", invoice_total: 1500.5 }, { shipment_id: "SYN-002", invoice_total: null }],
  total_rows: 2, offset: 0, limit: 25, notes: [],
  totals: [{ title: "Invoiced, by currency", rows: [{ label: "USD", value: 1500.5, display: "USD 1,500.50", note: "1 invoice(s)" }, { label: "EUR", value: 300, display: "EUR 300.00", note: "1 invoice(s)" }] }],
}

describe("Report builder", () => {
  it("previews a report with its filters, totals per currency and rows", async () => {
    mockApi({ "/api/session": ADMIN_SESSION, "/api/reporting/catalog": CATALOG, "/api/reporting/freight-audit-summary": PREVIEW })
    renderPage(<ReportBuilder allowed />)

    const preview = await screen.findByTestId("report-preview")
    expect(within(preview).getByText("No filter: every shipment")).toBeInTheDocument()
    expect(within(preview).getByText("USD 1,500.50")).toBeInTheDocument()
    expect(within(preview).getByText("EUR 300.00")).toBeInTheDocument()
    expect(within(preview).getByText("1,500.50")).toBeInTheDocument()
    expect(within(preview).getByText("n/a")).toBeInTheDocument()                 // a missing amount is never 0
  })

  it("offers only the filters a report takes and downloads with them", async () => {
    const { calls } = mockApi({
      "/api/session": ADMIN_SESSION, "/api/reporting/catalog": CATALOG, "/api/reporting/freight-audit-summary": PREVIEW,
      "/api/reporting/corrections": { ...PREVIEW, report: "corrections", title: "Corrections and re-audits", rows: [], total_rows: 0, totals: [] },
      "/api/reporting/corrections.csv": () => ({ text: "Report,Corrections", headers: { "Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="corrections.csv"' } }),
    })
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => undefined }))
    renderPage(<ReportBuilder allowed />)

    await user().click(await screen.findByRole("radio", { name: /Corrections and re-audits/ }))
    expect(screen.queryByLabelText("Carrier")).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Exception status")).not.toBeInTheDocument()
    await user().type(screen.getByLabelText("Shipment ID contains"), "SYN-001")
    await user().click(screen.getByRole("button", { name: "Preview" }))

    expect(await screen.findByText("No row matches the filters")).toBeInTheDocument()
    await waitFor(() => expect(calls.some((call) => call.url.pathname === "/api/reporting/corrections" && call.url.searchParams.get("shipment") === "SYN-001")).toBe(true))

    await user().click(screen.getByRole("button", { name: /Download CSV/ }))
    await waitFor(() => {
      const file = calls.find((call) => call.url.pathname === "/api/reporting/corrections.csv")
      expect(file?.url.searchParams.get("shipment")).toBe("SYN-001")
    })
  })

  it("hides the corrections report from a role without history and shows nothing without the permission", async () => {
    mockApi({ "/api/session": ADMIN_SESSION, "/api/reporting/catalog": { ...CATALOG, can_view_history: false }, "/api/reporting/freight-audit-summary": PREVIEW })
    const shown = renderPage(<ReportBuilder allowed />)
    await screen.findByTestId("report-preview")
    expect(screen.queryByRole("radio", { name: /Corrections and re-audits/ })).not.toBeInTheDocument()
    shown.unmount()

    vi.unstubAllGlobals()
    const { calls } = mockApi({ "/api/session": ADMIN_SESSION })
    renderPage(<ReportBuilder allowed={false} />)
    expect(screen.queryByRole("radiogroup", { name: "Report" })).not.toBeInTheDocument()
    expect(calls.filter((call) => call.url.pathname.startsWith("/api/reporting"))).toHaveLength(0)
  })
})
