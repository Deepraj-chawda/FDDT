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
import { Link, Navigate, useNavigate } from "react-router-dom"
import { keepPreviousData, useQuery } from "@tanstack/react-query"

import { getCaseSummary, listCasesPage } from "@/api/cases"
import { useAuth } from "@/hooks/useAuth"
import { Nav } from "@/design-system/Nav"
import { CASE_TYPE_LABELS, type CaseListItem, type CaseStatusGroup } from "@/types/case"

const PAGE_SIZE = 50

type StatusFilter = "Cleared" | "In Progress" | "Awaiting Review" | "Action Required"

// The status filter as GET /cases/page's status groups. "Awaiting Review"
// also covers cases whose automated checks are still running ("Analyzing" rows).
const STATUS_GROUPS: Record<StatusFilter, CaseStatusGroup> = {
  Cleared: "approved",
  "In Progress": "in_progress",
  "Awaiting Review": "pending_review",
  "Action Required": "action_required",
}

interface MyCaseItem {
  id: string
  caseNumber: string
  filename: string
  type: string
  docCount: number
  status: StatusFilter | "Analyzing"
  statusTone: "rose" | "blue" | "amber" | "emerald" | "slate"
  submitted: string
}

// The case's real file name(s): "invoice.pdf", "invoice.pdf + 2 more".
function describeDocuments(c: CaseListItem): string {
  const first = c.document_names?.[0]
  if (!first) return "No documents"
  const more = c.document_count - 1
  return more > 0 ? `${first} + ${more} more` : first
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
  } else if (c.status === "submitted" || c.status === "under_automated_review") {
    // Same word the case queue uses for this state.
    status = "Analyzing"
    statusTone = "slate"
  }

  const d = new Date(c.created_at)
  const submitted = `${d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })} · ${d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}`

  return {
    id: c.id,
    caseNumber: c.case_number,
    filename: describeDocuments(c),
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
  // A platform admin submits nothing (read-only, belongs to no company), so
  // there is no "my cases" for them — the case queue is their home.
  const isPlatformAdmin = Boolean(user?.is_platform_admin)
  const [search, setSearch] = React.useState("")
  const [debouncedSearch, setDebouncedSearch] = React.useState("")
  const [statusFilter, setStatusFilter] = React.useState<StatusFilter | "">("")
  const [page, setPage] = React.useState(0)

  // Don't hit the API on every keystroke.
  React.useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedSearch(search.trim())
      setPage(0)
    }, 300)
    return () => clearTimeout(t)
  }, [search])

  // Only the caller's own submissions; filtered, searched and paged on the server.
  const { data: casePage, isLoading, isFetching } = useQuery({
    queryKey: ["cases", "mine", token, debouncedSearch, statusFilter, page],
    queryFn: () =>
      listCasesPage(token as string, {
        mine: true,
        q: debouncedSearch || undefined,
        status_group: statusFilter ? STATUS_GROUPS[statusFilter] : undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    enabled: Boolean(token) && !isPlatformAdmin,
    placeholderData: keepPreviousData,
  })
  const { data: summary } = useQuery({
    queryKey: ["cases", "summary", "mine", token],
    queryFn: () => getCaseSummary(token as string, { mine: true }),
    enabled: Boolean(token) && !isPlatformAdmin,
  })

  const filteredCases = React.useMemo(() => (casePage?.items ?? []).map(mapApiToMyCase), [casePage])
  const total = casePage?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const totalSubmitted = summary?.total ?? 0
  const clearedCount = summary?.approved ?? 0
  const actionRequiredCount = summary?.action_required ?? 0
  // Awaiting review + in progress: everything not cleared or needing action.
  const underReviewCount = totalSubmitted - clearedCount - actionRequiredCount

  if (isPlatformAdmin) return <Navigate to="/" replace />

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
              placeholder="Search by Case ID..."
              type="text"
            />
          </div>

          <div className="flex items-center gap-2.5 sm:gap-3 w-full sm:w-auto">
            <div className="relative flex-1 sm:flex-initial">
              <select
                value={statusFilter}
                onChange={(e) => {
                  setStatusFilter(e.target.value as StatusFilter | "")
                  setPage(0)
                }}
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
                      {totalSubmitted === 0 && !debouncedSearch && !statusFilter
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
                      <div className="text-[11px] text-slate-400 mt-0.5 max-w-[16rem] truncate" title={row.filename}>{row.filename}</div>
                    </td>

                    <td className="py-4 px-6">
                      <div className="inline-flex items-center gap-2">
                        <FileTextIcon className="w-3.5 h-3.5 text-slate-400" />
                        <span className="font-medium text-slate-800">{row.type}</span>
                      </div>
                    </td>

                    <td className="py-4 px-6">
                      <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-slate-100 text-slate-600">
                        {row.docCount} doc{row.docCount === 1 ? "" : "s"}
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
                      {row.statusTone === "slate" && (
                        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold bg-slate-50 text-slate-600 border border-slate-200">
                          <span className="w-1.5 h-1.5 rounded-full bg-slate-400 animate-pulse"></span>
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
          <div className="flex flex-col sm:flex-row items-center justify-between gap-3 border-t border-slate-100 bg-slate-50/50 px-4 sm:px-6 py-3 text-xs text-slate-500">
            <div className="text-center sm:text-left">
              {total === 0 ? "0 cases" : <>Showing <span className="font-semibold text-slate-800">{page * PAGE_SIZE + 1}–{page * PAGE_SIZE + filteredCases.length}</span> of <span className="font-semibold text-slate-800">{total.toLocaleString()}</span></>}
              {isFetching && !isLoading && <span className="ml-2 text-slate-400">updating…</span>}
            </div>
            <div className="flex items-center gap-2">
              <button type="button" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 font-semibold text-slate-600 hover:bg-slate-50 disabled:opacity-40">
                Previous
              </button>
              <span className="tabular-nums">Page {page + 1} of {pageCount}</span>
              <button type="button" disabled={page + 1 >= pageCount} onClick={() => setPage((p) => p + 1)} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 font-semibold text-slate-600 hover:bg-slate-50 disabled:opacity-40">
                Next
              </button>
            </div>
          </div>
        </section>
      </main>
    </div>
  )
}
