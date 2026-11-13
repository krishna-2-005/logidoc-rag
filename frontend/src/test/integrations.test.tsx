import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { Route, Routes } from "react-router-dom"
import { afterEach, describe, expect, it, vi } from "vitest"
import App from "@/App"
import { IntegrationDetail } from "@/pages/IntegrationDetail"
import { Integrations } from "@/pages/Integrations"
import { ScheduledJobs } from "@/pages/ScheduledJobs"
import { DeliveryDetail, WebhookDetail } from "@/pages/WebhookDetail"
import { Webhooks } from "@/pages/Webhooks"
import { REVIEWER_SESSION, mockApi, renderPage } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const ADMIN = {
  mode: "required",
  authenticated: true,
  user: { username: "alice", role: "admin", label: "Administrator" },
  permissions: ["exceptions.view", "carriers.manage", "integrations.manage", "history.view"],
}

const SETTINGS = {
  delivery_enabled: true, problems: [], https_only: true, private_destinations_allowed: false, timeout_seconds: 10, max_attempts: 8,
  rotation_grace_hours: 24,
  event_types: [{ type: "carrier.created", description: "A carrier was registered." }, { type: "exception.created", description: "A new exception." }],
  signature: { signature_header: "LogiDoc-Signature", timestamp_header: "LogiDoc-Timestamp", event_id_header: "LogiDoc-Event-Id", scheme: "v1=hex(HMAC-SHA256(...))", tolerance_seconds: 300 },
}

const STATS = { deliveries: 50, succeeded: 48, failed: 2, waiting: 0, success_rate: 0.96, last_attempt_at: "2026-11-10T10:00:00+00:00", last_success_at: "2026-11-10T10:00:00+00:00", last_failure_at: "2026-11-09T10:00:00+00:00", window_days: 7 }

const ENDPOINT = {
  id: 1, name: "ERP receiver", url: "https://erp.partner-example.net/hooks?token=[redacted]", event_types: ["carrier.created"], enabled: true, archived: false,
  secret_version: 1, secret_rotated_at: "2026-11-01T09:00:00+00:00", previous_secret_expires_at: null, rotation_in_progress: false, created_by: "alice",
  created_at: "2026-11-01T09:00:00+00:00", updated_at: "2026-11-01T09:00:00+00:00", archived_at: null, archived_by: null, stats: STATS,
}

const DELIVERY = {
  id: 9, event_id: "evt_abc", endpoint_id: 1, endpoint_name: "ERP receiver", event_type: "carrier.created", occurred_at: "2026-11-09T10:00:00+00:00",
  status: "failed", attempts: 8, cycle_attempts: 8, next_attempt_at: null, last_attempt_at: "2026-11-09T12:00:00+00:00", last_status_code: 503, last_error: "http_5xx",
  test: false, manual_retries: 0, last_retry_by: null, last_retry_at: null, created_at: "2026-11-09T10:00:00+00:00", updated_at: "2026-11-09T12:00:00+00:00",
  completed_at: "2026-11-09T12:00:00+00:00",
}

const SECRET = "whsec_shown_once_value"

