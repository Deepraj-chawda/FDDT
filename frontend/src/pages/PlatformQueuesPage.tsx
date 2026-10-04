import * as React from "react"
import { useQuery } from "@tanstack/react-query"

import { getQueues, type LimiterStats, type QueueStatus } from "@/api/platform"
import { PlatformShell } from "@/components/platform/PlatformShell"
import { useAuth } from "@/hooks/useAuth"

const secs = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v.toFixed(1)} s`)

function Limiter({ label, stats }: { label: string; stats?: LimiterStats }) {
  if (!stats) return null
  if (!stats.available) return <div className="text-[11px] text-slate-400">{label}: unavailable</div>
  return (
    <div className="text-[11px] text-slate-500">
      <span className="font-semibold text-slate-700">{label}:</span> {stats.units_in_current_window} in current window ·{" "}
      {stats.throttled_calls_total} calls throttled ({stats.throttled_seconds_total?.toFixed(0)} s waited) ·{" "}
      <span className={stats.http_429_total ? "font-semibold text-rose-600" : ""}>{stats.http_429_total} Azure 429s</span>
      {stats.cooldown_remaining_seconds ? (
        <span className="ml-1 font-semibold text-amber-700">· paused {stats.cooldown_remaining_seconds} s after a 429</span>
      ) : null}
    </div>
  )
}

function QueueCard({ q }: { q: QueueStatus }) {
  const backlog = q.waiting > 0 && (q.oldest_waiting_seconds ?? 0) > 60
  return (
    <div className="rounded-2xl border border-slate-200/80 bg-white p-4 sm:p-5 shadow-2xs">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-bold text-slate-900">{q.queue}</div>
          <div className="text-[11px] text-slate-500">{q.service} · {q.rate_limit}</div>
        </div>
        <span className={`rounded-full px-2.5 py-1 text-[11px] font-bold shrink-0 ${backlog ? "bg-amber-100 text-amber-800" : "bg-emerald-50 text-emerald-700"}`}>
          {backlog ? "Backlog" : "Healthy"}
        </span>
      </div>
      <div className="mt-4 grid grid-cols-2 sm:grid-cols-3 gap-2.5 sm:gap-3">
        {[
          { label: "Waiting", value: String(q.waiting) },
          { label: "Running", value: String(q.running) },
          { label: "Oldest waiting", value: secs(q.oldest_waiting_seconds) },
          { label: "Avg wait", value: secs(q.avg_wait_seconds) },
          { label: "p95 wait", value: secs(q.p95_wait_seconds) },
          { label: "Avg run time", value: secs(q.avg_runtime_seconds) },
        ].map((m) => (
          <div key={m.label}>
            <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">{m.label}</div>
            <div className="text-base sm:text-lg font-extrabold tabular-nums text-slate-900">{m.value}</div>
          </div>
        ))}
      </div>
      <div className="mt-3 space-y-1 border-t border-slate-100 pt-3">
        <div className="text-[11px] text-slate-500">
          {q.started_in_window} started / {q.completed_in_window} finished in window · configured workers: {q.configured_workers} ·
          consumers online: {q.workers === null || q.workers === undefined ? "unknown" : q.workers.length}
        </div>
        {q.outstanding_by_company && q.outstanding_by_company.length > 0 && (
          <div className="text-[11px] text-slate-500">
            <span className="font-semibold text-slate-700">Outstanding by company (fair share):</span>{" "}
            {q.outstanding_by_company
              .slice(0, 6)
              .map((c) => `${c.company_name ?? c.company_id.slice(0, 8)} ${c.outstanding}`)
              .join(" · ")}
          </div>
        )}
        <Limiter label="Request limiter" stats={q.rate_limiter} />
        <Limiter label="Token limiter" stats={q.token_rate_limiter} />
      </div>
    </div>
  )
}

export function PlatformQueuesPage() {
  const { token } = useAuth()
  const [windowMinutes, setWindowMinutes] = React.useState(15)
  const { data, isLoading, dataUpdatedAt } = useQuery({
    queryKey: ["queues", token, windowMinutes],
    queryFn: () => getQueues(token as string, windowMinutes),
    enabled: Boolean(token),
    refetchInterval: 5_000,
  })

  return (
    <PlatformShell
      active="queues"
      title="Processing queues"
      description="Every document step is its own task on a shared queue, dispatched fair-share per company: a company with little work outstanding is served before the tail of another company's large batch, and no company has a fixed priority. Wait time is measured from enqueue to start. The two Azure queues are rate-limited globally across all workers; forensics is local CPU and unthrottled."
      actions={
        <label className="text-xs font-semibold text-slate-600">
          Window
          <select value={windowMinutes} onChange={(e) => setWindowMinutes(Number(e.target.value))} className="ml-2 rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs">
            {[5, 15, 60, 240].map((m) => <option key={m} value={m}>{m} min</option>)}
          </select>
        </label>
      }
    >
      {isLoading ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : !data?.available ? (
        <p className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{data?.error ?? "Queue data unavailable."}</p>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {data.queues.map((q) => <QueueCard key={q.queue} q={q} />)}
          </div>
          <p className="mt-3 text-[11px] text-slate-400">Refreshes every 5 s · last update {new Date(dataUpdatedAt).toLocaleTimeString()}</p>
        </>
      )}
    </PlatformShell>
  )
}
