import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { Route, Routes } from "react-router-dom"
import { afterEach, describe, expect, it, vi } from "vitest"
import { CarrierLinks } from "@/pages/CarrierLinks"
import { CarrierRegistry } from "@/pages/CarrierRegistry"
import { CarrierRegistryDetail } from "@/pages/CarrierRegistryDetail"
import { REVIEWER_SESSION, mockApi, renderPage } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const MANAGER = {
  mode: "required",
  authenticated: true,
  user: { username: "alice", role: "admin", label: "Administrator" },
  permissions: ["exceptions.view", "carriers.manage", "integrations.manage"],
}

const SWIFT = {
  id: 1, legal_name: "Swift Lines Inc", trading_name: "Swift", scac: "SWFT", mc_number: "123456", dot_number: "1234567",
  contact_name: "Pat Doe", contact_email: "ops@swift.example", contact_phone: "+1 555 010 2000", created_by: "alice",
  created_at: "2026-11-01T09:00:00+00:00", updated_at: "2026-11-05T10:00:00+00:00", document_names: ["Swift Lines"], status: "active",
  address_line1: "100 Main St", address_line2: null, city: "Chicago", region: "IL", postal_code: "60601", country: "US", pending_requests: 1,
}

const BLUE = { ...SWIFT, id: 2, legal_name: "Blue Ridge Haulers", trading_name: null, scac: null, mc_number: null, dot_number: null, contact_name: null, contact_email: null, contact_phone: null, status: "on_hold", document_names: [], pending_requests: 0 }

const page = (carriers: unknown[], total = carriers.length, offset = 0) => ({ carriers, total, offset, limit: 25 })
const EMPTY_QUEUE = { requests: [], total: 0, offset: 0, limit: 50 }

const REQUEST = {
  id: 7, name_key: "swift line", display_name: "Swift Line", carrier_id: 1, reason: "Same remit-to address on every invoice.", proposed_by: "dave",
  proposed_at: "2026-11-06T09:00:00+00:00", decision: null, decided_by: null, decided_at: null, decision_note: null,
  carrier_legal_name: "Swift Lines Inc", current_carrier_id: null, current_carrier_legal_name: null,
}

function bodyOf(init?: RequestInit) {
  return JSON.parse(String(init?.body ?? "{}"))
}

