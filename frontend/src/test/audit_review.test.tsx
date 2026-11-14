import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"
import { LineItemsTable } from "@/components/extraction/LineItems"
import { correctionFieldLabel, valueText } from "@/components/extraction/fields"
import { ReAuditPanel } from "@/components/shipment/ReAuditPanel"
import { TmsPanel } from "@/components/shipment/TmsPanel"
import { label } from "@/lib/format"
import { outcomeOf } from "@/pages/Corrections"
import type { DecisionResult, ExtractionDetail, LineItemView, ReAuditPreview, TmsComparison, TmsShipmentView } from "@/lib/types"
import { ADMIN_SESSION, REVIEWER_SESSION, mockApi, renderPage } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const user = () => userEvent.setup({ pointerEventsCheck: 0 })

const ADMIN = {
  ...ADMIN_SESSION,
  permissions: [...ADMIN_SESSION.permissions, "extraction.view", "extraction.correct", "extraction.approve", "history.view", "history.record"],
}
const REVIEWER = { ...REVIEWER_SESSION, permissions: [...REVIEWER_SESSION.permissions, "extraction.view", "extraction.correct"] }
const SHIPMENT = "SHJ-2026-101"

// ---------------------------------------------------------------------------
// Fixtures that follow the real response schemas (src/api/schemas.py, routes_extraction.py)
// ---------------------------------------------------------------------------

const line = (index: number, description: string, amount: number, over: Partial<LineItemView> = {}): LineItemView => ({
  index, field_name: `line_item:${index}`, extracted: { description, amount, page: 1 }, proposed: null, approved: null, applies: false,
  latest_correction_id: null, amount, source: "extracted", correction_id: null, ...over,
})

const invoice = (lines: LineItemView[]): ExtractionDetail => ({
  id: 7, filename: "INVOICE_SHJ-2026-101.pdf", sha256: "a".repeat(64), extractor_version: "1.1+abc", document_type: "INVOICE",
  status: "extracted", shipment_id: SHIPMENT, shipment_key: "SHJ2026101", created_at: "2026-11-14T10:00:00+00:00",
  payload: {
    filename: "INVOICE_SHJ-2026-101.pdf", document_type: "INVOICE", status: "extracted", pages: 1, pages_without_text: [], extractor_version: "1.1+abc",
    fields: {}, line_items: lines.map((item) => ({ description: item.extracted.description, amount: item.extracted.amount, quantity: null, rate: null, page: 1, warnings: [] })),
    unclassified_amounts: [], excluded_amounts: [], warnings: [], review_reasons: [], error: null,
  },
  effective: {}, corrections: [], limitations: [], line_items: lines,
})

const pendingCorrection = { id: 12, extraction_id: 7, field_name: "line_item:1", previous: { value: { description: "Fuel Surcharge", amount: 200 }, state: "EXTRACTED", source: "extracted" },
  corrected: { description: "Fuel Surcharge", amount: 180, of: { description: "Fuel Surcharge", amount: 200 } }, reason: "Credit note CN-17 applies.",
  actor: "bob", authenticated: true, at: "2026-11-14T10:05:00+00:00", based_on: null, legacy: false, status: "pending" as const }

const PREVIEW: ReAuditPreview = {
  shipment_id: SHIPMENT, shipment_key: "SHJ2026101", needed: true, token: "f".repeat(64),
  recorded: { run_id: 3, recorded_at: "2026-11-13T09:00:00+00:00", label: "Weekly", final_status: "DISCREPANCIES DETECTED", freight_outcome: "DISCREPANCY",
    financial_impact: { currency: "USD", billed_above_contract: 20, without_evidence: 0, pending_review: 75 } },
  current: { final_status: "DISCREPANCIES DETECTED", freight_outcome: "DISCREPANCY", financial_impact: { currency: "USD", billed_above_contract: 0, without_evidence: 0, pending_review: 75 } },
  status_change: null, value_changes: [{ field: "line_item_sum", label: "line-item sum", previous: 1675, current: 1655 }], state_changes: [],
  findings: { new: [{ finding_key: "SHJ2026101|freight-invoice-arithmetic", category: "Invoice", severity: "high", summary: "The charges add up to $1,655.00, not $1,675.00.", difference: 20 }], resolved: [], changed: [], unverified: [] },
  corrections: { applied: [12], added: [12], line_items: [12] }, tms_resolutions: [], open_tms_conflicts: 0,
}

