import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"
import App from "@/App"
import { ExtractionsSection } from "@/components/extraction/ExtractionPanel"
import { Corrections, outcomeOf } from "@/pages/Corrections"
import { RunsTab } from "@/pages/History"
import type { Correction, DecisionResult, DocumentFile } from "@/lib/types"
import { ADMIN_SESSION, REVIEWER_SESSION, mockApi, renderPage } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const user = () => userEvent.setup({ pointerEventsCheck: 0 })

const REVIEWER = { ...REVIEWER_SESSION, permissions: [...REVIEWER_SESSION.permissions, "extraction.view", "extraction.correct"] }
const ADMIN = {
  ...ADMIN_SESSION,
  permissions: [...ADMIN_SESSION.permissions, "extraction.view", "extraction.correct", "extraction.run", "extraction.approve", "history.view", "admin.maintenance"],
}
const SECOND_ADMIN = { ...ADMIN, user: { username: "dave", role: "admin", label: "Administrator" } }

const POD = "POD_SHJ-2026-001.pdf"
const LONG_CARRIER = "SwiftLine Logistics of the Southern Plains and Gulf Coast Freight Cooperative Incorporated Holdings Limited"
const EXTRACTOR = "1.1+abc123"

// ---------------------------------------------------------------------------
// Fixtures that follow the real response schemas (src/api/routes_extraction.py)
// ---------------------------------------------------------------------------

type Overrides = Record<string, unknown>

const field = (name: string, state: string, value: unknown, page: number | null, extra: Overrides = {}) => ({
  name, state, value, raw_text: null, source_file: POD, page, method: "label_pair",
  validation: state === "EXTRACTED" ? "valid" : "not_checked", basis: "exact_label", warnings: [], alternatives: [], needs_review: false, ...extra,
})

const FIELDS = {
  shipment_id: field("shipment_id", "EXTRACTED", "SHJ-2026-001", 1),
  carrier: field("carrier", "EXTRACTED", LONG_CARRIER, 1),
  delivery_date: field("delivery_date", "EXTRACTED", "2026-10-05", null),
  delivered_pieces: field("delivered_pieces", "EXTRACTED", 45, 1),
  delivery_status: field("delivery_status", "EXTRACTED", "Delivered", 1, {
    validation: "warning", needs_review: true, warnings: ["extra_lines_ignored: lines after the value were not used (this field takes at most 1)"],
    raw_text: "SECRET RAW SNIPPET",
  }),
  received_by: field("received_by", "CONFLICTING", null, null, {
    validation: "not_checked", alternatives: [{ value: "A. Smith", page: 1 }, { value: null, page: 2, problem: "unparseable" }],
  }),
  signature: field("signature", "UNREADABLE", null, null, { warnings: ["Some pages of the document have no text, so the field may be on one of them."] }),
  signature_image: field("signature_image", "UNSUPPORTED", null, null),
  damage_report: field("damage_report", "MISSING", null, null),
  received_time: field("received_time", "UNPARSEABLE", null, 1, { validation: "invalid", warnings: ["not a valid calendar date"] }),
} as Record<string, ReturnType<typeof field>>

const effective = (item: ReturnType<typeof field>, over: Overrides = {}) => ({
  extracted: { value: item.value, state: item.state, page: item.page }, proposed: null, approved: null, latest_correction_id: null,
  value: item.value, state: item.state, source: "extracted", page: item.page, correction_id: null, ...over,
})

const correction = (over: Partial<Correction> = {}): Correction => ({
  id: 5, extraction_id: 1, field_name: "delivered_pieces", previous: { value: 45, state: "EXTRACTED", source: "extracted" }, corrected: 48,
  reason: "The paper POD shows 48 pieces.", actor: "bob", authenticated: true, at: "2026-10-22T10:00:00+00:00", based_on: null, legacy: false,
  status: "pending", decision_id: null, decision: null, decision_note: null, decided_by: null, decided_at: null, filename: POD,
  document_type: "POD", shipment_key: "SHJ2026001", extractor_version: EXTRACTOR, ...over,
})

function detail(over: { effective?: Record<string, unknown>; corrections?: Correction[]; fields?: typeof FIELDS; status?: string; review?: string[] } = {}) {
  const fields = over.fields ?? FIELDS

  return {
    id: 1, filename: POD, sha256: "h", extractor_version: EXTRACTOR, document_type: "POD", status: over.status ?? "extracted",
    shipment_id: "SHJ-2026-001", shipment_key: "SHJ2026001", created_at: "2026-10-22T09:00:00+00:00",
    payload: {
      filename: POD, document_type: "POD", status: over.status ?? "extracted", pages: 1, pages_without_text: [], extractor_version: EXTRACTOR,
      fields, line_items: [], unclassified_amounts: [], excluded_amounts: [], warnings: [], review_reasons: over.review ?? [], error: null,
    },
    effective: { ...Object.fromEntries(Object.entries(fields).map(([name, item]) => [name, effective(item)])), ...(over.effective ?? {}) },
    corrections: over.corrections ?? [],
    limitations: ["A correction is a proposal until a different administrator approves it."],
  }
}

const SUMMARY = { id: 1, filename: POD, sha256: "h", extractor_version: EXTRACTOR, document_type: "POD", status: "extracted", shipment_id: "SHJ-2026-001", shipment_key: "SHJ2026001", created_at: "2026-10-22T09:00:00+00:00", correction_count: 0, pending_count: 0 }
const OK_STALENESS = { stale: [], corrections_available: true }
const STALE = { stale: [{ shipment_key: "SHJ2026001", recorded_run_id: 1, missing_correction_ids: [5], stale_run_ids: [1] }], corrections_available: true }

const DOCUMENTS = [{ filename: POD }, { filename: "BOL_SHJ-2026-001.pdf" }] as unknown as DocumentFile[]

function panel() {
  return renderPage(<ExtractionsSection shipmentId="SHJ-2026-001" shipmentKey="SHJ2026001" documents={DOCUMENTS} />)
}

const posts = (calls: { url: URL; init?: RequestInit }[], path?: string) =>
  calls.filter((call) => call.init?.method === "POST" && (!path || call.url.pathname === path)).map((call) => ({ path: call.url.pathname, body: JSON.parse(String(call.init?.body ?? "null")) }))

const gets = (calls: { url: URL }[], path: string) => calls.filter((call) => call.url.pathname === path).length

function rowOf(name: string) {
  return screen.getByText(name, { selector: "p.font-medium" }).closest("tr") as HTMLElement
}

async function panelFor(session: unknown, extra: Record<string, unknown> = {}, detailBody: unknown = detail()) {
  const api = mockApi({
    "/api/session": session,
    "/api/extractions": { extractions: [SUMMARY], extractor_version: EXTRACTOR },
    "/api/extractions/1": detailBody,
    "/api/extractions/staleness": OK_STALENESS,
    ...extra,
  })
  panel()
  await screen.findByText("Delivered pieces", { selector: "p.font-medium" })

  return api
}

