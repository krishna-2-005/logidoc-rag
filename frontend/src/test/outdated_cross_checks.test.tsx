import { cleanup, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { RecomputeStatus, auditFieldLabel } from "@/components/extraction/RecomputeStatus"
import { NA } from "@/lib/format"
import { RunsTab } from "@/pages/History"
import { ADMIN_SESSION, REVIEWER_SESSION, mockApi, renderPage } from "@/test/utils"

// Fixtures follow GET /api/extractions/staleness (src/api/routes_extraction.py). Synthetic; no real document is involved.

afterEach(() => vi.unstubAllGlobals())

const REVIEWER = { ...REVIEWER_SESSION, permissions: [...REVIEWER_SESSION.permissions, "extraction.view", "history.view"] }
const ADMIN = { ...ADMIN_SESSION, permissions: [...ADMIN_SESSION.permissions, "extraction.view", "extraction.run", "history.view", "history.record", "admin.maintenance"] }

const OUTDATED = {
  shipment_key: "SHJ2026001", shipment_id: "SHJ-2026-001", fields: ["delivered_pieces", "actual_date"],
  extractor_versions: ["1.1+oldparser"], current_version: "1.1+newparser",
}
const OTHER = { ...OUTDATED, shipment_key: "SHJ2026002", shipment_id: "SHJ-2026-002", fields: ["invoice_total"], extractor_versions: ["1.1+a", "1.1+b"] }
const STALE_RUN = { shipment_key: "SHJ2026001", recorded_run_id: 1, missing_correction_ids: [5], stale_run_ids: [1] }

const show = (session: unknown, staleness: unknown, shipmentKey?: string) => {
  const api = mockApi({ "/api/session": session, "/api/extractions/staleness": staleness })
  renderPage(<RecomputeStatus shipmentKey={shipmentKey} />)

  return api
}

describe("outdated structured cross-checks", () => {
  it("shows the shipment, the affected fields and both the stored and the current extractor version", async () => {
    show(REVIEWER, { stale: [], corrections_available: true, outdated_cross_checks: [OUTDATED, OTHER] })

    const first = await screen.findByTestId("outdated-SHJ2026001")

    expect(first).toHaveTextContent("The structured cross-check is out of date for SHJ-2026-001.")
    expect(first).toHaveTextContent("Affected fields: Delivered pieces, Actual delivery date")
    expect(first).not.toHaveTextContent("delivered_pieces")
    expect(first).not.toHaveTextContent("actual_date")
    expect(first).toHaveTextContent("extractor version 1.1+oldparser")
    expect(first).toHaveTextContent("the current version is 1.1+newparser")
    expect(screen.getByTestId("outdated-SHJ2026002")).toHaveTextContent("extractor version 1.1+a, 1.1+b")
  })

  it("does not say that any run was recalculated or recorded, and offers no recording for it", async () => {
    const api = show(ADMIN, { stale: [], corrections_available: true, outdated_cross_checks: [OUTDATED] })
    const alert = await screen.findByTestId("outdated-SHJ2026001")

    expect(alert).toHaveTextContent("no audit run has been recalculated or recorded because of this")
    expect(alert).toHaveTextContent("The audit values are read from the documents and are unchanged")
    expect(screen.queryByText(/Recorded run/)).toBeNull()
    expect(screen.queryByRole("button", { name: /Record/ })).toBeNull()
    expect(api.calls.filter((call) => call.init?.method === "POST")).toEqual([])
  })

  it("keeps the pending-correction notice separate from the outdated cross-check notice", async () => {
    show(ADMIN, { stale: [STALE_RUN], corrections_available: true, outdated_cross_checks: [OUTDATED] })

    expect(await screen.findByText(/A recalculated audit run is waiting to be recorded/)).toBeInTheDocument()
    expect(screen.getByTestId("outdated-SHJ2026001")).toBeInTheDocument()
    expect(screen.getAllByRole("button", { name: /Record the recalculated run/ })).toHaveLength(1)
  })

  it("shows only the open shipment's notice when the screen is scoped to one shipment", async () => {
    show(REVIEWER, { stale: [], corrections_available: true, outdated_cross_checks: [OUTDATED, OTHER] }, "SHJ2026002")

    const alert = await screen.findByTestId("outdated-SHJ2026002")

    expect(alert).toHaveTextContent("The structured cross-check is out of date.")
    expect(alert).not.toHaveTextContent("SHJ-2026-002.")
    expect(screen.queryByTestId("outdated-SHJ2026001")).toBeNull()
  })

  it("shows nothing when the list is empty, and says so in the existing way", async () => {
    show(REVIEWER, { stale: [], corrections_available: true, outdated_cross_checks: [] })

    expect(await screen.findByText("No recorded run is waiting for an approved correction.")).toBeInTheDocument()
    expect(screen.queryByText(/out of date/)).toBeNull()
  })

  it("shows nothing for a server that does not send the field", async () => {
    show(REVIEWER, { stale: [], corrections_available: true })

    expect(await screen.findByText("No recorded run is waiting for an approved correction.")).toBeInTheDocument()
    expect(screen.queryByText(/out of date/)).toBeNull()
  })

  it("shows nothing for a shipment other than the one open", async () => {
    show(REVIEWER, { stale: [], corrections_available: true, outdated_cross_checks: [OTHER] }, "SHJ2026001")

    expect(await screen.findByText("No recorded run is waiting for an approved correction.")).toBeInTheDocument()
    expect(screen.queryByTestId("outdated-SHJ2026002")).toBeNull()
  })

  it("reports a failed check instead of showing an empty or stale list, and offers a retry", async () => {
    mockApi({ "/api/session": REVIEWER, "/api/extractions/staleness": () => ({ status: 500, body: { detail: "Something went wrong on the server." } }) })
    renderPage(<RecomputeStatus />)

    expect(await screen.findByText(/Whether a recalculation is pending could not be checked/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Try again/ })).toBeInTheDocument()
    expect(screen.queryByText(/out of date/)).toBeNull()
    expect(screen.queryByText("No recorded run is waiting for an approved correction.")).toBeNull()
  })

  it("still shows the outdated notice when the correction store is unavailable, without claiming 'nothing is waiting'", async () => {
    show(REVIEWER, { stale: [], corrections_available: false, outdated_cross_checks: [OUTDATED] })

    expect(await screen.findByTestId("outdated-SHJ2026001")).toBeInTheDocument()
    expect(screen.queryByText("No recorded run is waiting for an approved correction.")).toBeNull()
  })

  it("shows nothing and requests nothing for a user who may not view history", async () => {
    const api = mockApi({ "/api/session": { ...REVIEWER_SESSION, permissions: [] } })
    renderPage(<RecomputeStatus />)

    await vi.waitFor(() => expect(api.calls.some((call) => call.url.pathname === "/api/session")).toBe(true))

    expect(screen.queryByText(/out of date/)).toBeNull()
    expect(api.calls.some((call) => call.url.pathname === "/api/extractions/staleness")).toBe(false)
  })

  it("shows a loading state without an outdated notice before the answer arrives", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => undefined)))
    renderPage(<RecomputeStatus />)

    expect(screen.queryByText(/out of date/)).toBeNull()
  })
})

