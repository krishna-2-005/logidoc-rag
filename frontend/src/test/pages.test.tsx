import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { Route, Routes } from "react-router-dom"
import { afterEach, describe, expect, it, vi } from "vitest"
import App from "@/App"
import { KpiCard } from "@/components/common/KpiCard"
import { StatusBadge } from "@/components/common/StatusBadge"
import { Exceptions, validateAction } from "@/pages/Exceptions"
import { Overview } from "@/pages/Overview"
import { Reports } from "@/pages/Reports"
import { ShipmentDetailPage } from "@/pages/ShipmentDetail"
import { Shipments } from "@/pages/Shipments"
import { ADMIN_SESSION, REVIEWER_SESSION, SHIPMENT, mockApi, renderPage } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const KPIS = {
  identified: 2, reconciled: 2, with_discrepancies: 1, needs_review: 0, no_discrepancies: 1,
  invoice_total: 2980, invoice_total_covered: 2, invoice_variance: 120, invoice_variance_net: 120,
  invoice_variance_covered: 2, discrepancy_rate: 50,
}

const OVERVIEW = {
  empty: false, kpis: KPIS, metric_definitions: { identified: "Distinct shipment IDs." },
  status_counts: { "DISCREPANCIES DETECTED": 1, "NO DISCREPANCIES": 1 },
  category_counts: { Invoice: 1 }, carriers: [{ carrier: "Swift Lines", shipments: 2, with_discrepancies: 1 }],
  exposure: { invoices: 2, verified: 1, mismatches: 1, unverifiable: 0, totals_read: 2, totals_missing: 0, stated_total: 2980, net_difference: 120, absolute_variance: 120, amount_under_review: null, unverifiable_without_total: 0, mixed_currency: false, currencies: [] },
  exposure_note: "A mismatch is an arithmetic finding. It does not prove overbilling.",
  completeness: { ready: 2, incomplete: 0, unusable_files: 0, unidentified_files: 0 },
  reviews: { open: 3, in_review: 0, resolved: 0, dismissed: 0 },
  open_ingestion_errors: 0, unreadable_files: [], unidentified_sources: [],
}

describe("components", () => {
  it("shows every status as text, not by colour alone", () => {
    renderPage(
      <>
        <StatusBadge status="DISCREPANCIES DETECTED" />
        <StatusBadge status="NO DISCREPANCIES" />
        <StatusBadge status="unsupported" />
        <StatusBadge status="something_new" />
      </>,
    )

    expect(screen.getByText("Discrepancies")).toBeInTheDocument()
    expect(screen.getByText("No discrepancies")).toBeInTheDocument()
    expect(screen.getByText("Unsupported")).toBeInTheDocument()
    expect(screen.getByText("Something new")).toBeInTheDocument()
  })

  it("a KPI card shows the value it is given and explains it on request", async () => {
    renderPage(<KpiCard title="Shipments" value="n/a" definition="Distinct shipment IDs." />)

    expect(screen.getByTestId("kpi-value")).toHaveTextContent("n/a")
    expect(screen.getByRole("button", { name: /what "shipments" means/i })).toBeInTheDocument()
  })
})

describe("Overview", () => {
  it("shows skeletons while loading, then the real figures", async () => {
    mockApi({ "/api/overview": OVERVIEW })
    renderPage(<Overview />)

    expect(screen.getAllByRole("status", { hidden: true }).length).toBeGreaterThan(0)

    expect(await screen.findByText("With discrepancies")).toBeInTheDocument()
    expect(screen.getAllByTestId("kpi-value")[0]).toHaveTextContent("2")
    expect(screen.getByText("$120.00")).toBeInTheDocument()
    expect(screen.getByText(/does not prove overbilling/i)).toBeInTheDocument()
  })

  it("shows the server's error with a way to retry", async () => {
    let calls = 0
    mockApi({ "/api/overview": () => (++calls === 1 ? { status: 500, body: { detail: "Something went wrong on the server." } } : { body: OVERVIEW }) })
    renderPage(<Overview />)

    expect(await screen.findByText("Something went wrong on the server.")).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: /try again/i }))
    expect(await screen.findByText("With discrepancies")).toBeInTheDocument()
  })

  it("shows a helpful empty state, not zeros, when there are no documents", async () => {
    mockApi({ "/api/overview": { empty: true, message: "No PDFs in the document folder yet." } })
    renderPage(<Overview />)

    expect(await screen.findByText("No documents yet")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: /add documents/i })).toHaveAttribute("href", "/documents")
    expect(screen.queryByTestId("kpi-value")).not.toBeInTheDocument()
  })

  it("warns that no shipment is clean while an ingestion error is open", async () => {
    mockApi({ "/api/overview": { ...OVERVIEW, open_ingestion_errors: 2 } })
    renderPage(<Overview />)

    expect(await screen.findByText(/2 documents could not be ingested/i)).toBeInTheDocument()
  })

  it("shows a missing invoice variance as n/a", async () => {
    mockApi({ "/api/overview": { ...OVERVIEW, kpis: { ...KPIS, invoice_variance: null, invoice_variance_covered: 0 } } })
    renderPage(<Overview />)

    expect(await screen.findByText("No invoice could be checked")).toBeInTheDocument()
    expect(screen.getAllByTestId("kpi-value")[3]).toHaveTextContent("n/a")
  })
})

