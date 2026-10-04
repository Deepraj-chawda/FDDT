import * as React from "react"
import { ActivityIcon, BarChart3Icon, BuildingIcon, SlidersIcon, UsersIcon } from "lucide-react"
import { Link } from "react-router-dom"

import { Nav } from "@/design-system/Nav"

export type PlatformTab = "companies" | "usage" | "queues" | "users" | "templates"

const TABS: { id: PlatformTab; label: string; href: string; icon: typeof BuildingIcon }[] = [
  { id: "companies", label: "Companies", href: "/platform/companies", icon: BuildingIcon },
  { id: "users", label: "Users", href: "/settings/users", icon: UsersIcon },
  { id: "templates", label: "Rule templates", href: "/platform/rule-templates", icon: SlidersIcon },
  { id: "usage", label: "Billing & usage", href: "/platform/usage", icon: BarChart3Icon },
  { id: "queues", label: "Processing queues", href: "/platform/queues", icon: ActivityIcon },
]

// Frame for the platform-admin screens (cross-company; platform admins only).
export function PlatformShell({
  active,
  title,
  description,
  actions,
  children,
}: {
  active: PlatformTab
  title: string
  description: string
  actions?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="min-h-screen flex flex-col font-sans bg-[#EDF2FA] text-slate-900">
      <Nav active="platform" />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-4 sm:px-6 py-5 sm:py-8">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-5 sm:mb-6">
          <div>
            <div className="text-[11px] font-bold tracking-widest text-purple-600 uppercase mb-1">Platform administration</div>
            <h1 className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">{title}</h1>
            <p className="text-xs sm:text-sm text-slate-500 mt-1 max-w-3xl">{description}</p>
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2 sm:gap-3 shrink-0">{actions}</div>}
        </div>
        <div className="border-b border-slate-200/90 mb-6 -mx-4 px-4 sm:mx-0 sm:px-0">
          <nav className="flex overflow-x-auto no-scrollbar scroll-smooth gap-4 sm:gap-8 pb-px" aria-label="Platform tabs">
            {TABS.map(({ id, label, href, icon: Icon }) => {
              const isActive = id === active
              return (
                <Link
                  key={id}
                  to={href}
                  aria-current={isActive ? "page" : undefined}
                  className={
                    isActive
                      ? "border-b-2 border-purple-600 py-3 sm:py-3.5 px-1 text-xs sm:text-sm font-bold text-purple-700 flex items-center gap-2 shrink-0 whitespace-nowrap"
                      : "border-b-2 border-transparent py-3 sm:py-3.5 px-1 text-xs sm:text-sm font-semibold text-slate-500 hover:text-slate-800 hover:border-slate-300 flex items-center gap-2 shrink-0 whitespace-nowrap transition-colors"
                  }
                >
                  <Icon className="w-4 h-4" />
                  {label}
                </Link>
              )
            })}
          </nav>
        </div>
        {children}
      </main>
    </div>
  )
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)))
  const value = bytes / 1024 ** i
  return `${value >= 100 || i === 0 ? value.toFixed(0) : value.toFixed(value >= 10 ? 1 : 2)} ${units[i]}`
}
