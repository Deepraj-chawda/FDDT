import { Navigate, Outlet, useLocation } from "react-router-dom"

import { useAuth } from "@/hooks/useAuth"
import { hadSessionThisLoad } from "@/lib/session"

export function ProtectedRoute() {
  const { token, user, isLoadingUser } = useAuth()
  const location = useLocation()

  // isLoadingUser also covers restoring the session from the refresh cookie
  // on page load, before which there is no token yet.
  if (isLoadingUser) {
    return (
      <div className="flex min-h-svh items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    )
  }

  // No session, or it didn't resolve to a user (rejected by the backend:
  // the session is cleared) — sign in. A link opened without a session
  // (e.g. a case link from an email) is returned to after signing in; after
  // a sign-out it is not (LoginPage).
  if (!token || !user) {
    const from = hadSessionThisLoad() ? undefined : location.pathname + location.search
    return <Navigate to="/login" replace state={from ? { from } : undefined} />
  }

  return <Outlet />
}