describe("Shipment Explorer", () => {
  const LIST = { shipments: [SHIPMENT], total: 1, carriers: ["Swift Lines"], statuses: ["DISCREPANCIES DETECTED"], categories: ["Invoice"] }

  it("lists shipments and sends filters to the server (never filters audit data itself)", async () => {
    const { calls } = mockApi({ "/api/shipments": LIST })
    renderPage(<Shipments />)

    expect(await screen.findByRole("link", { name: "SHJ-2026-001" })).toBeInTheDocument()
    expect(screen.getByText("$1,550.00")).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText("Carrier"), "Swift Lines")
    await userEvent.click(screen.getByLabelText("Invoice mismatch"))

    await waitFor(() => {
      const last = calls[calls.length - 1].url
      expect(last.searchParams.getAll("carrier")).toEqual(["Swift Lines"])
      expect(last.searchParams.get("invoice_mismatch")).toBe("true")
    })
  })

  it("explains an empty result and offers to clear the filters", async () => {
    mockApi({ "/api/shipments": () => ({ body: { ...LIST, shipments: [] } }) })
    renderPage(<Shipments />)

    expect(await screen.findByText("No shipment matches these filters")).toBeInTheDocument()
    expect(screen.getAllByRole("button", { name: /clear filters/i }).length).toBeGreaterThan(0)
  })

  it("tells the user to add documents when there are no shipments at all", async () => {
    mockApi({ "/api/shipments": { shipments: [], total: 0, carriers: [], statuses: [], categories: [] } })
    renderPage(<Shipments />)

    expect(await screen.findByText("No shipments yet")).toBeInTheDocument()
  })
})

describe("Shipment detail", () => {
  const DETAIL = (id: string) => ({
    shipment_id: id, shipment_key: id.replace(/-/g, ""),
    result: {
      success: true, final_status: "DISCREPANCIES DETECTED", carrier: { bol: "Swift Lines" }, route: { origin: "Chicago", destination: "Atlanta" },
      quantity: { bol: 48, pod: 45, difference: 3, status: "MISMATCH", reason: "", evidence: [] },
      delivery: { expected: "October 3, 2026", actual: "October 5, 2026", status: "DELAYED", days_late: 2, reason: "", evidence: [] },
      invoice: { base_freight: 1200, fuel_surcharge: 180, detention_charge: 0, other_charges: 50, total: 1550, line_items: [["Base Freight", 1200], ["Fuel Surcharge", 180]], line_item_sum: 1430, difference: 120, breakdown: null, status: "MISMATCH", reason: "", evidence: [] },
      discrepancies: [], review_items: [],
    },
    exceptions: [], explanations: [], documents: [{ filename: `BOL_${id}.pdf`, status: "parsed", document_type: "BOL", total_pages: 1, text_pages: 1 }], history: [],
  })

  it("shows the three comparisons, an honest invoice caveat and only this shipment's documents", async () => {
    mockApi({ "/api/shipments/SHJ-2026-001": DETAIL("SHJ-2026-001") })
    renderPage(
      <Routes>
        <Route path="/shipments/:id" element={<ShipmentDetailPage />} />
      </Routes>,
      "/shipments/SHJ-2026-001",
    )

    expect(await screen.findByRole("heading", { name: "SHJ-2026-001" })).toBeInTheDocument()
    expect(screen.getByText("3 pieces short")).toBeInTheDocument()
    expect(screen.getByText("2 days late")).toBeInTheDocument()
    expect(screen.getByText(/does not show overbilling/i)).toBeInTheDocument()
    expect(screen.getByText("BOL_SHJ-2026-001.pdf")).toBeInTheDocument()
    expect(screen.queryByText(/SHJ-2026-002/)).not.toBeInTheDocument()
  })

  it("says so when the shipment does not exist", async () => {
    mockApi({ "/api/shipments/SHJ-9": () => ({ status: 404, body: { detail: "No such shipment." } }) })
    renderPage(
      <Routes>
        <Route path="/shipments/:id" element={<ShipmentDetailPage />} />
      </Routes>,
      "/shipments/SHJ-9",
    )

    expect(await screen.findByText("Shipment not found")).toBeInTheDocument()
  })
})