const comparison = (over: Partial<TmsComparison>): TmsComparison => ({
  field: "expected_pieces", label: "Expected pieces", consumed: true, tms_field: "pieces", tms_value: 50, document_value: 48, tms_text: 50, document_text: 48,
  document_state: "extracted", document_source: "legacy_reader", state: "conflict", open: true, tms_corrected_by_hand: false, choices: ["document", "tms"],
  resolution: null, currency: null, protected_by: null, ...over,
})

const TMS: TmsShipmentView = {
  shipment_id: SHIPMENT, shipment_key: "SHJ2026101", configured: true,
  match: { integration_id: 1, integration_name: "Sandbox TMS", provider: "sandbox_tms", record_id: 41, external_id: "SBX-SHP-9101", reference: SHIPMENT,
    status: "delivered", source_updated_at: "2026-10-02T09:00:00+00:00", imported_at: "2026-11-14T08:00:00+00:00", first_run_id: 1, last_run_id: 2,
    corrected_fields: [], data: {} },
  matches: [], ambiguous: false,
  comparisons: [
    comparison({}),
    comparison({ field: "carrier", label: "Carrier", consumed: false, tms_field: "carrier_name", tms_value: "swift lines inc", document_value: "swift lines",
      tms_text: "Swift Lines Inc", document_text: "Swift Lines", choices: ["document"] }),
    comparison({ field: "actual_date", label: "Actual delivery date", tms_value: "2026-10-03", document_value: "2026-10-03", tms_text: "2026-10-03",
      document_text: "2026-10-03", state: "agrees", open: false, choices: [] }),
  ],
  open_conflicts: 2, applied_resolution_ids: [], history: [], unavailable: [],
}

// ---------------------------------------------------------------------------
// Invoice charges
// ---------------------------------------------------------------------------