describe("Webhooks", () => {
  it("adds an endpoint and shows its secret once, never in the list", async () => {
    const created: Record<string, unknown>[] = []
    mockApi({
      "/api/integrations/webhooks/settings": SETTINGS,
      "/api/integrations/webhooks": (_url: URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          created.push(JSON.parse(String(init.body)))
          return { status: 201, body: { endpoint: ENDPOINT, secret: SECRET, note: "Store this secret now: it is not shown again." } }
        }
        return { body: { endpoints: [ENDPOINT] } }
      },
    })
    renderPage(<Webhooks />, "/integrations/webhooks")

    const table = await screen.findByRole("table", { name: "Webhook endpoints" })
    expect(within(table).getByText("96% (48 of 50)")).toBeInTheDocument()
    expect(within(table).getByText(ENDPOINT.url)).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: /Add endpoint/ }))
    const form = screen.getByRole("form", { name: "Add a webhook endpoint" })
    await userEvent.type(within(form).getByLabelText("Name"), "AP system")
    await userEvent.clear(within(form).getByLabelText("Destination URL"))
    await userEvent.type(within(form).getByLabelText("Destination URL"), "https://ap.partner-example.net/hooks")
    await userEvent.click(within(form).getByRole("checkbox", { name: /exception.created/ }))
    await userEvent.click(within(form).getByRole("button", { name: "Add endpoint" }))

    expect(await screen.findByLabelText("Signing secret", { selector: "code" })).toHaveTextContent(SECRET)
    expect(created).toEqual([{ name: "AP system", url: "https://ap.partner-example.net/hooks", event_types: ["exception.created"] }])

    await userEvent.click(screen.getByRole("button", { name: "I have stored it" }))
    await waitFor(() => expect(screen.queryByText(SECRET)).not.toBeInTheDocument())
  })

  it("says why delivery is off and offers no endpoint", async () => {
    mockApi({
      "/api/integrations/webhooks/settings": { ...SETTINGS, delivery_enabled: false, problems: ["LOGIDOC_WEBHOOK_SECRET_KEY is not set (at least 32 characters)."] },
      "/api/integrations/webhooks": { endpoints: [] },
    })
    renderPage(<Webhooks />, "/integrations/webhooks")

    expect(await screen.findByText("Webhook delivery is off")).toBeInTheDocument()
    expect(screen.getByText(/LOGIDOC_WEBHOOK_SECRET_KEY is not set/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Add endpoint/ })).toBeDisabled()
  })

  it("lists an endpoint's deliveries and retries a failed one", async () => {
    const retried: string[] = []
    mockApi({
      "/api/integrations/webhooks/settings": SETTINGS,
      "/api/integrations/webhooks/1": { ...ENDPOINT, changes: [{ id: 1, endpoint_id: 1, at: "2026-11-01T09:00:00+00:00", actor: "alice", action: "create", before: null, after: {} }] },
      "/api/integrations/deliveries": { deliveries: [DELIVERY], total: 1, offset: 0, limit: 25 },
      "/api/integrations/deliveries/9/retry": (url: URL) => {
        retried.push(url.pathname)
        return { body: { ...DELIVERY, status: "succeeded", last_status_code: 200, last_error: null, payload_preview: {}, attempt_log: [], retryable: false } }
      },
    })
    renderPage(<Routes><Route path="/integrations/webhooks/:id" element={<WebhookDetail />} /></Routes>, "/integrations/webhooks/1")

    const table = await screen.findByRole("table", { name: "Deliveries" })
    expect(within(table).getByText("Failed")).toBeInTheDocument()
    expect(within(table).getByText("HTTP 503")).toBeInTheDocument()
    expect(screen.getByText(/Version 1, issued/)).toBeInTheDocument()
    expect(document.body.textContent).not.toContain("whsec_")

    await userEvent.click(within(table).getByRole("button", { name: /Retry/ }))
    await waitFor(() => expect(retried).toEqual(["/api/integrations/deliveries/9/retry"]))
    expect(await screen.findByText("Delivery 9 delivered")).toBeInTheDocument()
  })

  it("shows a delivery's attempts and its payload with sensitive values masked", async () => {
    mockApi({
      "/api/integrations/deliveries/9": {
        ...DELIVERY, retryable: true,
        payload_preview: { id: "evt_abc", type: "carrier.created", data: { carrier: { legal_name: "Swift Lines Inc", contact_email: "[redacted]" } } },
        attempt_log: [{ id: 1, attempt: 1, trigger: "schedule", started_at: "2026-11-09T10:00:00+00:00", duration_ms: 120, status_code: 503, outcome: "retry", error: "http_5xx", detail: "The receiver answered 503." }],
      },
    })
    renderPage(<Routes><Route path="/integrations/deliveries/:id" element={<DeliveryDetail />} /></Routes>, "/integrations/deliveries/9")

    const attempts = await screen.findByRole("table", { name: "Delivery attempts" })
    expect(within(attempts).getByText("The receiver answered 503.")).toBeInTheDocument()
    expect(within(attempts).getByText("120 ms")).toBeInTheDocument()
    expect(screen.getByLabelText("Payload preview")).toHaveTextContent('"contact_email": "[redacted]"')
    expect(screen.getByRole("button", { name: /Retry now/ })).toBeInTheDocument()
  })
})

