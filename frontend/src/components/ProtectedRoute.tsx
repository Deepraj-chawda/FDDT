import { Navigate, Outlet, useLocation } from "react-router-dom"

import { useAuth } from "@/hooks/useAuth"

export function ProtectedRoute() {
  const { token, user, isLoadingUser } = useAuth()
  const location = useLocation()

  if (!token) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }

  if (isLoadingUser) {
    return (
      <div className="flex min-h-svh items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    )
  }

  // Token didn't resolve to a user (e.g. rejected by the backend) and
  // useAuth already cleared it — fall through to login on the next render.
  if (!user) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }

  return <Outlet />
}
