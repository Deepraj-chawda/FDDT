import * as React from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"

import { getMe, login as loginRequest, logout as logoutRequest, type LoginPayload } from "@/api/auth"
import { clearSession, getAccessToken, onSessionChange, refreshSession, setSession } from "@/lib/session"
import type { CurrentUser } from "@/types/auth"

// The session lives in lib/session.ts (access token in memory, refresh token
// in an httpOnly cookie). `token` here is the access token the session
// started with: it only signals "signed in" to the rest of the app — every
// API call sends the live, auto-renewed token — so it changes on sign-in
// and sign-out, not on every renewal (which would refetch every query).
interface AuthContextValue {
  token: string | null
  user: CurrentUser | undefined
  isLoadingUser: boolean
  login: (payload: LoginPayload) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = React.createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient()
  const [token, setToken] = React.useState<string | null>(getAccessToken)
  // On page load the session is restored from the refresh cookie first.
  const [restoring, setRestoring] = React.useState(() => getAccessToken() === null)

  React.useEffect(() => {
    if (!restoring) return
    let cancelled = false
    refreshSession().then((restored) => {
      if (cancelled) return
      setToken(restored)
      setRestoring(false)
    })
    return () => {
      cancelled = true
    }
  }, [restoring])

  // Signed out (here, in another tab, or the session ended): drop everything.
  React.useEffect(
    () =>
      onSessionChange((current) => {
        if (current === null) {
          setToken(null)
          queryClient.clear()
        } else {
          setToken((previous) => previous ?? current)
        }
      }),
    [queryClient],
  )

  const {
    data: user,
    isLoading: isLoadingUser,
  } = useQuery({
    queryKey: ["me", token],
    queryFn: () => getMe(token as string),
    enabled: Boolean(token),
    retry: false,
  })

  const login = React.useCallback(async (payload: LoginPayload) => {
    const body = await loginRequest(payload)
    queryClient.clear()
    setToken(setSession(body))
  }, [queryClient])

  const logout = React.useCallback(async () => {
    await logoutRequest()
    clearSession()
  }, [])

  const value = React.useMemo(
    () => ({ token, user, isLoadingUser: restoring || (Boolean(token) && isLoadingUser), login, logout }),
    [token, user, restoring, isLoadingUser, login, logout]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = React.useContext(AuthContext)
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider")
  }
  return ctx
}
