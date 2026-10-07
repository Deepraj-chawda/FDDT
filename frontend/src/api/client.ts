// Minimal fetch wrapper. Base URL is env-var driven (SPECIFICATION.md section 4:
// "Use environment variables for ... environment-specific config").
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api"

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
  token?: string | null
}

export async function apiFetch<T>(
  path: string,
  { token, headers, ...options }: RequestOptions = {}
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
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

  // 204 No Content (e.g. changing your password) has no body to parse.
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}
