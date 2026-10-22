import { afterEach, describe, expect, it, vi } from "vitest"
import { ApiError, CLIENT_HEADER, UNAUTHORIZED_EVENT, download, get, post, qs } from "@/lib/api"
import { count, days, interval, money, NA, percent, pieces, rate } from "@/lib/format"
import { mockApi } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

describe("formatting never invents a value", () => {
  it("shows a missing number as n/a, never as zero", () => {
    expect(money(null)).toBe(NA)
    expect(money(undefined)).toBe(NA)
    expect(count(null)).toBe(NA)
    expect(percent(null)).toBe(NA)
    expect(pieces(null)).toBe(NA)
    expect(days(null)).toBe(NA)
  })

  it("keeps a real zero as zero", () => {
    expect(money(0)).toBe("$0.00")
    expect(count(0)).toBe("0")
    expect(days(0)).toBe("on the expected date")
  })

  it("formats money, negatives and currencies", () => {
    expect(money(1550)).toBe("$1,550.00")
    expect(money(-30)).toBe("-$30.00")
    expect(money(12.5, "CAD")).toBe("$12.50 CAD")
    expect(money(12.5, "Not stated")).toBe("$12.50")
  })

  it("states early and late deliveries", () => {
    expect(days(3)).toBe("3 days late")
    expect(days(-1)).toBe("1 day early")
  })

  it("shows a rate with the number it is based on", () => {
    expect(rate({ rate: 0.5, count: 2, n: 4 })).toBe("50% (2 of 4)")
    expect(rate({ rate: null, count: 0, n: 0 })).toBe("n/a (0 of 0)")
    expect(interval({ low: null, high: null })).toBe("")
    expect(interval({ low: 0.237, high: 0.763 })).toBe("24–76%")
  })
})

describe("the API client", () => {
  it("builds query strings and leaves out empty values", () => {
    expect(qs({ search: "a b", status: ["x", "y"], empty: "", none: null, off: false, on: true, n: 0 })).toBe("?search=a+b&status=x&status=y&on=true&n=0")
    expect(qs({})).toBe("")
  })

  it("sends the CSRF header and same-origin credentials on every request", async () => {
    const { calls } = mockApi({ "/api/x": { ok: true } })

    await get("/api/x")
    await post("/api/x", { a: 1 })

    expect(calls.map((call) => (call.init?.headers as Record<string, string>)[CLIENT_HEADER])).toEqual(["web", "web"])
    expect(calls[0].init?.credentials).toBe("same-origin")
    expect(calls[1].init?.method).toBe("POST")
    expect(JSON.parse(String(calls[1].init?.body))).toEqual({ a: 1 })
  })

  it("turns a refusal into an ApiError carrying the server's message", async () => {
    mockApi({ "/api/x": () => ({ status: 422, body: { detail: "Resolving needs at least one evidence reference." } }) })

    await expect(get("/api/x")).rejects.toMatchObject({ name: "ApiError", status: 422, message: "Resolving needs at least one evidence reference." })
  })

  it("announces a 401 so the app can show the sign-in screen", async () => {
    mockApi({ "/api/x": () => ({ status: 401, body: { detail: "Sign in to continue." } }) })
    const heard = vi.fn()
    window.addEventListener(UNAUTHORIZED_EVENT, heard)

    await expect(get("/api/x")).rejects.toBeInstanceOf(ApiError)
    expect(heard).toHaveBeenCalledTimes(1)

    window.removeEventListener(UNAUTHORIZED_EVENT, heard)
  })

  it("reports an unreachable server clearly", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")))

    await expect(get("/api/x")).rejects.toMatchObject({ status: 0, message: expect.stringContaining("could not be reached") })
  })

  it("downloads a report using the file name the server gave", async () => {
    mockApi({ "/api/reports/x.csv": () => ({ text: "a,b", headers: { "Content-Disposition": 'attachment; filename="freight_audit_SHJ.csv"', "Content-Type": "text/csv" } }) })
    URL.createObjectURL = vi.fn(() => "blob:x")
    URL.revokeObjectURL = vi.fn()
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {})

    const name = await download("/api/reports/x.csv", "fallback.csv")

    expect(name).toBe("freight_audit_SHJ.csv")
    expect(click).toHaveBeenCalledTimes(1)
    expect(URL.revokeObjectURL).toHaveBeenCalled()
  })
})
