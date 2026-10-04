import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2Icon, RefreshCwIcon } from "lucide-react"

import { ApiError } from "@/api/client"
import { getUsage, reconcileUsage, type UsagePeriod } from "@/api/platform"
import { formatBytes, PlatformShell } from "@/components/platform/PlatformShell"
import { StatusLine } from "@/components/settings/SettingsShell"
import { useAuth } from "@/hooks/useAuth"

const PERIODS: { value: UsagePeriod; label: string }[] = [
  { value: "this_month", label: "This month" },
  { value: "last_month", label: "Last month" },
  { value: "last_30_days", label: "Last 30 days" },
  { value: "this_year", label: "This year" },
  { value: "all_time", label: "All time" },
  { value: "custom", label: "Custom range" },
]

const n = (v: number) => v.toLocaleString("en-US")

export function PlatformUsagePage() {
  const { token } = useAuth()
  const queryClient = useQueryClient()
  const [period, setPeriod] = React.useState<UsagePeriod>("this_month")
  const [start, setStart] = React.useState("")
  const [end, setEnd] = React.useState("")
  const [notice, setNotice] = React.useState<string | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const customReady = period !== "custom" || (start !== "" && end !== "")

  const { data, isLoading, isError, error: loadError } = useQuery({
    queryKey: ["usage", token, period, start, end],
    queryFn: () => getUsage(token as string, period, start, end),
    enabled: Boolean(token) && customReady,
  })

  const reconcile = useMutation({
    mutationFn: () => reconcileUsage(token as string),
    onSuccess: (r) => {
      setError(null)
      setNotice(
        r.rows_corrected === 0
          ? `Counters verified against source data (${r.rows_checked} company-days): no drift.`
          : `Corrected ${r.rows_corrected} of ${r.rows_checked} company-days from source data (logged in the platform audit log).`,
      )
      void queryClient.invalidateQueries({ queryKey: ["usage"] })
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Reconciliation failed."),
  })

  const range = data
    ? data.start
      ? `${data.start} – ${data.end}`
      : "All time"
    : ""

  return (
    <PlatformShell
      active="usage"
      title="Billing & usage"
      description="Usage rolled up per company. Figures come from counters maintained as cases, documents and reports are created; a nightly job re-derives them from the source data and corrects any drift. Days are UTC."
      actions={
        <button
          type="button"
          onClick={() => reconcile.mutate()}
          disabled={reconcile.isPending}
          className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3.5 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60"
        >
          {reconcile.isPending ? <Loader2Icon className="size-3.5 animate-spin" /> : <RefreshCwIcon className="size-3.5" />}
          Reconcile now
        </button>
      }
    >
      <div className="mb-4 flex flex-wrap items-end gap-3 rounded-2xl border border-slate-200/80 bg-white p-3.5 sm:p-4 shadow-2xs">
        <label className="text-xs font-semibold text-slate-600">
          Period
          <select
            value={period}
            onChange={(e) => setPeriod(e.target.value as UsagePeriod)}
            className="ml-2 rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs font-semibold text-slate-800"
          >
            {PERIODS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
        </label>
        {period === "custom" && (
          <>
            <label className="text-xs font-semibold text-slate-600">
              From <input type="date" value={start} onChange={(e) => setStart(e.target.value)} className="ml-1 rounded-lg border border-slate-200 px-2 py-1 text-xs" />
            </label>
            <label className="text-xs font-semibold text-slate-600">
              To <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className="ml-1 rounded-lg border border-slate-200 px-2 py-1 text-xs" />
            </label>
          </>
        )}
        {range && <span className="ml-auto text-[11px] text-slate-400">{range}</span>}
      </div>

      <div className="mb-3"><StatusLine ok={notice} error={error ?? (isError ? (loadError as Error)?.message : null)} /></div>

      <div className="overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-2xs">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs min-w-[750px]">
            <thead>
              <tr className="border-b border-slate-200/70 bg-slate-50/50 text-[11px] font-bold uppercase tracking-wider text-slate-400">
                <th className="px-5 py-3.5">Company</th>
                <th className="px-4 py-3.5 text-right">Cases created</th>
                <th className="px-4 py-3.5 text-right">Documents uploaded</th>
                <th className="px-4 py-3.5 text-right">Files stored</th>
                <th className="px-4 py-3.5 text-right">Storage added</th>
                <th className="px-4 py-3.5 text-right">Total storage (all time)</th>
                <th className="px-5 py-3.5 text-right">Total files (all time)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {!customReady ? (
                <tr><td colSpan={7} className="py-12 text-center text-sm text-slate-400">Choose both dates.</td></tr>
              ) : isLoading ? (
                <tr><td colSpan={7} className="py-12 text-center text-sm text-slate-400">Loading usage…</td></tr>
              ) : !data || data.companies.length === 0 ? (
                <tr><td colSpan={7} className="py-12 text-center text-sm text-slate-400">No companies yet.</td></tr>
              ) : (
                data.companies.map((r) => (
                  <tr key={r.company_id} className={r.is_active ? "" : "text-slate-400"}>
                    <td className="px-5 py-3.5 font-bold text-slate-900">
                      {r.company_name}
                      {!r.is_active && <span className="ml-2 text-[10px] font-semibold uppercase text-slate-400">suspended</span>}
                    </td>
                    <td className="px-4 py-3.5 text-right tabular-nums">{n(r.cases_created)}</td>
                    <td className="px-4 py-3.5 text-right tabular-nums">{n(r.documents_uploaded)}</td>
                    <td className="px-4 py-3.5 text-right tabular-nums">{n(r.files_stored)}</td>
                    <td className="px-4 py-3.5 text-right tabular-nums">{formatBytes(r.storage_bytes)}</td>
                    <td className="px-4 py-3.5 text-right tabular-nums font-semibold">{formatBytes(r.total_storage_bytes)}</td>
                    <td className="px-5 py-3.5 text-right tabular-nums">{n(r.total_files_stored)}</td>
                  </tr>
                ))
              )}
            </tbody>
            {data && data.companies.length > 0 && (
              <tfoot>
                <tr className="border-t border-slate-200 bg-slate-50/60 font-bold text-slate-800">
                  <td className="px-5 py-3.5">All companies</td>
                  <td className="px-4 py-3.5 text-right tabular-nums">{n(data.totals.cases_created)}</td>
                  <td className="px-4 py-3.5 text-right tabular-nums">{n(data.totals.documents_uploaded)}</td>
                  <td className="px-4 py-3.5 text-right tabular-nums">{n(data.totals.files_stored)}</td>
                  <td className="px-4 py-3.5 text-right tabular-nums">{formatBytes(data.totals.storage_bytes)}</td>
                  <td className="px-4 py-3.5 text-right tabular-nums">{formatBytes(data.totals.total_storage_bytes)}</td>
                  <td className="px-5 py-3.5 text-right tabular-nums">{n(data.totals.total_files_stored)}</td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      </div>
      <p className="mt-3 text-[11px] text-slate-400">
        Files stored = original documents + generated report PDFs. Storage is computed from the exact byte size recorded when each file was written.
      </p>
    </PlatformShell>
  )
}
