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

export function getMe(token: string): Promise<CurrentUser> {
  return apiFetch<CurrentUser>("/auth/me", { token })
}
