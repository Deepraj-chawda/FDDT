import {
  ArrowRightIcon,
  ClockIcon,
  FileTextIcon,
  ShieldAlertIcon,
  ShieldCheckIcon,
} from "lucide-react"
import { Link, useNavigate } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"

import { listCases } from "@/api/cases"
import { CompanyPicker } from "@/components/CompanyPicker"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import { Nav } from "@/design-system/Nav"
import { CaseFlagBadge } from "@/components/case/CaseBadges"
import { CASE_STATUS_LABELS } from "@/types/case"

export function DashboardPage() {
  const navigate = useNavigate()
  const { token } = useAuth()
  const acting = useActingCompany()

  const { data: apiCases, isLoading } = useQuery({
    queryKey: ["cases", token, acting.companyId],
    queryFn: () => listCases(token as string, {}, acting.platformCompanyParam),
    // A platform admin's queue is one company at a time (never a mixed list).
    enabled: Boolean(token && acting.companyId),
  })

  const cases = apiCases ?? []
  const totalCases = cases.length
  const documentsInQueue = cases.reduce((acc, c) => acc + c.document_count, 0)
  const isOpen = (c: (typeof cases)[number]) => c.status !== "approved" && c.status !== "rejected" && c.status !== "closed"
  const highRiskCount = cases.filter((c) => c.flag?.flag === "high").length
  const mediumRiskCount = cases.filter((c) => c.flag?.flag === "medium").length
  const lowRiskCount = cases.filter((c) => c.flag?.flag === "low").length
  const pendingCount = cases.filter((c) => c.flag?.flag === "pending").length
  const escalatedCount = cases.filter((c) => c.assigned_tier === "l2" && isOpen(c)).length
  const clearedCount = cases.filter(
    (c) => c.status === "auto_approved" || c.status === "approved" || c.status === "closed"
  ).length

  // Open cases the user can act on (a Reviewer L1 doesn't see L2-escalated
  // ones here); escalated first, then highest risk score, most recent first
  // within a tie. Uses the real assessment tier/score from the risk-scoring
  // engine.
  const priorityQueue = cases
    .filter((c) => isOpen(c) && c.can_act)
    .sort((a, b) => {
      const esc = Number(b.assigned_tier === "l2") - Number(a.assigned_tier === "l2")
      if (esc !== 0) return esc
      const scoreDiff = (b.flag?.score ?? -1) - (a.flag?.score ?? -1)
      if (scoreDiff !== 0) return scoreDiff
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
    })
    .slice(0, 5)

  return (
    <div className="min-h-screen flex flex-col font-sans bg-[#EDF2FA] text-slate-900 selection:bg-blue-100 selection:text-blue-900">
      <Nav active="dashboard" />

      <main className="max-w-7xl mx-auto px-3.5 sm:px-6 lg:px-8 py-5 sm:py-8 w-full space-y-5 sm:space-y-6 flex-grow">
        <CompanyPicker />
        {/* PageHeaderTitleArea */}
        <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
          <div>
            <span className="text-[11px] font-bold tracking-widest uppercase text-blue-600">
              Operational Dashboard
            </span>
            <h1 className="text-2xl sm:text-3xl lg:text-4xl font-extrabold text-[#0b1930] tracking-tight mt-1">
              Integrity at a glance
            </h1>
            <p className="text-xs sm:text-sm text-slate-500 font-normal mt-1.5 max-w-2xl">
              Template-free extraction, cross-document validation, and explainable risk scoring.
            </p>
          </div>
        </div>

        {/* StatCardsRow */}
        <section aria-label="Key Performance Indicators" className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
          {/* Card 1: Documents in queue */}
          <div className="bg-white rounded-2xl p-4 sm:p-5 border border-slate-200/80 custom-shadow-card flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-500">Documents in queue</span>
              <ClockIcon className="w-4 h-4 text-slate-400" />
            </div>
            <div className="mt-3 sm:mt-4">
              <div className="text-2xl sm:text-3xl font-extrabold text-[#0b1930] tracking-tight">{documentsInQueue}</div>
              <div className="text-xs font-medium text-slate-500 mt-1">across {totalCases} case{totalCases === 1 ? "" : "s"}</div>
            </div>
          </div>

          {/* Card 2: Escalated to L2 */}
          <div className="bg-white rounded-2xl p-4 sm:p-5 border border-slate-200/80 custom-shadow-card flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-500">Escalated</span>
              <FileTextIcon className="w-4 h-4 text-slate-400" />
            </div>
            <div className="mt-3 sm:mt-4">
              <div className="text-2xl sm:text-3xl font-extrabold text-[#0b1930] tracking-tight">{escalatedCount}</div>
              <div className="text-xs font-medium text-slate-500 mt-1">open cases with a Reviewer L2</div>
            </div>
          </div>

          {/* Card 3: High-risk flagged */}
          <div className="bg-white rounded-2xl p-4 sm:p-5 border border-slate-200/80 custom-shadow-card flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-500">High-risk flagged</span>
              <ShieldAlertIcon className="w-4 h-4 text-slate-400" />
            </div>
            <div className="mt-3 sm:mt-4">
              <div className="text-2xl sm:text-3xl font-extrabold text-red-600 tracking-tight">{highRiskCount}</div>
              <div className="text-xs font-medium text-red-500 mt-1">{mediumRiskCount} more at medium risk</div>
            </div>
          </div>

          {/* Card 4: Clean cases */}
          <div className="bg-white rounded-2xl p-4 sm:p-5 border border-slate-200/80 custom-shadow-card flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-500">Low-risk cases</span>
              <ShieldCheckIcon className="w-4 h-4 text-slate-400" />
            </div>
            <div className="mt-3 sm:mt-4">
              <div className="text-2xl sm:text-3xl font-extrabold text-emerald-600 tracking-tight">{lowRiskCount}</div>
              <div className="text-xs font-medium text-slate-500 mt-1">below the medium-risk threshold</div>
            </div>
          </div>
        </section>

        {/* TwoColumnDashboardSection */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 sm:gap-5">
          {/* Left Column: Case Flag Distribution */}
          <section
            aria-labelledby="risk-distribution-heading"
            className="bg-white rounded-2xl p-4 sm:p-6 border border-slate-200/80 custom-shadow-card flex flex-col justify-between"
          >
            <div>
              <div className="flex items-center justify-between">
                <h2 className="text-base font-bold text-[#0b1930]" id="risk-distribution-heading">
                  Risk tier distribution
                </h2>
                <span className="text-[10px] font-bold tracking-wider text-slate-400 border border-slate-200 bg-slate-50 px-2 py-0.5 rounded uppercase">
                  {totalCases} total
                </span>
              </div>
              <p className="text-xs text-slate-500 mt-1">
                Each case's risk tier from the weighted, explainable rules engine. Cases still being analysed are shown separately.
              </p>

              {/* Distribution Bars */}
              <div className="mt-6 space-y-5">
                {([
                  { label: "High risk", count: highRiskCount, barClass: "bg-red-500", textClass: "text-red-500" },
                  { label: "Medium risk", count: mediumRiskCount, barClass: "bg-amber-500", textClass: "text-amber-500" },
                  { label: "Low risk", count: lowRiskCount, barClass: "bg-emerald-500", textClass: "text-emerald-500" },
                  { label: "Analyzing", count: pendingCount, barClass: "bg-slate-300", textClass: "text-slate-500" },
                ] as const).map((row) => {
                  const pct = totalCases > 0 ? Math.round((row.count / totalCases) * 100) : 0
                  return (
                    <div key={row.label}>
                      <div className="flex justify-between text-xs font-semibold mb-1.5">
                        <span className="text-slate-700">{row.label}</span>
                        <span className={`${row.textClass} font-bold`}>{row.count}</span>
                      </div>
                      <div className="w-full bg-slate-100 rounded-full h-2 overflow-hidden">
                        <div className={`${row.barClass} h-2 rounded-full`} style={{ width: `${pct}%` }}></div>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          </section>

          {/* Right Column: Priority Review Queue */}
          <section
            aria-labelledby="review-queue-heading"
            className="bg-white rounded-2xl p-6 border border-slate-200/80 custom-shadow-card flex flex-col justify-between"
          >
            <div>
              <div className="flex items-center justify-between">
                <h2 className="text-base font-bold text-[#0b1930]" id="review-queue-heading">
                  Priority review queue
                </h2>
                <Link
                  to="/cases"
                  className="text-xs font-bold text-blue-600 hover:text-blue-700 inline-flex items-center space-x-1 group"
                >
                  <span>View all</span>
                  <ArrowRightIcon className="w-3.5 h-3.5 transform group-hover:translate-x-0.5 transition-transform" />
                </Link>
              </div>
              <p className="text-xs text-slate-500 mt-1">Mismatches and evidence-required cases first, most recent first.</p>

              {/* Queue Items List */}
              <div className="mt-4 divide-y divide-slate-100">
                {isLoading && (
                  <p className="py-6 text-center text-xs text-slate-400">Loading cases...</p>
                )}
                {!isLoading && priorityQueue.length === 0 && (
                  <p className="py-6 text-center text-xs text-slate-400">No cases submitted yet.</p>
                )}
                {!isLoading && priorityQueue.map((c) => (
                  <div
                    key={c.id}
                    onClick={() => navigate(`/cases/${c.id}`)}
                    className="py-3 flex items-center justify-between gap-3 cursor-pointer hover:bg-slate-50/70 rounded-lg px-2 transition-colors"
                  >
                    <div className="min-w-0">
                      <div className="text-xs font-bold text-[#0b1930] hover:text-blue-600 transition-colors">
                        {c.case_number}
                      </div>
                      <div className="text-[11px] text-slate-500 mt-0.5 truncate">
                        {c.document_count} doc{c.document_count === 1 ? "" : "s"} · {CASE_STATUS_LABELS[c.status]}
                      </div>
                    </div>
                    {c.flag && <CaseFlagBadge flag={c.flag} />}
                  </div>
                ))}
              </div>
            </div>
          </section>
        </div>

        {/* BottomActivityBanner */}
        <section
          aria-label="Latest Activity"
          className="bg-white rounded-2xl p-5 sm:p-6 border border-slate-200/80 custom-shadow-card flex flex-col sm:flex-row sm:items-center justify-between gap-4"
        >
          <div>
            <span className="text-[10px] font-bold tracking-widest uppercase text-blue-600">
              Case Summary
            </span>
            <h3 className="text-lg font-bold text-[#0b1930] tracking-tight mt-0.5">
              {totalCases} case{totalCases === 1 ? "" : "s"} on record
            </h3>
            <p className="text-xs text-slate-500 mt-0.5">
              {documentsInQueue} document{documentsInQueue === 1 ? "" : "s"} analyzed · {escalatedCount} escalated · {clearedCount} cleared
            </p>
          </div>
          <div>
            <button
              onClick={() => navigate("/cases/new")}
              className="inline-flex items-center space-x-2 bg-[#0b1930] hover:bg-[#13233f] text-white px-5 py-2.5 rounded-full text-xs font-semibold tracking-wide transition-all shadow-sm"
              type="button"
            >
              <span>Analyze documents</span>
              <ArrowRightIcon className="w-3.5 h-3.5" />
            </button>
          </div>
        </section>
      </main>
    </div>
  )
}
