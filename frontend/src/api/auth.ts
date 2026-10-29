import { apiFetch } from "@/api/client"
import type { CurrentUser, TokenResponse } from "@/types/auth"

export interface LoginPayload {
  email: string
  password: string
}

export function login(payload: LoginPayload): Promise<TokenResponse> {
  return apiFetch<TokenResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

export interface ChangePasswordPayload {
  current_password: string
  new_password: string
}

// Change your own password (every role). A forgotten password is reset by a
// platform admin instead (Settings → Users → Reset password). Ends every
// other sign-in of the account; this one continues with the token returned.
export function changePassword(payload: ChangePasswordPayload, token: string): Promise<TokenResponse> {
  return apiFetch<TokenResponse>("/auth/me/password", { method: "POST", body: JSON.stringify(payload), token })
}

// Ends this sign-in (revokes the refresh cookie's session). Never throws.
export function logout(): Promise<void> {
  return apiFetch<void>("/auth/logout", {
    method: "POST",
    headers: { "X-Requested-With": "fddt-web" },
  }).catch(() => undefined)
}

// Ends every sign-in of the account, on every device.
export function logoutEverywhere(token: string): Promise<void> {
  return apiFetch<void>("/auth/logout-all", { method: "POST", token })
}

export function getMe(token: string): Promise<CurrentUser> {
  return apiFetch<CurrentUser>("/auth/me", { token })
}
