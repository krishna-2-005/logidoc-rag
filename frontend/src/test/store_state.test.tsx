import { screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ExtractionsSection } from "@/components/extraction/ExtractionPanel"
import { StoreStateNotice, storeWriteBlock } from "@/components/extraction/storeState"
import { Corrections } from "@/pages/Corrections"
import type { Correction, DocumentFile } from "@/lib/types"
import { ADMIN_SESSION, REVIEWER_SESSION, mockApi, renderPage } from "@/test/utils"

// Fixtures follow src/api/routes_extraction.py. Synthetic: nothing here measures real documents.

afterEach(() => vi.unstubAllGlobals())

const POD = "POD_SHJ-2026-001.pdf"
const EXTRACTOR = "1.1+abc123"
const REVIEWER = { ...REVIEWER_SESSION, permissions: [...REVIEWER_SESSION.permissions, "extraction.view", "extraction.correct", "extraction.run"] }
const ADMIN = { ...ADMIN_SESSION, permissions: [...ADMIN_SESSION.permissions, "extraction.view", "extraction.correct", "extraction.run", "extraction.approve", "history.view"] }

const NO_RECORD = { attempt: "none", code: null, at: null, record: "missing" }
const FAILED = { attempt: "failed", code: "backup_failed", at: "2026-10-23T08:00:00+00:00", record: "ok" }

const store = (state: string, migration: unknown = NO_RECORD, reason = "A server sentence that must never be shown: OperationalError at C:\\data\\extractions.sqlite3") => ({
  state, version: 4, reason, migration,
})
const staleness = (state: string | null, extra: Record<string, unknown> = {}, migration: unknown = state === "migration_failed" ? FAILED : NO_RECORD) => ({
  stale: [], corrections_available: true, outdated_cross_checks: [], ...(state === null ? {} : { store: store(state, migration) }), ...extra,
})

const SUMMARY = {
  id: 1, filename: POD, sha256: "h", extractor_version: EXTRACTOR, document_type: "POD", status: "extracted", shipment_id: "SHJ-2026-001",
  shipment_key: "SHJ2026001", created_at: "2026-10-22T09:00:00+00:00", correction_count: 0, pending_count: 0,
}

const field = (name: string, value: unknown) => ({
  name, state: "EXTRACTED", value, raw_text: null, source_file: POD, page: 1, method: "label_pair", validation: "valid", basis: "exact_label",
  warnings: [], alternatives: [], needs_review: false,
})

const FIELDS = { shipment_id: field("shipment_id", "SHJ-2026-001"), delivered_pieces: field("delivered_pieces", 45) }

const effective = (item: ReturnType<typeof field>) => ({
  extracted: { value: item.value, state: item.state, page: item.page }, proposed: null, approved: null, latest_correction_id: null,
  value: item.value, state: item.state, source: "extracted", page: item.page, correction_id: null,
})

const alias = (id: number, filename: string, relation: string, type = "POD") => ({
  id, filename, inferred_type: type, relation, first_seen_at: "2026-10-22T09:00:00+00:00", seen_by: id === 1 ? null : "alice",
})

function detail(extra: Record<string, unknown> = {}) {
  return {
    ...SUMMARY, payload: {
      filename: POD, document_type: "POD", status: "extracted", pages: 1, pages_without_text: [], extractor_version: EXTRACTOR, fields: FIELDS,
      line_items: [], unclassified_amounts: [], excluded_amounts: [], warnings: [], review_reasons: [], error: null,
    },
    effective: Object.fromEntries(Object.entries(FIELDS).map(([name, item]) => [name, effective(item)])), corrections: [],
    limitations: ["A correction is a proposal until a different administrator approves it."], ...extra,
  }
}

const DOCUMENTS = [{ filename: POD }, { filename: "BOL_SHJ-2026-001.pdf" }] as unknown as DocumentFile[]

function panel(session: unknown, stale: unknown, extra: Record<string, unknown> = {}) {
  const api = mockApi({
    "/api/session": session, "/api/extractions": { extractions: [SUMMARY], extractor_version: EXTRACTOR }, "/api/extractions/1": detail(),
    "/api/extractions/staleness": stale, ...extra,
  })
  renderPage(<ExtractionsSection shipmentId="SHJ-2026-001" shipmentKey="SHJ2026001" documents={DOCUMENTS} />)

  return api
}

