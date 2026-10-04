/**
 * "Export report" action for the case-detail header (reviewer/admin only —
 * the backend 403s everyone else regardless of this UI).
 *
 * Generates a standalone PDF report of the case (backend/app/services/
 * case_report_*.py): cover + risk summary, per-document checks, annotated
 * page renders, cross-document consistency, audit excerpt and a
 * "Limitations" section. Generation is synchronous and takes a few
 * seconds, so the button just shows a busy state — there is no
 * poll-until-ready step. Each click adds a NEW report to the case's history
 * (a report is a point-in-time record and is never overwritten).
 *
 * The annotated pages inside that PDF are a separate artifact from the
 * live overlays drawn in PdfOverlayViewer: those stay dynamic and never
 * save anything; the report burns findings into its own rendered copies.
 */
import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ChevronDownIcon, FileTextIcon, Loader2Icon } from "lucide-react"

import { generateCaseReport, listCaseReports } from "@/api/cases"
import { ApiError } from "@/api/client"
import { cn } from "@/lib/utils"
import type { CaseReport } from "@/types/case"

const TIER_CLASSES: Record<string, string> = {
  low: "bg-emerald-50 text-emerald-700 border-emerald-200",
  medium: "bg-amber-50 text-amber-700 border-amber-200",
  high: "bg-red-50 text-red-700 border-red-200",
}

function formatWhen(iso: string): string {
  const d = new Date(iso)
  return `${d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })} · ${d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}`
}

function formatSize(bytes: number): string {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

export function CaseReportExport({ caseId, token }: { caseId: string; token: string }) {
  const queryClient = useQueryClient()
  const [historyOpen, setHistoryOpen] = React.useState(false)
  const [justCreated, setJustCreated] = React.useState<CaseReport | null>(null)

  const queryKey = ["caseReports", caseId, token]
  const { data: reports = [] } = useQuery({
    queryKey,
    queryFn: () => listCaseReports(caseId, token),
  })

  const generate = useMutation({
    mutationFn: () => generateCaseReport(caseId, token),
    onSuccess: (report) => {
      setJustCreated(report)
      queryClient.invalidateQueries({ queryKey })
      queryClient.invalidateQueries({ queryKey: ["caseAuditLog", caseId, token] })
      // Open it right away; if the browser blocks the popup (this runs
      // after an async call), the "Open PDF" link below still works.
      window.open(report.download_url, "_blank", "noopener,noreferrer")
    },
  })

  const errorMessage =
    generate.error instanceof ApiError
      ? generate.error.message
      : generate.error
      ? "Couldn't generate the report. Please try again."
      : null

  return (
    <div className="relative flex flex-col items-end gap-1">
      <div className="flex items-center gap-1.5">
        <button
          className="inline-flex items-center space-x-2 bg-white hover:bg-slate-50 border border-slate-300 text-slate-700 text-xs font-semibold px-4 py-2 rounded-lg shadow-sm transition-colors disabled:opacity-60 disabled:cursor-wait"
          type="button"
          onClick={() => generate.mutate()}
          disabled={generate.isPending}
          title="Generate a standalone PDF report of this case"
        >
          {generate.isPending ? (
            <Loader2Icon className="w-4 h-4 animate-spin text-slate-500" />
          ) : (
            <FileTextIcon className="w-4 h-4 text-slate-500" />
          )}
          <span>{generate.isPending ? "Generating report…" : "Export report"}</span>
        </button>
        {reports.length > 0 && (
          <button
            type="button"
            onClick={() => setHistoryOpen((o) => !o)}
            aria-expanded={historyOpen}
            className="inline-flex items-center gap-1 rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-xs font-semibold text-slate-600 shadow-sm hover:bg-slate-50"
            title="Previously generated reports"
          >
            <span>History ({reports.length})</span>
            <ChevronDownIcon className={cn("w-3.5 h-3.5 transition-transform", historyOpen && "rotate-180")} />
          </button>
        )}
      </div>

      {errorMessage && <p className="max-w-xs text-right text-[11px] text-red-600">{errorMessage}</p>}
      {!errorMessage && justCreated && !generate.isPending && (
        <p className="text-[11px] text-slate-500">
          Report ready ·{" "}
          <a
            href={justCreated.download_url}
            target="_blank"
            rel="noopener noreferrer"
            className="font-semibold text-blue-600 hover:underline"
          >
            Open PDF
          </a>
        </p>
      )}

      {historyOpen && reports.length > 0 && (
        <div className="absolute right-0 top-full z-20 mt-2 w-80 rounded-xl border border-slate-200 bg-white p-2 shadow-lg">
          <p className="px-2 pb-1 text-[10px] font-bold uppercase tracking-wider text-slate-400">
            Generated reports
          </p>
          <ul className="max-h-72 space-y-1 overflow-y-auto">
            {reports.map((r) => (
              <li key={r.id}>
                <a
                  href={r.download_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-xs hover:bg-slate-50"
                >
                  <span className="min-w-0">
                    <span className="block font-semibold text-slate-800">{formatWhen(r.generated_at)}</span>
                    <span className="block truncate text-[11px] text-slate-500">
                      {r.generated_by_name ?? "Unknown"} · {r.page_count} pages · {formatSize(r.file_size_bytes)}
                    </span>
                  </span>
                  {r.risk_tier && (
                    <span
                      className={cn(
                        "shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase",
                        TIER_CLASSES[r.risk_tier],
                      )}
                    >
                      {r.risk_tier} {r.risk_score}
                    </span>
                  )}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