const JOB = {
  name: "webhooks.deliver_pending", description: "Makes the first attempt of every new webhook delivery.", enabled: true, interval_seconds: 15,
  next_run_at: "2026-11-10T10:00:15+00:00", last_started_at: "2026-11-10T10:00:00+00:00", last_finished_at: "2026-11-10T10:00:01+00:00",
  last_success_at: "2026-11-10T10:00:01+00:00", last_failure_at: null, last_error: null, consecutive_failures: 0, max_retries: 3, timeout_seconds: 300,
  running: false, running_here: false, delayed: false, delay_seconds: 0, updated_at: "2026-11-10T10:00:01+00:00",
}

describe("Scheduled jobs", () => {
  it("shows each job's state, runs one on request and shows its history", async () => {
    const started: string[] = []
    mockApi({
      "/api/integrations/jobs": {
        configured: true, running_here: true, webhook_delivery: { enabled: true, problems: [] },
        owner: { since: "2026-11-10T08:00:00+00:00", heartbeat_at: "2026-11-10T10:00:00+00:00", this_process: true, stale: false },
        jobs: [JOB, { ...JOB, name: "integrations.sync", interval_seconds: 60, delayed: true, delay_seconds: 600, consecutive_failures: 2, last_failure_at: "2026-11-10T09:00:00+00:00", last_error: "RuntimeError: unavailable" }],
      },
      "/api/integrations/jobs/integrations.sync/run": (url: URL) => {
        started.push(url.pathname)
        return { status: 202, body: { run_id: 4, job: "integrations.sync", trigger: "manual" } }
      },
      "/api/integrations/jobs/integrations.sync/runs": { runs: [{ id: 4, job: "integrations.sync", trigger: "manual", requested_by: "alice", started_at: "2026-11-10T10:01:00+00:00", finished_at: "2026-11-10T10:01:02+00:00", status: "succeeded", duration_ms: 2000, delay_seconds: 0, summary: { synced: 1 }, error_category: null, error_message: null, ran_here: true }], total: 1, offset: 0, limit: 20 },
    })
    renderPage(<ScheduledJobs />, "/integrations/jobs")

    const table = await screen.findByRole("table", { name: "Scheduled jobs" })
    expect(screen.getByText(/The schedule runs in this server process/)).toBeInTheDocument()
    expect(within(table).getByText("every 15 s")).toBeInTheDocument()
    expect(within(table).getByText("Late by 10 min")).toBeInTheDocument()
    expect(within(table).getByText("2 failed in a row")).toBeInTheDocument()
    expect(within(table).getByText("RuntimeError: unavailable")).toBeInTheDocument()
    expect(within(table).getByRole("button", { name: "Retry integrations.sync" })).toBeInTheDocument()

    await userEvent.click(within(table).getByRole("button", { name: "Run integrations.sync now" }))
    await waitFor(() => expect(started).toEqual(["/api/integrations/jobs/integrations.sync/run"]))
    expect(await screen.findByRole("table", { name: "Runs of integrations.sync" })).toHaveTextContent("manual by alice")
  })

  it("warns when the scheduler stopped checking in", async () => {
    mockApi({
      "/api/integrations/jobs": { configured: true, running_here: false, webhook_delivery: { enabled: true, problems: [] }, owner: { since: "2026-11-09T08:00:00+00:00", heartbeat_at: "2026-11-09T09:00:00+00:00", this_process: false, stale: true }, jobs: [JOB] },
    })
    renderPage(<ScheduledJobs />, "/integrations/jobs")

    expect(await screen.findByText(/has not checked in since/)).toBeInTheDocument()
  })
})

const RUN = {
  id: 3, integration_id: 1, trigger: "schedule", requested_by: null, started_at: "2026-11-10T09:00:00+00:00", finished_at: "2026-11-10T09:00:05+00:00",
  status: "partial", cursor_before: {}, cursor_after: { shipments: { updated_at: "2026-10-01T09:40:00Z", id: "SBX-SHP-0040" } }, fetched: 46, imported: 42,
  updated: 0, unchanged: 0, duplicates: 0, rejected: 4, conflicts: 0, pages: 7, retries: 2, error_category: null, error_message: null,
}

