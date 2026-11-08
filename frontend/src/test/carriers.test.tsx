import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it } from "vitest"
import { CarrierIntelligence } from "@/pages/CarrierIntelligence"
import { REVIEWER_SESSION, mockApi, renderPage } from "@/test/utils"

const rateOf = (count: number, n: number) => ({ count, n, rate: n ? count / n : null, low: null, high: null })

function figures(overrides: Record<string, unknown> = {}) {
  return {
    shipments: 3,
    delivery: { completed: 3, measured: 2, on_time: 1, late: 1, on_time_rate: rateOf(1, 2), average_delay_days: 2, not_measured: { "no BOL pickup date": 1 } },
    financial: {
      by_currency: [{ currency: "USD", invoices_with_total: 3, invoiced_total: 4675, invoices_compared_with_contract: 1, contracted_total: 1580, audited_billed_total: 1675, verified_variance: 95, average_invoice_variance: 95, cost_per_shipment: 1558.33 }],
      invoices: 3,
      invoices_total_not_read: 0,
    },
    documents: { shipments: 3, complete: 2, missing_bol: 0, missing_invoice: 0, missing_pod: 1, completeness_rate: rateOf(2, 3) },
    audit: {
      invoices: 3, audited_against_contract: 1, without_contract: 2, matched: 0, discrepant: 1, pending_review: 0, insufficient_evidence: 0, duplicate: 0,
      audit_pass_rate: rateOf(0, 1), arithmetic_checked: 3, arithmetic_match: 2, arithmetic_mismatch: 1, arithmetic_needs_review: 0, invoice_accuracy_rate: rateOf(2, 3),
    },
    exceptions: {
      total: 4, open: 2, by_status: { open: 2, disputed: 1, waived: 1 }, by_category: { "Delivery delay": 2 }, exceptions_per_shipment: 1.333,
      repeated: [{ category: "Delivery delay", shipments: ["SHJ-2026-001", "SHJ-2026-003"] }],
      amounts: { open: [], disputed: [{ currency: "USD", amount: 20, exceptions: 1 }], waived: [{ currency: "USD", amount: 75, exceptions: 1 }], resolved: [] },
    },
    ...overrides,
  }
}

const MONTHS = [{ month: "2026-10", shipments: 3, measured: 2, on_time: 1, late: 1, on_time_rate: rateOf(1, 2), invoiced: [], arithmetic_mismatches: 1, contract_discrepancies: 1, exceptions: 4 }]

const LIST = {
  period: { start: null, end: null },
  definitions: { on_time_rate: "On time or early / deliveries measured." },
  carriers: [
    { carrier_key: "swift lines", carrier: "Swift Lines", names: ["Swift Lines"], ...figures() },
    { carrier_key: "blue ridge haulers", carrier: "Blue Ridge Haulers", names: ["Blue Ridge Haulers"], ...figures({ shipments: 1 }) },
  ],
  unattributed: { not_stated: ["SHJ-2026-009"], conflicting: [{ shipment_id: "SHJ-2026-007", names: ["Desert Sun Carriers", "Swift Lines"] }], undated_excluded: [] },
  possible_duplicates: [["Swift Lines Inc", "Swift Lines, Inc."]],
  totals: figures({ shipments: 6 }),
  monthly: MONTHS,
}

