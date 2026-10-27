import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { Route, Routes, useLocation } from "react-router-dom"
import { afterEach, describe, expect, it, vi } from "vitest"
import App from "@/App"
import { AskPanel } from "@/components/common/AskPanel"
import { Invoices } from "@/pages/Invoices"
import { Overview } from "@/pages/Overview"
import { Shipments } from "@/pages/Shipments"
import { ADMIN_SESSION, SHIPMENT, mockApi, renderPage } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const SUMMARY = {
  invoices: 3, verified: 1, mismatches: 2, unverifiable: 0, totals_read: 3, totals_missing: 0, stated_total: 6205.5, net_difference: 70,
  absolute_variance: 170, amount_under_review: null, unverifiable_without_total: 0, mixed_currency: false, currencies: [],
}

const invoice = (shipment_id: string, classification: string, stated: number, items: number) => ({
  shipment_id, shipment_key: shipment_id.replace(/-/g, ""), carrier: "Swift Lines", invoice_status: classification === "verified" ? "MATCH" : "MISMATCH",
  classification, stated_total: stated, line_item_sum: items, signed_difference: stated - items, absolute_difference: Math.abs(stated - items),
  amount_under_review: null, currency: null, discrepancy_categories: [],
})

const INVOICES = [invoice("SHJ-2026-002", "verified", 2875.5, 2875.5), invoice("SHJ-2026-010", "mismatch", 1780, 1830), invoice("SHJ-2026-001", "mismatch", 1550, 1430)]

/** /api/exposure answering for the classification asked, as the server does */
const exposure = (url: URL) => {
  const wanted = url.searchParams.getAll("classification")
  const rows = wanted.length ? INVOICES.filter((row) => wanted.includes(row.classification)) : INVOICES
  return { body: { rows, summary: SUMMARY, by_carrier: [], by_category: [], metric_definitions: {}, note: "A mismatch is an arithmetic finding." } }
}

const OVERVIEW = {
  empty: false,
  kpis: { identified: 3, reconciled: 3, with_discrepancies: 2, needs_review: 0, no_discrepancies: 1, invoice_total: 6205.5, invoice_total_covered: 3, invoice_variance: 170, invoice_variance_net: 70, invoice_variance_covered: 3, discrepancy_rate: 66.7 },
  metric_definitions: {},
  status_counts: { "DISCREPANCIES DETECTED": 2, "NO DISCREPANCIES": 1 },
  category_counts: { Invoice: 2 },
  carriers: [],
  exposure: { ...SUMMARY },
  exposure_note: "A mismatch is an arithmetic finding.",
  completeness: { ready: 3, incomplete: 1, unusable_files: 0, unidentified_files: 0 },
  reviews: { open: 4, in_review: 0, resolved: 0, dismissed: 0 },
  open_ingestion_errors: 0, unreadable_files: [], unidentified_sources: [],
}

const LIST = { shipments: [SHIPMENT], total: 3, carriers: ["Swift Lines"], statuses: ["DISCREPANCIES DETECTED"], categories: ["Invoice"] }

const DASHBOARD = {
  "/api/overview": OVERVIEW,
  "/api/shipments": LIST,
  "/api/exposure": exposure,
  "/api/documents": { files: [], completeness: { shipments: [], unidentified: [], unusable: [], duplicates: [] } },
}

function Where() {
  const location = useLocation()
  return <p data-testid="location">{location.pathname + location.search}</p>
}