const posts = (calls: { url: URL; init?: RequestInit }[]) => calls.filter((call) => call.init?.method === "POST")

const notice = (session: unknown, stale: unknown) => {
  const api = mockApi({ "/api/session": session, "/api/extractions/staleness": stale })
  renderPage(<StoreStateNotice />)

  return api
}

// ---------------------------------------------------------------------------
// The store states
// ---------------------------------------------------------------------------

describe("store state notice", () => {
  it.each([
    ["absent", "No extraction store exists yet. The first extraction creates it."],
    ["older", "from before approvals existed and needs an explicit migration"],
    ["migration_pending", "waiting for an explicit migration"],
    ["migration_failed", "was attempted and failed"],
  ])("says what the %s state means", async (state, text) => {
    notice(REVIEWER, staleness(state))

    expect(await screen.findByTestId(`store-state-${state}`)).toHaveTextContent(text)
  })

  it("says nothing for a current store", async () => {
    const api = notice(REVIEWER, staleness("current"))

    await vi.waitFor(() => expect(api.calls.some((call) => call.url.pathname === "/api/extractions/staleness")).toBe(true))

    expect(screen.queryByTestId(/store-state/)).toBeNull()
  })

  it("explains that approvals are still read and writes wait while the migration is pending, without offering a migration", async () => {
    const api = notice(ADMIN, staleness("migration_pending"))
    const alert = await screen.findByTestId("store-state-migration_pending")

    expect(alert).toHaveTextContent("approved corrections and their history are still read")
    expect(alert).toHaveTextContent("Extracting documents, proposing corrections and deciding them are unavailable")
    expect(alert).toHaveTextContent("it is not run from this page")
    expect(alert).toHaveTextContent("does not mean there are none")
    expect(alert).toHaveTextContent("No migration attempt has been recorded for this store.")
    expect(within(alert).queryByRole("button")).toBeNull()
    expect(screen.queryByRole("button", { name: /migrat/i })).toBeNull()
    expect(posts(api.calls)).toEqual([])
  })

  it("says a failed migration is confirmed, with the time and what it leaves untouched", async () => {
    notice(ADMIN, staleness("migration_failed"))

    const alert = await screen.findByTestId("store-state-migration_failed")

    expect(alert).toHaveTextContent("was attempted and failed")
    expect(alert).toHaveTextContent("It was recorded")
    expect(alert).toHaveTextContent("approved corrections and their history are still read, and audits are unaffected")
    expect(alert).toHaveTextContent("It is not run from this page")
    expect(within(alert).queryByRole("button")).toBeNull()
  })

  it.each([
    ["history_verification_failed", "correction history did not pass verification"],
    ["integrity_check_failed", "failed its integrity check"],
    ["backup_failed", "backup could not be made and verified"],
    ["store_changed", "changed while the migration was starting"],
    ["rolled_back", "stopped part-way and was rolled back"],
    ["verification_failed", "checks failed before it committed"],
    ["a_code_from_the_future", "did not complete, and the store was left as it was"],
  ])("gives recovery guidance for the confirmed failure code %s without echoing the code", async (code, text) => {
    notice(ADMIN, staleness("migration_failed", {}, { ...FAILED, code }))

    const alert = await screen.findByTestId("store-state-migration_failed")

    expect(alert).toHaveTextContent(text)
    expect(alert).not.toHaveTextContent(code)
  })

  it.each([
    ["no migration object", undefined],
    ["a missing record", { attempt: "none", code: null, at: null, record: "missing" }],
    ["an ignored record", { attempt: "none", code: null, at: null, record: "ignored" }],
    ["an unreadable record", { attempt: "none", code: null, at: null, record: "unreadable" }],
    ["a failed attempt whose record is not confirmed", { attempt: "failed", code: "rolled_back", at: null, record: "ignored" }],
  ])("does not claim a failure for migration_failed with %s", async (_name, migration) => {
    notice(ADMIN, { ...staleness("migration_failed"), store: { state: "migration_failed", version: 4, reason: "x", ...(migration === undefined ? {} : { migration }) } })

    const alert = await screen.findByTestId("store-state-migration_failed_unconfirmed")

    expect(alert).toHaveTextContent("could not confirm it from a status record for this store, so no failure is claimed")
    expect(screen.queryByTestId("store-state-migration_failed")).toBeNull()
    expect(alert).not.toHaveTextContent("was attempted and failed")
  })

  it("tells a confirmed failure, a pending migration and an unfinished attempt apart", async () => {
    const failed = notice(ADMIN, staleness("migration_failed"))
    expect(await screen.findByTestId("store-state-migration_failed")).toBeInTheDocument()
    failed.fetchMock.mockClear()
    document.body.innerHTML = ""

    notice(ADMIN, staleness("migration_pending"))
    const pending = await screen.findByTestId("store-state-migration_pending")

    expect(pending).toHaveTextContent("No migration attempt has been recorded")
    expect(pending).not.toHaveTextContent("failed")
    document.body.innerHTML = ""

    notice(ADMIN, staleness("migration_pending", {}, { attempt: "unfinished", code: null, at: "2026-10-23T08:00:00+00:00", record: "ok" }))
    const unfinished = await screen.findByTestId("store-state-migration_pending")

    expect(unfinished).toHaveTextContent("has not reported an outcome")
    expect(unfinished).toHaveTextContent("may still be running, or it may have been interrupted")
    expect(unfinished).toHaveTextContent("This is not a confirmed failure")
    expect(unfinished).not.toHaveTextContent("was attempted and failed")
  })

  it.each([
    ["unreadable", "could not be read, so this page makes no claim about earlier attempts"],
    ["ignored", "does not apply to this store as it is now and was ignored"],
    ["missing", "No migration attempt has been recorded for this store."],
  ])("says what a %s status record means for a pending store, without a failure claim", async (record, text) => {
    notice(ADMIN, staleness("migration_pending", {}, { attempt: "none", code: null, at: null, record }))

    const note = await screen.findByTestId("migration-record-note")

    expect(note).toHaveTextContent(text)
    expect(screen.queryByTestId("store-state-migration_failed")).toBeNull()
  })

  it("shows the unfinished note for an older store too", async () => {
    notice(ADMIN, staleness("older", {}, { attempt: "unfinished", code: null, at: null, record: "ok" }))

    expect(await screen.findByTestId("migration-record-note")).toHaveTextContent("has not reported an outcome")
  })

  it("shows a migrated store that failed verification as its own state, even when the corrections warning is showing", async () => {
    notice(ADMIN, staleness("unavailable", { corrections_available: false }, { attempt: "unverified", code: "post_verification_failed", at: null, record: "ok" }))

    const alert = await screen.findByTestId("store-state-unverified")

    expect(alert).toHaveTextContent("was migrated but failed verification afterwards")
    expect(alert).toHaveTextContent("restore the backup that the migration kept")
    expect(within(alert).queryByRole("button")).toBeNull()
  })

  it("says nothing extra for a current store whose last migration succeeded", async () => {
    const api = notice(ADMIN, staleness("current", {}, { attempt: "succeeded", code: null, at: "2026-10-23T08:00:00+00:00", record: "ok" }))

    await vi.waitFor(() => expect(api.calls.some((call) => call.url.pathname === "/api/extractions/staleness")).toBe(true))

    expect(screen.queryByTestId(/store-state/)).toBeNull()
  })

  it("never shows a code from the server's record as text, a path or an exception name", async () => {
    notice(ADMIN, staleness("migration_failed", {}, { ...FAILED, code: "OperationalError at C:\\data" }))

    await screen.findByTestId("store-state-migration_failed")

    expect(document.body).not.toHaveTextContent(/OperationalError|C:\\/)
  })

  it("shows a state of its own for an unavailable store, unless the correction warning already says so", async () => {
    notice(REVIEWER, staleness("unavailable", { corrections_available: true }))

    expect(await screen.findByTestId("store-state-unavailable")).toHaveTextContent("cannot be used right now")
  })

  it("does not repeat the unavailable-corrections warning", async () => {
    const api = notice(REVIEWER, staleness("unavailable", { corrections_available: false }))

    await vi.waitFor(() => expect(api.calls.some((call) => call.url.pathname === "/api/extractions/staleness")).toBe(true))

    expect(screen.queryByTestId("store-state-unavailable")).toBeNull()
  })

  it("falls back safely for a state it does not know, and never echoes it", async () => {
    notice(REVIEWER, { ...staleness("something_new_v9"), store: { state: "something_new_v9", version: 9, reason: "x" } })

    const alert = await screen.findByTestId("store-state-unknown")

    expect(alert).toHaveTextContent("does not recognise")
    expect(alert).not.toHaveTextContent("something_new_v9")
  })

  it("shows nothing when the server sends no store object", async () => {
    const api = notice(REVIEWER, staleness(null))

    await vi.waitFor(() => expect(api.calls.some((call) => call.url.pathname === "/api/extractions/staleness")).toBe(true))

    expect(screen.queryByTestId(/store-state/)).toBeNull()
  })

  it("never shows the server's reason, a path or an exception name, for any state", async () => {
    for (const state of ["absent", "older", "migration_pending", "migration_failed", "unavailable", "weird"]) {
      const view = notice(REVIEWER, staleness(state))

      await screen.findByTestId(/store-state/)

      expect(document.body).not.toHaveTextContent(/OperationalError|C:\\|extractions\.sqlite3|must never be shown/)
      view.fetchMock.mockClear()
      document.body.innerHTML = ""
    }
  })

  it("is quiet on a request error, leaving the existing messages to say it", async () => {
    mockApi({ "/api/session": REVIEWER, "/api/extractions/staleness": () => ({ status: 500, body: { detail: "Something went wrong on the server." } }) })
    renderPage(<StoreStateNotice />)

    await vi.waitFor(() => expect(screen.queryByTestId(/store-state/)).toBeNull())
  })
})