describe("Carrier Registry list", () => {
  it("shows each carrier's identifiers, contact and status, and asks the server to search, filter, sort and page", async () => {
    const { calls } = mockApi({
      "/api/session": REVIEWER_SESSION,
      "/api/carrier-registry/links": EMPTY_QUEUE,
      "/api/carrier-registry/carriers": (url: URL) => ({ body: page(url.searchParams.get("offset") === "25" ? [BLUE] : [SWIFT, BLUE], 26, Number(url.searchParams.get("offset") ?? 0)) }),
    })
    renderPage(<CarrierRegistry />, "/carrier-registry")

    const table = await screen.findByRole("table", { name: "Registered carriers" })
    const swift = within(table).getByRole("link", { name: "Swift Lines Inc" }).closest("tr") as HTMLElement

    expect(within(swift).getByText("SWFT")).toBeInTheDocument()
    expect(within(swift).getByText("MC-123456")).toBeInTheDocument()
    expect(within(swift).getByText("ops@swift.example")).toBeInTheDocument()
    expect(within(swift).getByText("Active")).toBeInTheDocument()
    expect(within(swift).getByText("1 awaiting approval")).toBeInTheDocument()
    expect(within(table).getByText("On hold")).toBeInTheDocument()

    // A reviewer reads; only administrators register
    expect(screen.queryByRole("button", { name: /Register carrier/ })).not.toBeInTheDocument()

    await userEvent.type(screen.getByLabelText("Search"), "MC 123456")
    await waitFor(() => expect(calls.some((call) => call.url.searchParams.get("search") === "MC 123456")).toBe(true))

    await userEvent.selectOptions(screen.getByLabelText("Registration status"), "on_hold")
    await waitFor(() => expect(calls.some((call) => call.url.searchParams.get("status") === "on_hold")).toBe(true))

    await userEvent.selectOptions(screen.getByLabelText("Sort by"), "updated_at")
    await userEvent.click(screen.getByRole("button", { name: /Sorted ascending/ }))
    await waitFor(() => expect(calls.some((call) => call.url.searchParams.get("sort") === "updated_at" && call.url.searchParams.get("order") === "desc")).toBe(true))

    await userEvent.click(await screen.findByRole("button", { name: /Next/ }))
    await waitFor(() => expect(calls.some((call) => call.url.searchParams.get("offset") === "25")).toBe(true))
    expect(await screen.findByText(/Showing/)).toHaveTextContent("Showing 26–26 of 26")
  })

  it("says when nothing is registered, and offers registering to an administrator", async () => {
    mockApi({ "/api/session": MANAGER, "/api/carrier-registry/links": EMPTY_QUEUE, "/api/carrier-registry/carriers": page([]) })
    renderPage(<CarrierRegistry />, "/carrier-registry")

    expect(await screen.findByText("No carrier is registered yet")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Register the first carrier/ })).toBeInTheDocument()
  })

  it("explains a refused duplicate and registers it only after saying it is a different company", async () => {
    const posted: Record<string, unknown>[] = []
    mockApi({
      "/api/session": MANAGER,
      "/api/carrier-registry/links": EMPTY_QUEUE,
      "/api/carrier-registry/carriers": page([SWIFT]),
      "/api/carrier-registry": (_url: URL, init?: RequestInit) => {
        const body = bodyOf(init)
        posted.push(body)
        if (!body.confirm_distinct) return { status: 409, body: { detail: "Possibly the same company as carrier 1 (Swift Lines Inc). Link its document names to that carrier, or say that it is a different company (confirm_distinct)." } }
        return { status: 201, body: { ...SWIFT, id: 3, legal_name: body.legal_name } }
      },
      "/api/carrier-registry/carriers/3": { carrier: { ...SWIFT, id: 3 }, name_links: [], pending_requests: [], recent_decisions: [], history: null, intelligence_key: null },
    })
    renderPage(
      <Routes>
        <Route path="/carrier-registry" element={<CarrierRegistry />} />
        <Route path="/carrier-registry/:id" element={<p>Opened carrier</p>} />
      </Routes>,
      "/carrier-registry",
    )

    await userEvent.click(await screen.findByRole("button", { name: /Register carrier/ }))
    const form = screen.getByRole("form", { name: "Register a carrier" })
    await userEvent.type(within(form).getByLabelText(/Legal name/), "SWIFT LINES INC")
    await userEvent.type(within(form).getByLabelText("SCAC"), "swfx")
    await userEvent.click(within(form).getByRole("button", { name: "Register carrier" }))

    expect(await within(form).findByRole("alert")).toHaveTextContent("Possibly the same company as carrier 1 (Swift Lines Inc)")
    // What was typed is sent as typed: the server decides, nothing is cleaned here
    expect(posted[0]).toMatchObject({ legal_name: "SWIFT LINES INC", scac: "swfx", confirm_distinct: false, status: "active" })

    await userEvent.click(within(form).getByRole("checkbox", { name: /this is a different company/ }))
    await userEvent.click(within(form).getByRole("button", { name: "Register carrier" }))

    expect(await screen.findByText("Opened carrier")).toBeInTheDocument()
    expect(posted[1]).toMatchObject({ legal_name: "SWIFT LINES INC", confirm_distinct: true })
  })

  it("shows a malformed identifier's rule from the server", async () => {
    mockApi({
      "/api/session": MANAGER,
      "/api/carrier-registry/links": EMPTY_QUEUE,
      "/api/carrier-registry/carriers": page([SWIFT]),
      "/api/carrier-registry": () => ({ status: 422, body: { detail: "'SW1' is not a SCAC: 2 to 4 letters." } }),
    })
    renderPage(<CarrierRegistry />, "/carrier-registry")

    await userEvent.click(await screen.findByRole("button", { name: /Register carrier/ }))
    const form = screen.getByRole("form", { name: "Register a carrier" })
    await userEvent.type(within(form).getByLabelText(/Legal name/), "Other Carrier")
    await userEvent.type(within(form).getByLabelText("SCAC"), "SW1")
    await userEvent.click(within(form).getByRole("button", { name: "Register carrier" }))

    expect(await within(form).findByRole("alert")).toHaveTextContent("'SW1' is not a SCAC: 2 to 4 letters.")
    expect(within(form).queryByRole("checkbox")).not.toBeInTheDocument()
  })
})

const RATE = (count: number, n: number) => ({ count, n, rate: n ? count / n : null, low: null, high: null })

