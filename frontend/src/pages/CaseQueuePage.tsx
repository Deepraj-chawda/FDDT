import * as React from "react"
import {
  BriefcaseIcon,
  ChevronDownIcon,
  ClockIcon,
  CreditCardIcon,
  DownloadIcon,
  ExternalLinkIcon,
  FileCheckIcon,
  FileTextIcon,
  FlameIcon,
  SearchIcon,
  ShieldAlertIcon,
  ShieldCheckIcon,
  UserIcon,
} from "lucide-react"
import { Link, useNavigate } from "react-router-dom"
import { keepPreviousData, useQuery } from "@tanstack/react-query"

import { getCaseSummary, listAllCases, listCasesPage } from "@/api/cases"
import { CompanyPicker } from "@/components/CompanyPicker"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import { Nav } from "@/design-system/Nav"
import { downloadCsv, toCsv } from "@/lib/csv"
import type { UserRole } from "@/types/auth"
import {
  CASE_TYPE_LABELS,
  type CaseListItem,
  type CasePageQuery,
  type CaseStatusGroup,
  type CaseType,
} from "@/types/case"

const PAGE_SIZE = 50

interface QueueCase {
  id: string
  caseNumber: string
  deleted: boolean
  deletedAt: string | null
  createdAt: string
  company: string
  timeAgo: string
  type: string
  typeCategory: "invoice" | "contract" | "bank" | "identity" | "tax"
  docCount: number
  // "Not shared": a submitter's own view — the risk tier is never disclosed
  // to them (the API masks it), so nothing scoring-related is implied.
  flag: "Low risk" | "Medium risk" | "High risk" | "Analyzing" | "Not shared"
  score: number | null
  // Escalated = still-open case assigned to the L2 tier (NOT a status); for
  // Reviewer L2/admin such cases arrive first from the API.
  escalated: boolean
  // Assigned to L2 (open or decided).
  l2: boolean
  // False for a Reviewer L1 on an L2 case: they can open it read-only.
  canAct: boolean
  status: "Analyzing" | "Awaiting Review" | "Approved" | "Rejected"
  reviewer: {
    initials: string
    name: string
    color: string
  } | null
  submittedDate: string
  submittedTime: string
}

// Risk-tier pill: green = low, amber = medium, red = high (same convention
// as components/case/CaseBadges.tsx); "Analyzing" until the case is scored.
const TIER_PILL_STYLES: Record<QueueCase["flag"], { pill: string; dot: string }> = {
  "Low risk": { pill: "bg-emerald-50 text-emerald-700 border-emerald-200/90", dot: "bg-emerald-600" },
  "Medium risk": { pill: "bg-amber-50 text-amber-700 border-amber-200/90", dot: "bg-amber-500" },
  "High risk": { pill: "bg-rose-50 text-rose-700 border-rose-200/90", dot: "bg-rose-600" },
  Analyzing: { pill: "bg-slate-50 text-slate-500 border-slate-200", dot: "bg-slate-400 animate-pulse" },
  "Not shared": { pill: "bg-slate-50 text-slate-500 border-slate-200", dot: "bg-slate-300" },
}

function TierPill({ flag, score }: { flag: QueueCase["flag"]; score: number | null }) {
  const style = TIER_PILL_STYLES[flag]
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold border ${style.pill}`}
      title={flag === "Not shared" ? "Risk scores are visible to the review team only." : undefined}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${style.dot}`}></span>
      {flag}
      {score !== null && <span className="font-mono text-[10px] opacity-70">{score}</span>}
    </span>
  )
}