// ---------------------------------------------------------------------------
// A. Fields, provenance and status
// ---------------------------------------------------------------------------

describe("extraction panel: provenance and status", () => {
  it("shows each field with its typed value, source document and page", async () => {
    await panelFor(REVIEWER)
    const row = rowOf("Delivered pieces")

    expect(within(row).getByTestId("original-delivered_pieces")).toHaveTextContent("45")
    expect(within(row).getByText(POD)).toBeInTheDocument()
    expect(within(row).getByText("page 1")).toBeInTheDocument()
    expect(within(row).getByText("Valid")).toBeInTheDocument()
    expect(within(rowOf("Delivery date")).getByTestId("original-delivery_date")).toHaveTextContent("2026-10-05")
  })

  it("never invents a page: an unknown page says so", async () => {
    await panelFor(REVIEWER)

    expect(within(rowOf("Delivery date")).getByText("page not recorded")).toBeInTheDocument()
    expect(within(rowOf("Delivery date")).queryByText(/page \d/)).toBeNull()
  })

  it("names missing, conflicting, unreadable, unparseable and unsupported values in words, never as a number", async () => {
    await panelFor(REVIEWER)

    const expectations: [string, string, RegExp][] = [
      ["Damage report", "Missing", /Not found on the document/],
      ["Received by", "Conflicting", /different values; none was chosen/],
      ["Signature", "Unreadable", /cannot be said whether the field is absent/],
      ["Received time", "Unparseable", /not a valid value for this field/],
      ["Signature image", "Unsupported", /extractor cannot read this field/],
    ]

    for (const [name, badge, text] of expectations) {
      const block = within(rowOf(name)).getByTestId(`original-${FIELD_KEY[name]}`)

      expect(within(block).getByText(badge)).toBeInTheDocument()
      expect(block).toHaveTextContent(text)
      expect(block.textContent).not.toMatch(/\b0\b/)
    }
  })

  it("lists the conflicting values with their pages and the occurrence that could not be read", async () => {
    await panelFor(REVIEWER)
    const row = rowOf("Received by")

    expect(within(row).getByText(/A\. Smith \(page 1\)/)).toBeInTheDocument()
    expect(within(row).getByText(/an occurrence that could not be read \(page 2\)/)).toBeInTheDocument()
  })

  it("shows warnings and the needs-review flag, and never the raw document text", async () => {
    await panelFor(REVIEWER)
    const row = rowOf("Delivery status")

    expect(within(row).getByText("Warning")).toBeInTheDocument()
    expect(within(row).getByText("Needs review")).toBeInTheDocument()
    expect(within(row).getByText(/extra_lines_ignored/)).toBeInTheDocument()
    expect(screen.queryByText(/SECRET RAW SNIPPET/)).toBeNull()
  })

  it("keeps a very long value readable", async () => {
    await panelFor(REVIEWER)

    expect(screen.getByTestId("original-carrier")).toHaveTextContent(LONG_CARRIER)
    expect(within(screen.getByTestId("original-carrier")).getByText(LONG_CARRIER).closest(".break-words")).not.toBeNull()
  })

  it("keeps the original, proposed, approved, rejected and legacy values apart", async () => {
    const approved = correction({ id: 7, status: "approved", corrected: 48, decided_by: "dave", decided_at: "2026-10-22T11:00:00+00:00", decision: "approved" })
    const proposed = correction({ id: 9, field_name: "delivery_status", corrected: "Damaged", previous: { value: "Delivered", state: "EXTRACTED", source: "extracted" } })
    const rejected = correction({ id: 8, field_name: "damage_report", corrected: "None", status: "rejected", decision: "rejected", decided_by: "dave", previous: { value: null, state: "MISSING", source: "extracted" } })
    const legacy = correction({ id: 3, field_name: "carrier", corrected: "Other Lines", status: "legacy", legacy: true })

    await panelFor(REVIEWER, {}, detail({
      corrections: [legacy, approved, rejected, proposed],
      effective: {
        delivered_pieces: effective(FIELDS.delivered_pieces, { approved, latest_correction_id: 7, value: 48, source: "approved_correction", page: null, correction_id: 7 }),
        delivery_status: effective(FIELDS.delivery_status, { proposed, latest_correction_id: 9 }),
      },
    }))

    const pieces = rowOf("Delivered pieces")
    expect(within(pieces).getByTestId("original-delivered_pieces")).toHaveTextContent("45")
    expect(within(pieces).getByTestId("approved-delivered_pieces")).toHaveTextContent("48")
    expect(within(pieces).getByTestId("approved-delivered_pieces")).toHaveTextContent(/Approved by dave/)
    expect(within(pieces).getByTestId("approved-delivered_pieces")).toHaveTextContent(/typed by a reviewer, so it has no page/)

    const status = rowOf("Delivery status")
    expect(within(status).getByTestId("proposed-delivery_status")).toHaveTextContent("Damaged")
    expect(within(status).getByText("Awaiting approval")).toBeInTheDocument()
    expect(within(status).getByTestId("original-delivery_status")).toHaveTextContent("Delivered")

    expect(within(rowOf("Damage report")).getByTestId("rejected-damage_report")).toHaveTextContent(/rejected by dave\. It has no effect/)
    expect(within(rowOf("Carrier")).getByTestId("legacy-carrier")).toHaveTextContent(/before approvals existed. It is not applied/)
    expect(within(rowOf("Carrier")).getByTestId("original-carrier")).toHaveTextContent(LONG_CARRIER)
  })

  it("explains which calculation an approved correction would feed", async () => {
    await panelFor(REVIEWER)

    expect(within(rowOf("Delivered pieces")).getByText(/Read by the quantity audit once a correction is approved/)).toBeInTheDocument()
    expect(within(rowOf("Carrier")).queryByText(/Read by/)).toBeNull()
  })

  it("explains a partial or unreadable extraction and its review reasons", async () => {
    await panelFor(REVIEWER, {}, detail({ status: "partial", review: ["required field delivered_pieces is unreadable"] }))

    expect(screen.getByText(/Pages without text/)).toBeInTheDocument()
    expect(screen.getByText("required field delivered_pieces is unreadable")).toBeInTheDocument()
  })
})

const FIELD_KEY: Record<string, string> = {
  "Damage report": "damage_report", "Received by": "received_by", Signature: "signature", "Received time": "received_time", "Signature image": "signature_image",
}

// ---------------------------------------------------------------------------
// B. Proposing a correction
// ---------------------------------------------------------------------------

async function openDialog(name = "Delivered pieces") {
  const person = user()
  await person.click(within(rowOf(name)).getByRole("button", { name: new RegExp(`Propose a correction for ${name}`, "i") }))

  return { person, dialog: await screen.findByRole("dialog") }
}

