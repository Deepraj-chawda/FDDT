import * as React from "react"
import { useQuery } from "@tanstack/react-query"
import { AlertTriangleIcon, ArrowRightIcon, CheckCircle2Icon, ChevronDownIcon, FileCheckIcon, SearchIcon, ZapIcon } from "lucide-react"
import { Link } from "react-router-dom"

import { listAuditEventTypes, listAuditEvents } from "@/api/settings"
import { Nav } from "@/design-system/Nav"
import { CompanyPicker } from "@/components/CompanyPicker"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import { auditLabel, auditTone, describeAuditEvent, type AuditTone } from "@/lib/audit"
import type { AuditEvent } from "@/types/settings"

const PAGE_SIZE = 50

const TONE_STYLES: Record<AuditTone, { pill: string; dot: string }> = {
  rose: { pill: "bg-rose-50 text-rose-700 border-rose-200/80", dot: "bg-rose-500" },
  emerald: { pill: "bg-emerald-50 text-emerald-700 border-emerald-200/80", dot: "bg-emerald-500" },
  amber: { pill: "bg-amber-50 text-amber-700 border-amber-200/80", dot: "bg-amber-500" },
  blue: { pill: "bg-blue-50 text-blue-700 border-blue-200/80", dot: "bg-blue-500" },
  slate: { pill: "bg-slate-50 text-slate-600 border-slate-200", dot: "bg-slate-400" },
}

