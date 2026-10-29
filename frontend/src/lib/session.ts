// The signed-in session, in memory only.
//
// The access token (a JWT valid for ~15 minutes) lives in this module and
// nowhere else: never in localStorage/sessionStorage, where any injected
// script could read it. The session itself is kept by the refresh token, an
// httpOnly cookie the server sets on sign-in that page scripts cannot read
// (backend/app/api/auth.py). `refreshSession()` trades that cookie for a new
// access token — on page load, shortly before the token expires, and after a
// 401.
//
// Across tabs: refreshes are serialised with a Web Lock, so two tabs never
// present the same refresh token at once (the server would otherwise take
// the second one for a stolen copy), and signing out in one tab signs out
// every tab (BroadcastChannel).
import { API_BASE_URL, ApiError } from "@/api/client"

interface TokenBody {
  access_token: string
  expires_in: number
}

type Listener = (token: string | null) => void

const REFRESH_LOCK = "fddt-auth-refresh"
const CHANNEL = "fddt-auth"
// Renew this long before the access token expires.
const REFRESH_MARGIN_MS = 60_000

let accessToken: string | null = null
// Whether this page load has had a session at all (see hadSessionThisLoad).
let hadSession = false
let expiresAt = 0
let refreshTimer: ReturnType<typeof setTimeout> | undefined
let inFlight: Promise<string | null> | null = null
const listeners = new Set<Listener>()

const channel: BroadcastChannel | null =
  typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(CHANNEL) : null
channel?.addEventListener("message", (event) => {
  if (event.data === "logout") clearSession({ broadcast: false })
})

// The pre-refresh-token build kept the JWT in localStorage; never leave it there.
try {
  localStorage.removeItem("docauth.token")
} catch {
  // storage unavailable — nothing stored either
}

function notify() {
  for (const listener of listeners) listener(accessToken)
}

export function onSessionChange(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getAccessToken(): string | null {
  return accessToken
}

/** False until a session exists in this page load: a sign-in page reached
 * from a deep link (nobody signed in yet) rather than by signing out. */
export function hadSessionThisLoad(): boolean {
  return hadSession
}

export function setSession({ access_token, expires_in }: TokenBody): string {
  accessToken = access_token
  hadSession = true
  expiresAt = Date.now() + expires_in * 1000
  clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => void refreshSession(), Math.max(expires_in * 1000 - REFRESH_MARGIN_MS, 5_000))
  notify()
  return access_token
}

export function clearSession({ broadcast = true }: { broadcast?: boolean } = {}) {
  const hadSession = accessToken !== null
  accessToken = null
  expiresAt = 0
  clearTimeout(refreshTimer)
  if (broadcast) channel?.postMessage("logout")
  if (hadSession || broadcast) notify()
}

async function requestRefresh(): Promise<string | null> {
  const response = await fetch(`${API_BASE_URL}/auth/refresh`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-Requested-With": "fddt-web" },
  })
  if (response.status === 401 || response.status === 403) return null
  if (!response.ok) throw new ApiError(response.status, response.statusText)
  return setSession((await response.json()) as TokenBody)
}

/** A new access token from the refresh cookie, or null when there is no
 * session any more (then everything signed-in is cleared). One request at a
 * time per tab, and one at a time across tabs. */
export function refreshSession(): Promise<string | null> {
  if (inFlight) return inFlight
  const run = async () => {
    try {
      const token = await (typeof navigator !== "undefined" && navigator.locks
        ? navigator.locks.request(REFRESH_LOCK, requestRefresh)
        : requestRefresh())
      if (token === null) clearSession({ broadcast: false })
      return token
    } catch {
      // Network trouble: keep the session; the next request will try again.
      return accessToken
    } finally {
      inFlight = null
    }
  }
  inFlight = run()
  return inFlight
}

/** The access token to send now, renewed first if it is about to expire. */
export async function currentAccessToken(): Promise<string | null> {
  if (accessToken && Date.now() < expiresAt - 5_000) return accessToken
  if (!accessToken) return null
  return refreshSession()
}

/** Run an authenticated request; on a 401, renew the session once and retry. */
export async function withSession<T>(send: (token: string) => Promise<T>): Promise<T> {
  const token = await currentAccessToken()
  if (!token) throw new ApiError(401, "Your session has ended. Please sign in again.")
  try {
    return await send(token)
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 401) throw err
    const fresh = await refreshSession() // null: session over, already cleared
    if (!fresh || fresh === token) throw err
    return send(fresh)
  }
}
