/**
 * The only way the interface talks to the server.
 *
 * - Every request carries X-LogiDoc-Client, which the API requires on anything
 *   that changes data (a page of another origin cannot send it without a CORS
 *   preflight the API refuses).
 * - The session lives in an HttpOnly cookie the browser sends by itself: no
 *   token is ever readable from JavaScript, so none is stored here.
 * - Errors become ApiError with the message the API gave. A 401 raises
 *   UNAUTHORIZED_EVENT so the shell can show the sign-in screen at once.
 */

export const CLIENT_HEADER = "X-LogiDoc-Client"
export const UNAUTHORIZED_EVENT = "logidoc:unauthorized"

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

type Params = Record<string, string | number | boolean | null | undefined | (string | number)[]>

/** A query string from the parameters given; empty values and empty lists are left out. */
export function qs(params: Params = {}): string {
  const search = new URLSearchParams()

  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "" || value === false) continue

    if (Array.isArray(value)) {
      value.forEach((item) => search.append(key, String(item)))
    } else {
      search.append(key, String(value))
    }
  }

  const text = search.toString()
  return text ? `?${text}` : ""
}

async function failure(response: Response): Promise<ApiError> {
  let message = `The request failed (${response.status}).`

  try {
    const body = await response.json()
    if (typeof body?.detail === "string") message = body.detail
  } catch {
    // keep the generic message
  }

  if (response.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))

  return new ApiError(response.status, message)
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  let response: Response

  try {
    response = await fetch(path, {
      credentials: "same-origin",
      ...init,
      headers: { [CLIENT_HEADER]: "web", ...(init.headers ?? {}) },
    })
  } catch {
    throw new ApiError(0, "The server could not be reached. Check that the API is running.")
  }

  if (!response.ok) throw await failure(response)
  return response
}

export async function get<T>(path: string, params?: Params): Promise<T> {
  return (await request(`${path}${qs(params)}`)).json() as Promise<T>
}

export async function post<T>(path: string, body?: unknown, params?: Params): Promise<T> {
  const response = await request(`${path}${qs(params)}`, {
    method: "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return response.json() as Promise<T>
}

export async function patch<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  return response.json() as Promise<T>
}

export async function postForm<T>(path: string, form: FormData, params?: Params): Promise<T> {
  return (await request(`${path}${qs(params)}`, { method: "POST", body: form })).json() as Promise<T>
}

/** The file name the server suggested, or the fallback. */
export function filenameOf(response: Response, fallback: string): string {
  const header = response.headers.get("Content-Disposition") ?? ""
  const match = /filename="?([^";]+)"?/i.exec(header)
  return match ? match[1] : fallback
}

/** Downloads a report: fetches it with the session, then saves the bytes as a file. */
export async function download(path: string, fallbackName: string, params?: Params): Promise<string> {
  const response = await request(`${path}${qs(params)}`)
  const blob = await response.blob()
  const name = filenameOf(response, fallbackName)

  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = name
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)

  return name
}