describe("which writes the state allows", () => {
  it("allows them for a current or absent store and when nothing is known, and blocks them otherwise", () => {
    const of = (state: string | null) => storeWriteBlock(state === null ? ({ stale: [], corrections_available: true } as never) : (staleness(state) as never))

    expect([of("current"), of("absent"), of(null), storeWriteBlock(undefined)]).toEqual([null, null, null, null])

    for (const state of ["older", "migration_pending", "migration_failed", "unavailable", "never_heard_of_it"]) {
      expect(of(state)).toContain("not ready for changes")
    }
  })
})

// ---------------------------------------------------------------------------
// Write actions in the extraction panel
// ---------------------------------------------------------------------------

describe("extraction panel while the store cannot take writes", () => {
  it.each(["migration_pending", "older", "migration_failed", "unavailable", "brand_new_state"])("disables extracting and proposing for %s, and sends nothing", async (state) => {
    const api = panel(REVIEWER, staleness(state, { corrections_available: state !== "unavailable" }))

    const propose = await screen.findByRole("button", { name: "Propose a correction for Delivered pieces" })
    const extract = screen.getByRole("button", { name: "Extract fields from BOL_SHJ-2026-001.pdf" })

    await vi.waitFor(() => expect(propose).toBeDisabled())

    expect(extract).toBeDisabled()
    expect(propose).toHaveAttribute("title", expect.stringContaining("not ready for changes"))
    expect(posts(api.calls)).toEqual([])
  })

  it("keeps the stored extraction and its values readable while the migration is pending", async () => {
    panel(REVIEWER, staleness("migration_pending"))

    expect(await screen.findByText("Delivered pieces", { selector: "p.font-medium" })).toBeInTheDocument()
    expect(await screen.findByTestId("store-state-migration_pending")).toBeInTheDocument()
  })

  it.each(["current", "absent"])("leaves extracting and proposing enabled for %s", async (state) => {
    panel(REVIEWER, staleness(state))

    const propose = await screen.findByRole("button", { name: "Propose a correction for Delivered pieces" })

    expect(propose).toBeEnabled()
    expect(screen.getByRole("button", { name: "Extract fields from BOL_SHJ-2026-001.pdf" })).toBeEnabled()
  })

  it("leaves them enabled when the server sends no store object (the server still checks every request)", async () => {
    panel(REVIEWER, staleness(null))

    expect(await screen.findByRole("button", { name: "Propose a correction for Delivered pieces" })).toBeEnabled()
  })

  it("keeps the existing pending-correction and unavailable-corrections behaviour", async () => {
    panel(ADMIN, { stale: [{ shipment_key: "SHJ2026001", recorded_run_id: 1, missing_correction_ids: [5], stale_run_ids: [1] }], corrections_available: false, store: store("unavailable") })

    expect(await screen.findByText("Corrections cannot be verified right now.")).toBeInTheDocument()
    expect(await screen.findByText(/A recalculated audit run is waiting to be recorded/)).toBeInTheDocument()
    expect(screen.queryByTestId("store-state-unavailable")).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Aliases
// ---------------------------------------------------------------------------

describe("aliases in the extraction detail", () => {
  const withAliases = (aliases: unknown[], extra: Record<string, unknown> = {}) =>
    panel(REVIEWER, staleness("current"), { "/api/extractions/1": detail({ aliases, aliases_available: true, ...extra }) })

  it("shows each name with the type it was read as, and tells original, same-type and type-conflict apart", async () => {
    withAliases([alias(1, POD, "original"), alias(2, "PROOF_copy.pdf", "same_type"), alias(3, "INVOICE_copy.pdf", "type_conflict", "INVOICE")])

    const list = await screen.findByTestId("aliases")

    expect(within(screen.getByTestId("alias-original")).getByText(POD)).toBeInTheDocument()
    expect(within(screen.getByTestId("alias-original")).getByText("Original name")).toBeInTheDocument()
    expect(within(screen.getByTestId("alias-same_type")).getByText("PROOF_copy.pdf")).toBeInTheDocument()
    expect(within(screen.getByTestId("alias-same_type")).getByText("Same type")).toBeInTheDocument()
    expect(within(screen.getByTestId("alias-type_conflict")).getByText("INVOICE_copy.pdf")).toBeInTheDocument()
    expect(within(screen.getByTestId("alias-type_conflict")).getByText("read as INVOICE")).toBeInTheDocument()
    expect(within(screen.getByTestId("alias-type_conflict")).getByText("Type conflict")).toBeInTheDocument()
    expect(list).toHaveTextContent("not used for any audit calculation and have no corrections")
    expect(list).toHaveTextContent("File names are information only")
  })

  it("does not mention conflicts when there are none", async () => {
    withAliases([alias(1, POD, "original"), alias(2, "PROOF_copy.pdf", "same_type")])

    const list = await screen.findByTestId("aliases")

    expect(list).not.toHaveTextContent("Type-conflicting")
    expect(screen.queryByTestId("alias-type_conflict")).toBeNull()
  })

  it("says the records are not available while the store awaits its migration, never that there are none", async () => {
    panel(REVIEWER, staleness("migration_pending"), { "/api/extractions/1": detail({ aliases: [], aliases_available: false }) })

    const box = await screen.findByTestId("aliases-unavailable")

    expect(box).toHaveTextContent("not available until the extraction store is migrated")
    expect(box).toHaveTextContent("does not mean there are none")
    expect(screen.queryByTestId("aliases-empty")).toBeNull()
    expect(screen.queryByText(/No file names are recorded/)).toBeNull()
  })

  it("says plainly when the store has the records and none are recorded", async () => {
    withAliases([])

    expect(await screen.findByTestId("aliases-empty")).toHaveTextContent("No file names are recorded for this content.")
    expect(screen.queryByTestId("aliases-unavailable")).toBeNull()
  })

  it("shows nothing about names for a server that does not send them", async () => {
    panel(REVIEWER, staleness("current"))

    await screen.findByText("Delivered pieces", { selector: "p.font-medium" })

    expect(screen.queryByTestId(/aliases/)).toBeNull()
  })

  it("renders an unknown relation safely", async () => {
    withAliases([alias(1, POD, "original"), alias(2, "odd.pdf", "from_the_future")])

    expect(await screen.findByText("Unknown relation")).toBeInTheDocument()
  })
})

describe("aliases in the extraction list", () => {
  const listed = (extra: Record<string, unknown>) =>
    panel(REVIEWER, staleness("current"), { "/api/extractions": { extractions: [{ ...SUMMARY, ...extra }], extractor_version: EXTRACTOR } })

  it("shows the count of names and a conflict flag when the API provides them", async () => {
    listed({ alias_count: 3, has_type_conflict: true })

    expect(await screen.findByText("3 file names")).toBeInTheDocument()
    expect(screen.getByText("File name type conflict")).toBeInTheDocument()
  })

  it("shows nothing for one name and no conflict", async () => {
    listed({ alias_count: 1, has_type_conflict: false })

    await screen.findByText("Delivered pieces", { selector: "p.font-medium" })

    expect(screen.queryByText(/file names/)).toBeNull()
    expect(screen.queryByText("File name type conflict")).toBeNull()
  })

  it("infers nothing from null values (the store is awaiting its migration)", async () => {
    listed({ alias_count: null, has_type_conflict: null })

    await screen.findByText("Delivered pieces", { selector: "p.font-medium" })

    expect(screen.queryByText(/file names/)).toBeNull()
    expect(screen.queryByText("File name type conflict")).toBeNull()
  })

  it("infers nothing from absent values", async () => {
    listed({})

    await screen.findByText("Delivered pieces", { selector: "p.font-medium" })

    expect(screen.queryByText(/file names/)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The approval queue
// ---------------------------------------------------------------------------

const PENDING: Correction = {
  id: 5, extraction_id: 1, field_name: "delivered_pieces", previous: { value: 45, state: "EXTRACTED", source: "extracted" }, corrected: 48,
  reason: "The paper POD shows 48 pieces.", actor: "bob", authenticated: true, at: "2026-10-22T10:00:00+00:00", based_on: null, legacy: false,
  status: "pending", decision_id: null, decision: null, decision_note: null, decided_by: null, decided_at: null, filename: POD,
  document_type: "POD", shipment_key: "SHJ2026001", extractor_version: EXTRACTOR,
}

function queue(stale: unknown) {
  const api = mockApi({
    "/api/session": ADMIN, "/api/extractions/pending": { pending: [PENDING] }, "/api/extractions": { extractions: [SUMMARY], extractor_version: EXTRACTOR },
    "/api/extractions/staleness": stale,
  })
  renderPage(<Corrections />)

  return api
}

describe("approval queue while the store cannot take writes", () => {
  it("shows the notice, keeps the proposal readable, and disables approving and rejecting", async () => {
    const api = queue(staleness("migration_pending"))

    expect(await screen.findByTestId("store-state-migration_pending")).toBeInTheDocument()
    expect(await screen.findByText("The paper POD shows 48 pieces.")).toBeInTheDocument()

    const approve = screen.getByRole("button", { name: /Approve/ })

    await vi.waitFor(() => expect(approve).toBeDisabled())

    expect(screen.getByRole("button", { name: /Reject/ })).toBeDisabled()
    expect(approve).toHaveAttribute("title", expect.stringContaining("not ready for changes"))
    expect(posts(api.calls)).toEqual([])
  })

  it("leaves them enabled for a current store, as before", async () => {
    queue(staleness("current"))

    expect(await screen.findByRole("button", { name: /Approve/ })).toBeEnabled()
    expect(screen.getByRole("button", { name: /Reject/ })).toBeEnabled()
    expect(screen.queryByTestId(/store-state/)).toBeNull()
  })

  it("never starts a migration, a recalculation or any request other than the reads", async () => {
    const api = queue(staleness("older"))

    await screen.findByTestId("store-state-older")

    expect(posts(api.calls)).toEqual([])
    expect(api.calls.every((call) => !call.init?.method || call.init.method === "GET")).toBe(true)
    expect(api.calls.some((call) => /migrat|recompute/.test(call.url.pathname))).toBe(false)
  })
})