describe("Exception Center", () => {
  const ITEM = {
    exception_key: "SHJ2026001|invoice", shipment_key: "SHJ2026001", shipment_id: "SHJ-2026-001", category: "Invoice mismatch",
    status: "in_review", assignee: "bob", assigned_at: null, evidence_refs: [], approval_state: null, approval_by: null, resolved_by: null,
    needs_recheck: 0, absent_since: null, created_at: "2026-10-01T00:00:00+00:00", updated_at: "2026-10-02T00:00:00+00:00", note: null, age_days: 4,
    detected: true, description: "Invoice line items do not add up.", severity: "HIGH", kind: "Discrepancy", allowed_actions: ["note", "resolve"],
  }

  const DETAIL = { ...ITEM, evidence: [], review: ITEM, history: [{ id: 1, exception_key: ITEM.exception_key, event_type: "start", from_status: "open", to_status: "in_review", actor: "bob", actor_role: "reviewer", authenticated: 1, assignee: "bob", note: null, evidence_refs: [], created_at: "2026-10-02T00:00:00+00:00" }] }

  const SETTINGS = { minimum_note_length: 10, resolution_needs_evidence: true }

  it("validates the same rules the server enforces", () => {
    expect(validateAction("resolve", "short", ["e"], false, 10, true, "")).toMatch(/at least 10 characters/)
    expect(validateAction("resolve", "Checked the rate sheet.", [], false, 10, true, "")).toMatch(/evidence reference/)
    expect(validateAction("resolve", "Checked the rate sheet.", [], true, 10, true, "")).toBeNull()
    expect(validateAction("resolve", "Checked the rate sheet.", ["INVOICE p.1"], false, 10, true, "")).toBeNull()
    expect(validateAction("dismiss", "", [], false, 10, true, "")).toMatch(/reason/)
    expect(validateAction("assign", "", [], false, 10, true, " ")).toMatch(/who/)
    expect(validateAction("note", "", [], false, 10, true, "")).toMatch(/note or add/)
    expect(validateAction("start", "", [], false, 10, true, "")).toBeNull()
  })

  function setup(overrides: Record<string, unknown> = {}) {
    return mockApi({
      "/api/session": REVIEWER_SESSION,
      "/api/settings": SETTINGS,
      "/api/exceptions": { exceptions: [ITEM], total: 1, assignees: ["bob"], categories: ["Invoice mismatch"], statuses: ["open", "in_review", "resolved", "dismissed"] },
      "/api/exceptions/SHJ2026001%7Cinvoice": DETAIL,
      ...overrides,
    })
  }

  it("keeps Resolve disabled until the note and evidence are valid, then sends them", async () => {
    const { calls } = setup({ "/api/exceptions/SHJ2026001%7Cinvoice/actions": () => ({ body: { review: ITEM, allowed_actions: [] } }) })
    renderPage(<Exceptions />, "/exceptions?key=SHJ2026001%7Cinvoice")

    await screen.findByText("Take action")
    await userEvent.selectOptions(await screen.findByLabelText("What do you want to do?"), "resolve")

    const submit = screen.getByRole("button", { name: "Resolve" })
    expect(submit).toBeDisabled()
    expect(screen.getAllByText(/at least 10 characters/i).length).toBeGreaterThan(0)

    await userEvent.type(screen.getByLabelText(/resolution note/i), "Checked the rate sheet carefully.")
    expect(submit).toBeDisabled()
    expect(screen.getByText(/at least one evidence reference/i)).toBeInTheDocument()

    await userEvent.type(screen.getByLabelText(/evidence references/i), "INVOICE_SHJ-2026-001.pdf p.1")
    expect(submit).toBeEnabled()

    await userEvent.click(submit)

    await waitFor(() => {
      const post = calls.find((call) => call.init?.method === "POST")
      expect(post).toBeDefined()
      expect(JSON.parse(String(post!.init!.body))).toMatchObject({ action: "resolve", note: "Checked the rate sheet carefully.", evidence: ["INVOICE_SHJ-2026-001.pdf p.1"] })
    })
  })

  it("shows the server's refusal as it was given", async () => {
    setup({ "/api/exceptions/SHJ2026001%7Cinvoice/actions": () => ({ status: 403, body: { detail: "This exception is assigned to carol." } }) })
    renderPage(<Exceptions />, "/exceptions?key=SHJ2026001%7Cinvoice")

    await userEvent.selectOptions(await screen.findByLabelText("What do you want to do?"), "note")
    await userEvent.type(screen.getByLabelText("Note"), "A note about the carrier call.")
    await userEvent.click(screen.getByRole("button", { name: "Add a note or evidence" }))

    expect(await screen.findByText("This exception is assigned to carol.")).toBeInTheDocument()
  })

  it("shows who changed what and when, including whether the user was signed in", async () => {
    setup()
    renderPage(<Exceptions />, "/exceptions?key=SHJ2026001%7Cinvoice")

    const history = await screen.findByText("Review started")
    const row = history.closest("tr") as HTMLElement

    expect(within(row).getByText("bob")).toBeInTheDocument()
    expect(within(row).getByText(/reviewer/)).toBeInTheDocument()
    expect(within(row).queryByText(/not authenticated/)).not.toBeInTheDocument()
  })

  it("says plainly when no action is available to the user", async () => {
    setup({ "/api/exceptions/SHJ2026001%7Cinvoice": { ...DETAIL, allowed_actions: [] } })
    renderPage(<Exceptions />, "/exceptions?key=SHJ2026001%7Cinvoice")

    expect(await screen.findByText(/no action is available to you/i)).toBeInTheDocument()
  })

  it("filters on the server by assignee, state and age", async () => {
    const { calls } = setup()
    renderPage(<Exceptions />, "/exceptions")

    await screen.findByText("Invoice mismatch", { selector: "td" })
    await userEvent.selectOptions(screen.getByLabelText("Assignee"), "bob")
    await userEvent.selectOptions(screen.getByLabelText("State"), "in_review")
    await userEvent.type(screen.getByLabelText("At least (days old)"), "7")

    await waitFor(() => {
      const url = calls.filter((call) => call.url.pathname === "/api/exceptions").pop()!.url
      expect(url.searchParams.get("assignee")).toBe("bob")
      expect(url.searchParams.getAll("status")).toEqual(["in_review"])
      expect(url.searchParams.get("min_age_days")).toBe("7")
    })
  })

  it("shows an empty state for an empty queue", async () => {
    setup({ "/api/exceptions": { exceptions: [], total: 0, assignees: [], categories: [], statuses: [] } })
    renderPage(<Exceptions />, "/exceptions")

    expect(await screen.findByText("No exceptions")).toBeInTheDocument()
  })
})