const FIGURES = {
  carrier_key: "registered:1", carrier: "Swift Lines Inc", names: ["Swift Lines"], registered: null, shipments: 2,
  delivery: { completed: 2, measured: 2, on_time: 1, late: 1, on_time_rate: RATE(1, 2), average_delay_days: 2, not_measured: {} },
  financial: { by_currency: [], invoices: 2, invoices_total_not_read: 0 },
  documents: { shipments: 2, complete: 2, missing_bol: 0, missing_invoice: 0, missing_pod: 0, completeness_rate: RATE(2, 2) },
  audit: { invoices: 2, audited_against_contract: 1, without_contract: 1, matched: 0, discrepant: 1, pending_review: 0, insufficient_evidence: 0, duplicate: 0, audit_pass_rate: RATE(0, 1), arithmetic_checked: 2, arithmetic_match: 2, arithmetic_mismatch: 0, arithmetic_needs_review: 0, invoice_accuracy_rate: RATE(2, 2) },
  exceptions: { total: 3, open: 2, by_status: { open: 2, disputed: 1 }, by_category: {}, exceptions_per_shipment: 1.5, repeated: [], amounts: { open: [], disputed: [], waived: [], resolved: [] } },
  identifier: "SCAC SWFT", contact: "Pat Doe", identity_note: "", period: { start: null, end: null }, definitions: {}, monthly: [], shipment_count: 2, offset: 0, limit: 25,
  shipment_rows: [{ shipment_id: "SHJ-2026-101", shipment_key: "SHJ2026101", carrier_key: "registered:1", carrier: "Swift Lines Inc", carrier_names: ["Swift Lines"], date: "2026-10-01", date_basis: "pickup", final_status: "DISCREPANCIES DETECTED", completed: true, delivery_status: "ON TIME", measured: true, not_measured_reason: null, days_late: 0, invoices: 1, invoice_total: 1675, currency: "USD", arithmetic_status: "MATCH", arithmetic_difference: 0, has_contract: true, freight_outcome: "DISCREPANCY", contracted: 1580, contract_variance: 95, complete: true, missing: [], exceptions: [] }],
}

const DETAIL = {
  carrier: SWIFT,
  name_links: [{ name_key: "swift lines", display_name: "Swift Lines", carrier_id: 1, request_id: 3, linked_at: "2026-11-02T09:00:00+00:00", proposed_by: "alice", decided_by: "dave", legal_name: "Swift Lines Inc" }],
  pending_requests: [REQUEST],
  recent_decisions: [],
  history: {
    events: [
      { id: 4, at: "2026-11-05T10:00:00+00:00", actor: "alice", action: "update", carrier_id: 1, request_id: null, before: { scac: null }, after: { scac: "SWFT" } },
      { id: 1, at: "2026-11-01T09:00:00+00:00", actor: "alice", action: "create", carrier_id: 1, request_id: null, before: null, after: {} },
    ],
    total: 2, offset: 0, limit: 50,
  },
  intelligence_key: "registered:1",
}

function detail(session: unknown, overrides: Record<string, unknown> = {}, extra: Record<string, unknown> = {}) {
  const data = { ...DETAIL, ...overrides }
  return mockApi({
    "/api/session": session,
    "/api/carrier-registry/carriers/1": data,
    "/api/carrier-registry/carriers/1/events": data.history ?? { events: [], total: 0, offset: 0, limit: 50 },
    "/api/carriers/registered%3A1": FIGURES,
    ...extra,
  })
}

