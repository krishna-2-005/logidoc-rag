import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { lazy } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import App from "@/App"
import { RouteBoundary } from "@/components/common/RouteBoundary"
import { ADMIN_SESSION, SHIPMENT, mockApi, renderPage } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const READY = { status: "ready", checks: { documents: { ok: true }, data_writable: { ok: true }, extraction_store: { ok: true }, model_service: { ok: true } } }

const SUMMARY = {
  invoices: 1, verified: 1, mismatches: 0, unverifiable: 0, totals_read: 1, totals_missing: 0, stated_total: 100, net_difference: 0,
  absolute_variance: 0, amount_under_review: null, unverifiable_without_total: 0, mixed_currency: false, currencies: [],
}

const API = {
  "/api/session": ADMIN_SESSION,
  "/api/ready": READY,
  "/api/overview": {
    empty: false,
    kpis: { identified: 1, reconciled: 1, with_discrepancies: 0, needs_review: 0, no_discrepancies: 1, invoice_total: 100, invoice_total_covered: 1, invoice_variance: 0, invoice_variance_net: 0, invoice_variance_covered: 1, discrepancy_rate: 0 },
    metric_definitions: {}, status_counts: { "NO DISCREPANCIES": 1 }, category_counts: {}, carriers: [], exposure: { ...SUMMARY },
    exposure_note: "", completeness: { ready: 1, incomplete: 0, unusable_files: 0, unidentified_files: 0 },
    reviews: { open: 0, in_review: 0, resolved: 0, dismissed: 0 }, open_ingestion_errors: 0, unreadable_files: [], unidentified_sources: [],
  },
  "/api/shipments": { shipments: [SHIPMENT], total: 1, carriers: ["Swift Lines"], statuses: ["NO DISCREPANCIES"], categories: [] },
  "/api/exposure": { rows: [], summary: SUMMARY, by_carrier: [], by_category: [], metric_definitions: {}, note: "" },
  "/api/documents": { files: [], completeness: { shipments: [], unidentified: [], unusable: [], duplicates: [] } },
}

describe("Lazily loaded routes", () => {
  it("shows the shell at once and the page when its code has arrived", async () => {
    mockApi(API)
    renderPage(<App />, "/shipments")

    // The shell (navigation, search) does not wait for the page's chunk
    expect(await screen.findByRole("navigation", { name: "Main" })).toBeInTheDocument()
    expect(await screen.findByRole("heading", { name: "Shipment Explorer" })).toBeInTheDocument()
    expect(screen.queryByText("Loading the page…")).not.toBeInTheDocument()
  })

  it("loads each page when the navigation opens its route", async () => {
    mockApi(API)
    renderPage(<App />, "/shipments")

    const nav = await screen.findByRole("navigation", { name: "Main" })
    expect(await screen.findByRole("heading", { name: "Shipment Explorer" })).toBeInTheDocument()

    await userEvent.click(within(nav).getByRole("link", { name: "Invoice Register" }))
    expect(await screen.findByRole("heading", { name: "Invoice Register" })).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Shipment Explorer" })).not.toBeInTheDocument()

    await userEvent.click(within(nav).getByRole("link", { name: "Shipment Explorer" }))
    expect(await screen.findByRole("heading", { name: "Shipment Explorer" })).toBeInTheDocument()
  })

  it("opens a deep link directly and sends an unknown address to the overview", { timeout: 30000 }, async () => {
    mockApi(API)
    const { unmount } = renderPage(<App />, "/invoices?check=mismatch")
    expect(await screen.findByRole("heading", { name: "Invoice Register" })).toBeInTheDocument()
    unmount()

    renderPage(<App />, "/no-such-page")
    // The overview brings the charts with it: a larger chunk to import
    expect(await screen.findByRole("heading", { name: "Overview" }, { timeout: 15000 })).toBeInTheDocument()
  })
})

describe("RouteBoundary", () => {
  it("shows a loading state while a page's code is on its way", async () => {
    let arrive: (module: { default: () => JSX.Element }) => void = () => undefined
    const Slow = lazy(() => new Promise<{ default: () => JSX.Element }>((resolve) => { arrive = resolve }))

    render(<RouteBoundary><Slow /></RouteBoundary>)
    expect(screen.getByRole("status")).toHaveTextContent("Loading the page")

    arrive({ default: () => <h1>Arrived</h1> })
    expect(await screen.findByRole("heading", { name: "Arrived" })).toBeInTheDocument()
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
  })

  it("says so when a page cannot be loaded, and retrying reloads the application", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined)
    const Broken = lazy(() => Promise.reject(new Error("Failed to fetch dynamically imported module")))
    const reload = vi.fn()

    render(<RouteBoundary reload={reload}><Broken /></RouteBoundary>)

    expect(await screen.findByText("This page could not be loaded")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: /Try again/ }))
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
  })
})