const DETAIL = {
  ...LIST.carriers[0],
  identifier: null,
  contact: null,
  identity_note: "The documents' carrier name is the identity: carrier numbers (SCAC, MC, DOT) and contact details are not extracted yet.",
  period: { start: null, end: null },
  definitions: {},
  monthly: MONTHS,
  shipment_count: 3,
  offset: 0,
  limit: 25,
  shipment_rows: [
    { shipment_id: "SHJ-2026-101", shipment_key: "SHJ2026101", date: "2026-10-01", date_basis: "pickup", final_status: "DISCREPANCIES DETECTED", delivery_status: "ON TIME", measured: true, not_measured_reason: null, days_late: 0,
      invoice_total: 1675, currency: "USD", arithmetic_status: "MATCH", freight_outcome: "DISCREPANCY", contracted: 1580, contract_variance: 95, complete: true, missing: [], exceptions: [] },
    { shipment_id: "SHJ-2026-003", shipment_key: "SHJ2026003", date: null, date_basis: null, final_status: "NOT RECONCILED", delivery_status: null, measured: false, not_measured_reason: "not reconciled", days_late: null,
      invoice_total: null, currency: "Not stated", arithmetic_status: null, freight_outcome: null, contracted: null, contract_variance: null, complete: false, missing: ["POD"], exceptions: [] },
  ],
}

function setup(overrides: Record<string, unknown> = {}) {
  return mockApi({ "/api/session": REVIEWER_SESSION, "/api/carriers": LIST, "/api/carriers/swift%20lines": DETAIL, ...overrides })
}