describe("Carrier detail", () => {
  it("shows the master data, linked names, approvals waiting, figures and history", async () => {
    detail(MANAGER)
    renderPage(<Routes><Route path="/carrier-registry/:id" element={<CarrierRegistryDetail />} /></Routes>, "/carrier-registry/1")

    expect(await screen.findByRole("heading", { name: "Swift Lines Inc" })).toBeInTheDocument()
    const master = screen.getByLabelText("Carrier master data")
    expect(within(master).getByText("MC-123456")).toBeInTheDocument()
    expect(within(master).getByText(/100 Main St/)).toBeInTheDocument()

    const links = screen.getByRole("table", { name: "Linked document names" })
    expect(within(links).getByText("Swift Lines")).toBeInTheDocument()
    expect(within(links).getByText("dave")).toBeInTheDocument()

    expect(await screen.findByRole("table", { name: "Shipments of the carrier" })).toHaveTextContent("SHJ-2026-101")
    expect(screen.getByLabelText("Freight audit statistics")).toHaveTextContent("Discrepancies")
    expect(screen.getByLabelText("Exception statistics")).toHaveTextContent("Disputed")

    const history = await screen.findByRole("list", { name: "Carrier history" })
    expect(history).toHaveTextContent("SCAC: empty → SWFT")
  })

  it("lets a second administrator decide, needs a note to reject, and never the proposer", async () => {
    const decisions: Record<string, unknown>[] = []
    detail(MANAGER, {}, {
      "/api/carrier-registry/links/7/decision": (_url: URL, init?: RequestInit) => {
        decisions.push(bodyOf(init))
        return { body: { ...REQUEST, decision: "rejected" } }
      },
    })
    renderPage(<Routes><Route path="/carrier-registry/:id" element={<CarrierRegistryDetail />} /></Routes>, "/carrier-registry/1")

    await userEvent.click(await screen.findByRole("button", { name: "Reject the request for Swift Line" }))
    const dialog = screen.getByRole("dialog")
    const confirm = within(dialog).getByRole("button", { name: "Reject" })
    expect(confirm).toBeDisabled()
    await userEvent.type(within(dialog).getByLabelText("Why it is rejected"), "Different MC number on the invoices.")
    await userEvent.click(confirm)

    await waitFor(() => expect(decisions).toEqual([{ decision: "reject", note: "Different MC number on the invoices." }]))
  })

  it("tells the proposer that another administrator decides", async () => {
    detail({ ...MANAGER, user: { username: "dave", role: "admin", label: "Administrator" } })
    renderPage(<Routes><Route path="/carrier-registry/:id" element={<CarrierRegistryDetail />} /></Routes>, "/carrier-registry/1")

    expect(await screen.findByText("You proposed this: another administrator decides it.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Approve the request/ })).not.toBeInTheDocument()
  })

  it("shows a reviewer no change buttons and no history", async () => {
    detail(REVIEWER_SESSION, { history: null })
    renderPage(<Routes><Route path="/carrier-registry/:id" element={<CarrierRegistryDetail />} /></Routes>, "/carrier-registry/1")

    expect(await screen.findByRole("heading", { name: "Swift Lines Inc" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument()
    expect(screen.getByText("The carrier history is shown to administrators.")).toBeInTheDocument()
  })

  it("explains that a carrier has no figures until a name is linked", async () => {
    detail(MANAGER, { name_links: [], pending_requests: [], intelligence_key: null })
    renderPage(<Routes><Route path="/carrier-registry/:id" element={<CarrierRegistryDetail />} /></Routes>, "/carrier-registry/1")

    expect(await screen.findByText("No figures yet")).toBeInTheDocument()
    expect(screen.getByText("No document name is linked yet")).toBeInTheDocument()
  })
})

const NAMES = {
  names: [
    {
      name_key: "swift line", name: "Swift Line", spellings: ["Swift Line", "SWIFT LINE"], shipments: 2, shipment_ids: ["SHJ-2026-101", "SHJ-2026-102"],
      documents: [{ shipment_id: "SHJ-2026-101", file: "INV_SHJ-2026-101.pdf", document_type: "INVOICE" }],
      state: "unlinked", linked_carrier: null, pending_request: null, suggestions: [{ id: 1, legal_name: "Swift Lines Inc" }],
    },
  ],
  total: 1, offset: 0, limit: 25, counts: { unlinked: 1, pending: 0, linked: 0 },
}

describe("Name link approvals", () => {
  it("shows each document name with its shipments and documents, and proposes a link with a reason", async () => {
    const proposals: Record<string, unknown>[] = []
    mockApi({
      "/api/session": MANAGER,
      "/api/carrier-registry/links": (_url: URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          proposals.push(bodyOf(init))
          return { status: 201, body: { ...REQUEST, id: 8 } }
        }
        return { body: { requests: [REQUEST], total: 1, offset: 0, limit: 50 } }
      },
      "/api/carrier-registry/document-names": NAMES,
      "/api/carrier-registry/carriers": page([SWIFT, BLUE]),
    })
    renderPage(<CarrierLinks />, "/carrier-registry/links?tab=names")

    const table = await screen.findByRole("table", { name: "Carrier names on the documents" })
    await userEvent.click(within(table).getByText(/2 shipments, 1 document shown/))
    expect(within(table).getByRole("link", { name: "SHJ-2026-101" })).toBeInTheDocument()
    expect(within(table).getByText(/INV_SHJ-2026-101.pdf/)).toBeInTheDocument()
    expect(within(table).getByText(/Same name registered: Swift Lines Inc \(not linked\)/)).toBeInTheDocument()

    await userEvent.click(within(table).getByRole("button", { name: "Propose a link" }))
    const form = await screen.findByRole("form", { name: "Propose a name link" })
    const submit = within(form).getByRole("button", { name: "Propose" })
    await waitFor(() => expect(within(form).getByLabelText("Registered carrier")).toHaveValue("1"))
    expect(submit).toBeDisabled()

    await userEvent.type(within(form).getByLabelText("Reason"), "Remit-to address matches the registered one.")
    await userEvent.click(submit)

    await waitFor(() => expect(proposals).toEqual([{ name: "Swift Line", carrier_id: 1, reason: "Remit-to address matches the registered one." }]))
  })

  it("lists the requests awaiting approval with what each would change", async () => {
    mockApi({ "/api/session": REVIEWER_SESSION, "/api/carrier-registry/links": { requests: [REQUEST], total: 1, offset: 0, limit: 50 } })
    renderPage(<CarrierLinks />, "/carrier-registry/links")

    const table = await screen.findByRole("table", { name: "Link requests awaiting approval" })
    expect(within(table).getByRole("link", { name: "Swift Lines Inc" })).toBeInTheDocument()
    expect(within(table).getByText("Same remit-to address on every invoice.")).toBeInTheDocument()
    expect(within(table).getByText("An administrator decides")).toBeInTheDocument()
    expect(screen.getByText(/Proposing and deciding links is for administrators/)).toBeInTheDocument()
  })
})