describe("Reports", () => {
  const SHIPMENTS = { shipments: [SHIPMENT], total: 1, carriers: [], statuses: [], categories: [] }

  function stubDownload() {
    URL.createObjectURL = vi.fn(() => "blob:x")
    URL.revokeObjectURL = vi.fn()
    return vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {})
  }

  it("downloads the shipment CSV and PDF through the API with the user's session", async () => {
    const click = stubDownload()
    const { calls } = mockApi({
      "/api/session": ADMIN_SESSION,
      "/api/shipments": SHIPMENTS,
      "/api/reports/shipments/SHJ-2026-001.csv": () => ({ text: "a", headers: { "Content-Disposition": 'attachment; filename="freight_audit_SHJ-2026-001.csv"' } }),
      "/api/reports/shipments/SHJ-2026-001.pdf": () => ({ text: "%PDF", headers: { "Content-Type": "application/pdf" } }),
    })
    renderPage(<Reports />)

    await screen.findByLabelText("Shipment")
    const csv = screen.getAllByRole("button", { name: /download csv/i })[0]
    await waitFor(() => expect(csv).toBeEnabled())

    await userEvent.click(csv)
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1))

    // Buttons are disabled while a report is being prepared
    const pdf = screen.getByRole("button", { name: /download pdf/i })
    await waitFor(() => expect(pdf).toBeEnabled())
    await userEvent.click(pdf)

    await waitFor(() => expect(click).toHaveBeenCalledTimes(2))
    expect(calls.map((call) => call.url.pathname)).toEqual(expect.arrayContaining(["/api/reports/shipments/SHJ-2026-001.csv", "/api/reports/shipments/SHJ-2026-001.pdf"]))
    expect(await screen.findByText("Downloaded freight_audit_SHJ-2026-001.csv")).toBeInTheDocument()
  })

  it("shows a failed download as an error, not a silent nothing", async () => {
    stubDownload()
    mockApi({
      "/api/session": ADMIN_SESSION,
      "/api/shipments": SHIPMENTS,
      "/api/reports/register.csv": () => ({ status: 403, body: { detail: "The Auditor / reviewer role cannot perform this action (reports.download)." } }),
    })
    renderPage(<Reports />)

    const card = (await screen.findByText("Shipment register")).closest("div.rounded-lg") as HTMLElement
    const button = within(card).getByRole("button", { name: /download csv/i })
    await waitFor(() => expect(button).toBeEnabled())
    await userEvent.click(button)

    expect(await screen.findByText(/cannot perform this action/i)).toBeInTheDocument()
  })

  it("disables downloads for a role that may not download", async () => {
    mockApi({ "/api/session": { ...REVIEWER_SESSION, permissions: ["exceptions.view"] }, "/api/shipments": SHIPMENTS })
    renderPage(<Reports />)

    expect(await screen.findByText(/cannot download reports/i)).toBeInTheDocument()
    screen.getAllByRole("button", { name: /download/i }).forEach((button) => expect(button).toBeDisabled())
  })
})

