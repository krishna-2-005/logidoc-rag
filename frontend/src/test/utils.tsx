import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render } from "@testing-library/react"
import type { ReactElement } from "react"
import { MemoryRouter } from "react-router-dom"
import { vi } from "vitest"
import { ToastProvider } from "@/components/common/toast"
import { TooltipProvider } from "@/components/ui/tooltip"

export type Handler = (url: URL, init?: RequestInit) => { status?: number; body?: unknown; headers?: Record<string, string>; text?: string } | undefined

/** Replaces fetch with routes; any request nobody handles fails the test loudly. */
export function mockApi(handlers: Record<string, Handler | unknown>) {
  const calls: { url: URL; init?: RequestInit }[] = []

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost")
    calls.push({ url, init })

    const handler = handlers[url.pathname]
    if (handler === undefined) throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url.pathname}${url.search}`)

    const result = typeof handler === "function" ? (handler as Handler)(url, init) : { body: handler }
    const status = result?.status ?? 200
    const body = result?.text ?? JSON.stringify(result?.body ?? {})

    return new Response(body, { status, headers: { "Content-Type": "application/json", ...(result?.headers ?? {}) } })
  })

  vi.stubGlobal("fetch", fetchMock)

  return { calls, fetchMock }
}

export function renderPage(ui: ReactElement, route = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } })

  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
        </ToastProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

export const ADMIN_SESSION = {
  mode: "required",
  authenticated: true,
  user: { username: "alice", role: "admin", label: "Administrator" },
  permissions: ["exceptions.view", "exceptions.update", "exceptions.dismiss", "history.record", "reports.download", "ingestion.write", "ingestion.view", "admin.users"],
}

export const REVIEWER_SESSION = {
  mode: "required",
  authenticated: true,
  user: { username: "bob", role: "reviewer", label: "Auditor / reviewer" },
  permissions: ["exceptions.view", "exceptions.update", "reports.download", "ingestion.view", "history.view"],
}

export const SHIPMENT = {
  shipment_id: "SHJ-2026-001",
  shipment_key: "SHJ2026001",
  reconciled: true,
  error: null,
  carrier: "Swift Lines",
  origin: "Chicago, Illinois",
  destination: "Atlanta, Georgia",
  quantity_status: "MISMATCH",
  delivery_status: "DELAYED",
  invoice_status: "MISMATCH",
  final_status: "DISCREPANCIES DETECTED",
  invoice_total: 1550,
  invoice_difference: 120,
  discrepancy_count: 3,
  review_count: 0,
  discrepancy_categories: ["Delivery Date", "Invoice", "Quantity"],
  needs_review: false,
}