function formatRelativeTime(date: Date): string {
  const diffMs = Date.now() - date.getTime()
  const diffMinutes = Math.floor(diffMs / 60000)
  if (diffMinutes < 1) return "just now"
  if (diffMinutes < 60) return `${diffMinutes}m ago`
  const diffHours = Math.floor(diffMinutes / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  const diffDays = Math.floor(diffHours / 24)
  return `${diffDays}d ago`
}

function mapApiCaseToQueueCase(c: CaseListItem, riskHidden: boolean): QueueCase {
  let typeCategory: QueueCase["typeCategory"] = "invoice"
  if (c.case_type.includes("bank")) typeCategory = "bank"
  else if (c.case_type.includes("tax") || c.case_type.includes("school")) typeCategory = "tax"
  else if (c.case_type.includes("procurement") || c.case_type.includes("quotation")) typeCategory = "contract"

  const flag: QueueCase["flag"] = riskHidden
    ? "Not shared"
    : c.flag?.flag === "high"
      ? "High risk"
      : c.flag?.flag === "medium"
        ? "Medium risk"
        : c.flag?.flag === "low"
          ? "Low risk"
          : "Analyzing"

  let status: QueueCase["status"] = "Awaiting Review"
  if (c.status === "auto_approved" || c.status === "approved" || c.status === "closed") {
    status = "Approved"
  } else if (c.status === "rejected") {
    status = "Rejected"
  } else if (c.status === "submitted" || c.status === "under_automated_review") {
    status = "Analyzing"
  }
  const l2 = c.assigned_tier === "l2"
  const escalated = l2 && status !== "Approved" && status !== "Rejected"

  const d = new Date(c.created_at)
  const timeAgo = formatRelativeTime(d)
  const submittedDate = d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
  const submittedTime = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })

  const submitterName = c.submitted_by?.full_name || c.submitted_by?.email || "Analyst"
  const initials = submitterName
    .split(" ")
    .map((p) => p[0])
    .slice(0, 2)
    .join("")
    .toUpperCase()

  return {
    id: c.id,
    caseNumber: c.case_number,
    deleted: Boolean(c.deleted_at),
    deletedAt: c.deleted_at ?? null,
    createdAt: c.created_at,
    company: submitterName,
    timeAgo,
    type: CASE_TYPE_LABELS[c.case_type] || c.case_type,
    typeCategory,
    docCount: c.document_count,
    flag,
    score: c.flag?.score ?? null,
    escalated,
    l2,
    canAct: c.can_act,
    status,
    reviewer: {
      initials: initials || "AN",
      name: submitterName,
      color: "bg-blue-100 text-blue-700",
    },
    submittedDate,
    submittedTime,
  }
}

// Queue tabs per role. A Reviewer L1's default is what they can act on, with
// L2-escalated cases one tab away (read-only). A Reviewer L2's default is every
// case, with open escalations sorted to the top by the API, plus a tab for just
// the L2 queue. Submitters and admins get the single list.
// "l2": every case assigned to L2 (open or decided) — a Reviewer L1 has no
// "All cases" tab, so this is where they follow their escalations' outcome.
// "l2_open": the Reviewer L2's work queue — open escalations only, the same
// cases the "escalated to L2" KPI counts.
type QueueView = "all" | "actionable" | "l2" | "l2_open"

const QUEUE_TABS: Partial<Record<UserRole, { view: QueueView; label: string }[]>> = {
  reviewer_l1: [
    { view: "actionable", label: "My queue" },
    { view: "l2", label: "Escalated to L2 · view only" },
  ],
  reviewer_l2: [
    { view: "all", label: "All cases" },
    { view: "l2_open", label: "Escalated queue (L2)" },
  ],
}

// The tab as GET /cases/page filters.
function viewQuery(view: QueueView): CasePageQuery {
  if (view === "actionable") return { actionable: true }
  if (view === "l2") return { assigned_tier: "l2" }
  if (view === "l2_open") return { assigned_tier: "l2", status_group: "open" }
  return {}
}