describe("App and sign-in", () => {
  it("shows only the sign-in form to a visitor when sign-in is required", async () => {
    mockApi({ "/api/session": { mode: "required", authenticated: false, user: null, permissions: [] } })
    renderPage(<App />)

    expect(await screen.findByRole("heading", { name: /sign in to logidoc/i })).toBeInTheDocument()
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: /sign in/i })).toBeDisabled()
  })

  it("signs in through the API and then shows the application", async () => {
    let signedIn = false
    const { calls } = mockApi({
      "/api/session": () => ({ body: signedIn ? ADMIN_SESSION : { mode: "required", authenticated: false, user: null, permissions: [] } }),
      "/api/auth/login": () => { signedIn = true; return { body: ADMIN_SESSION } },
      "/api/overview": { empty: true, message: "No PDFs." },
    })
    renderPage(<App />)

    await userEvent.type(await screen.findByLabelText("User name"), "alice")
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery")
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }))

    expect(await screen.findByRole("navigation", { name: "Main" })).toBeInTheDocument()
    expect(JSON.parse(String(calls.find((call) => call.url.pathname === "/api/auth/login")!.init!.body))).toEqual({ username: "alice", password: "correct horse battery" })
    expect(screen.getByText("alice")).toBeInTheDocument()
  })

  it("shows a refused sign-in and stays on the form", async () => {
    mockApi({
      "/api/session": { mode: "required", authenticated: false, user: null, permissions: [] },
      "/api/auth/login": () => ({ status: 401, body: { detail: "Wrong user name or password." } }),
    })
    renderPage(<App />)

    await userEvent.type(await screen.findByLabelText("User name"), "bob")
    await userEvent.type(screen.getByLabelText("Password"), "wrong password here")
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }))

    expect(await screen.findByText("Wrong user name or password.")).toBeInTheDocument()
  })

  it("warns in local development mode that actions are not authenticated", async () => {
    mockApi({
      "/api/session": { mode: "local", authenticated: false, user: { username: "local-dev", role: "admin", label: "Administrator" }, permissions: [] },
      "/api/overview": { empty: true, message: "No PDFs." },
    })
    renderPage(<App />)

    expect(await screen.findAllByText(/local development mode/i)).not.toHaveLength(0)
    expect(screen.queryByRole("button", { name: /sign out/i })).not.toBeInTheDocument()
  })

  it("explains an unreachable server instead of showing a blank page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")))
    renderPage(<App />)

    expect(await screen.findByText("The server is not reachable")).toBeInTheDocument()
  })

  it("lists every screen in the navigation", async () => {
    mockApi({ "/api/session": ADMIN_SESSION, "/api/overview": { empty: true, message: "x" } })
    renderPage(<App />)

    const nav = await screen.findByRole("navigation", { name: "Main" })

    for (const name of ["Overview", "Shipment Explorer", "Document Intelligence", "Freight Audit", "Exception Center", "Audit History", "Document Completeness", "Reports", "Settings"]) {
      expect(within(nav).getByRole("link", { name })).toBeInTheDocument()
    }
  })
})

