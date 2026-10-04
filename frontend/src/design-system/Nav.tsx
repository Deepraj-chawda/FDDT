import {
  BuildingIcon,
  ClockIcon,
  FileTextIcon,
  LayoutGridIcon,
  LogOutIcon,
  SettingsIcon,
  ShieldCheckIcon,
  UploadCloudIcon,
} from "lucide-react"
import { Link, useNavigate } from "react-router-dom"

import { useAuth } from "@/hooks/useAuth"
import { cn } from "@/lib/utils"
import { APP_FULL_NAME, APP_NAME } from "@/lib/appInfo"
import { hasRank, type CurrentUser } from "@/types/auth"

export type NavItemId =
  | "dashboard"
  | "review_queue"
  | "cases"
  | "my_cases"
  | "audit_history"
  | "settings"
  | "platform"

// Who sees each item (the routes are separately guarded, and the backend
// enforces the same limits — hiding the link is just a convenience).
const NAV_VISIBLE: Record<NavItemId, (u: CurrentUser | undefined) => boolean> = {
  dashboard: (u) => !u?.is_platform_admin,
  review_queue: () => true,
  cases: () => true,
  my_cases: (u) => !u?.is_platform_admin,
  audit_history: (u) => Boolean(u?.is_platform_admin) || hasRank(u?.role, "reviewer_l1"),
  settings: (u) => Boolean(u?.is_platform_admin) || hasRank(u?.role, "reviewer_l2"),
  platform: (u) => Boolean(u?.is_platform_admin),
}

const NAV_ITEMS: { id: NavItemId; label: string; icon: any; href: string }[] = [
  { id: "dashboard", label: "Dashboard", icon: LayoutGridIcon, href: "/dashboard" },
  { id: "cases", label: "Cases", icon: ShieldCheckIcon, href: "/" },
  { id: "my_cases", label: "My cases", icon: FileTextIcon, href: "/my-cases" },
  { id: "audit_history", label: "Audit history", icon: ClockIcon, href: "/audit-history" },
  { id: "settings", label: "Settings", icon: SettingsIcon, href: "/settings/issuer-registry" },
  { id: "platform", label: "Platform", icon: BuildingIcon, href: "/platform/companies" },
]

function initialsFor(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  const initials = parts
    .slice(0, 2)
    .map((p) => p[0])
    .join("")
    .toUpperCase()
  return initials || "?"
}

export interface NavProps {
  active: NavItemId
  onNewUploadClick?: () => void
}

export function Nav({ active, onNewUploadClick }: NavProps) {
  const navigate = useNavigate()
  const { user, logout } = useAuth()
  const displayName = user?.full_name || user?.email || ""

  return (
    <header className="sticky top-0 z-30 w-full border-b border-slate-200/90 bg-white px-6 py-2.5">
      <div className="mx-auto flex max-w-[1600px] items-center justify-between">
        <div className="flex items-center gap-6">
          {/* Logo mark + wordmark */}
          <Link to="/" className="group flex items-center gap-2.5">
            <img src="/logo.png" alt="" className="size-9 shrink-0 rounded-xl shadow-sm shadow-blue-500/30" />
            <div>
              <div className="text-[15px] font-bold leading-tight tracking-tight text-slate-900">
                {APP_NAME}
              </div>
              <div className="text-[9.5px] font-semibold uppercase tracking-wider text-slate-400">
                {APP_FULL_NAME}
              </div>
            </div>
          </Link>

          {/* Nav links */}
          <nav aria-label="Main navigation" className="hidden items-center gap-1 xl:flex">
            {NAV_ITEMS.filter((item) => NAV_VISIBLE[item.id](user)).map((item) => {
              const isActive = item.id === active
              const Icon = item.icon
              const className = cn(
                "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs transition font-medium",
                isActive
                  ? "border border-blue-200/60 bg-blue-50/90 font-semibold text-blue-700 shadow-xs"
                  : "text-slate-600 hover:bg-slate-100/70 hover:text-slate-900"
              )
              return (
                <Link key={item.id} to={item.href} className={className}>
                  <Icon className={cn("size-3.5", isActive ? "text-blue-600" : "text-slate-400")} />
                  {item.label}
                </Link>
              )
            })}
          </nav>
        </div>

        <div className="flex items-center gap-3">
          {user && (
            <div className="hidden text-right leading-tight lg:block">
              <div className="text-[11px] font-semibold text-slate-700">
                {user.is_platform_admin ? "Platform" : user.company_name}
              </div>
              <div className="text-[10px] text-slate-400">{user.role_label}</div>
            </div>
          )}
          {!user?.is_platform_admin && (
          <button
            type="button"
            onClick={onNewUploadClick ?? (() => navigate("/cases/new"))}
            className="inline-flex items-center gap-2 rounded-lg bg-[#0b1930] hover:bg-[#14233e] px-3.5 py-2 text-xs font-semibold text-white shadow-sm transition"
          >
            <UploadCloudIcon className="size-3.5" />
            New upload
          </button>
          )}

          {user && (
            <div
              title={displayName}
              className="flex size-8 shrink-0 items-center justify-center rounded-full bg-blue-100 text-[11px] font-bold text-blue-700"
            >
              {initialsFor(displayName)}
            </div>
          )}
          <button
            type="button"
            aria-label="Sign out"
            title="Sign out"
            onClick={logout}
            className="flex items-center gap-1.5 rounded-full border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-500 transition hover:border-red-200 hover:bg-red-50 hover:text-red-600"
          >
            <LogOutIcon className="size-3.5" />
            Sign out
          </button>
        </div>
      </div>
    </header>
  )
}
