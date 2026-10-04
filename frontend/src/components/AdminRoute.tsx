import { Outlet } from "react-router-dom"

import { Nav } from "@/design-system/Nav"
import { useAuth } from "@/hooks/useAuth"
import { hasRank, isPlatformAdmin, ROLE_LABELS, type CompanyRole } from "@/types/auth"

// Role gate for a group of routes (used inside <ProtectedRoute/>, so the user
// is already loaded). This is a convenience: the backend independently 403s
// every endpoint these pages call, so a user who reaches the URL still can't
// read or change anything.
//  - `minRole`: company roles ranked at least this (see hasRank).
//  - `platformAdmin`: also admit platform admins ("allow"), or ONLY them ("only").
export function RoleRoute({
  minRole,
  platformAdmin = "deny",
  label,
}: {
  minRole?: CompanyRole
  platformAdmin?: "deny" | "allow" | "only"
  label: string
}) {
  const { user } = useAuth()
  const pa = isPlatformAdmin(user?.role)
  const allowed =
    platformAdmin === "only" ? pa : (pa && platformAdmin === "allow") || (!!minRole && hasRank(user?.role, minRole))
  if (user && allowed) return <Outlet />
  return (
    <div className="min-h-screen flex flex-col bg-[#EDF2FA] text-slate-900">
      <Nav active="cases" />
      <main className="mx-auto w-full max-w-xl px-6 py-20 text-center">
        <h1 className="text-lg font-bold text-slate-800">{label} access required</h1>
        <p className="mt-2 text-sm text-slate-500">
          Your account ({user ? ROLE_LABELS[user.role] : "unknown"} role) doesn't have access to this page.
        </p>
      </main>
    </div>
  )
}