describe("AI Assistant", () => {
  const REPLY = {
    status: "answered", shipment_id: "SHJ-2026-001", answer: "BOL: 48 pieces; POD delivered: 45. 3 piece(s) short.",
    findings: [{ label: "Difference (BOL - POD)", value: 3, status: "MISMATCH" }],
    citations: [{ source: "BOL_SHJ-2026-001.pdf", page: 1, document_type: "BOL", shipment_id: "SHJ-2026-001", text: "Number of Pieces: 48" }],
    uncertainty: [], next_steps: [], tools_used: [{ name: "get_reconciliation_result", arguments: {} }], generated: false,
  }

  it("sends the question with the chosen shipment and shows answer, sources and tools", async () => {
    const { calls } = mockApi({
      "/api/shipments": { shipments: [SHIPMENT], total: 1, carriers: [], statuses: [], categories: [] },
      "/api/assistant": () => ({ body: REPLY }),
    })
    const { Assistant } = await import("@/pages/Assistant")
    renderPage(<Assistant />)

    await screen.findByRole("option", { name: "SHJ-2026-001" })
    await userEvent.selectOptions(screen.getByLabelText("Shipment"), "SHJ-2026-001")
    await userEvent.type(screen.getByLabelText("Your question"), "How many pieces were short?")
    await userEvent.click(screen.getByRole("button", { name: /ask/i }))

    expect(await screen.findByText(/3 piece\(s\) short/)).toBeInTheDocument()
    expect(screen.getByText(/BOL_SHJ-2026-001.pdf · p.1/)).toBeInTheDocument()
    expect(screen.getByText("get_reconciliation_result")).toBeInTheDocument()
    expect(JSON.parse(String(calls.find((c) => c.url.pathname === "/api/assistant")!.init!.body))).toEqual({ question: "How many pieces were short?", shipment_id: "SHJ-2026-001" })
  })

  it("shows insufficient evidence and refusals distinctly, and the server error", async () => {
    let n = 0
    mockApi({
      "/api/shipments": { shipments: [], total: 0, carriers: [], statuses: [], categories: [] },
      "/api/assistant": () => (++n === 1 ? { body: { ...REPLY, status: "insufficient_evidence", answer: "Could not verify.", uncertainty: ["POD pieces was not found."], citations: [] } } : n === 2 ? { body: { ...REPLY, status: "refused", answer: "Limited to SHJ-2026-001.", citations: [], tools_used: [] } } : { status: 403, body: { detail: "Not allowed." } }),
    })
    const { Assistant } = await import("@/pages/Assistant")
    renderPage(<Assistant />)

    for (const q of ["one", "two", "three"]) {
      await userEvent.type(await screen.findByLabelText("Your question"), q)
      await userEvent.click(screen.getByRole("button", { name: /ask/i }))
      await waitFor(() => expect(screen.getByLabelText("Your question")).toHaveValue(""))
    }

    expect(await screen.findByText("Insufficient evidence")).toBeInTheDocument()
    expect(screen.getByText("POD pieces was not found.")).toBeInTheDocument()
    expect(screen.getByText("Refused")).toBeInTheDocument()
    expect(await screen.findByText("Not allowed.")).toBeInTheDocument()
  })
})
