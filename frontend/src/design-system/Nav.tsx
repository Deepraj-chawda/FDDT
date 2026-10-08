import * as React from "react"
import {
  BuildingIcon,
  ClockIcon,
  FileTextIcon,
  KeyRoundIcon,
  LayoutGridIcon,
  LogOutIcon,
  MenuIcon,
  SettingsIcon,
  ShieldCheckIcon,
  UploadCloudIcon,
  XIcon,
} from "lucide-react"
import { createPortal } from "react-dom"
import { Link, useLocation, useNavigate } from "react-router-dom"

import { ChangePasswordModal } from "@/components/ChangePasswordModal"
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
  const location = useLocation()
  const { user, logout } = useAuth()
  const [mobileOpen, setMobileOpen] = React.useState(false)
  const [changingPassword, setChangingPassword] = React.useState(false)
  const displayName = user?.full_name || user?.email || ""

  const handleSignOut = React.useCallback(() => {
    logout()
    navigate("/login", { replace: true })
  }, [logout, navigate])

  // Close mobile drawer when route changes
  React.useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  // Close mobile drawer on Escape key
  React.useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMobileOpen(false)
    }
    window.addEventListener("keydown", handleKey)
    return () => window.removeEventListener("keydown", handleKey)
  }, [])

  const visibleNavItems = NAV_ITEMS.filter((item) => NAV_VISIBLE[item.id](user))

  return (
    <header className="sticky top-0 z-40 w-full border-b border-slate-200/90 bg-white/95 backdrop-blur-md px-3 sm:px-6 py-2.5 transition-all">
      <div className="mx-auto flex max-w-[1600px] items-center justify-between gap-2 sm:gap-4">
        {/* Left: Logo + Desktop Links */}
        <div className="flex items-center gap-3 sm:gap-6 min-w-0">
          {/* Logo mark + wordmark */}
          <Link to="/" className="group flex items-center gap-2 sm:gap-2.5 shrink-0">
            <img src="/logo.png" alt="" className="size-8 sm:size-9 shrink-0 rounded-xl shadow-sm shadow-blue-500/30" />
            <div className="min-w-0">
              <div className="text-sm sm:text-[15px] font-bold leading-tight tracking-tight text-slate-900 truncate">
                {APP_NAME}
              </div>
              <div className="hidden sm:block text-[9.5px] font-semibold uppercase tracking-wider text-slate-400 truncate">
                {APP_FULL_NAME}
              </div>
            </div>
          </Link>

          {/* Desktop Nav links (visible on xl screens >= 1280px) */}
          <nav aria-label="Main navigation" className="hidden items-center gap-1 xl:flex">
            {visibleNavItems.map((item) => {
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

        {/* Right: Actions & User Info */}
        <div className="flex items-center gap-1.5 sm:gap-3 shrink-0">
          {user && (
            <div className="hidden text-right leading-tight lg:block">
              <div className="text-[11px] font-semibold text-slate-700 max-w-[160px] truncate">
                {user.is_platform_admin ? "Platform" : user.company_name}
              </div>
              <div className="text-[10px] text-slate-400">{user.role_label}</div>
            </div>
          )}

          {!user?.is_platform_admin && (
            <button
              type="button"
              onClick={onNewUploadClick ?? (() => navigate("/cases/new"))}
              className="inline-flex items-center gap-1.5 sm:gap-2 rounded-lg bg-[#0b1930] hover:bg-[#14233e] px-2.5 sm:px-3.5 py-1.5 sm:py-2 text-xs font-semibold text-white shadow-sm transition active:scale-95"
            >
              <UploadCloudIcon className="size-3.5" />
              <span className="hidden xs:inline sm:inline">New upload</span>
            </button>
          )}

          {user && (
            <div
              title={displayName}
              className="flex size-7 sm:size-8 shrink-0 items-center justify-center rounded-full bg-blue-100 text-[11px] font-bold text-blue-700"
            >
              {initialsFor(displayName)}
            </div>
          )}

          {/* Change your own password (every role) */}
          {user && (
            <button
              type="button"
              aria-label="Change password"
              title="Change password"
              onClick={() => setChangingPassword(true)}
              className="hidden sm:flex size-8 items-center justify-center rounded-full border border-slate-200 text-slate-500 transition hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700"
            >
              <KeyRoundIcon className="size-3.5" />
            </button>
          )}

          {/* Desktop Sign Out */}
          <button
            type="button"
            aria-label="Sign out"
            title="Sign out"
            onClick={handleSignOut}
            className="hidden sm:flex items-center gap-1.5 rounded-full border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-500 transition hover:border-red-200 hover:bg-red-50 hover:text-red-600"
          >
            <LogOutIcon className="size-3.5" />
            <span>Sign out</span>
          </button>

          {/* Mobile / Tablet Hamburger Toggle (visible < xl) */}
          <button
            type="button"
            aria-label={mobileOpen ? "Close navigation menu" : "Open navigation menu"}
            aria-expanded={mobileOpen}
            onClick={() => setMobileOpen((prev) => !prev)}
            className="flex xl:hidden size-8 sm:size-9 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-700 hover:bg-slate-100 transition active:scale-95"
          >
            {mobileOpen ? <XIcon className="size-4" /> : <MenuIcon className="size-4" />}
          </button>
        </div>
      </div>

      {/* Mobile & Tablet Drawer Menu (< xl: 1280px) */}
      {mobileOpen && (
        <div className="xl:hidden mt-2.5 border-t border-slate-200/80 pt-3 pb-2 space-y-3 animate-in slide-in-from-top-2 duration-150">
          {/* User badge on mobile */}
          {user && (
            <div className="flex items-center justify-between px-2 py-2 rounded-xl bg-slate-50 border border-slate-200/60">
              <div className="min-w-0">
                <div className="text-xs font-bold text-slate-900 truncate">
                  {displayName}
                </div>
                <div className="text-[11px] text-slate-500 truncate">
                  {user.is_platform_admin ? "Platform Admin" : `${user.company_name} · ${user.role_label}`}
                </div>
              </div>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => setChangingPassword(true)}
                  className="flex items-center gap-1 text-[11px] font-semibold text-slate-600 hover:text-blue-700 px-2 py-1 rounded-md hover:bg-blue-50 transition"
                >
                  <KeyRoundIcon className="size-3" />
                  Change password
                </button>
                <button
                  type="button"
                  onClick={handleSignOut}
                  className="flex items-center gap-1 text-[11px] font-semibold text-rose-600 hover:text-rose-700 px-2 py-1 rounded-md hover:bg-rose-50 transition"
                >
                  <LogOutIcon className="size-3" />
                  Sign out
                </button>
              </div>
            </div>
          )}

          {/* Mobile Navigation Links */}
          <nav aria-label="Mobile navigation" className="grid grid-cols-1 sm:grid-cols-2 gap-1">
            {visibleNavItems.map((item) => {
              const isActive = item.id === active
              const Icon = item.icon
              return (
                <Link
                  key={item.id}
                  to={item.href}
                  onClick={() => setMobileOpen(false)}
                  className={cn(
                    "flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-xs font-semibold transition",
                    isActive
                      ? "border border-blue-200 bg-blue-50 text-blue-700 shadow-xs"
                      : "text-slate-700 hover:bg-slate-100 hover:text-slate-900"
                  )}
                >
                  <Icon className={cn("size-4", isActive ? "text-blue-600" : "text-slate-400")} />
                  <span>{item.label}</span>
                </Link>
              )
            })}
          </nav>
        </div>
      )}
      {/* Portalled: the header's backdrop blur would otherwise clip a fixed overlay to the header. */}
      {changingPassword &&
        createPortal(<ChangePasswordModal onClose={() => setChangingPassword(false)} />, document.body)}
    </header>
  )
}