describe("Overview key figures", () => {
  it("every card opens the records behind its figure", async () => {
    mockApi(DASHBOARD)
    renderPage(<Overview />)

    const expected: Record<string, string> = {
      Shipments: "/shipments",
      "With discrepancies": "/shipments?status=DISCREPANCIES%20DETECTED",
      "Needs review": "/shipments?review=1",
      "Invoice variance": "/invoices?check=mismatch",
      "Stated invoice total": "/invoices",
      "Arithmetic mismatches": "/invoices?check=mismatch",
      "Verified invoices": "/invoices?check=verified",
      "Could not be verified": "/invoices?check=unverifiable",
      Ready: "/completeness",
      Incomplete: "/completeness?incomplete=1",
      "Files not used": "/completeness",
    }

    for (const [name, href] of Object.entries(expected)) {
      expect(await screen.findByRole("link", { name })).toHaveAttribute("href", href)
    }
  })

  it("opens the mismatched invoices from the invoice variance card", async () => {
    mockApi(DASHBOARD)
    renderPage(
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/invoices" element={<Where />} />
      </Routes>,
    )

    await userEvent.click(await screen.findByRole("link", { name: "Invoice variance" }))
    expect(await screen.findByTestId("location")).toHaveTextContent("/invoices?check=mismatch")
  })
})

describe("Invoice Register", () => {
  it("asks the server for the invoices of the chosen check and lists the largest difference first", async () => {
    const { calls } = mockApi({ "/api/exposure": exposure })
    renderPage(<Invoices />, "/invoices?check=mismatch")

    expect(await screen.findByText("+$120.00")).toBeInTheDocument()
    expect(screen.getByText("-$50.00")).toBeInTheDocument()
    expect(calls.some((call) => call.url.searchParams.getAll("classification").join() === "mismatch")).toBe(true)

    const shipments = screen.getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell")[0].textContent)
    expect(shipments).toEqual(["SHJ-2026-001", "SHJ-2026-010"])
    expect(screen.getByRole("tab", { name: /Mismatch/ })).toHaveAttribute("aria-selected", "true")
    expect(screen.getByRole("link", { name: "Invoice variance" })).toHaveAttribute("href", "/invoices?check=mismatch")
  })

  it("switches the list with the tabs, still filtered by the server", async () => {
    const { calls } = mockApi({ "/api/exposure": exposure })
    renderPage(<Invoices />, "/invoices")

    await screen.findByText("+$120.00")
    await userEvent.click(screen.getByRole("tab", { name: /Verified/ }))

    await waitFor(() => expect(calls.some((call) => call.url.searchParams.getAll("classification").join() === "verified")).toBe(true))
    await waitFor(() => expect(screen.queryByText("+$120.00")).not.toBeInTheDocument())
    expect(screen.getByRole("link", { name: "SHJ-2026-002" })).toHaveAttribute("href", "/shipments/SHJ-2026-002")
  })
})

describe("Shipment Explorer from a link", () => {
  it("starts with the quick filters and the search the link carries, and sends them to the server", async () => {
    const { calls } = mockApi({ "/api/shipments": LIST })
    renderPage(<Shipments />, "/shipments?invoice=mismatch&delayed=1&review=1&search=SHJ-2026-001")

    await waitFor(() => {
      const last = calls[calls.length - 1].url
      expect(last.searchParams.get("invoice_mismatch")).toBe("true")
      expect(last.searchParams.get("delivery_delayed")).toBe("true")
      expect(last.searchParams.get("needs_review")).toBe("true")
      expect(last.searchParams.get("search")).toBe("SHJ-2026-001")
    })
    expect(screen.getByLabelText("Invoice mismatch")).toBeChecked()
    expect(screen.getByLabelText("Search shipment ID")).toHaveValue("SHJ-2026-001")
  })
})