export function CaseQueuePage() {
  const navigate = useNavigate()
  const { token, user } = useAuth()
  const acting = useActingCompany()
  // Submitters never learn risk tiers (the API masks them): no risk column
  // values, risk filter or high-risk counts that would read as "not scored".
  const riskHidden = user?.role === "user"
  const tabs = (user && QUEUE_TABS[user.role]) || []
  const [view, setView] = React.useState<QueueView | null>(null)
  const activeView: QueueView = view ?? tabs[0]?.view ?? "all"
  const [searchQuery, setSearchQuery] = React.useState("")
  const [debouncedSearch, setDebouncedSearch] = React.useState("")
  // "Deleted" (platform admins): only the cases a platform admin deleted;
  // every other status filter is about live cases. "Escalated" is a tier
  // filter, not a status (see QueueCase.escalated).
  const [statusFilter, setStatusFilter] = React.useState<CaseStatusGroup | "">("")
  const [typeFilter, setTypeFilter] = React.useState<CaseType | "">("")
  const [flagFilter, setFlagFilter] = React.useState<CasePageQuery["risk"] | "">("")
  const [page, setPage] = React.useState(0)
  const [exporting, setExporting] = React.useState(false)

  // Don't hit the API on every keystroke.
  React.useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedSearch(searchQuery.trim())
      setPage(0)
    }, 300)
    return () => clearTimeout(t)
  }, [searchQuery])

  // Filtering, search and paging all happen on the server.
  const tabQuery = viewQuery(activeView)
  const query: CasePageQuery = {
    ...tabQuery,
    q: debouncedSearch || undefined,
    // A status filter narrows within the tab; without one, the tab's own scope applies.
    status_group: statusFilter || tabQuery.status_group,
    case_type: typeFilter || undefined,
    risk: flagFilter || undefined,
  }
  // A platform admin's queue is one company at a time (never a mixed list).
  const enabled = Boolean(token && acting.companyId)

  const { data: casePage, isLoading, isFetching } = useQuery({
    queryKey: ["cases", "page", token, acting.companyId, query, page],
    queryFn: () =>
      listCasesPage(token as string, { ...query, limit: PAGE_SIZE, offset: page * PAGE_SIZE }, acting.platformCompanyParam),
    enabled,
    placeholderData: keepPreviousData,
  })
  // The metric cards and tab counts: live cases, whatever the filters.
  const { data: summary } = useQuery({
    queryKey: ["cases", "summary", token, acting.companyId],
    queryFn: () => getCaseSummary(token as string, {}, acting.platformCompanyParam),
    enabled,
  })

  const pageCases = React.useMemo(
    () => (casePage?.items ?? []).map((c) => mapApiCaseToQueueCase(c, riskHidden)),
    [casePage, riskHidden],
  )
  const total = casePage?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const filtered = Boolean(debouncedSearch || statusFilter || typeFilter || flagFilter)

  const activeCasesCount = summary?.total ?? 0
  const highRiskCount = summary?.high_risk_open ?? 0
  const escalatedCount = summary?.escalated ?? 0
  const awaitingReviewCount = summary?.awaiting_review ?? 0
  const analyzingCount = summary?.analyzing ?? 0
  const clearedCount = summary?.approved ?? 0
  const autoClearedRate = activeCasesCount > 0 ? Math.round((clearedCount / activeCasesCount) * 1000) / 10 : null
  const tabCount = (v: QueueView) =>
    v === "actionable"
      ? summary?.actionable
      : v === "l2"
        ? summary?.l2
        : v === "l2_open"
          ? summary?.escalated
          : summary?.total

  // Every case matching the current tab, search and filters (all pages).
  const exportCsv = async () => {
    setExporting(true)
    try {
      const all = (await listAllCases(token as string, query, acting.platformCompanyParam)).map((c) => mapApiCaseToQueueCase(c, riskHidden))
      const isAdmin = acting.isPlatformAdmin
      const header = [
        "Case ID", "Type", "Documents", "Risk", "Risk score", "Status", "Escalated to L2",
        "Submitted by", "Submitted at (UTC)", ...(isAdmin ? ["Deleted at (UTC)"] : []),
      ]
      const rows = all.map((c) => [
        c.caseNumber, c.type, c.docCount, c.flag, c.score, c.status, c.escalated ? "Yes" : "No",
        c.company, c.createdAt, ...(isAdmin ? [c.deletedAt ?? ""] : []),
      ])
      const company = (acting.companyName ?? "cases").replace(/[^\w-]+/g, "-").replace(/^-|-$/g, "") || "cases"
      downloadCsv(`${company}-cases-${new Date().toISOString().slice(0, 10)}.csv`, toCsv(header, rows))
    } finally {
      setExporting(false)
    }
  }

  const resetFilters = () => {
    setSearchQuery("")
    setDebouncedSearch("")
    setStatusFilter("")
    setTypeFilter("")
    setFlagFilter("")
    setPage(0)
  }

  const renderTypeIcon = (category: QueueCase["typeCategory"]) => {
    switch (category) {
      case "invoice":
      case "contract":
        return <FileTextIcon className="w-3.5 h-3.5 text-slate-400" />
      case "bank":
        return <CreditCardIcon className="w-3.5 h-3.5 text-slate-400" />
      case "identity":
        return <UserIcon className="w-3.5 h-3.5 text-slate-400" />
      case "tax":
        return <FileCheckIcon className="w-3.5 h-3.5 text-slate-400" />
    }
  }

  return (
    <div className="min-h-screen flex flex-col font-sans bg-[#EDF2FA] text-slate-900 selection:bg-blue-100 selection:text-blue-900">
      {/* Shared Global Navigation */}
      <Nav active="cases" />

      {/* Main Content */}
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-3.5 sm:px-6 py-5 sm:py-7">
        <CompanyPicker />
        {/* Page Header Section */}
        <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4 mb-5 sm:mb-6">
          <div>
            <div className="text-[11px] font-bold tracking-widest text-blue-600 uppercase mb-1">
              CASE INVESTIGATIONS
            </div>
            <h1 className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">
              Active Case Queue
            </h1>
            <p className="text-xs sm:text-sm text-slate-500 mt-1 max-w-2xl">
              Prioritized triage for forensic validation, anomaly flags, and multi-document cross-consistency checks.
            </p>
          </div>

          {/* Quick Metrics & Export */}
          <div className="flex flex-wrap items-center gap-2 sm:gap-2.5">
            <div className="inline-flex items-center gap-2 bg-white px-3 sm:px-3.5 py-1.5 sm:py-2 rounded-xl border border-slate-200/80 shadow-xs text-xs font-medium text-slate-700">
              <span className="text-slate-400 font-normal">Active cases:</span>
              <span className="font-bold text-slate-900">{activeCasesCount}</span>
              {!riskHidden && (
                <>
                  <span className="w-1.5 h-1.5 rounded-full bg-rose-500"></span>
                  <span className="text-rose-600 font-semibold">{highRiskCount} High risk</span>
                </>
              )}
            </div>
            <button
              className="inline-flex items-center gap-1.5 bg-white hover:bg-slate-50 text-slate-700 px-3 sm:px-3.5 py-1.5 sm:py-2 rounded-xl border border-slate-200/80 shadow-xs text-xs font-semibold transition active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
              type="button"
              onClick={() => void exportCsv()}
              disabled={total === 0 || exporting}
              title={total === 0 ? "No cases to export" : `Export the ${total} matching case(s)`}
            >
              <DownloadIcon className="w-3.5 h-3.5 text-slate-400" />
              <span>{exporting ? "Exporting…" : "Export CSV"}</span>
            </button>
          </div>
        </div>

        {/* 4 Top Metric Cards */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4 mb-5 sm:mb-6">
          {/* Card 1: Active cases */}
          <div className="bg-white rounded-2xl border border-slate-200/80 p-4 sm:p-5 custom-shadow-card flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Active cases</span>
              <div className="w-8 h-8 rounded-lg bg-blue-50 border border-blue-100 flex items-center justify-center text-blue-600">
                <BriefcaseIcon className="w-4 h-4" />
              </div>
            </div>
            <div className="mt-3">
              <div className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">{activeCasesCount}</div>
              <div className="text-xs text-slate-500 mt-1 flex items-center gap-1.5">
                <span className="font-medium text-slate-600">{awaitingReviewCount}</span> awaiting review
              </div>
            </div>
          </div>

          {/* Card 2: High-risk flagged (a submitter: still being analyzed — tiers aren't shared with them) */}
          {riskHidden ? (
          <div className="bg-white rounded-2xl border border-slate-200/80 p-4 sm:p-5 custom-shadow-card flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Analyzing</span>
              <div className="w-8 h-8 rounded-lg bg-slate-50 border border-slate-200 flex items-center justify-center text-slate-600">
                <ClockIcon className="w-4 h-4" />
              </div>
            </div>
            <div className="mt-3">
              <div className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">{analyzingCount}</div>
              <div className="text-xs text-slate-500 font-medium mt-1 flex items-center gap-1">
                <span>Automated checks still running</span>
              </div>
            </div>
          </div>
          ) : (
          <div className="bg-white rounded-2xl border border-slate-200/80 p-4 sm:p-5 custom-shadow-card flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">High-risk flagged</span>
              <div className="w-8 h-8 rounded-lg bg-rose-50 border border-rose-100 flex items-center justify-center text-rose-600">
                <ShieldAlertIcon className="w-4 h-4" />
              </div>
            </div>
            <div className="mt-3">
              <div className="text-2xl sm:text-3xl font-extrabold text-rose-600 tracking-tight">{highRiskCount}</div>
              <div className="text-xs text-rose-600 font-medium mt-1 flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-rose-500"></span>
                <span>{escalatedCount} escalated to L2</span>
              </div>
            </div>
          </div>
          )}

          {/* Card 3: Awaiting review */}
          <div className="bg-white rounded-2xl border border-slate-200/80 p-4 sm:p-5 custom-shadow-card flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Awaiting review</span>
              <div className="w-8 h-8 rounded-lg bg-slate-50 border border-slate-200 flex items-center justify-center text-slate-600">
                <ClockIcon className="w-4 h-4" />
              </div>
            </div>
            <div className="mt-3">
              <div className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">{awaitingReviewCount}</div>
              <div className="text-xs text-slate-500 font-medium mt-1 flex items-center gap-1">
                <span>Not yet actioned by a reviewer</span>
              </div>
            </div>
          </div>

          {/* Card 4: Cleared rate */}
          <div className="bg-white rounded-2xl border border-slate-200/80 p-4 sm:p-5 custom-shadow-card flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Approved rate</span>
              <div className="w-8 h-8 rounded-lg bg-emerald-50 border border-emerald-100 flex items-center justify-center text-emerald-600">
                <ShieldCheckIcon className="w-4 h-4" />
              </div>
            </div>
            <div className="mt-3">
              <div className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">
                {autoClearedRate === null ? "—" : `${autoClearedRate}%`}
              </div>
              <div className="text-xs text-slate-500 font-medium mt-1 flex items-center gap-1">
                <span>{clearedCount} of {activeCasesCount} cases</span>
              </div>
            </div>
          </div>
        </div>

        {/* Table Container Card */}
        <div className="bg-white rounded-2xl sm:rounded-3xl border border-slate-200/80 custom-shadow-card overflow-hidden">
          {tabs.length > 0 && (
            <div role="tablist" aria-label="Queue view" className="flex gap-1 border-b border-slate-100 px-3 pt-2 sm:px-5 overflow-x-auto no-scrollbar scroll-smooth">
              {tabs.map((t) => {
                const count = tabCount(t.view)
                const selected = activeView === t.view
                return (
                  <button
                    key={t.view}
                    type="button"
                    role="tab"
                    aria-selected={selected}
                    onClick={() => {
                      setView(t.view)
                      setPage(0)
                    }}
                    className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-semibold shrink-0 whitespace-nowrap transition ${
                      selected ? "border-blue-600 text-blue-700" : "border-transparent text-slate-500 hover:text-slate-800"
                    }`}
                  >
                    {t.view === "l2" && <FlameIcon className="size-3.5 text-rose-500" />}
                    {t.label}
                    <span className="rounded-full bg-slate-100 px-1.5 py-px text-[10px] font-bold text-slate-600">{count ?? "…"}</span>
                  </button>
                )
              })}
            </div>
          )}
          {/* Filters Bar */}
          <div className="p-3.5 sm:p-5 border-b border-slate-100 bg-white flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-3 sm:gap-3.5">
            {/* Search Field */}
            <div className="relative w-full lg:w-80 shrink-0">
              <div className="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none">
                <SearchIcon className="h-4 w-4 text-slate-400" />
              </div>
              <input
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="block w-full pl-9 pr-4 py-2 text-xs sm:text-[13px] bg-slate-50/60 border border-slate-200/90 rounded-full placeholder-slate-400 text-slate-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition shadow-2xs"
                placeholder="Filter cases by ID or issuer..."
                type="text"
              />
            </div>

            {/* Filter Dropdowns Group */}
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:flex lg:items-center gap-2 sm:gap-2.5 lg:gap-3">
              {/* Status Dropdown */}
              <div className="relative w-full sm:w-auto">
                <select
                  value={statusFilter}
                  onChange={(e) => {
                    setStatusFilter(e.target.value as CaseStatusGroup | "")
                    setPage(0)
                  }}
                  aria-label="Filter by status"
                  className="w-full sm:w-auto appearance-none bg-white border border-slate-200 hover:border-slate-300 text-slate-700 text-xs font-medium pl-3.5 pr-8 py-2 rounded-full focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 cursor-pointer transition shadow-2xs whitespace-nowrap"
                >
                  <option value="">Status: All Statuses</option>
                  <option value="analyzing">Status: Analyzing</option>
                  <option value="awaiting_review">Status: Awaiting Review</option>
                  <option value="escalated">Status: Escalated (L2)</option>
                  <option value="approved">Status: Approved</option>
                  <option value="rejected">Status: Rejected</option>
                  {acting.isPlatformAdmin && <option value="deleted">Status: Deleted</option>}
                </select>
                <ChevronDownIcon className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 size-3.5 text-slate-400" />
              </div>

              {/* Type Dropdown */}
              <div className="relative w-full sm:w-auto">
                <select
                  value={typeFilter}
                  onChange={(e) => {
                    setTypeFilter(e.target.value as CaseType | "")
                    setPage(0)
                  }}
                  aria-label="Filter by case type"
                  className="w-full sm:w-auto appearance-none bg-white border border-slate-200 hover:border-slate-300 text-slate-700 text-xs font-medium pl-3.5 pr-8 py-2 rounded-full focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 cursor-pointer transition shadow-2xs whitespace-nowrap"
                >
                  <option value="">Type: All Types</option>
                  {Object.entries(CASE_TYPE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>Type: {label}</option>
                  ))}
                </select>
                <ChevronDownIcon className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 size-3.5 text-slate-400" />
              </div>

              {/* Risk / Flag Dropdown (not for submitters: every case would match "Analyzing") */}
              {!riskHidden && (
              <div className="relative w-full col-span-2 sm:col-span-1 sm:w-auto">
                <select
                  value={flagFilter}
                  onChange={(e) => {
                    setFlagFilter(e.target.value as CasePageQuery["risk"] | "")
                    setPage(0)
                  }}
                  aria-label="Filter by risk tier"
                  className="w-full sm:w-auto appearance-none bg-white border border-slate-200 hover:border-slate-300 text-slate-700 text-xs font-medium pl-3.5 pr-8 py-2 rounded-full focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 cursor-pointer transition shadow-2xs whitespace-nowrap"
                >
                  <option value="">Risk: All Tiers</option>
                  <option value="high">Risk: High</option>
                  <option value="medium">Risk: Medium</option>
                  <option value="low">Risk: Low</option>
                  <option value="pending">Risk: Analyzing</option>
                </select>
                <ChevronDownIcon className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 size-3.5 text-slate-400" />
              </div>
              )}

              {/* Reset Action */}
              <button
                onClick={resetFilters}
                className="col-span-2 sm:col-span-3 lg:col-span-1 text-xs font-medium text-slate-500 hover:text-slate-800 px-2 py-2 text-center rounded-full lg:rounded-none transition cursor-pointer shrink-0"
                type="button"
              >
                Reset
              </button>
            </div>
          </div>

          {/* Table Content */}
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-slate-700 text-xs">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/60 text-[11px] font-bold text-slate-500 uppercase tracking-wider">
                  <th className="py-3.5 pl-6 pr-4" scope="col">Case ID</th>
                  <th className="py-3.5 px-4" scope="col">Type</th>
                  <th className="py-3.5 px-4" scope="col">Documents</th>
                  <th className="py-3.5 px-4" scope="col">Flag</th>
                  <th className="py-3.5 px-4" scope="col">Status</th>
                  <th className="py-3.5 px-4" scope="col">Submitted by</th>
                  <th className="py-3.5 px-4" scope="col">Submitted</th>
                  <th className="py-3.5 pl-4 pr-6 text-right" scope="col">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 font-normal">
                {isLoading ? (
                  <tr>
                    <td colSpan={8} className="py-12 text-center text-slate-400 text-sm">
                      Loading cases...
                    </td>
                  </tr>
                ) : pageCases.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="py-12 text-center text-slate-400 text-sm">
                      {!filtered && activeView === "l2"
                        ? "No cases have been escalated to L2."
                        : !filtered && summary?.total === 0
                          ? "No cases have been submitted yet."
                          : "No cases match the selected filters."}
                    </td>
                  </tr>
                ) : (
                  pageCases.map((row) => (
                    <tr
                      key={row.id}
                      onClick={() => navigate(`/cases/${row.id}`)}
                      className={`transition-colors group cursor-pointer ${
                        row.escalated
                          ? "bg-rose-50/40 hover:bg-rose-50/70 border-l-[3px] border-l-rose-500"
                          : "hover:bg-slate-50/80"
                      }`}
                    >
                      {/* Case ID */}
                      <td className="py-4 pl-6 pr-4 whitespace-nowrap">
                        <div className="flex flex-col">
                          <span className="font-bold text-slate-900 group-hover:text-blue-600 transition flex items-center gap-1.5 text-xs">
                            {row.caseNumber}
                            {row.deleted && (
                              <span className="inline-flex items-center rounded-full border border-slate-300 bg-slate-100 px-1.5 py-px text-[10px] font-bold uppercase tracking-wide text-slate-600">
                                Deleted
                              </span>
                            )}
                            {row.escalated && (
                              <span className="inline-flex items-center gap-1 rounded-full border border-rose-200 bg-rose-100 px-1.5 py-px text-[10px] font-bold uppercase tracking-wide text-rose-700">
                                <FlameIcon className="size-2.5" />
                                Escalated · L2
                              </span>
                            )}
                            {!row.canAct && (
                              <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-1.5 py-px text-[10px] font-bold uppercase tracking-wide text-slate-500">
                                View only
                              </span>
                            )}
                            <ExternalLinkIcon className="w-3 h-3 text-slate-400 group-hover:text-blue-600 transition opacity-0 group-hover:opacity-100" />
                          </span>
                          <span className="text-[11px] text-slate-400 mt-0.5">
                            {row.company} • {row.timeAgo}
                          </span>
                        </div>
                      </td>

                      {/* Type */}
                      <td className="py-4 px-4 whitespace-nowrap">
                        <span className="inline-flex items-center gap-1.5 text-slate-800 font-medium text-xs">
                          {renderTypeIcon(row.typeCategory)}
                          {row.type}
                        </span>
                      </td>

                      {/* Documents */}
                      <td className="py-4 px-4 whitespace-nowrap">
                        <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-slate-100 text-slate-700 font-semibold text-[11px]">
                          <svg className="w-3 h-3 text-slate-500" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                            <rect height="18" rx="2" ry="2" width="18" x="3" y="3"></rect>
                          </svg>
                          {row.docCount} doc{row.docCount === 1 ? "" : "s"}
                        </span>
                      </td>

                      {/* Flag */}
                      <td className="py-4 px-4 whitespace-nowrap">
                        <TierPill flag={row.flag} score={row.score} />
                      </td>

                      {/* Status */}
                      <td className="py-4 px-4 whitespace-nowrap">
                        <div className="flex items-center gap-2">
                          <span
                            className={`w-2 h-2 rounded-full ${
                              row.status === "Awaiting Review"
                                ? "bg-amber-500"
                                : row.status === "Approved"
                                  ? "bg-emerald-500"
                                  : row.status === "Rejected"
                                    ? "bg-rose-500"
                                    : "bg-blue-500"
                            }`}
                          ></span>
                          <span className="text-slate-700 font-medium text-xs">{row.status}</span>
                        </div>
                      </td>

                      {/* Submitted by */}
                      <td className="py-4 px-4 whitespace-nowrap">
                        {row.reviewer ? (
                          <div className="flex items-center gap-2">
                            <div className={`w-6 h-6 rounded-full flex items-center justify-center font-bold text-[10px] ${row.reviewer.color}`}>
                              {row.reviewer.initials}
                            </div>
                            <span className="text-slate-700 font-medium text-xs">{row.reviewer.name}</span>
                          </div>
                        ) : (
                          <button
                            type="button"
                            onClick={(e) => e.stopPropagation()}
                            className="inline-flex items-center gap-1 px-2 py-1 rounded border border-dashed border-slate-300 text-slate-500 text-[11px] hover:border-slate-400 hover:bg-slate-50 transition"
                          >
                            + Assign
                          </button>
                        )}
                      </td>

                      {/* Submitted */}
                      <td className="py-4 px-4 whitespace-nowrap">
                        <div className="text-xs text-slate-600 font-medium">
                          {row.submittedDate}{" "}
                          <span className="text-slate-400 font-normal ml-1">{row.submittedTime}</span>
                        </div>
                      </td>

                      {/* Actions */}
                      <td className="py-4 pl-4 pr-6 text-right whitespace-nowrap">
                        <Link
                          to={`/cases/${row.id}`}
                          onClick={(e) => e.stopPropagation()}
                          className="inline-flex items-center gap-1 text-xs font-bold text-blue-600 hover:text-blue-800 bg-blue-50/70 hover:bg-blue-100/70 px-3 py-1.5 rounded-lg transition"
                        >
                          {row.canAct ? "Review" : "View"}
                          <span aria-hidden="true">→</span>
                        </Link>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {/* Table Footer */}
          <div className="p-4 sm:px-6 sm:py-3.5 border-t border-slate-100 bg-slate-50/50 flex flex-col sm:flex-row items-center justify-between gap-3 text-xs text-slate-500">
            <div className="text-center sm:text-left">
              {total === 0 ? (
                "0 cases"
              ) : (
                <>
                  Showing{" "}
                  <span className="font-semibold text-slate-800">
                    {page * PAGE_SIZE + 1}–{page * PAGE_SIZE + pageCases.length}
                  </span>{" "}
                  of <span className="font-semibold text-slate-800">{total.toLocaleString()}</span> cases
                </>
              )}
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
        </div>
      </main>
    </div>
  )
}