const INTEGRATION = {
  id: 1, name: "Sandbox TMS", provider: "sandbox_tms", enabled: true, settings: { page_size: 10 }, credential_env: "LOGIDOC_INTEGRATION_SANDBOX_TOKEN",
  credential_configured: true, sync_interval_seconds: 3600, next_sync_at: "2026-11-10T10:00:00+00:00", cursor: RUN.cursor_after, connection_status: "connected",
  connection_checked_at: "2026-11-10T09:00:00+00:00", connection_error: null, last_success_at: "2026-11-10T09:00:05+00:00", last_failure_at: null, last_error: null,
  consecutive_failures: 0, running: false, created_by: "alice", created_at: "2026-11-10T08:00:00+00:00", updated_at: "2026-11-10T09:00:05+00:00",
  last_run: RUN, totals: { imported: 42, updated: 0, rejected: 4, runs: 1 }, records: { carriers: 5, shipments: 37 },
}

const PROVIDER = {
  key: "sandbox_tms", label: "Sandbox TMS (synthetic data)", description: "A simulated transport management system.", entities: ["carriers", "shipments"],
  accepts_audit_runs: true, sandbox: true, available: true, unavailable_reason: null, default_credential_env: "LOGIDOC_INTEGRATION_SANDBOX_TOKEN",
}

describe("TMS and ERP integrations", () => {
  it("lists each integration with its connection, syncs and counts, and never a credential value", async () => {
    mockApi({ "/api/integrations/connections": { integrations: [INTEGRATION], providers: [PROVIDER] } })
    renderPage(<Integrations />, "/integrations")

    const table = await screen.findByRole("table", { name: "Integrations" })
    const row = within(table).getByRole("link", { name: "Sandbox TMS" }).closest("tr") as HTMLElement
    expect(within(row).getByText("Connected")).toBeInTheDocument()
    expect(within(row).getByText("Partial")).toBeInTheDocument()
    expect(within(row).getByText("42")).toBeInTheDocument()
    expect(screen.getByText(/No real vendor is connected yet/)).toBeInTheDocument()
    expect(document.body.textContent).not.toContain("sandbox-token-value")
  })

  it("configures the sandbox with a credential variable, never a credential", async () => {
    const posted: Record<string, unknown>[] = []
    mockApi({
      "/api/integrations/connections": (_url: URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          posted.push(JSON.parse(String(init.body)))
          return { status: 201, body: INTEGRATION }
        }
        return { body: { integrations: [], providers: [PROVIDER] } }
      },
    })
    renderPage(<Routes><Route path="/integrations" element={<Integrations />} /><Route path="/integrations/connections/:id" element={<p>Opened integration</p>} /></Routes>, "/integrations")

    await userEvent.click(await screen.findByRole("button", { name: /Configure integration/ }))
    const form = screen.getByRole("form", { name: "Configure an integration" })
    expect(within(form).getByLabelText("Credential variable")).toHaveValue("LOGIDOC_INTEGRATION_SANDBOX_TOKEN")
    await userEvent.clear(within(form).getByLabelText("Rate limit every Nth call"))
    await userEvent.type(within(form).getByLabelText("Rate limit every Nth call"), "3")
    await userEvent.click(within(form).getByRole("button", { name: "Configure" }))

    expect(await screen.findByText("Opened integration")).toBeInTheDocument()
    expect(posted[0]).toMatchObject({ provider: "sandbox_tms", credential_env: "LOGIDOC_INTEGRATION_SANDBOX_TOKEN", sync_interval_seconds: 3600, settings: { page_size: 10, rate_limit_every: 3, timeout_every: 0, simulate_auth_failure: false } })
  })

  it("tests the connection, starts a sync and shows a run's rejected records", async () => {
    const calls: string[] = []
    mockApi({
      "/api/integrations/connections/1": { ...INTEGRATION, changes: [] },
      "/api/integrations/connections/1/runs": { runs: [RUN], total: 1, offset: 0, limit: 20 },
      "/api/integrations/connections/1/test": (url: URL) => {
        calls.push(url.pathname)
        return { body: { ok: false, category: "authentication_failed", detail: "The sandbox TMS refused the credential.", latency_ms: 4 } }
      },
      "/api/integrations/connections/1/sync": (url: URL) => {
        calls.push(url.pathname)
        return { status: 202, body: { run_id: 4 } }
      },
      "/api/integrations/sync-runs/3": { ...RUN, rejections: [{ id: 1, entity: "shipments", external_id: "SBX-SHP-0013", reason: "status 'lost' is not one of booked, dispatched.", at: "2026-11-10T09:00:03+00:00" }] },
    })
    renderPage(<Routes><Route path="/integrations/connections/:id" element={<IntegrationDetail />} /></Routes>, "/integrations/connections/1")

    expect(await screen.findByRole("heading", { name: "Sandbox TMS" })).toBeInTheDocument()
    expect(screen.getByLabelText("Current cursor")).toHaveTextContent("SBX-SHP-0040")

    await userEvent.click(screen.getByRole("button", { name: /Test connection/ }))
    expect(await screen.findByText("authentication_failed: The sandbox TMS refused the credential.")).toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: /Sync now/ }))
    expect(await screen.findByText("Sync #4 started")).toBeInTheDocument()
    expect(calls).toEqual(["/api/integrations/connections/1/test", "/api/integrations/connections/1/sync"])

    const runs = await screen.findByRole("table", { name: "Sync runs" })
    await userEvent.click(within(runs).getByRole("button", { name: "#3" }))
    const rejected = await screen.findByRole("table", { name: "Rejected records" })
    expect(within(rejected).getByText("SBX-SHP-0013")).toBeInTheDocument()
  })
})