describe("field labels", () => {
  it("names every audit field in words and keeps the ids out of the screen", () => {
    expect(["expected_pieces", "delivered_pieces", "expected_date", "actual_date", "invoice_total"].map(auditFieldLabel)).toEqual([
      "Expected pieces", "Delivered pieces", "Expected delivery date", "Actual delivery date", "Invoice total",
    ])
  })

  it("falls back to the id's own words for an unknown field, and to the app's not-available text for an empty one", () => {
    expect(auditFieldLabel("some_new_field")).toBe("Some new field")
    expect(auditFieldLabel("")).toBe(NA)
  })

  it("shows an unknown field from the server as words, and never as markup", async () => {
    show(REVIEWER, { stale: [], corrections_available: true, outdated_cross_checks: [{ ...OUTDATED, fields: ["some_new_field", "<b>x</b>"] }] })

    const alert = await screen.findByTestId("outdated-SHJ2026001")

    expect(alert).toHaveTextContent("Affected fields: Some new field, <b>x</b>")
    expect(alert.querySelector("b")).toBeNull()
  })
})

describe("who can act on an outdated cross-check", () => {
  it("tells a user who can run the extraction where to do it, with no automatic request", async () => {
    const api = show(ADMIN, { stale: [], corrections_available: true, outdated_cross_checks: [OUTDATED] })
    const alert = await screen.findByTestId("outdated-SHJ2026001")

    expect(alert).toHaveTextContent("Run the extraction again for this shipment's documents (Extract fields)")
    expect(alert).not.toHaveTextContent("You cannot run the extraction")
    expect(within(alert).queryByRole("button")).toBeNull()                          // the notice itself offers no action
    expect(api.calls.filter((call) => call.init?.method === "POST")).toEqual([])
    expect(api.calls.map((call) => call.url.pathname).sort()).toEqual(["/api/extractions/staleness", "/api/session"])
  })

  it("tells a user who cannot run it to ask an authorized reviewer or administrator, and shows no action", async () => {
    const api = show(REVIEWER, { stale: [], corrections_available: true, outdated_cross_checks: [OUTDATED] })
    const alert = await screen.findByTestId("outdated-SHJ2026001")

    expect(alert).toHaveTextContent("You cannot run the extraction. Ask an authorized reviewer or administrator to run it again.")
    expect(alert).not.toHaveTextContent("(Extract fields)")
    expect(within(alert).queryByRole("button")).toBeNull()
    expect(api.calls.filter((call) => call.init?.method === "POST")).toEqual([])
  })

  it("keeps the warning, the version details and the no-recalculation statement for both kinds of user", async () => {
    for (const session of [REVIEWER, ADMIN]) {
      const view = show(session, { stale: [], corrections_available: true, outdated_cross_checks: [OUTDATED] })
      const alert = await screen.findByTestId("outdated-SHJ2026001")

      expect(alert).toHaveTextContent("1.1+oldparser")
      expect(alert).toHaveTextContent("1.1+newparser")
      expect(alert).toHaveTextContent("no audit run has been recalculated or recorded because of this")
      expect(view.calls.filter((call) => call.init?.method === "POST")).toEqual([])
      cleanup()
    }
  })
})