function initialsFor(name: string): string {
  return (
    name
      .split(/[\s@.]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((p) => p[0])
      .join("")
      .toUpperCase() || "?"
  )
}

function ActorCell({ evt }: { evt: AuditEvent }) {
  if (!evt.actor_name) {
    return (
      <div className="flex items-center gap-2">
        <div className="flex size-6 items-center justify-center rounded-full bg-blue-100 text-[10px] font-bold text-blue-700">AI</div>
        <span className="font-medium text-slate-500">System pipeline</span>
      </div>
    )
  }
  return (
    <div className="flex items-center gap-2" title={evt.actor_email ?? undefined}>
      <div className="flex size-6 items-center justify-center rounded-full bg-slate-900 text-[10px] font-bold text-white">{initialsFor(evt.actor_name)}</div>
      <span className="font-medium text-slate-800">{evt.actor_name}</span>
    </div>
  )
}

export function AuditHistoryPage() {
  const { token } = useAuth()
  const [search, setSearch] = React.useState("")
  const [debouncedSearch, setDebouncedSearch] = React.useState("")
  const [eventType, setEventType] = React.useState("")
  const [page, setPage] = React.useState(0)
  const acting = useActingCompany()
  // Platform admins read one company's log at a time, or the platform-level
  // log (companies/users created, usage reconciliations) — never a mix.
  const [platformLog, setPlatformLog] = React.useState(false)
  const scopeCompany = acting.isPlatformAdmin && !platformLog ? acting.platformCompanyParam : null
  const scopeKey = acting.isPlatformAdmin ? (platformLog ? "platform" : acting.companyId) : "own"

  // Don't hit the API on every keystroke.
  React.useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedSearch(search)
      setPage(0)
    }, 300)
    return () => clearTimeout(t)
  }, [search])

  const { data, isLoading, isError, isFetching } = useQuery({
    queryKey: ["auditEvents", token, scopeKey, debouncedSearch, eventType, page],
    queryFn: () =>
      listAuditEvents(token as string, {
        q: debouncedSearch,
        event_type: eventType || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        company_id: scopeCompany,
      }),
    enabled: Boolean(token && (scopeKey !== null)),
    placeholderData: (prev) => prev,
  })
  const { data: eventTypes = [] } = useQuery({
    queryKey: ["auditEventTypes", token, scopeKey],
    queryFn: () => listAuditEventTypes(token as string, scopeCompany),
    enabled: Boolean(token),
  })

  const items = data?.items ?? []
  const total = data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const automated = items.filter((e) => !e.actor_name).length
  const human = items.length - automated
  const decisions = items.filter((e) => ["case_approved", "case_rejected", "case_escalated"].includes(e.event_type)).length

  return (
    <div className="min-h-screen flex flex-col font-sans bg-[#EDF2FA] text-slate-900 selection:bg-blue-100 selection:text-blue-900">
      <Nav active="audit_history" />

      <main className="flex-1 max-w-[1600px] w-full mx-auto px-3 sm:px-6 py-4 sm:py-8 space-y-4 sm:space-y-6">
        {acting.isPlatformAdmin && (
          <div>
            {!platformLog && <CompanyPicker note="Reading a company's log is itself recorded in the platform audit log." />}
            <label className="flex items-center gap-2 text-xs font-semibold text-slate-600 mt-2">
              <input
                type="checkbox"
                checked={platformLog}
                onChange={(e) => {
                  setPlatformLog(e.target.checked)
                  setPage(0)
                }}
              />
              Show the platform-level log instead (companies, users, usage reconciliation)
            </label>
          </div>
        )}
        <section>
          <p className="text-[11px] font-bold uppercase tracking-wider text-blue-600 mb-1">System Governance &amp; Compliance</p>
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-slate-950">Audit History</h1>
          <p className="text-xs sm:text-sm text-slate-500 mt-1 max-w-3xl">
            The append-only record of every automated check, reviewer decision and administrative change. Entries are only ever added, never edited or removed.
          </p>
        </section>

        <section aria-label="Summary" className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
          {[
            { label: "Matching events", value: total, note: eventType || debouncedSearch ? "for the current filters" : "logged in total", icon: FileCheckIcon, tone: "bg-blue-50 text-blue-600" },
            { label: "Automated", value: automated, note: `of the ${items.length} shown`, icon: ZapIcon, tone: "bg-amber-50 text-amber-600" },
            { label: "By people", value: human, note: `of the ${items.length} shown`, icon: CheckCircle2Icon, tone: "bg-emerald-50 text-emerald-600" },
            { label: "Reviewer decisions", value: decisions, note: "approve / reject / escalate shown", icon: AlertTriangleIcon, tone: "bg-rose-50 text-rose-600" },
          ].map((c) => (
            <div key={c.label} className="bg-white rounded-2xl p-4 sm:p-5 border border-slate-200/90 shadow-sm flex items-start justify-between">
              <div>
                <span className="text-[11px] sm:text-xs font-bold uppercase tracking-wider text-slate-500">{c.label}</span>
                <div className="mt-2 sm:mt-3 text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">{c.value.toLocaleString()}</div>
                <div className="text-[11px] sm:text-xs text-slate-500 mt-1">{c.note}</div>
              </div>
              <div className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${c.tone}`}><c.icon className="w-4 h-4" /></div>
            </div>
          ))}
        </section>

        <section className="bg-white p-3.5 sm:p-5 rounded-2xl border border-slate-200/90 shadow-sm flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-3 sm:gap-3.5">
          <div className="relative w-full lg:w-80 shrink-0">
            <SearchIcon className="w-4 h-4 absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              aria-label="Search audit history"
              className="w-full pl-9 pr-4 py-2 bg-slate-50/60 border border-slate-200/90 rounded-full text-xs sm:text-[13px] placeholder:text-slate-400 text-slate-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition shadow-2xs"
              placeholder="Search by case number, person or event type…"
              type="text"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2.5 sm:gap-3">
            <div className="relative flex-1 sm:flex-none">
              <select
                value={eventType}
                onChange={(e) => {
                  setEventType(e.target.value)
                  setPage(0)
                }}
                aria-label="Filter by event type"
                className="w-full sm:w-auto appearance-none bg-white border border-slate-200 hover:border-slate-300 text-slate-700 text-xs font-medium pl-3.5 pr-8 py-2 rounded-full focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 cursor-pointer transition shadow-2xs whitespace-nowrap"
              >
                <option value="">Event type: all events</option>
                {eventTypes.map((t) => (
                  <option key={t} value={t}>{auditLabel(t)}</option>
                ))}
              </select>
              <ChevronDownIcon className="w-3.5 h-3.5 absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" />
            </div>
            <button
              onClick={() => {
                setSearch("")
                setDebouncedSearch("")
                setEventType("")
                setPage(0)
              }}
              className="text-xs font-medium text-slate-500 hover:text-slate-800 px-2 py-2 transition cursor-pointer shrink-0"
              type="button"
            >
              Reset
            </button>
          </div>
        </section>

        <section className="bg-white rounded-2xl border border-slate-200/90 shadow-sm overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs min-w-[700px]">
              <thead>
                <tr className="border-b border-slate-200/80 text-[11px] font-bold uppercase tracking-wider text-slate-400 bg-slate-50/50">
                  <th className="py-3.5 px-4 sm:px-5" scope="col">Timestamp</th>
                  <th className="py-3.5 px-3 sm:px-4" scope="col">Actor</th>
                  <th className="py-3.5 px-3 sm:px-4" scope="col">Event</th>
                  <th className="py-3.5 px-3 sm:px-4" scope="col">Case</th>
                  <th className="py-3.5 px-3 sm:px-4 min-w-[240px] sm:min-w-[320px]" scope="col">Detail</th>
                  <th className="py-3.5 px-4 sm:px-5 text-right" scope="col"><span className="sr-only">Open</span></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {isLoading ? (
                  <tr><td colSpan={6} className="py-12 text-center text-sm text-slate-400">Loading audit history…</td></tr>
                ) : isError ? (
                  <tr><td colSpan={6} className="py-12 text-center text-sm text-rose-600">Couldn't load the audit history.</td></tr>
                ) : items.length === 0 ? (
                  <tr><td colSpan={6} className="py-12 text-center text-sm text-slate-400">No events match the current filters.</td></tr>
                ) : (
                  items.map((evt) => {
                    const tone = TONE_STYLES[auditTone(evt.event_type)]
                    const when = new Date(evt.created_at)
                    const detail = describeAuditEvent(evt.event_type, evt.event_data) ?? evt.document_filename
                    return (
                      <tr key={evt.id} className="hover:bg-blue-50/30 transition">
                        <td className="py-3.5 sm:py-4 px-4 sm:px-5 whitespace-nowrap">
                          <div className="font-medium text-slate-800 text-xs">
                            {when.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })} ·{" "}
                            {when.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                          </div>
                          <div className="font-mono text-[10px] sm:text-[11px] text-slate-400">{when.toISOString().slice(11, 23)} UTC</div>
                        </td>
                        <td className="py-3.5 sm:py-4 px-3 sm:px-4 whitespace-nowrap"><ActorCell evt={evt} /></td>
                        <td className="py-3.5 sm:py-4 px-3 sm:px-4 whitespace-nowrap">
                          <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] sm:text-xs font-semibold border ${tone.pill}`}>
                            <span className={`w-1.5 h-1.5 rounded-full ${tone.dot}`}></span>
                            {auditLabel(evt.event_type)}
                          </span>
                        </td>
                        <td className="py-3.5 sm:py-4 px-3 sm:px-4 whitespace-nowrap">
                          {evt.case_id ? (
                            <Link to={`/cases/${evt.case_id}`} className="font-mono text-xs font-semibold text-blue-600 hover:underline">
                              {evt.case_number}
                            </Link>
                          ) : (
                            <span className="text-slate-300">—</span>
                          )}
                        </td>
                        <td className="py-3.5 sm:py-4 px-3 sm:px-4 text-xs text-slate-600 leading-relaxed">
                          {detail ?? <span className="text-slate-300">—</span>}
                          {evt.document_filename && detail !== evt.document_filename && (
                            <div className="text-[11px] text-slate-400">{evt.document_filename}</div>
                          )}
                        </td>
                        <td className="py-3.5 sm:py-4 px-4 sm:px-5 text-right whitespace-nowrap">
                          {evt.case_id && (
                            <Link to={`/cases/${evt.case_id}`} className="text-xs font-semibold text-blue-600 hover:text-blue-700 inline-flex items-center gap-1">
                              <span>Open case</span>
                              <ArrowRightIcon className="w-3.5 h-3.5" />
                            </Link>
                          )}
                        </td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
          <div className="flex flex-col sm:flex-row items-center justify-between gap-3 border-t border-slate-100 bg-slate-50/50 px-4 sm:px-6 py-3 text-xs text-slate-500">
            <div className="text-center sm:text-left">
              {total === 0 ? "0 events" : <>Showing <span className="font-semibold text-slate-800">{page * PAGE_SIZE + 1}–{Math.min(total, page * PAGE_SIZE + items.length)}</span> of <span className="font-semibold text-slate-800">{total.toLocaleString()}</span></>}
              {isFetching && <span className="ml-2 text-slate-400">updating…</span>}
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