const MONITORING = {
  window: { start: "2026-11-07", end: "2026-11-13", days: 7 },
  totals: { deliveries: 3, succeeded: 2, failed: 1, waiting: 0, cancelled: 0, attempts: 4, retry_attempts: 1, success_rate: 0.6667 },
  last_success_at: "2026-11-13T10:00:00+00:00",
  last_failure_at: "2026-11-13T09:00:00+00:00",
  endpoints: [{ endpoint_id: 1, name: "ERP receiver", enabled: true, archived: false, deliveries: 3, succeeded: 2, failed: 1, waiting: 0, last_success_at: "2026-11-13T10:00:00+00:00", last_failure_at: "2026-11-13T09:00:00+00:00" }],
  event_types: [{ event_type: "carrier.created", deliveries: 3, succeeded: 2, failed: 1, waiting: 0 }],
  errors: [{ category: "http_4xx", attempts: 1 }, { category: "http_5xx", attempts: 1 }],
  daily: ["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"].map((day, index) => ({
    day, succeeded: index === 6 ? 2 : 0, failed: index === 6 ? 1 : 0, waiting: 0, attempts: index === 6 ? 4 : 0,
    latency_p50_ms: index === 6 ? 120 : null, latency_p95_ms: index === 6 ? 480 : null,
  })),
}

