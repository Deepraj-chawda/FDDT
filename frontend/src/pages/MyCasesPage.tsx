import * as React from "react"
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  ChevronDownIcon,
  ClockIcon,
  DownloadIcon,
  FileTextIcon,
  PlusIcon,
  SearchIcon,
} from "lucide-react"
import { Link, useNavigate } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"

import { listCases } from "@/api/cases"
import { useAuth } from "@/hooks/useAuth"
import { Nav } from "@/design-system/Nav"
import { CASE_TYPE_LABELS, type CaseListItem } from "@/types/case"

interface MyCaseItem {
  id: string
  caseNumber: string
  filename: string
  type: string
  docCount: number
  status: "Action Required" | "In Progress" | "Awaiting Review" | "Cleared"
  statusTone: "rose" | "blue" | "amber" | "emerald"
  submitted: string
}

function mapApiToMyCase(c: CaseListItem): MyCaseItem {
  let status: MyCaseItem["status"] = "Awaiting Review"
  let statusTone: MyCaseItem["statusTone"] = "amber"

  if (c.status === "auto_approved" || c.status === "approved" || c.status === "closed") {
    status = "Cleared"
    statusTone = "emerald"
  } else if (c.status === "under_investigation") {
    status = "In Progress"
    statusTone = "blue"
  } else if (c.status === "rejected" || c.flag?.flag === "high") {
    status = "Action Required"
    statusTone = "rose"
  }

  const d = new Date(c.created_at)
  const submitted = `${d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })} · ${d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}`

  return {
    id: c.id,
    caseNumber: c.case_number,
    filename: `${c.case_number}.pdf`,
    type: CASE_TYPE_LABELS[c.case_type] || c.case_type,
    docCount: c.document_count,
    status,
    statusTone,
    submitted,
  }
}