describe("Invoice line-item corrections", () => {
  it("shows each charge as read, proposed and approved, apart", () => {
    mockApi({ "/api/session": ADMIN, "/api/extractions/staleness": { stale: [], corrections_available: true }, })
    const lines = [
      line(0, "Base Freight", 1400, { approved: { ...pendingCorrection, id: 9, status: "approved", decided_by: "dave", decided_at: "2026-11-14T11:00:00+00:00",
        corrected: { description: "Base Freight", amount: 1350, of: { description: "Base Freight", amount: 1400 } } }, applies: true, amount: 1350, source: "approved_correction", correction_id: 9 }),
      line(1, "Fuel Surcharge", 200, { proposed: pendingCorrection, latest_correction_id: 12 }),
    ]

    renderPage(<LineItemsTable detail={invoice(lines)} canPropose />)

    const base = screen.getByTestId("line-0")
    expect(within(base).getByText("1,400.00")).toBeInTheDocument()
    expect(within(base).getByText("1,350.00")).toBeInTheDocument()
    expect(within(base).getByText("Approved")).toBeInTheDocument()

    const fuel = screen.getByTestId("line-1")
    expect(within(fuel).getByText("Awaiting approval")).toBeInTheDocument()
    expect(within(fuel).getByText(/changes nothing until approved/)).toBeInTheDocument()
    // A line with a pending proposal offers no second one
    expect(within(fuel).queryByRole("button", { name: /Propose a corrected amount/ })).not.toBeInTheDocument()
  })

  it("proposes the amount with the version it was based on, and refuses an empty amount or a short reason", async () => {
    const { calls } = mockApi({
      "/api/session": ADMIN,
      "/api/extractions/staleness": { stale: [], corrections_available: true },
      "/api/extractions/7/line-items/1/corrections": () => ({ body: pendingCorrection }),
    })

    renderPage(<LineItemsTable detail={invoice([line(1, "Fuel Surcharge", 200, { latest_correction_id: 5 })])} canPropose />)
    await user().click(screen.getByRole("button", { name: /Propose a corrected amount for line 2, Fuel Surcharge/ }))

    const dialog = await screen.findByRole("dialog")
    await user().click(within(dialog).getByRole("button", { name: "Propose correction" }))
    expect(within(dialog).getByText("Enter the corrected amount.")).toBeInTheDocument()
    expect(within(dialog).getByText(/at least 10 characters/)).toBeInTheDocument()
    expect(calls.filter((call) => call.init?.method === "POST")).toHaveLength(0)

    await user().type(within(dialog).getByLabelText("Corrected amount"), "180.00")
    await user().type(within(dialog).getByLabelText("Reason (required)"), "Credit note CN-17 applies.")
    await user().click(within(dialog).getByRole("button", { name: "Propose correction" }))

    expect(await within(dialog).findByText(/Proposal #12 recorded: awaiting approval/)).toBeInTheDocument()
    const sent = calls.find((call) => call.init?.method === "POST")
    expect(JSON.parse(String(sent?.init?.body))).toEqual({ amount: "180.00", reason: "Credit note CN-17 applies.", based_on: 5 })
  })

  it("says a stale line must be reloaded, without sending anything again", async () => {
    mockApi({
      "/api/session": ADMIN,
      "/api/extractions/staleness": { stale: [], corrections_available: true },
      "/api/extractions/7/line-items/1/corrections": () => ({ status: 409, body: { detail: "Line 2 was corrected (correction 13) after the version this change is based on (none). Reload it and decide again." } }),
    })

    renderPage(<LineItemsTable detail={invoice([line(1, "Fuel Surcharge", 200)])} canPropose />)
    await user().click(screen.getByRole("button", { name: /Propose a corrected amount/ }))
    const dialog = await screen.findByRole("dialog")
    await user().type(within(dialog).getByLabelText("Corrected amount"), "180")
    await user().type(within(dialog).getByLabelText("Reason (required)"), "Credit note CN-17 applies.")
    await user().click(within(dialog).getByRole("button", { name: "Propose correction" }))

    expect(await within(dialog).findByText("This line changed while you were editing.")).toBeInTheDocument()
    expect(within(dialog).getByRole("button", { name: /Load the latest state/ })).toBeInTheDocument()
  })

  it("names a charge line and shows its value as written", () => {
    expect(correctionFieldLabel("line_item:1", label)).toBe("Charge line 2")
    expect(correctionFieldLabel("invoice_total", label)).toBe(label("invoice_total"))
    expect(valueText("line_item:1", { description: "Fuel Surcharge", amount: 180 })).toBe("Fuel Surcharge: 180.00")
  })

  it("says an approved charge correction waits for a re-audit, never that a run was recorded", () => {
    const result: DecisionResult = {
      decision_id: 4, correction_id: 12, decision: "approved", note: null, actor: "dave", at: "2026-11-14T11:00:00+00:00", proposed_by: "bob",
      extraction_id: 7, field_name: "line_item:1", document_type: "INVOICE", filename: "INVOICE_SHJ-2026-101.pdf", shipment_key: "SHJ2026101",
      corrected: { description: "Fuel Surcharge", amount: 180 }, recompute: { status: "re_audit_required", shipments: ["SHJ2026101"] },
    }

    const outcome = outcomeOf(result)
    expect(outcome.text).toContain("Charge line 2 of INVOICE_SHJ-2026-101.pdf")
    expect(outcome.text).toContain("previews and confirms")
    expect(outcome.text).not.toContain("was recorded")
    expect(outcome.reAudit).toBe("SHJ2026101")
  })
})

// ---------------------------------------------------------------------------
// Controlled re-audit
// ---------------------------------------------------------------------------

describe("Controlled re-audit", () => {
  it("shows what recording would change, and records the previewed state with a reason", async () => {
    const { calls } = mockApi({
      "/api/session": ADMIN,
      [`/api/shipments/${SHIPMENT}/re-audit`]: (_url: URL, init?: RequestInit) =>
        init?.method === "POST"
          ? { status: 201, body: { recorded: true, run_id: 4, shipment_id: SHIPMENT, shipment_key: "SHJ2026101", previous_run_id: 3, snapshot_written: true, corrections_added: [12], tms_resolutions: [] } }
          : { body: PREVIEW },
    })

    renderPage(<ReAuditPanel shipmentId={SHIPMENT} />)

    const panel = await screen.findByTestId("re-audit")
    expect(within(panel).getByText(/latest recorded run \(#3/)).toBeInTheDocument()
    // Billed above the contract: $20.00 recorded, $0.00 now
    const above = within(panel).getByText("Billed above the contract").closest("tr") as HTMLElement
    expect(within(above).getByText("$20.00 USD")).toBeInTheDocument()
    expect(within(above).getByText("$0.00 USD")).toBeInTheDocument()
    const change = within(panel).getByTestId("change-line_item_sum")
    expect(within(change).getByText("1,675")).toBeInTheDocument()
    expect(within(change).getByText("1,655")).toBeInTheDocument()
    expect(within(panel).getByText("The charges add up to $1,655.00, not $1,675.00.")).toBeInTheDocument()
    expect(within(panel).getByText("Charge correction #12")).toBeInTheDocument()

    await user().click(within(panel).getByRole("button", { name: /Confirm and record the re-audit/ }))
    expect(within(panel).getByText(/at least 10 characters/)).toBeInTheDocument()

    await user().type(within(panel).getByLabelText("Reason (required)"), "Credit note CN-17 applied.")
    await user().click(within(panel).getByRole("button", { name: /Confirm and record the re-audit/ }))

    await waitFor(() => expect(calls.some((call) => call.init?.method === "POST")).toBe(true))
    const sent = calls.find((call) => call.init?.method === "POST")
    expect(JSON.parse(String(sent?.init?.body))).toEqual({ token: PREVIEW.token, reason: "Credit note CN-17 applied." })
  })

  it("reloads the preview when the audit changed since it was shown", async () => {
    let previews = 0
    mockApi({
      "/api/session": ADMIN,
      [`/api/shipments/${SHIPMENT}/re-audit`]: (_url: URL, init?: RequestInit) => {
        if (init?.method === "POST") return { status: 409, body: { detail: "The audit of this shipment changed since the preview. Review the new preview and confirm again." } }
        previews += 1
        return { body: PREVIEW }
      },
    })

    renderPage(<ReAuditPanel shipmentId={SHIPMENT} />)
    const panel = await screen.findByTestId("re-audit")
    await user().type(within(panel).getByLabelText("Reason (required)"), "Credit note CN-17 applied.")
    await user().click(within(panel).getByRole("button", { name: /Confirm and record the re-audit/ }))

    expect(await within(panel).findByText(/changed since the preview/)).toBeInTheDocument()
    await waitFor(() => expect(previews).toBeGreaterThan(1))
  })

  it("is not shown when nothing waits, and a reviewer cannot confirm", async () => {
    mockApi({ "/api/session": ADMIN, [`/api/shipments/${SHIPMENT}/re-audit`]: { ...PREVIEW, needed: false } })
    const first = renderPage(<ReAuditPanel shipmentId={SHIPMENT} />)
    await waitFor(() => expect(first.container.querySelector("[data-testid=re-audit]")).toBeNull())
    first.unmount()

    vi.unstubAllGlobals()
    mockApi({ "/api/session": REVIEWER, [`/api/shipments/${SHIPMENT}/re-audit`]: PREVIEW })
    renderPage(<ReAuditPanel shipmentId={SHIPMENT} />)
    expect(await screen.findByText(/An administrator who can record audit history confirms/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Confirm and record/ })).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// TMS comparison
// ---------------------------------------------------------------------------

describe("TMS comparison", () => {
  it("compares each field with the TMS record and its provenance", async () => {
    mockApi({ "/api/session": ADMIN, [`/api/shipments/${SHIPMENT}/tms`]: TMS })
    renderPage(<TmsPanel shipmentId={SHIPMENT} />)

    expect(await screen.findByText("SBX-SHP-9101")).toBeInTheDocument()
    expect(screen.getByText("Sandbox TMS")).toBeInTheDocument()

    const pieces = screen.getByTestId("tms-expected_pieces")
    expect(within(pieces).getByText("Differs")).toBeInTheDocument()
    expect(within(pieces).getByRole("button", { name: "Use the TMS value for Expected pieces" })).toBeInTheDocument()

    // A cross-check can be noted, never used; an agreeing field needs nothing
    const carrier = screen.getByTestId("tms-carrier")
    expect(within(carrier).getByText("Swift Lines Inc")).toBeInTheDocument()
    expect(within(carrier).queryByRole("button", { name: /Use the TMS value/ })).not.toBeInTheDocument()
    expect(within(carrier).getByRole("button", { name: "Keep the documents' value for Carrier" })).toBeInTheDocument()
    expect(within(screen.getByTestId("tms-actual_date")).getByText("Agrees")).toBeInTheDocument()
  })

  it("decides on exactly the values it showed, with a reason", async () => {
    const { calls } = mockApi({
      "/api/session": ADMIN,
      [`/api/shipments/${SHIPMENT}/tms`]: TMS,
      [`/api/shipments/${SHIPMENT}/tms/resolutions`]: () => ({ status: 201, body: { resolution: {}, shipment_id: SHIPMENT, re_audit: "required" } }),
    })

    renderPage(<TmsPanel shipmentId={SHIPMENT} />)
    await user().click(await screen.findByRole("button", { name: "Use the TMS value for Expected pieces" }))
    const dialog = await screen.findByRole("dialog")
    await user().click(within(dialog).getByRole("button", { name: "Use the TMS value" }))
    expect(within(dialog).getByText(/at least 10 characters/)).toBeInTheDocument()

    await user().type(within(dialog).getByLabelText("Reason (required)"), "The shipper amended the booking to 50 pieces.")
    await user().click(within(dialog).getByRole("button", { name: "Use the TMS value" }))

    await waitFor(() => expect(calls.some((call) => call.init?.method === "POST")).toBe(true))
    const sent = calls.find((call) => call.init?.method === "POST")
    expect(JSON.parse(String(sent?.init?.body))).toEqual({
      field: "expected_pieces", choice: "tms", reason: "The shipper amended the booking to 50 pieces.", record_id: 41, tms_value: 50, document_value: 48,
    })
  })

  it("lets only an administrator decide, says when several records match, and stays hidden without a TMS", async () => {
    mockApi({ "/api/session": REVIEWER, [`/api/shipments/${SHIPMENT}/tms`]: TMS })
    const reviewer = renderPage(<TmsPanel shipmentId={SHIPMENT} />)
    expect(await screen.findAllByText("An administrator decides.")).toHaveLength(2)
    expect(screen.queryByRole("button", { name: /Keep the documents' value/ })).not.toBeInTheDocument()
    reviewer.unmount()

    vi.unstubAllGlobals()
    mockApi({ "/api/session": ADMIN, [`/api/shipments/${SHIPMENT}/tms`]: { ...TMS, match: null, ambiguous: true, comparisons: [], matches: [TMS.match, { ...TMS.match, external_id: "SBX-SHP-9102" }] } })
    const several = renderPage(<TmsPanel shipmentId={SHIPMENT} />)
    expect(await screen.findByText(/Several TMS records name this shipment/)).toBeInTheDocument()
    several.unmount()

    vi.unstubAllGlobals()
    mockApi({ "/api/session": ADMIN, [`/api/shipments/${SHIPMENT}/tms`]: { ...TMS, configured: false, match: null, comparisons: [] } })
    const { container } = renderPage(<TmsPanel shipmentId={SHIPMENT} />)
    await waitFor(() => expect(container.querySelector("section")).toBeNull())
  })
})