describe("Webhook monitoring", () => {
  it("shows recorded outcomes with their denominators, failing endpoints and why attempts failed", async () => {
    const asked: URL[] = []
    mockApi({
      "/api/integrations/webhooks/settings": SETTINGS,
      "/api/integrations/webhooks": { endpoints: [ENDPOINT] },
      "/api/integrations/monitoring/webhooks": (url: URL) => {
        asked.push(url)
        return { body: MONITORING }
      },
    })
    renderPage(<Webhooks />, "/integrations/webhooks?tab=monitoring")

    const figures = await screen.findByRole("region", { name: "Delivery figures" })
    expect(within(figures).getByText("67%")).toBeInTheDocument()
    expect(within(figures).getByText("2 delivered of 3 finished")).toBeInTheDocument()
    expect(within(figures).getByText("Of 4 attempts in the period")).toBeInTheDocument()

    const endpoints = screen.getByRole("table", { name: "Deliveries by endpoint" })
    expect(within(endpoints).getByRole("link", { name: "ERP receiver" })).toHaveAttribute("href", "/integrations/webhooks/1")
    expect(screen.getByRole("list", { name: "Failed attempts by error category" })).toHaveTextContent("http 4xx")
    expect(screen.getAllByText("1 of 4", { selector: "span" })).toHaveLength(2) // one attempt in each category

    // The numbers behind the charts are one click away
    await userEvent.click(screen.getAllByText("Show the numbers")[1])
    expect(screen.getByRole("table", { name: "Attempt latency per day" })).toHaveTextContent("480")

    await userEvent.selectOptions(screen.getByLabelText("Period"), "30")
    await userEvent.selectOptions(screen.getByLabelText("Event type"), "carrier.created")
    await waitFor(() => expect(asked.some((url) => url.searchParams.get("days") === "30" && url.searchParams.get("event_type") === "carrier.created")).toBe(true))
  })

  it("says when nothing was delivered instead of drawing empty charts", async () => {
    mockApi({
      "/api/integrations/webhooks/settings": SETTINGS,
      "/api/integrations/webhooks": { endpoints: [] },
      "/api/integrations/monitoring/webhooks": { ...MONITORING, totals: { ...MONITORING.totals, deliveries: 0, succeeded: 0, failed: 0, attempts: 0, retry_attempts: 0, success_rate: null }, endpoints: [], event_types: [], errors: [], last_success_at: null, last_failure_at: null },
    })
    renderPage(<Webhooks />, "/integrations/webhooks?tab=monitoring")

    expect(await screen.findByText("No delivery in this period")).toBeInTheDocument()
    expect(screen.getByText("No delivery finished in the period")).toBeInTheDocument()
    expect(screen.queryByRole("figure")).not.toBeInTheDocument()
  })

  it("filters the delivery history by status, event type and day", async () => {
    const asked: URL[] = []
    mockApi({
      "/api/integrations/webhooks/settings": SETTINGS,
      "/api/integrations/webhooks": { endpoints: [ENDPOINT] },
      "/api/integrations/deliveries": (url: URL) => {
        asked.push(url)
        return { body: { deliveries: [DELIVERY], total: 1, offset: 0, limit: 25 } }
      },
    })
    renderPage(<Webhooks />, "/integrations/webhooks?tab=history")

    const table = await screen.findByRole("table", { name: "Delivery history" })
    expect(within(table).getByRole("link", { name: "#9" })).toHaveAttribute("href", "/integrations/deliveries/9")

    await userEvent.selectOptions(screen.getByLabelText("Status"), "failed")
    await userEvent.selectOptions(screen.getByLabelText("Event type"), "carrier.created")
    await userEvent.type(screen.getByLabelText("Created from"), "2026-11-01")
    await waitFor(() =>
      expect(asked.some((url) => url.searchParams.get("status") === "failed" && url.searchParams.get("event_type") === "carrier.created" && url.searchParams.get("from") === "2026-11-01")).toBe(true),
    )

    await userEvent.type(screen.getByLabelText("Created to"), "2026-10-01")
    expect(await screen.findByText("The start day is after the end day.")).toBeInTheDocument()
  })
})

const DASHBOARD = {
  window: { start: "2026-11-07", end: "2026-11-13", days: 7 },
  totals: { runs: 2, succeeded: 0, partial: 1, failed: 1, interrupted: 0, imported: 42, updated: 0, rejected: 4, retries: 2, success_rate: 0 },
  daily: ["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"].map((day, index) => ({
    day, succeeded: 0, partial: index === 6 ? 1 : 0, failed: index === 6 ? 1 : 0, interrupted: 0, imported: index === 6 ? 42 : 0, updated: 0, rejected: index === 6 ? 4 : 0,
  })),
  errors: [{ category: "authentication_failed", runs: 1 }],
  integrations: [
    {
      id: 1, name: "Sandbox TMS", provider: "sandbox_tms", enabled: true, connection_status: "connected", connection_error: null, last_success_at: "2026-11-13T09:00:05+00:00",
      last_failure_at: null, next_sync_at: "2026-11-13T10:00:00+00:00", last_error: null, consecutive_failures: 0, running: false, records: { carriers: 5, shipments: 37 },
      window: { runs: 1, succeeded: 0, partial: 1, failed: 0, interrupted: 0, imported: 42, updated: 0, rejected: 4, retries: 2, error_categories: {} }, recent_runs: [RUN],
    },
    {
      id: 2, name: "Refused TMS", provider: "sandbox_tms", enabled: true, connection_status: "auth_failed", connection_error: "authentication_failed: refused", last_success_at: null,
      last_failure_at: "2026-11-13T09:10:00+00:00", next_sync_at: "2026-11-13T09:11:00+00:00", last_error: "authentication_failed: refused", consecutive_failures: 1, running: false, records: {},
      window: { runs: 1, succeeded: 0, partial: 0, failed: 1, interrupted: 0, imported: 0, updated: 0, rejected: 0, retries: 0, error_categories: { authentication_failed: 1 } }, recent_runs: [{ ...RUN, id: 4, status: "failed" }],
    },
  ],
}