export function MyCasesPage() {
  const navigate = useNavigate()
  const { token, user } = useAuth()
  const [search, setSearch] = React.useState("")
  const [statusFilter, setStatusFilter] = React.useState("")

  const { data: apiCases, isLoading } = useQuery({
    queryKey: ["cases", token],
    queryFn: () => listCases(token as string),
    enabled: Boolean(token),
  })

  const allMyCases = React.useMemo(() => {
    if (!apiCases) return []
    const userCases = apiCases.filter(
      (c) => !user?.id || c.submitted_by?.id === user.id || c.submitted_by?.email === user.email
    )
    return userCases.map(mapApiToMyCase)
  }, [apiCases, user])

  const filteredCases = React.useMemo(() => {
    return allMyCases.filter((c) => {
      if (search.trim()) {
        const q = search.toLowerCase()
        if (!c.caseNumber.toLowerCase().includes(q) && !c.filename.toLowerCase().includes(q)) {
          return false
        }
      }
      if (statusFilter && c.status !== statusFilter) return false
      return true
    })
  }, [allMyCases, search, statusFilter])

  const totalSubmitted = allMyCases.length
  const underReviewCount = allMyCases.filter((c) => c.status === "Awaiting Review" || c.status === "In Progress").length
  const clearedCount = allMyCases.filter((c) => c.status === "Cleared").length
  const actionRequiredCount = allMyCases.filter((c) => c.status === "Action Required").length

  return (
    <div className="min-h-screen flex flex-col font-sans bg-[#EDF2FA] text-slate-900 selection:bg-blue-100 selection:text-blue-900">
      <Nav active="my_cases" />

      <main className="flex-1 max-w-7xl w-full mx-auto px-3.5 sm:px-6 lg:px-8 py-5 sm:py-8 space-y-5 sm:space-y-6">
        {/* PageHeader */}
        <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
          <div>
            <span className="text-xs font-extrabold uppercase tracking-wider text-blue-600 block mb-1">
              Submission Tracker
            </span>
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-slate-900">
              My Cases
            </h1>
            <p className="text-xs sm:text-sm text-slate-500 mt-1">
              Track the verification and clearance progress of your submitted document packages.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2 sm:gap-2.5">
            <button
              className="inline-flex items-center gap-1.5 px-3 sm:px-3.5 py-1.5 sm:py-2 rounded-lg border border-slate-300 bg-white text-xs font-semibold text-slate-700 hover:bg-slate-50 shadow-sm transition-colors"
              type="button"
            >
              <DownloadIcon className="w-3.5 h-3.5 text-slate-500" />
              Export CSV
            </button>
            <button
              onClick={() => navigate("/cases/new")}
              className="inline-flex items-center gap-1.5 px-3.5 sm:px-4 py-1.5 sm:py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-xs font-semibold text-white shadow-sm transition-colors active:scale-95"
              type="button"
            >
              <PlusIcon className="w-3.5 h-3.5" />
              Submit new case
            </button>
          </div>
        </div>

        {/* SummaryStats */}
        <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
          <div className="bg-white p-4 sm:p-5 rounded-2xl border border-blue-100/70 shadow-sm flex flex-col justify-between">
            <div className="flex items-center justify-between text-slate-500 mb-3">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Total Submitted
              </span>
              <div className="p-2 rounded-lg bg-blue-50 text-blue-600">
                <FileTextIcon className="w-4 h-4" />
              </div>
            </div>
            <div>
              <div className="text-2xl sm:text-3xl font-bold text-slate-900 leading-none">{totalSubmitted}</div>
            </div>
          </div>

          <div className="bg-white p-4 sm:p-5 rounded-2xl border border-blue-100/70 shadow-sm flex flex-col justify-between">
            <div className="flex items-center justify-between text-slate-500 mb-3">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Under Review
              </span>
              <div className="p-2 rounded-lg bg-amber-50 text-amber-600">
                <ClockIcon className="w-4 h-4" />
              </div>
            </div>
            <div>
              <div className="text-2xl sm:text-3xl font-bold text-slate-900 leading-none">{underReviewCount}</div>
            </div>
          </div>

          <div className="bg-white p-4 sm:p-5 rounded-2xl border border-blue-100/70 shadow-sm flex flex-col justify-between">
            <div className="flex items-center justify-between text-slate-500 mb-3">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Cleared / Verified
              </span>
              <div className="p-2 rounded-lg bg-emerald-50 text-emerald-600">
                <CheckCircle2Icon className="w-4 h-4" />
              </div>
            </div>
            <div>
              <div className="text-2xl sm:text-3xl font-bold text-emerald-600 leading-none">{clearedCount}</div>
            </div>
          </div>

          <div className="bg-white p-4 sm:p-5 rounded-2xl border border-blue-100/70 shadow-sm flex flex-col justify-between">
            <div className="flex items-center justify-between text-slate-500 mb-3">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Action Required
              </span>
              <div className="p-2 rounded-lg bg-rose-50 text-rose-600">
                <AlertTriangleIcon className="w-4 h-4" />
              </div>
            </div>
            <div>
              <div className="text-2xl sm:text-3xl font-bold text-rose-600 leading-none">{actionRequiredCount}</div>
            </div>
          </div>
        </section>

        {/* SearchAndFilters */}
        <section className="bg-white p-3.5 sm:p-5 rounded-2xl border border-slate-200/80 shadow-sm flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-3 sm:gap-3.5">
          <div className="relative w-full lg:w-80 shrink-0">
            <SearchIcon className="w-4 h-4 absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full pl-9 pr-4 py-2 bg-slate-50/60 focus:bg-white text-xs sm:text-[13px] border border-slate-200/90 rounded-full text-slate-800 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition shadow-2xs"
              placeholder="Search by Case ID or filename..."
              type="text"
            />
          </div>

          <div className="flex items-center gap-2.5 sm:gap-3 w-full sm:w-auto">
            <div className="relative flex-1 sm:flex-initial">
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="w-full sm:w-auto appearance-none bg-white border border-slate-200 hover:border-slate-300 text-slate-700 text-xs font-medium pl-3.5 pr-8 py-2 rounded-full focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 cursor-pointer transition shadow-2xs whitespace-nowrap"
              >
                <option value="">Status: All Statuses</option>
                <option value="Cleared">Status: Cleared</option>
                <option value="In Progress">Status: In Progress</option>
                <option value="Awaiting Review">Status: Awaiting Review</option>
                <option value="Action Required">Status: Action Required</option>
              </select>
              <ChevronDownIcon className="w-3.5 h-3.5 absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" />
            </div>

            <button
              onClick={() => {
                setSearch("")
                setStatusFilter("")
              }}
              className="text-xs text-slate-500 hover:text-slate-800 font-medium px-2 py-2 transition shrink-0 cursor-pointer"
              type="button"
            >
              Reset
            </button>
          </div>
        </section>

        {/* CasesDataTable */}
        <section className="bg-white rounded-2xl border border-blue-100/70 shadow-sm overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/50 text-[11px] font-bold tracking-wider uppercase text-slate-400">
                  <th className="py-3.5 px-6" scope="col">Case ID</th>
                  <th className="py-3.5 px-6" scope="col">Type</th>
                  <th className="py-3.5 px-6" scope="col">Documents</th>
                  <th className="py-3.5 px-6" scope="col">Status</th>
                  <th className="py-3.5 px-6" scope="col">Submitted</th>
                  <th className="py-3.5 px-6 text-right" scope="col">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 text-xs font-normal text-slate-700">
                {isLoading && (
                  <tr>
                    <td colSpan={6} className="py-12 text-center text-slate-400 text-sm">
                      Loading your cases...
                    </td>
                  </tr>
                )}
                {!isLoading && filteredCases.length === 0 && (
                  <tr>
                    <td colSpan={6} className="py-12 text-center text-slate-400 text-sm">
                      {allMyCases.length === 0
                        ? "You haven't submitted any cases yet."
                        : "No cases match the selected filters."}
                    </td>
                  </tr>
                )}
                {!isLoading && filteredCases.map((row) => (
                  <tr key={row.id} className="hover:bg-slate-50/80 transition-colors">
                    <td className="py-4 px-6">
                      <Link to={`/cases/${row.id}`} className="font-bold text-slate-900 hover:text-blue-600 transition-colors">
                        {row.caseNumber}
                      </Link>
                      <div className="text-[11px] text-slate-400 mt-0.5">{row.filename}</div>
                    </td>

                    <td className="py-4 px-6">
                      <div className="inline-flex items-center gap-2">
                        <FileTextIcon className="w-3.5 h-3.5 text-slate-400" />
                        <span className="font-medium text-slate-800">{row.type}</span>
                      </div>
                    </td>

                    <td className="py-4 px-6">
                      <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-slate-100 text-slate-600">
                        {row.docCount} doc{row.docCount > 1 ? "s" : ""}
                      </span>
                    </td>

                    <td className="py-4 px-6">
                      {row.statusTone === "rose" && (
                        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold bg-rose-50 text-rose-700 border border-rose-200/50">
                          <span className="w-1.5 h-1.5 rounded-full bg-rose-500"></span>
                          {row.status}
                        </span>
                      )}
                      {row.statusTone === "blue" && (
                        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold bg-blue-50 text-blue-700 border border-blue-200/50">
                          <span className="w-1.5 h-1.5 rounded-full bg-blue-500"></span>
                          {row.status}
                        </span>
                      )}
                      {row.statusTone === "amber" && (
                        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold bg-amber-50 text-amber-700 border border-amber-200/50">
                          <span className="w-1.5 h-1.5 rounded-full bg-amber-500"></span>
                          {row.status}
                        </span>
                      )}
                      {row.statusTone === "emerald" && (
                        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200/50">
                          <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
                          {row.status}
                        </span>
                      )}
                    </td>

                    <td className="py-4 px-6 text-slate-500 whitespace-nowrap">{row.submitted}</td>

                    <td className="py-4 px-6 text-right whitespace-nowrap">
                      <Link
                        to={`/cases/${row.id}`}
                        className="inline-flex items-center text-xs font-semibold text-blue-600 hover:text-blue-800 transition-colors"
                      >
                        View details <span className="ml-1">→</span>
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </main>
    </div>
  )
}