describe("Ask the documents", () => {
  const reply = (init?: RequestInit) => {
    const body = JSON.parse(String(init?.body))
    const label = body.shipment_id ? `Shipment ${body.shipment_id} (selected)` : "All shipments"
    return { body: { answer: "Net 30.", status: "answered", answer_method: "exact", generated: true, scope: { label }, evidence: [], notice: "Generated from the indexed documents." } }
  }

  it("asks about one shipment when one is chosen, and about all of them otherwise", async () => {
    const { calls } = mockApi({ "/api/shipments": { ...LIST, shipments: [SHIPMENT, { ...SHIPMENT, shipment_id: "SHJ-2026-002", shipment_key: "SHJ2026002" }] }, "/api/chat": (_url: URL, init?: RequestInit) => reply(init) })
    renderPage(<AskPanel />)
    const sent = () => calls.filter((call) => call.url.pathname === "/api/chat").map((call) => JSON.parse(String(call.init?.body)))

    await screen.findByRole("option", { name: "SHJ-2026-002" })
    await userEvent.selectOptions(screen.getByLabelText("Shipments to search"), "SHJ-2026-002")
    await userEvent.type(screen.getByLabelText("Your question"), "What are the payment terms?")
    await userEvent.click(screen.getByRole("button", { name: /Ask/ }))

    expect(await screen.findByText(/searched: Shipment SHJ-2026-002 \(selected\)/)).toBeInTheDocument()
    expect(sent()).toEqual([{ question: "What are the payment terms?", shipment_id: "SHJ-2026-002" }])

    await userEvent.selectOptions(screen.getByLabelText("Shipments to search"), "")
    await userEvent.click(screen.getByRole("button", { name: /Ask/ }))
    await waitFor(() => expect(sent()).toHaveLength(2))
    expect(sent()[1]).toEqual({ question: "What are the payment terms?" })
  })

  it("on a shipment's own page asks about that shipment only, with no list to choose from", async () => {
    const { calls } = mockApi({ "/api/chat": (_url: URL, init?: RequestInit) => reply(init) })
    renderPage(<AskPanel shipmentId="SHJ-2026-001" />)

    expect(screen.queryByLabelText("Shipments to search")).not.toBeInTheDocument()
    await userEvent.type(screen.getByLabelText("Your question"), "Was it late?")
    await userEvent.click(screen.getByRole("button", { name: /Ask/ }))

    expect(await screen.findByText(/searched: Shipment SHJ-2026-001/)).toBeInTheDocument()
    expect(calls.map((call) => call.url.pathname)).toEqual(["/api/chat"])
  })
})

describe("Application shell", () => {
  const READY = { status: "ready", checks: { documents: { ok: true }, data_writable: { ok: true }, extraction_store: { ok: true }, model_service: { ok: true } } }

  it("shows the server's readiness, live counts outside the link names, and the invoice register", async () => {
    mockApi({ ...DASHBOARD, "/api/session": ADMIN_SESSION, "/api/ready": READY })
    renderPage(<App />)

    expect(await screen.findByText("All systems ready")).toBeInTheDocument()
    const nav = screen.getByRole("navigation", { name: "Main" })
    await waitFor(() => expect(within(nav).getByText("4")).toBeInTheDocument())
    expect(within(nav).getByRole("link", { name: "Exception Center" })).toHaveAttribute("href", "/exceptions")
    expect(within(nav).getByRole("link", { name: "Invoice Register" })).toHaveAttribute("href", "/invoices")
  })

  it("says the server is not ready when the readiness check answers 503", async () => {
    mockApi({ ...DASHBOARD, "/api/session": ADMIN_SESSION, "/api/ready": () => ({ status: 503, body: { status: "not_ready", checks: { documents: { ok: false } } } }) })
    renderPage(<App />)

    expect(await screen.findByText("Not ready")).toBeInTheDocument()
  })

  it("finds a shipment from the search box in the top bar", async () => {
    const { calls } = mockApi({ ...DASHBOARD, "/api/session": ADMIN_SESSION, "/api/ready": READY })
    renderPage(<App />)

    await userEvent.type(await screen.findByLabelText("Find a shipment"), "SHJ-2026-001{enter}")

    expect(await screen.findByRole("heading", { name: "Shipment Explorer" })).toBeInTheDocument()
    await waitFor(() => expect(calls.some((call) => call.url.pathname === "/api/shipments" && call.url.searchParams.get("search") === "SHJ-2026-001")).toBe(true))
  })
})