describe("Integration health", () => {
  it("shows recorded runs, records, retries, error categories and each integration", async () => {
    mockApi({
      "/api/integrations/connections": { integrations: [INTEGRATION], providers: [PROVIDER] },
      "/api/integrations/monitoring/connections": DASHBOARD,
    })
    renderPage(<Integrations />, "/integrations?tab=health")

    const figures = await screen.findByRole("region", { name: "Sync figures" })
    expect(within(figures).getByText("0%")).toBeInTheDocument()
    expect(within(figures).getByText("0 succeeded of 2 finished")).toBeInTheDocument()
    expect(within(figures).getByText("42")).toBeInTheDocument()

    expect(screen.getByRole("list", { name: "Sync runs by error category" })).toHaveTextContent("authentication failed")
    expect(screen.getByText("1 of 2", { selector: "span" })).toBeInTheDocument()

    const table = screen.getByRole("table", { name: "Integration health" })
    const refused = within(table).getByRole("link", { name: "Refused TMS" }).closest("tr") as HTMLElement
    expect(within(refused).getByText("Credential refused")).toBeInTheDocument()
    expect(within(refused).getByText("authentication failed: 1")).toBeInTheDocument()
    expect(within(refused).getByText("1 failed in a row")).toBeInTheDocument()

    await userEvent.click(screen.getAllByText("Show the numbers")[0])
    expect(screen.getByRole("table", { name: "Sync runs per day" })).toHaveTextContent("13 Nov")
  })

  it("shows an empty period honestly", async () => {
    mockApi({
      "/api/integrations/connections": { integrations: [], providers: [PROVIDER] },
      "/api/integrations/monitoring/connections": { ...DASHBOARD, totals: { ...DASHBOARD.totals, runs: 0, partial: 0, failed: 0, imported: 0, rejected: 0, retries: 0, success_rate: null }, errors: [], integrations: [] },
    })
    renderPage(<Integrations />, "/integrations?tab=health")

    expect(await screen.findByText("No sync run in this period")).toBeInTheDocument()
    expect(screen.getByText("No run finished in the period")).toBeInTheDocument()
    expect(screen.getByText("No integration configured")).toBeInTheDocument()
  })
})

describe("Navigation", () => {
  const SHELL = {
    "/api/overview": { empty: true },
    "/api/ready": { status: "ready", checks: {} },
    "/api/carrier-registry/carriers": { carriers: [], total: 0, offset: 0, limit: 25 },
    "/api/carrier-registry/links": { requests: [], total: 0, offset: 0, limit: 50 },
  }

  it("shows the integration screens to administrators only, and the registry to every reader", async () => {
    mockApi({ "/api/session": REVIEWER_SESSION, ...SHELL })
    renderPage(<App />, "/carrier-registry")

    const sidebar = await screen.findAllByRole("navigation", { name: "Main" })
    expect(within(sidebar[0]).getByRole("link", { name: "Carrier Registry" })).toHaveAttribute("aria-current", "page")
    expect(within(sidebar[0]).queryByRole("link", { name: "Webhooks" })).not.toBeInTheDocument()
  })

  it("marks only the most specific entry as the current page", async () => {
    mockApi({ "/api/session": ADMIN, ...SHELL })
    renderPage(<App />, "/carrier-registry/links")

    const [sidebar] = await screen.findAllByRole("navigation", { name: "Main" })
    expect(within(sidebar).getByRole("link", { name: "Name Link Approvals" })).toHaveAttribute("aria-current", "page")
    expect(within(sidebar).getByRole("link", { name: "Carrier Registry" })).not.toHaveAttribute("aria-current")
    expect(within(sidebar).getByRole("link", { name: "Webhooks" })).toBeInTheDocument()
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent("Name Link Approvals")
  })
})
