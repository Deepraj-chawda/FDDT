// Minimal fetch wrapper. Base URL is env-var driven (SPECIFICATION.md section 4:
// "Use environment variables for ... environment-specific config").
import { withSession } from "@/lib/session"

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api"

/** True when a request failed before a response arrived, or the response broke
 * off part-way (a dropped connection, a download cut short): `fetch` throws a
 * plain `TypeError` for those, never an `ApiError`. Such a request may well
 * succeed when sent again; an HTTP error (404, 403, 500) is final. */
export function isNetworkError(error: unknown): boolean {
  return !(error instanceof ApiError) && error instanceof TypeError
}

export class ApiError extends Error {
  status: number
  /** Machine-readable reason, when the backend sends `detail: {code, message}`
   * (e.g. upload rejections: "file_too_large", "file_password_protected"). */
  code?: string
  constructor(status: number, message: string, code?: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

/** The user-facing message (and code) from a FastAPI error body, whether
 * `detail` is a plain string or a `{code, message}` object. */
export function parseErrorDetail(body: unknown): { message?: string; code?: string } {
  if (!body || typeof body !== "object" || !("detail" in body)) return {}
  const detail = (body as { detail: unknown }).detail
  if (typeof detail === "string") return { message: detail }
  // FastAPI request-validation errors (422): a list of {loc, msg}. Show the
  // messages rather than the bare status text ("Unprocessable Content").
  if (Array.isArray(detail)) {
    const messages = detail
      .map((d) => {
        if (!d || typeof d !== "object") return null
        const { msg, loc } = d as { msg?: unknown; loc?: unknown }
        if (typeof msg !== "string") return null
        const field = Array.isArray(loc) ? loc.filter((p) => p !== "body").join(".") : ""
        return field ? `${field}: ${msg}` : msg
      })
      .filter((m): m is string => m !== null)
    return messages.length > 0 ? { message: messages.join("; ") } : {}
  }
  if (detail && typeof detail === "object") {
    const { message, code } = detail as { message?: unknown; code?: unknown }
    return {
      message: typeof message === "string" ? message : undefined,
      code: typeof code === "string" ? code : undefined,
    }
  }
  return {}
}

interface RequestOptions extends RequestInit {
  /** Pass the signed-in user's token (from useAuth) for an authenticated
   * call. The live access token is used — renewed first if it is about to
   * expire, and once more after a 401 (lib/session.ts). */
  token?: string | null
}

export async function apiFetch<T>(
  path: string,
  { token, ...options }: RequestOptions = {}
): Promise<T> {
  if (!token) return send<T>(path, null, options)
  return withSession((bearer) => send<T>(path, bearer, options))
}

async function send<T>(path: string, bearer: string | null, { headers, ...options }: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
      ...headers,
    },
  })

  if (!response.ok) {
    let detail = response.statusText
    let code: string | undefined
    try {
      const parsed = parseErrorDetail(await response.json())
      detail = parsed.message ?? detail
      code = parsed.code
    } catch {
      // response had no JSON body; fall back to statusText
    }
    throw new ApiError(response.status, detail, code)
  }

  // 204 No Content (e.g. signing out) has no body to parse.
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}