describe("the History page's Stale badge", () => {
  const RUN = (id: number) => ({
    id, created_at: "2026-10-22T10:00:00+00:00", label: "All shipments", shipments_checked: 1, snapshots_written: 1, calculation_version: "2",
    supersedes: [], superseded_by: [],
  })

  const history = (staleness: unknown) => {
    const api = mockApi({ "/api/session": REVIEWER, "/api/history/runs": [RUN(1)], "/api/extractions/staleness": staleness })
    renderPage(<RunsTab />)

    return api
  }

  it("says what it means: approved corrections missing from the latest recorded run, not the extractor version", async () => {
    history({ stale: [STALE_RUN], corrections_available: true, outdated_cross_checks: [] })

    const badge = await screen.findByText("Stale: missing approved corrections")

    expect(badge).toHaveAttribute("title", expect.stringContaining("does not include approved corrections the audit now reads"))
    expect(badge).toHaveAttribute("title", expect.stringContaining("not about the extractor version"))
  })

  it("does not mark a run stale because a cross-check is outdated", async () => {
    const api = history({ stale: [], corrections_available: true, outdated_cross_checks: [OUTDATED] })

    expect(await screen.findByText("Not superseded")).toBeInTheDocument()
    expect(screen.queryByText(/Stale/)).toBeNull()
    expect(screen.queryByText(/out of date/)).toBeNull()                              // no cross-check claim on a historical row
    expect(api.calls.filter((call) => call.init?.method === "POST")).toEqual([])
  })

  it("marks a run stale only for missing corrections even when both conditions hold", async () => {
    history({ stale: [STALE_RUN], corrections_available: true, outdated_cross_checks: [OUTDATED] })

    expect(await screen.findByText("Stale: missing approved corrections")).toBeInTheDocument()
    expect(screen.queryByText(/out of date/)).toBeNull()
  })
})