describe("proposing a correction", () => {
  it("needs a value and a reason before anything is sent", async () => {
    const { calls } = await panelFor(REVIEWER)
    const { person, dialog } = await openDialog()

    await person.click(within(dialog).getByRole("button", { name: "Propose correction" }))

    expect(within(dialog).getByText("Enter the corrected value.")).toBeInTheDocument()
    expect(within(dialog).getByText(/at least 10 characters/)).toBeInTheDocument()

    await person.type(within(dialog).getByLabelText("Proposed value"), "48")
    await person.type(within(dialog).getByLabelText(/Reason/), "too short")
    await person.click(within(dialog).getByRole("button", { name: "Propose correction" }))

    expect(within(dialog).getByText(/at least 10 characters/)).toBeInTheDocument()
    expect(posts(calls)).toEqual([])
  })

  it("shows the original value and what approval would change", async () => {
    await panelFor(REVIEWER)
    const { dialog } = await openDialog()

    expect(within(dialog).getByTestId("original-value")).toHaveTextContent("45")
    expect(within(dialog).getByTestId("original-value")).toHaveTextContent("page 1")
    expect(within(dialog).getByText(/replaces the document's value in the quantity audit/)).toBeInTheDocument()
  })

  it("sends the proposal with the version it is based on and does not call it approved", async () => {
    const approved = correction({ id: 7, status: "approved", corrected: 47, decision: "approved", decided_by: "dave" })
    const { calls } = await panelFor(
      REVIEWER,
      { "/api/extractions/1/corrections": () => ({ body: correction({ id: 9, corrected: 48, based_on: 7 }) }) },
      detail({ corrections: [approved], effective: { delivered_pieces: effective(FIELDS.delivered_pieces, { approved, latest_correction_id: 7, value: 47, source: "approved_correction", page: null }) } }),
    )
    const { person, dialog } = await openDialog()

    expect(within(dialog).getByTestId("approved-value")).toHaveTextContent("47")
    await person.type(within(dialog).getByLabelText("Proposed value"), "48")
    await person.type(within(dialog).getByLabelText(/Reason/), "The paper POD shows 48 pieces.")
    await person.click(within(dialog).getByRole("button", { name: "Propose correction" }))

    expect(await within(dialog).findByText(/Proposal #9 recorded: awaiting approval/)).toBeInTheDocument()
    expect(within(dialog).getByText(/has not been approved and has not changed any audit/)).toBeInTheDocument()
    expect(posts(calls, "/api/extractions/1/corrections")).toEqual([
      { path: "/api/extractions/1/corrections", body: { field: "delivered_pieces", value: "48", reason: "The paper POD shows 48 pieces.", based_on: 7 } },
    ])
  })

  it("sends null as the version when the field has never been corrected", async () => {
    const { calls } = await panelFor(REVIEWER, { "/api/extractions/1/corrections": () => ({ body: correction() }) })
    const { person, dialog } = await openDialog()

    await person.type(within(dialog).getByLabelText("Proposed value"), "48")
    await person.type(within(dialog).getByLabelText(/Reason/), "The paper POD shows 48 pieces.")
    await person.click(within(dialog).getByRole("button", { name: "Propose correction" }))
    await within(dialog).findByText(/recorded: awaiting approval/)

    expect(posts(calls, "/api/extractions/1/corrections")[0].body.based_on).toBeNull()
  })

  it("does not send twice while a request is in progress", async () => {
    const api = await panelFor(REVIEWER, { "/api/extractions/1/corrections": () => ({ body: correction() }) })
    const original = api.fetchMock.getMockImplementation()!
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => (release = resolve))

    let started = 0
    api.fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/corrections") && init?.method === "POST") {
        started += 1
        await gate
      }
      return original(input, init)
    })

    const { person, dialog } = await openDialog()
    await person.type(within(dialog).getByLabelText("Proposed value"), "48")
    await person.type(within(dialog).getByLabelText(/Reason/), "The paper POD shows 48 pieces.")

    const submit = within(dialog).getByRole("button", { name: "Propose correction" })
    await person.dblClick(submit)

    expect(await within(dialog).findByRole("button", { name: "Submitting…" })).toBeDisabled()
    expect(started).toBe(1)

    release()
    await within(dialog).findByText(/recorded: awaiting approval/)
    expect(posts(api.calls, "/api/extractions/1/corrections")).toHaveLength(1)
  })

  it("on a stale version asks the user to reload, then sends the new version", async () => {
    let latest: number | null = null
    let attempts = 0
    const current = () => {
      const approved = latest === null ? null : correction({ id: latest, status: "approved", corrected: 47, decision: "approved", decided_by: "dave" })
      return detail({ corrections: approved ? [approved] : [], effective: { delivered_pieces: effective(FIELDS.delivered_pieces, { approved, latest_correction_id: latest, value: approved ? 47 : 45, source: approved ? "approved_correction" : "extracted" }) } })
    }

    const { calls } = await panelFor(REVIEWER, {
      "/api/extractions/1": () => ({ body: current() }),
      "/api/extractions/1/corrections": (_url: URL, init?: RequestInit) => {
        attempts += 1
        const based = JSON.parse(String(init?.body)).based_on
        return based === latest ? { body: correction({ id: 9, based_on: based }) } : { status: 409, body: { detail: "delivered_pieces was corrected (correction 7) after the version this change is based on (none). Reload it and decide again." } }
      },
    })
    const { person, dialog } = await openDialog()

    await person.type(within(dialog).getByLabelText("Proposed value"), "48")
    await person.type(within(dialog).getByLabelText(/Reason/), "The paper POD shows 48 pieces.")
    latest = 7                                         // someone else decided in the meantime
    await person.click(within(dialog).getByRole("button", { name: "Propose correction" }))

    expect(await within(dialog).findByText("This field changed while you were editing.")).toBeInTheDocument()
    expect(within(dialog).getByText(/was corrected \(correction 7\)/)).toBeInTheDocument()
    expect(within(dialog).queryByText(/recorded: awaiting approval/)).toBeNull()

    await person.click(within(dialog).getByRole("button", { name: "Load the latest state" }))

    expect(await within(dialog).findByText(/Check it, then submit again; nothing was sent yet/)).toBeInTheDocument()
    expect(await within(dialog).findByTestId("approved-value")).toHaveTextContent("47")
    expect(attempts).toBe(1)

    await person.click(within(dialog).getByRole("button", { name: "Propose correction" }))
    await within(dialog).findByText(/recorded: awaiting approval/)

    expect(posts(calls, "/api/extractions/1/corrections").map((item) => item.body.based_on)).toEqual([null, 7])
  })

  it("shows the server's validation message exactly and keeps the form", async () => {
    await panelFor(REVIEWER, { "/api/extractions/1/corrections": () => ({ status: 422, body: { detail: "not a whole number" } }) })
    const { person, dialog } = await openDialog()

    await person.type(within(dialog).getByLabelText("Proposed value"), "forty")
    await person.type(within(dialog).getByLabelText(/Reason/), "The paper POD shows 48 pieces.")
    await person.click(within(dialog).getByRole("button", { name: "Propose correction" }))

    expect(await within(dialog).findByText("not a whole number")).toBeInTheDocument()
    expect(within(dialog).getByLabelText("Proposed value")).toHaveValue("forty")
    expect(within(dialog).getByRole("button", { name: "Propose correction" })).toBeEnabled()
  })

  it("shows a permission or server failure instead of pretending it worked", async () => {
    await panelFor(REVIEWER, { "/api/extractions/1/corrections": () => ({ status: 403, body: { detail: "The Auditor / reviewer role cannot perform this action." } }) })
    const { person, dialog } = await openDialog()

    await person.type(within(dialog).getByLabelText("Proposed value"), "48")
    await person.type(within(dialog).getByLabelText(/Reason/), "The paper POD shows 48 pieces.")
    await person.click(within(dialog).getByRole("button", { name: "Propose correction" }))

    expect(await within(dialog).findByText(/cannot perform this action/)).toBeInTheDocument()
    expect(within(dialog).queryByText(/recorded/)).toBeNull()
  })

  it("does not offer shipment ID corrections, or corrections of unsupported fields or fields already awaiting a decision", async () => {
    const proposed = correction({ id: 9, field_name: "delivery_status", corrected: "Damaged" })
    await panelFor(REVIEWER, {}, detail({ corrections: [proposed], effective: { delivery_status: effective(FIELDS.delivery_status, { proposed, latest_correction_id: 9 }) } }))

    expect(within(rowOf("Shipment id")).queryByRole("button")).toBeNull()
    expect(within(rowOf("Shipment id")).getByText("Shipment ID corrections are not available.")).toBeInTheDocument()
    expect(within(rowOf("Signature image")).queryByRole("button")).toBeNull()
    expect(within(rowOf("Signature image")).getByText("Not supported by the extractor.")).toBeInTheDocument()
    expect(within(rowOf("Delivery status")).queryByRole("button")).toBeNull()
    expect(within(rowOf("Delivery status")).getByText("A correction is awaiting a decision.")).toBeInTheDocument()
    expect(within(rowOf("Delivered pieces")).getByRole("button", { name: /Propose a correction for Delivered pieces/i })).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// C. Reviewer and administrator controls
// ---------------------------------------------------------------------------

describe("reviewer and administrator controls", () => {
  it("lets a reviewer propose but not extract, approve or check integrity", async () => {
    mockApi({
      "/api/session": REVIEWER, "/api/extractions": { extractions: [], extractor_version: EXTRACTOR }, "/api/extractions/staleness": OK_STALENESS,
    })
    panel()

    expect(await screen.findByText("No extraction stored for this shipment")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Extract fields/ })).toBeNull()
    expect(screen.getAllByText("An administrator can extract it.")).toHaveLength(2)
    expect(screen.queryByRole("link", { name: "Correction approvals" })).toBeNull()
    expect(screen.queryByRole("button", { name: /integrity/ })).toBeNull()
  })

  it("lets an administrator extract a document, and shows the server's answer", async () => {
    const { calls } = mockApi({
      "/api/session": ADMIN, "/api/extractions": { extractions: [], extractor_version: EXTRACTOR }, "/api/extractions/staleness": OK_STALENESS,
      "/api/extractions/run": () => ({ body: { id: 1, created: true, status: "extracted", complete: false, review_reasons: ["required field delivered_pieces is missing"] } }),
    })
    panel()

    await user().click(await screen.findByRole("button", { name: `Extract fields from ${POD}` }))

    expect(await screen.findByText(`${POD}: extracted, needs review.`)).toBeInTheDocument()
    expect(posts(calls)).toEqual([{ path: "/api/extractions/run", body: { document: POD } }])
    expect(screen.getByRole("link", { name: "Correction approvals" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Check correction history integrity/ })).toBeInTheDocument()
  })

  it("shows a user who cannot view extractions a plain explanation and requests nothing", async () => {
    const { calls } = mockApi({ "/api/session": { ...REVIEWER, permissions: ["exceptions.view"] } })
    panel()

    expect(await screen.findByText("Extractions are available to reviewers and administrators.")).toBeInTheDocument()
    expect(calls.filter((call) => call.url.pathname.startsWith("/api/extractions"))).toEqual([])
  })

  it("hides the approval page from a reviewer, who cannot reach the queue's data either", async () => {
    const { calls } = mockApi({ "/api/session": REVIEWER })
    renderPage(<Corrections />, "/corrections")

    expect(await screen.findByText("Administrators only")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull()
    expect(calls.filter((call) => call.url.pathname.startsWith("/api/extractions"))).toEqual([])
  })

  it("shows the approvals entry in the navigation to administrators only", async () => {
    mockApi({ "/api/session": REVIEWER })
    const reviewer = renderPage(<App />, "/corrections")
    await screen.findByText("Administrators only")
    expect(screen.queryByRole("link", { name: "Correction Approvals" })).toBeNull()
    reviewer.unmount()

    mockApi({
      "/api/session": ADMIN, "/api/extractions/pending": { pending: [] }, "/api/extractions": { extractions: [], extractor_version: EXTRACTOR }, "/api/extractions/staleness": OK_STALENESS,
    })
    renderPage(<App />, "/corrections")

    expect((await screen.findAllByRole("link", { name: "Correction Approvals" })).length).toBeGreaterThan(0)
  })
})

// ---------------------------------------------------------------------------
// D. The approval queue
// ---------------------------------------------------------------------------

function queue(session: unknown, items: Correction[], extra: Record<string, unknown> = {}) {
  let pending = items

  const api = mockApi({
    "/api/session": session,
    "/api/extractions/pending": () => ({ body: { pending } }),
    "/api/extractions": { extractions: [SUMMARY], extractor_version: EXTRACTOR },
    "/api/extractions/staleness": OK_STALENESS,
    ...extra,
  })

  return { ...api, resolve: (id: number) => (pending = pending.filter((item) => item.id !== id)) }
}

const RECORDED: DecisionResult = {
  decision_id: 1, correction_id: 5, decision: "approved", note: null, actor: "dave", at: "2026-10-22T11:00:00+00:00", proposed_by: "bob", extraction_id: 1,
  field_name: "delivered_pieces", document_type: "POD", filename: POD, shipment_key: "SHJ2026001", corrected: 48, recompute: { status: "recorded", run_id: 3, shipments: ["SHJ2026001"] },
}

describe("the approval queue", () => {
  it("lists each proposal with its shipment, document, values, reason, proposer and version", async () => {
    queue(SECOND_ADMIN, [correction({ based_on: 2 })])
    renderPage(<Corrections />, "/corrections")
    const card = (await screen.findByRole("heading", { name: /SHJ-2026-001/ })).closest("div.rounded-lg") as HTMLElement

    expect(screen.getByRole("link", { name: "SHJ-2026-001" })).toHaveAttribute("href", "/shipments/SHJ2026001")
    expect(within(card).getByText(POD, { exact: false })).toBeInTheDocument()
    expect(within(card).getByText("Delivered pieces", { exact: false })).toBeInTheDocument()
    expect(within(card).getByText("45")).toBeInTheDocument()
    expect(within(card).getByText("48")).toBeInTheDocument()
    expect(within(card).getByText("The paper POD shows 48 pieces.")).toBeInTheDocument()
    expect(within(card).getByText(/bob ·/)).toBeInTheDocument()
    expect(within(card).getByText(/Based on correction #2/)).toBeInTheDocument()
    expect(within(card).getByText("Awaiting approval")).toBeInTheDocument()
    expect(within(card).getByText(/not applied until approved/)).toBeInTheDocument()
  })

  it("never lets the proposer approve or reject their own correction, and says why", async () => {
    queue(ADMIN, [correction({ id: 5, actor: "alice" }), correction({ id: 6, actor: "bob", field_name: "delivery_date", corrected: "2026-10-06", previous: { value: "2026-10-05", state: "EXTRACTED", source: "extracted" } })])
    renderPage(<Corrections />, "/corrections")
    await screen.findAllByRole("heading", { level: 3 })

    const cards = screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.closest("div.rounded-lg") as HTMLElement)
    const own = cards.find((card) => card.textContent?.includes("alice ·"))!
    const other = cards.find((card) => card.textContent?.includes("bob ·"))!

    expect(within(own).getByRole("button", { name: /Approve/ })).toBeDisabled()
    expect(within(own).getByText(/You proposed this correction. A different administrator must approve or reject it./)).toBeInTheDocument()
    expect(within(own).getByRole("button", { name: /Reject/ })).toBeDisabled()
    expect(within(other).getByRole("button", { name: /Approve/ })).toBeEnabled()
    expect(within(other).getByRole("button", { name: /Reject/ })).toBeEnabled()
  })

  it("approves, shows what the server confirmed, and refreshes the queue", async () => {
    const api = queue(SECOND_ADMIN, [correction()], {
      "/api/extractions/corrections/5/decision": () => {
        api.resolve(5)
        return { body: RECORDED }
      },
    })
    renderPage(<Corrections />, "/corrections")
    const person = user()

    await person.click(await screen.findByRole("button", { name: /Approve/ }))
    const dialog = await screen.findByRole("dialog")
    expect(within(dialog).getByText(/replaces the document's value|makes this the value the quantity audit reads/)).toBeInTheDocument()
    await person.type(within(dialog).getByLabelText(/Note/), "Checked against the paper POD.")
    await person.click(within(dialog).getByRole("button", { name: "Approve correction" }))

    expect(await screen.findByText(/was approved\. Audit run #3 was recorded for SHJ2026001/)).toBeInTheDocument()
    expect(await screen.findByText("No corrections are waiting for approval")).toBeInTheDocument()
    expect(posts(api.calls)).toEqual([{ path: "/api/extractions/corrections/5/decision", body: { decision: "approve", note: "Checked against the paper POD." } }])
    expect(gets(api.calls, "/api/extractions/pending")).toBeGreaterThan(1)
  })

  it("rejects only with a reason, and says the proposal had no effect", async () => {
    const api = queue(SECOND_ADMIN, [correction()], {
      "/api/extractions/corrections/5/decision": () => {
        api.resolve(5)
        return { body: { ...RECORDED, decision: "rejected", recompute: { status: "not_needed" } } }
      },
    })
    renderPage(<Corrections />, "/corrections")
    const person = user()

    await person.click(await screen.findByRole("button", { name: /Reject/ }))
    const dialog = await screen.findByRole("dialog")
    await person.click(within(dialog).getByRole("button", { name: "Reject correction" }))

    expect(within(dialog).getByText(/Write why it is rejected \(at least 10 characters\)/)).toBeInTheDocument()
    expect(posts(api.calls)).toEqual([])

    await person.type(within(dialog).getByLabelText(/Reason for rejecting/), "The paper POD says 45 pieces.")
    await person.click(within(dialog).getByRole("button", { name: "Reject correction" }))

    expect(await screen.findByText(/was rejected\. It had no effect on any audit/)).toBeInTheDocument()
    expect(posts(api.calls)).toEqual([{ path: "/api/extractions/corrections/5/decision", body: { decision: "reject", note: "The paper POD says 45 pieces." } }])
  })

  it("does not send an approval twice", async () => {
    const api = queue(SECOND_ADMIN, [correction()], { "/api/extractions/corrections/5/decision": () => ({ body: RECORDED }) })
    renderPage(<Corrections />, "/corrections")
    const original = api.fetchMock.getMockImplementation()!
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => (release = resolve))
    let started = 0
    api.fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/decision")) {
        started += 1
        await gate
      }
      return original(input, init)
    })
    const person = user()

    await person.click(await screen.findByRole("button", { name: /Approve/ }))
    const dialog = await screen.findByRole("dialog")
    await person.dblClick(within(dialog).getByRole("button", { name: "Approve correction" }))

    expect(await within(dialog).findByRole("button", { name: "Saving…" })).toBeDisabled()
    expect(started).toBe(1)
    release()
    await screen.findByText(/was approved/)
    expect(posts(api.calls, "/api/extractions/corrections/5/decision")).toHaveLength(1)
  })

  it("shows a conflict (already decided) and refreshes the queue instead of the stale item", async () => {
    const api = queue(SECOND_ADMIN, [correction()], {
      "/api/extractions/corrections/5/decision": () => ({ status: 409, body: { detail: "Correction 5 was already approved." } }),
    })
    renderPage(<Corrections />, "/corrections")
    const person = user()

    await person.click(await screen.findByRole("button", { name: /Approve/ }))
    const dialog = await screen.findByRole("dialog")
    const before = gets(api.calls, "/api/extractions/pending")
    await person.click(within(dialog).getByRole("button", { name: "Approve correction" }))

    expect(await within(dialog).findByText("Correction 5 was already approved.")).toBeInTheDocument()
    await waitFor(() => expect(gets(api.calls, "/api/extractions/pending")).toBeGreaterThan(before))
  })

  it("shows the server's refusal of a self-approval or a missing permission without claiming success", async () => {
    queue(SECOND_ADMIN, [correction()], {
      "/api/extractions/corrections/5/decision": () => ({ status: 403, body: { detail: "You cannot decide your own correction: another administrator must." } }),
    })
    renderPage(<Corrections />, "/corrections")
    const person = user()

    await person.click(await screen.findByRole("button", { name: /Approve/ }))
    const dialog = await screen.findByRole("dialog")
    await person.click(within(dialog).getByRole("button", { name: "Approve correction" }))

    expect(await within(dialog).findByText(/cannot decide your own correction/)).toBeInTheDocument()
    expect(screen.queryByText(/was approved/)).toBeNull()
  })

  it("says plainly when an approval was recorded but the recalculated run was not, and retries only on request", async () => {
    let retries = 0
    const api = queue(SECOND_ADMIN, [correction()], {
      "/api/extractions/corrections/5/decision": () => ({ body: { ...RECORDED, recompute: { status: "failed", message: "The approval is recorded; the run was not. Record it again." } } }),
      "/api/extractions/recompute": () => {
        retries += 1
        return retries === 1 ? { status: 500, body: { detail: "Something went wrong on the server." } } : { body: { recorded: true, run_id: 4, shipments: ["SHJ2026001"] } }
      },
    })
    renderPage(<Corrections />, "/corrections")
    const person = user()

    await person.click(await screen.findByRole("button", { name: /Approve/ }))
    await person.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Approve correction" }))

    expect(await screen.findByText(/The audit already uses it, but the recalculated run could not be recorded/)).toBeInTheDocument()
    expect(screen.queryByText(/Recorded run/)).toBeNull()
    expect(posts(api.calls, "/api/extractions/recompute")).toEqual([])

    await person.click(screen.getByRole("button", { name: "Retry recording the run" }))
    expect(await screen.findByText(/The run was not recorded: Something went wrong on the server\./)).toBeInTheDocument()

    await person.click(screen.getByRole("button", { name: "Retry recording the run" }))
    expect(await screen.findByText("Recorded run #4 for SHJ2026001.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Retry recording the run" })).toBeNull()
    expect(posts(api.calls, "/api/extractions/recompute")).toEqual([
      { path: "/api/extractions/recompute", body: { shipment_id: "SHJ2026001" } },
      { path: "/api/extractions/recompute", body: { shipment_id: "SHJ2026001" } },
    ])
  })

  it("describes every other server outcome of an approval accurately", () => {
    const text = (status: DecisionResult["recompute"]["status"]) => outcomeOf({ ...RECORDED, recompute: { status } }).text

    expect(text("no_calculation_consumes_this_field")).toMatch(/No audit calculation reads this field, so no run was recorded/)
    expect(text("no_recorded_history")).toMatch(/no earlier run was superseded/)
    expect(text("no_shipment")).toMatch(/not attributed to a shipment/)
    expect(text("not_authorized")).toMatch(/You cannot record audit runs/)
    expect(outcomeOf({ ...RECORDED, recompute: { status: "failed" } }).retry).toBe("SHJ2026001")
    expect(outcomeOf(RECORDED).retry).toBeNull()
  })

  it("shows loading, empty and failure states", async () => {
    queue(SECOND_ADMIN, [])
    const empty = renderPage(<Corrections />, "/corrections")
    expect(await screen.findByText("No corrections are waiting for approval")).toBeInTheDocument()
    empty.unmount()

    mockApi({ "/api/session": SECOND_ADMIN, "/api/extractions/pending": () => ({ status: 500, body: { detail: "Something went wrong on the server." } }), "/api/extractions": { extractions: [], extractor_version: EXTRACTOR }, "/api/extractions/staleness": OK_STALENESS })
    renderPage(<Corrections />, "/corrections")
    expect(await screen.findByText("The pending corrections could not be loaded")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Try again/ })).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// E. Availability, staleness, recompute and integrity
// ---------------------------------------------------------------------------

describe("store availability, staleness and recomputation", () => {
  it("warns that corrections cannot be verified when the store is unavailable, instead of a bare error", async () => {
    mockApi({
      "/api/session": REVIEWER, "/api/extractions": () => ({ status: 500, body: { detail: "Something went wrong on the server." } }),
      "/api/extractions/staleness": { stale: [], corrections_available: false },
    })
    panel()

    expect(await screen.findByText("Corrections cannot be verified right now.")).toBeInTheDocument()
    expect(screen.getByText(/must not be taken as approved/)).toBeInTheDocument()
    expect(screen.queryByText("The extractions could not be loaded")).toBeNull()
  })

  it("keeps the warning above the values when the store is unavailable but the extractions load", async () => {
    await panelFor(REVIEWER, { "/api/extractions/staleness": { stale: [], corrections_available: false } })

    expect(await screen.findByText("Corrections cannot be verified right now.")).toBeInTheDocument()
    expect(within(rowOf("Delivered pieces")).queryByTestId("approved-delivered_pieces")).toBeNull()
    expect(within(rowOf("Delivered pieces")).getByText("Extracted (original)")).toBeInTheDocument()
  })

  it("shows a general failure with a retry when the extractions cannot be loaded for another reason", async () => {
    mockApi({ "/api/session": REVIEWER, "/api/extractions": () => ({ status: 500, body: { detail: "Something went wrong on the server." } }), "/api/extractions/staleness": OK_STALENESS })
    panel()

    expect(await screen.findByText("The extractions could not be loaded")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Try again/ })).toBeInTheDocument()
    expect(screen.queryByText("Corrections cannot be verified right now.")).toBeNull()
  })

  it("shows a loading state first", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => undefined)))
    panel()

    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true")
  })

  it("shows when the audit is ahead of its recorded history, and records the run only on request", async () => {
    const api = mockApi({
      "/api/session": ADMIN, "/api/extractions": { extractions: [], extractor_version: EXTRACTOR }, "/api/extractions/staleness": STALE,
      "/api/extractions/recompute": () => ({ body: { recorded: true, run_id: 2, shipments: ["SHJ2026001"] } }),
    })
    panel()

    expect(await screen.findByText("A recalculated audit run is waiting to be recorded.")).toBeInTheDocument()
    expect(screen.getByText(/latest recorded run \(#1\) does not include 1 approved correction\(s\)/)).toBeInTheDocument()
    expect(posts(api.calls)).toEqual([])
    expect(screen.queryByText(/Recorded run/)).toBeNull()

    await user().click(screen.getByRole("button", { name: "Record the recalculated run" }))

    expect(await screen.findByText(/Recorded run #2 for SHJ2026001\. It supersedes the earlier run/)).toBeInTheDocument()
    expect(posts(api.calls)).toEqual([{ path: "/api/extractions/recompute", body: { shipment_id: "SHJ2026001" } }])
  })

  it("does not start a second recording while one is running, and reports the server's failure", async () => {
    const api = mockApi({
      "/api/session": ADMIN, "/api/extractions": { extractions: [], extractor_version: EXTRACTOR }, "/api/extractions/staleness": STALE,
      "/api/extractions/recompute": () => ({ status: 500, body: { detail: "Something went wrong on the server." } }),
    })
    const original = api.fetchMock.getMockImplementation()!
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => (release = resolve))
    let started = 0
    api.fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/recompute")) {
        started += 1
        await gate
      }
      return original(input, init)
    })
    panel()
    const person = user()

    await person.dblClick(await screen.findByRole("button", { name: "Record the recalculated run" }))
    expect(await screen.findByRole("button", { name: "Recording…" })).toBeDisabled()
    expect(started).toBe(1)

    release()
    expect(await screen.findByText(/The run was not recorded: Something went wrong on the server\./)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Retry recording the run" })).toBeEnabled()
    expect(screen.queryByText(/Recorded run/)).toBeNull()
  })

  it("says when another request had already recorded the run", async () => {
    mockApi({
      "/api/session": ADMIN, "/api/extractions": { extractions: [], extractor_version: EXTRACTOR }, "/api/extractions/staleness": STALE,
      "/api/extractions/recompute": () => ({ body: { recorded: false, run_id: null, shipments: [] } }),
    })
    panel()

    await user().click(await screen.findByRole("button", { name: "Record the recalculated run" }))

    expect(await screen.findByText("Nothing needed recording: the recalculated run had already been recorded.")).toBeInTheDocument()
  })

  it("tells a reviewer that an administrator must record the run", async () => {
    mockApi({ "/api/session": REVIEWER, "/api/extractions": { extractions: [], extractor_version: EXTRACTOR }, "/api/extractions/staleness": STALE })
    panel()

    expect(await screen.findByText("An administrator can record it.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Record the recalculated run/ })).toBeNull()
  })

  it("says when nothing is waiting", async () => {
    await panelFor(REVIEWER)

    expect(await screen.findByText("No recorded run is waiting for an approved correction.")).toBeInTheDocument()
  })

  it("runs the integrity check on request and shows what the server found", async () => {
    let report: unknown = { ok: true, corrections_checked: 3, decisions_checked: 2, extractions_checked: 4, unchained_legacy: 1, problems: [] }
    const api = mockApi({
      "/api/session": ADMIN, "/api/extractions": { extractions: [], extractor_version: EXTRACTOR }, "/api/extractions/staleness": OK_STALENESS,
      "/api/extractions/integrity": () => ({ body: report }),
    })
    panel()
    const person = user()

    expect(gets(api.calls, "/api/extractions/integrity")).toBe(0)
    await person.click(await screen.findByRole("button", { name: /Check correction history integrity/ }))

    expect(await screen.findByText(/Verified 3 correction\(s\), 2 decision\(s\) and 4 stored extraction\(s\)\. 1 older row\(s\) predate the hash chain/)).toBeInTheDocument()
    expect(screen.getByText(/not tamper-proof against someone who can write the database file/)).toBeInTheDocument()

    report = { ok: false, corrections_checked: 3, decisions_checked: 2, extractions_checked: 4, unchained_legacy: 0, problems: ["decision 1: correction 5 was decided by its own proposer"] }
    await person.click(screen.getByRole("button", { name: /Check correction history integrity/ }))

    expect(await screen.findByText("The stored history failed verification.")).toBeInTheDocument()
    expect(screen.getByText("decision 1: correction 5 was decided by its own proposer")).toBeInTheDocument()
  })

  it("says when the integrity check itself fails to run", async () => {
    mockApi({
      "/api/session": ADMIN, "/api/extractions": { extractions: [], extractor_version: EXTRACTOR }, "/api/extractions/staleness": OK_STALENESS,
      "/api/extractions/integrity": () => ({ status: 500, body: { detail: "Something went wrong on the server." } }),
    })
    panel()

    await user().click(await screen.findByRole("button", { name: /Check correction history integrity/ }))

    expect(await screen.findByText(/The integrity check could not run: Something went wrong on the server\./)).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// F. Recorded runs: status, superseded links and inspection
// ---------------------------------------------------------------------------

const RUNS = [
  { id: 2, created_at: "2026-10-22T11:00:00+00:00", label: "Approved correction 5", shipments_checked: 1, snapshots_written: 1, calculation_version: "2",
    supersedes: [{ shipment_key: "SHJ2026001", old_run_id: 1, correction_ids: [5] }], superseded_by: [] },
  { id: 1, created_at: "2026-10-22T10:00:00+00:00", label: "All shipments", shipments_checked: 3, snapshots_written: 3, calculation_version: "2",
    supersedes: [], superseded_by: [{ shipment_key: "SHJ2026001", new_run_id: 2, correction_ids: [5] }] },
  { id: 0, created_at: "2026-10-21T10:00:00+00:00", label: "Before inputs were recorded", shipments_checked: 1, snapshots_written: 1, calculation_version: null,
    supersedes: [], superseded_by: [] },
]

const input = (over: Record<string, unknown>) => ({
  id: 1, run_id: 2, shipment_key: "SHJ2026001", shipment_id: "SHJ-2026-001", field_name: "delivered_pieces", value: 48, source: "approved_correction", resolved: true, state: "extracted",
  correction_id: 5, extraction_id: 1, extractor_version: EXTRACTOR, document: POD, page: null, validation: "approved", warnings: [], consumed_by: ["quantity"], structured: null, calculation_version: "2", ...over,
})

const INPUTS_2 = {
  run_id: 2, recorded: true, superseded_by: { SHJ2026001: null },
  inputs: [
    input({ id: 1 }),
    input({ id: 2, field_name: "expected_pieces", value: 48, source: "legacy_reader", correction_id: null, extraction_id: null, extractor_version: null, document: "BOL_SHJ-2026-001.pdf", page: 1, validation: "valid" }),
    input({ id: 3, field_name: "actual_date", value: null, source: "legacy_reader", resolved: false, state: "conflicting", correction_id: null, extraction_id: null, extractor_version: null, document: null, page: null, validation: "invalid", warnings: ["approved_corrections_disagree"], consumed_by: ["delivery"] }),
  ],
}

const INPUTS_1 = { run_id: 1, recorded: true, superseded_by: { SHJ2026001: 2 }, inputs: [input({ id: 9, run_id: 1, value: 45, source: "legacy_reader", correction_id: null, extraction_id: null, extractor_version: null, document: POD, page: 1, validation: "valid" })] }

const HISTORY = (runId: number) => [{
  snapshot: { id: 1, shipment_id: "SHJ-2026-001", created_at: "2026-10-22T10:00:00+00:00", final_status: runId === 2 ? "DISCREPANCIES DETECTED" : "DISCREPANCIES DETECTED", discrepancy_count: 2, review_count: 0, carrier: "SwiftLine",
    quantity_status: runId === 2 ? "MATCH" : "SHORTAGE", delivery_status: "DELAYED", invoice_status: "MISMATCH", bol_pieces: 48, pod_pieces: runId === 2 ? 48 : 45, invoice_total: 1550,
    runs: [{ run_id: runId, created_at: "2026-10-22T10:00:00+00:00" }], findings: [] },
  changes: { first: true, input_changed: false, status_change: null, new: [], reappeared: [], persistent: [], changed: [], resolved: [], unverified: [], value_changes: [], state_changes: [] },
}]

function runs(extra: Record<string, unknown> = {}, stale: unknown = OK_STALENESS) {
  return mockApi({
    "/api/session": REVIEWER, "/api/history/runs": RUNS, "/api/extractions/staleness": stale,
    "/api/history/runs/2/inputs": INPUTS_2, "/api/history/runs/1/inputs": INPUTS_1,
    "/api/history/runs/0/inputs": { run_id: 0, inputs: [], recorded: false, superseded_by: {} },
    "/api/history/shipments/SHJ-2026-001": (url: URL) => ({ body: HISTORY(url.searchParams.get("run") === "1" ? 1 : 2) }),
    ...extra,
  })
}

describe("recorded runs", () => {
  it("shows the calculation version, what each run superseded and was superseded by, and not-recorded versions", async () => {
    runs()
    renderPage(<RunsTab />)
    const rows = await screen.findAllByRole("row")
    const run2 = rows.find((row) => within(row).queryByText("#2"))!
    const run1 = rows.find((row) => within(row).queryByText("#1"))!
    const run0 = rows.find((row) => within(row).queryByText("#0"))!

    expect(within(run2).getByText("v2")).toBeInTheDocument()
    expect(within(run2).getByText("Supersedes run #1 (SHJ2026001)")).toBeInTheDocument()
    expect(within(run1).getByRole("button", { name: /Superseded by run #2/ })).toBeInTheDocument()
    expect(within(run0).getByText("not recorded")).toBeInTheDocument()
    expect(within(run0).getByText("Not superseded")).toBeInTheDocument()
  })

  it("marks the latest recorded run of a shipment as stale when the API says it lacks approved corrections", async () => {
    runs({}, STALE)
    renderPage(<RunsTab />)
    const rows = await screen.findAllByRole("row")
    const run1 = rows.find((row) => within(row).queryByText("#1"))!

    expect(await within(run1).findByText("Stale: missing approved corrections")).toBeInTheDocument()
    expect(within(rows.find((row) => within(row).queryByText("#2"))!).queryByText(/Stale/)).toBeNull()
  })

  it("inspects the inputs a run consumed: source, correction, document, page, checks and what consumed them", async () => {
    runs()
    renderPage(<RunsTab />)
    const person = user()

    await person.click(await screen.findByRole("button", { name: "Inspect the inputs of run 2" }))
    const region = await screen.findByLabelText("Inputs of run 2")
    const pieces = (await within(region).findByText("Delivered pieces")).closest("tr") as HTMLElement

    expect(within(pieces).getByText("48")).toBeInTheDocument()
    expect(within(pieces).getByText("Approved correction #5")).toBeInTheDocument()
    expect(within(pieces).getByText(/Extraction #1/)).toBeInTheDocument()
    expect(within(pieces).getByText(POD)).toBeInTheDocument()
    expect(within(pieces).getByText("typed by a reviewer: no page")).toBeInTheDocument()

    const expected = within(region).getByText("Expected pieces").closest("tr") as HTMLElement
    expect(within(expected).getByText("Document reader")).toBeInTheDocument()
    expect(within(expected).getByText("page 1")).toBeInTheDocument()

    const date = within(region).getByText("Actual date").closest("tr") as HTMLElement
    expect(within(date).getByText("Unresolved")).toBeInTheDocument()
    expect(within(date).getByText("conflicting")).toBeInTheDocument()
    expect(within(date).getByText("approved_corrections_disagree")).toBeInTheDocument()
    expect(date.textContent).not.toMatch(/\b0\b/)
    expect(within(region).getByText(/calculation version 2/)).toBeInTheDocument()
  })

  it("shows the result the run recorded, read from the shipment's history", async () => {
    runs()
    renderPage(<RunsTab />)

    await user().click(await screen.findByRole("button", { name: "Inspect the inputs of run 2" }))
    const result = await screen.findByLabelText("Result recorded in this run")

    expect(within(result).getByText("Match")).toBeInTheDocument()
    expect(within(result).getByText("48 / 48")).toBeInTheDocument()
  })

  it("follows a superseded-by link to the later run's inputs without changing the earlier run", async () => {
    const api = runs()
    renderPage(<RunsTab />)
    const person = user()

    await person.click(await screen.findByRole("button", { name: "Inspect the inputs of run 1" }))
    const first = await screen.findByLabelText("Inputs of run 1")
    expect(within(first).getByText("45")).toBeInTheDocument()
    expect(within(first).getByText("Document reader")).toBeInTheDocument()

    await person.click(within(first).getByRole("button", { name: "Superseded by run #2" }))

    expect(await screen.findByLabelText("Inputs of run 2")).toBeInTheDocument()
    expect(within(await screen.findByLabelText("Inputs of run 2")).getByText("Approved correction #5")).toBeInTheDocument()
    expect(posts(api.calls)).toEqual([])
  })

  it("says that a run recorded before inputs existed has none, instead of reconstructing them", async () => {
    runs()
    renderPage(<RunsTab />)

    await user().click(await screen.findByRole("button", { name: "Inspect the inputs of run 0" }))

    expect(await screen.findByText(/recorded before audit inputs were recorded\. They are not reconstructed/)).toBeInTheDocument()
    expect(screen.queryByLabelText("Inputs of run 0")).toBeNull()
  })

  it("shows loading and failure states when inspecting, and can retry", async () => {
    let fail = true
    runs({ "/api/history/runs/2/inputs": () => (fail ? { status: 500, body: { detail: "Something went wrong on the server." } } : { body: INPUTS_2 }) })
    renderPage(<RunsTab />)
    const person = user()

    await person.click(await screen.findByRole("button", { name: "Inspect the inputs of run 2" }))
    expect(await screen.findByText("The inputs of run #2 could not be loaded")).toBeInTheDocument()

    fail = false
    await person.click(screen.getByRole("button", { name: /Try again/ }))
    expect(await screen.findByLabelText("Inputs of run 2")).toBeInTheDocument()
  })

  it("shows the empty and failed states of the run list", async () => {
    runs({ "/api/history/runs": [] })
    const empty = renderPage(<RunsTab />)
    expect(await screen.findByText("No audit run recorded")).toBeInTheDocument()
    empty.unmount()

    runs({ "/api/history/runs": () => ({ status: 500, body: { detail: "Something went wrong on the server." } }) })
    renderPage(<RunsTab />)
    expect(await screen.findByText("This could not be loaded")).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Checks across the shipment's documents
// ---------------------------------------------------------------------------

describe("extraction panel: checks across documents", () => {
  const ISSUES = [
    { code: "invoice_below_rate_confirmation", severity: "info", message: "The invoice total is below the agreed rate.", sources: ["INVOICE.pdf", "RATE.pdf"] },
    { code: "delivery_before_pickup", severity: "error", message: "The POD delivery date is before the pickup date.", sources: ["POD.pdf", "BOL.pdf"] },
  ]

  it("lists each finding with its severity and documents, the most serious first", async () => {
    await panelFor(REVIEWER, { "/api/extractions": { extractions: [SUMMARY], extractor_version: EXTRACTOR, cross_document_issues: ISSUES } })

    const block = screen.getByLabelText("Checks across documents")
    const items = within(block).getAllByRole("listitem")

    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent("Error: The POD delivery date is before the pickup date.")
    expect(items[0]).toHaveTextContent("Documents: POD.pdf, BOL.pdf")
    expect(items[1]).toHaveTextContent("Note: The invoice total is below the agreed rate.")
  })

  it("shows nothing when the documents agree", async () => {
    await panelFor(REVIEWER, { "/api/extractions": { extractions: [SUMMARY], extractor_version: EXTRACTOR, cross_document_issues: [] } })

    expect(screen.queryByLabelText("Checks across documents")).toBeNull()
  })
})
