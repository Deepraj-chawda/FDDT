import { Building2Icon, EyeIcon } from "lucide-react"

import { useActingCompany } from "@/hooks/useActingCompany"

// Platform admins only: choose which company a company-scoped screen shows.
// Renders nothing for company users (they only ever see their own company).
export function CompanyPicker({ note = "Read-only support access. Every view is recorded in the platform audit log (the company does not see it)." }: { note?: string }) {
  const { isPlatformAdmin, companies, companyId, setCompanyId } = useActingCompany()
  if (!isPlatformAdmin) return null
  return (
    <div className="mb-5 flex flex-col gap-2 rounded-xl border border-amber-200 bg-amber-50/70 px-3.5 sm:px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      <label className="flex flex-wrap items-center gap-2 text-xs font-semibold text-amber-900">
        <span className="flex items-center gap-1.5 shrink-0">
          <Building2Icon className="size-4 text-amber-600" />
          Company:
        </span>
        <select
          value={companyId ?? ""}
          onChange={(e) => setCompanyId(e.target.value)}
          aria-label="Active company"
          className="max-w-full sm:max-w-xs truncate rounded-md border border-amber-300 bg-white px-2 py-1 text-xs font-medium text-slate-800 focus:outline-none focus:ring-2 focus:ring-amber-500/20"
        >
          {companies.length === 0 && <option value="">No companies yet</option>}
          {companies.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
              {c.is_active ? "" : " (suspended)"}
            </option>
          ))}
        </select>
      </label>
      <p className="flex items-center gap-1.5 text-[11px] text-amber-800 leading-tight">
        <EyeIcon className="size-3.5 shrink-0" />
        <span>{note}</span>
      </p>
    </div>
  )
}
