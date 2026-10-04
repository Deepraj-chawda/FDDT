import * as React from "react"
import { useQuery } from "@tanstack/react-query"

import { listCompanies, type Company } from "@/api/platform"
import { useAuth } from "@/hooks/useAuth"

// Which company the current screen works on.
//
// A company user always works on their own company (the backend confines them
// there regardless of what the UI sends). A platform admin belongs to no
// company, so company-scoped screens (case queue, audit history, issuer
// registry, risk rules) need one chosen — that choice lives here, shared by
// every page and remembered per browser. Each platform-admin read of company
// data is audited server-side.

const STORAGE_KEY = "fddt.platform.companyId"

interface ActingCompany {
  isPlatformAdmin: boolean
  /** The company the screen shows — own company, or the platform admin's pick. */
  companyId: string | null
  companyName: string | null
  /** What to send as `company_id`: the pick for platform admins, else null. */
  platformCompanyParam: string | null
  companies: Company[]
  setCompanyId: (id: string) => void
}

const ActingCompanyContext = React.createContext<ActingCompany | null>(null)

function readStored(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

export function ActingCompanyProvider({ children }: { children: React.ReactNode }) {
  const { user, token } = useAuth()
  const isPlatformAdmin = Boolean(user?.is_platform_admin)
  const [picked, setPicked] = React.useState<string | null>(readStored)

  const { data: companies = [] } = useQuery({
    queryKey: ["companies", token],
    queryFn: () => listCompanies(token as string),
    enabled: isPlatformAdmin && Boolean(token),
  })

  const valid = companies.find((c) => c.id === picked) ? picked : (companies.find((c) => c.is_active) ?? companies[0])?.id ?? null

  const setCompanyId = React.useCallback((id: string) => {
    setPicked(id)
    try {
      localStorage.setItem(STORAGE_KEY, id)
    } catch {
      // storage unavailable — the choice just isn't remembered
    }
  }, [])

  const value = React.useMemo<ActingCompany>(() => {
    if (!isPlatformAdmin) {
      return {
        isPlatformAdmin: false,
        companyId: user?.company_id ?? null,
        companyName: user?.company_name ?? null,
        platformCompanyParam: null,
        companies: [],
        setCompanyId,
      }
    }
    return {
      isPlatformAdmin: true,
      companyId: valid,
      companyName: companies.find((c) => c.id === valid)?.name ?? null,
      platformCompanyParam: valid,
      companies,
      setCompanyId,
    }
  }, [isPlatformAdmin, user, valid, companies, setCompanyId])

  return <ActingCompanyContext.Provider value={value}>{children}</ActingCompanyContext.Provider>
}

export function useActingCompany(): ActingCompany {
  const ctx = React.useContext(ActingCompanyContext)
  if (!ctx) throw new Error("useActingCompany must be used within an ActingCompanyProvider")
  return ctx
}
