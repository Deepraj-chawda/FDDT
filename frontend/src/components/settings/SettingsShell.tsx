import * as React from "react"
import { Building2Icon, SlidersIcon, UsersIcon } from "lucide-react"
import { Link } from "react-router-dom"

import { CompanyPicker } from "@/components/CompanyPicker"
import { Nav } from "@/design-system/Nav"
import { useActingCompany } from "@/hooks/useActingCompany"

export type SettingsTab = "issuers" | "risk-rules" | "users"

// Issuer Registry and Risk Rules are per company (Reviewer L2 + platform
// admin); Users is platform-admin only.
const TABS: { id: SettingsTab; label: string; href: string; icon: typeof Building2Icon; platformOnly?: boolean }[] = [
  { id: "issuers", label: "Issuer Registry", href: "/settings/issuer-registry", icon: Building2Icon },
  { id: "risk-rules", label: "Risk Rules", href: "/settings/risk-rules", icon: SlidersIcon },
  { id: "users", label: "Users", href: "/settings/users", icon: UsersIcon, platformOnly: true },
]

// Shared frame for the Settings tabs: page header, tab bar, (for platform
// admins on company tabs) the company picker, body.
export function SettingsShell({
  active,
  title,
  description,
  actions,
  children,
}: {
  active: SettingsTab
  title?: string
  description: string
  actions?: React.ReactNode
  children: React.ReactNode
}) {
  const { isPlatformAdmin, companyName } = useActingCompany()
  const companyTab = active !== "users"
  const heading = title ?? (companyTab ? `${companyName ?? "Company"} settings` : "Platform users")
  return (
    <div className="min-h-screen flex flex-col font-sans bg-[#EDF2FA] text-slate-900 selection:bg-blue-100 selection:text-blue-900">
      <Nav active="settings" />
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-4 sm:px-6 py-5 sm:py-8">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-5 sm:mb-6">
          <div>
            <div className="text-[11px] font-bold tracking-widest text-blue-600 uppercase mb-1">
              {companyTab ? "Company configuration" : "Platform administration"}
            </div>
            <h1 className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">{heading}</h1>
            <p className="text-xs sm:text-sm text-slate-500 mt-1 max-w-2xl">{description}</p>
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2 sm:gap-3 shrink-0">{actions}</div>}
        </div>

        <div className="border-b border-slate-200/90 mb-6 -mx-4 px-4 sm:mx-0 sm:px-0">
          <nav className="flex overflow-x-auto no-scrollbar scroll-smooth gap-4 sm:gap-8 pb-px" aria-label="Settings Tabs">
            {TABS.filter((t) => isPlatformAdmin || !t.platformOnly).map(({ id, label, href, icon: Icon }) => {
              const isActive = id === active
              return (
                <Link
                  key={id}
                  to={href}
                  aria-current={isActive ? "page" : undefined}
                  className={
                    isActive
                      ? "border-b-2 border-blue-600 py-3 sm:py-3.5 px-1 text-xs sm:text-sm font-bold text-blue-600 flex items-center space-x-2 shrink-0 whitespace-nowrap"
                      : "border-b-2 border-transparent py-3 sm:py-3.5 px-1 text-xs sm:text-sm font-semibold text-slate-500 hover:text-slate-800 hover:border-slate-300 flex items-center space-x-2 shrink-0 whitespace-nowrap transition-colors"
                  }
                >
                  <Icon className={isActive ? "w-4 h-4 text-blue-600" : "w-4 h-4 text-slate-400"} />
                  <span>{label}</span>
                </Link>
              )
            })}
          </nav>
        </div>

        {companyTab && <CompanyPicker note="Changes apply to this company only and are recorded in its audit log." />}
        {children}
      </main>
    </div>
  )
}

// Shared inline status line for save results ("Saved" / error).
export function StatusLine({ error, ok }: { error?: string | null; ok?: string | null }) {
  if (error) {
    return (
      <p role="alert" className="rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700 border border-rose-200">
        {error}
      </p>
    )
  }
  if (ok) {
    return (
      <p role="status" className="rounded-md bg-emerald-50 px-3 py-2 text-xs text-emerald-700 border border-emerald-200">
        {ok}
      </p>
    )
  }
  return null
}