describe("Carrier Intelligence", () => {
  it("shows every carrier's figures with what each rate is counted out of", async () => {
    setup()
    renderPage(<CarrierIntelligence />, "/carriers")

    const table = await screen.findByRole("table", { name: "Carrier comparison" })
    const swift = within(table).getByRole("button", { name: "Open carrier Swift Lines" }).closest("tr") as HTMLElement

    expect(within(swift).getAllByText("50% (1 of 2)").length).toBeGreaterThan(0)
    // Complete documents and invoice accuracy are both 2 of 3 in this fixture
    expect(within(swift).getAllByText("67% (2 of 3)")).toHaveLength(2)
    expect(within(swift).getByText("0% (0 of 1)")).toBeInTheDocument()
    expect(within(swift).getByText("$4,675.00 USD")).toBeInTheDocument()
    expect(within(swift).getByText(/Repeated: Delivery delay/)).toBeInTheDocument()

    const totals = screen.getByLabelText("Totals")
    expect(within(totals).getByText("6")).toBeInTheDocument()
    expect(screen.getByLabelText("On-time delivery percentage by carrier")).toBeInTheDocument()
    expect(screen.getByLabelText("Complete and incomplete shipments by carrier")).toBeInTheDocument()
    expect(screen.getByLabelText("Invoice discrepancies by carrier")).toBeInTheDocument()
    expect(screen.getByLabelText("Monthly trend: all carriers")).toBeInTheDocument()
  })

  it("explains similar names and shipments it could not attribute, never merging them", async () => {
    setup()
    renderPage(<CarrierIntelligence />, "/carriers")

    expect(await screen.findByText(/Possibly the same carrier, kept apart until a person decides: Swift Lines Inc \/ Swift Lines, Inc\./)).toBeInTheDocument()
    expect(screen.getByText(/SHJ-2026-007 is not counted for any carrier: its documents name Desert Sun Carriers and Swift Lines/)).toBeInTheDocument()
    expect(screen.getByText(/No carrier stated on 1 shipment/)).toBeInTheDocument()
  })

  it("opens a carrier: money per currency, exception amounts by state and its shipments", async () => {
    setup()
    renderPage(<CarrierIntelligence />, "/carriers")

    await userEvent.click(await screen.findByRole("button", { name: "Open carrier Swift Lines" }))

    const detail = await screen.findByLabelText("Carrier Swift Lines")
    expect(screen.getByText(/carrier numbers \(SCAC, MC, DOT\) and contact details are not extracted yet/)).toBeInTheDocument()

    const amounts = within(detail).getByLabelText("Exception amounts")
    expect(amounts).toHaveTextContent("Exceptions disputed$20.00 USD (1)")
    expect(amounts).toHaveTextContent("Exceptions waived$75.00 USD (1)")
    expect(amounts).toHaveTextContent("Exceptions openno amount")

    const shipments = within(detail).getByRole("table", { name: "Shipments of the carrier" })
    expect(within(shipments).getByRole("link", { name: "SHJ-2026-101" })).toHaveAttribute("href", "/shipments/SHJ-2026-101")
    expect(within(shipments).getByText("not reconciled")).toBeInTheDocument()
    expect(within(shipments).getByText("Missing POD")).toBeInTheDocument()
    expect(within(detail).getByText("Repeated: Delivery delay on 2 shipments (SHJ-2026-001, SHJ-2026-003)")).toBeInTheDocument()
  })

  it("sends the date range to the server and refuses a range that ends before it starts", async () => {
    const { calls } = setup()
    renderPage(<CarrierIntelligence />, "/carriers")

    await screen.findByRole("table", { name: "Carrier comparison" })
    await userEvent.type(screen.getByLabelText("From"), "2026-10-01")
    await userEvent.type(screen.getByLabelText("To"), "2026-10-31")

    await waitFor(() => {
      const lists = calls.filter((call) => call.url.pathname === "/api/carriers")
      const last = lists[lists.length - 1]
      expect(last.url.searchParams.get("start")).toBe("2026-10-01")
      expect(last.url.searchParams.get("end")).toBe("2026-10-31")
    })

    await userEvent.clear(screen.getByLabelText("To"))
    await userEvent.type(screen.getByLabelText("To"), "2026-09-01")
    expect(await screen.findByRole("alert")).toHaveTextContent("The start date is after the end date.")
    expect(screen.getByRole("button", { name: /Export CSV/ })).toBeDisabled()
  })

  it("exports the carrier report with the same filters", async () => {
    const { calls } = setup({ "/api/reports/carriers.csv": () => ({ body: "carrier\r\n", headers: { "content-type": "text/csv", "content-disposition": 'attachment; filename="carrier_performance.csv"' } }) })
    renderPage(<CarrierIntelligence />, "/carriers?start=2026-10-01")

    await screen.findByRole("table", { name: "Carrier comparison" })
    await userEvent.click(screen.getByRole("button", { name: /Export CSV/ }))

    await waitFor(() => {
      const report = calls.find((call) => call.url.pathname === "/api/reports/carriers.csv")
      expect(report).toBeDefined()
      expect(report!.url.searchParams.get("start")).toBe("2026-10-01")
    })
  })

  it("shows a registered carrier's master data", async () => {
    const registered = { id: 1, legal_name: "Swift Lines Inc", trading_name: null, scac: "SWFT", mc_number: "123456", dot_number: null, contact_name: null, contact_email: "ops@swiftlines.example", contact_phone: null }
    setup({
      "/api/carriers": { ...LIST, carriers: [{ ...LIST.carriers[0], carrier_key: "registered:1", carrier: "Swift Lines Inc", registered }] },
      "/api/carriers/registered%3A1": { ...DETAIL, carrier_key: "registered:1", carrier: "Swift Lines Inc", registered, identifier: "SCAC SWFT · MC 123456", contact: "ops@swiftlines.example" },
    })
    renderPage(<CarrierIntelligence />, "/carriers")

    const table = await screen.findByRole("table", { name: "Carrier comparison" })
    expect(within(table).getByText("Registered · SCAC SWFT")).toBeInTheDocument()

    await userEvent.click(within(table).getByRole("button", { name: "Open carrier Swift Lines Inc" }))
    const data = await screen.findByLabelText("Carrier master data")
    expect(data).toHaveTextContent("SCAC SWFT · MC 123456")
    expect(data).toHaveTextContent("ops@swiftlines.example")
  })

  it("says so when there is nothing in the period", async () => {
    setup({ "/api/carriers": { ...LIST, carriers: [], totals: figures({ shipments: 0 }), monthly: [], possible_duplicates: [], unattributed: { not_stated: [], conflicting: [], undated_excluded: [] } } })
    renderPage(<CarrierIntelligence />, "/carriers")

    expect(await screen.findByText("No shipment in this period")).toBeInTheDocument()
    expect(screen.queryByRole("table", { name: "Carrier comparison" })).toBeNull()
  })
})
