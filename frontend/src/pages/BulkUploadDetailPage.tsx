import * as React from "react"
import { useQuery } from "@tanstack/react-query"
import { AlertTriangleIcon, FileXIcon, InfoIcon } from "lucide-react"
import { Link, useNavigate, useParams } from "react-router-dom"

import { getBulkUpload } from "@/api/bulkUploads"
import { ApiError } from "@/api/client"
import { CaseFlagBadge } from "@/components/case/CaseBadges"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { Nav } from "@/design-system/Nav"
import { PageHeader } from "@/design-system/PageHeader"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import { formatFileSize } from "@/lib/uploadLimits"
import { cn } from "@/lib/utils"
import { BULK_STATUS_LABELS, type BulkCaseLiveStatus, type BulkUploadCase } from "@/types/bulkUpload"
import { CASE_TYPE_LABELS } from "@/types/case"

const LIVE_LABELS: Record<BulkCaseLiveStatus, string> = {
  validating: "Checking files",
  failed: "Not created",
  queued: "Queued",
  processing: "Processing",
  done: "Done",
  flagged: "Flagged",
}

const LIVE_VARIANTS: Record<BulkCaseLiveStatus, "secondary" | "destructive" | "info" | "success" | "warning" | "outline"> = {
  validating: "outline",
  failed: "destructive",
  queued: "secondary",
  processing: "info",
  done: "success",
  flagged: "warning",
}

type Filter = "all" | "attention" | "in_progress" | "done"

const FILTERS: { id: Filter; label: string; match: (c: BulkUploadCase) => boolean }[] = [
  { id: "all", label: "All", match: () => true },
  {
    id: "attention",
    label: "Needs attention",
    match: (c) => c.live_status === "failed" || c.files.some((f) => f.status === "rejected"),
  },
  {
    id: "in_progress",
    label: "In progress",
    match: (c) => ["validating", "queued", "processing"].includes(c.live_status),
  },
  { id: "done", label: "Done", match: (c) => c.live_status === "done" || c.live_status === "flagged" },
]

export function BulkUploadDetailPage() {
  const { bulkUploadId } = useParams<{ bulkUploadId: string }>()
  const { token } = useAuth()
  const { platformCompanyParam } = useActingCompany()
  const navigate = useNavigate()
  const [filter, setFilter] = React.useState<Filter>("all")

  const { data, error, isLoading } = useQuery({
    queryKey: ["bulk-upload", bulkUploadId, platformCompanyParam],
    queryFn: () => getBulkUpload(bulkUploadId!, token!, platformCompanyParam),
    enabled: Boolean(token && bulkUploadId),
    // Each case reports its own progress as it goes; stop once nothing can change.
    refetchInterval: (query) => (query.state.data?.settled ? false : 3000),
    refetchIntervalInBackground: false,
  })

  if (isLoading || !data) {
    return (
      <div className="min-h-svh bg-background">
        <Nav active="cases" />
        <main className="mx-auto max-w-[1440px] px-6 py-10 text-sm text-muted-foreground">
          {error ? (error instanceof ApiError ? error.message : "Could not load this bulk upload.") : "Loading…"}
        </main>
      </div>
    )
  }

  const total = data.cases.length
  const finished = data.progress.done + data.progress.flagged + data.progress.failed
  const visible = data.cases.filter(FILTERS.find((f) => f.id === filter)!.match)

  return (
    <div className="min-h-svh bg-background">
      <Nav active="cases" />
      <main className="mx-auto flex max-w-[1440px] flex-col gap-5 px-3.5 py-5 sm:gap-6 sm:px-6 sm:py-7">
        <PageHeader
          eyebrow="Bulk upload"
          title={<span className="break-all">{data.original_filename}</span>}
          description={
            <>
              {CASE_TYPE_LABELS[data.case_type]} · {formatFileSize(data.zip_size_bytes)} · uploaded{" "}
              {new Date(data.created_at).toLocaleString()}
              {data.uploaded_by && ` by ${data.uploaded_by.full_name || data.uploaded_by.email}`}
            </>
          }
          actions={
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => navigate("/bulk-uploads")}>
                All bulk uploads
              </Button>
              <Button variant="outline" onClick={() => navigate("/cases/bulk")}>
                New bulk upload
              </Button>
              <Button variant="secondary" onClick={() => navigate("/")}>
                Case queue
              </Button>
            </div>
          }
        />

        {data.status === "failed" && (
          <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm">
            <p className="font-medium text-destructive">{BULK_STATUS_LABELS.failed}</p>
            <p className="text-foreground">{data.error_message}</p>
          </div>
        )}
        {data.warnings.map((w) => (
          <p key={w} className="flex items-start gap-2 rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-sm">
            <AlertTriangleIcon className="mt-0.5 size-4 shrink-0" />
            {w}
          </p>
        ))}
        {(data.wrapper_folder || data.ignored_entry_count > 0) && (
          <div className="flex items-start gap-2 rounded-xl border bg-status-info-bg px-4 py-3 text-sm">
            <InfoIcon className="mt-0.5 size-4 shrink-0 text-accent" />
            <div>
              {data.wrapper_folder && (
                <p>
                  Everything was inside one folder, <strong dir="auto">{data.wrapper_folder}</strong>, so the folders
                  inside it were treated as the case folders.
                </p>
              )}
              {data.ignored_entry_count > 0 && (
                <p>
                  {data.ignored_entry_count} file{data.ignored_entry_count === 1 ? " was" : "s were"} not inside a case
                  folder and {data.ignored_entry_count === 1 ? "was" : "were"} ignored:{" "}
                  <span dir="auto">{data.ignored_entries.slice(0, 10).join(", ")}</span>
                  {data.ignored_entry_count > 10 && ", …"}
                </p>
              )}
            </div>
          </div>
        )}

        <div className="rounded-2xl border border-border bg-card p-5 shadow-card">
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
            <p className="text-sm font-semibold">
              {BULK_STATUS_LABELS[data.status]}
              <span className="ml-2 font-normal text-muted-foreground">
                {finished} of {total} case{total === 1 ? "" : "s"} finished
              </span>
            </p>
            <p className="text-xs text-muted-foreground">
              {data.documents_accepted} document{data.documents_accepted === 1 ? "" : "s"} accepted
              {data.documents_rejected > 0 && ` · ${data.documents_rejected} rejected`}
            </p>
          </div>
          <Progress value={total ? (finished / total) * 100 : 0} />
          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            {(Object.keys(LIVE_LABELS) as BulkCaseLiveStatus[])
              .filter((s) => data.progress[s] > 0)
              .map((s) => (
                <Badge key={s} variant={LIVE_VARIANTS[s]}>
                  {LIVE_LABELS[s]}: {data.progress[s]}
                </Badge>
              ))}
          </div>
        </div>

        <div className="overflow-hidden rounded-2xl border border-border bg-card shadow-card">
          <div className="flex flex-wrap gap-1 border-b px-4 py-3">
            {FILTERS.map((f) => {
              const count = data.cases.filter(f.match).length
              return (
                <button
                  key={f.id}
                  type="button"
                  onClick={() => setFilter(f.id)}
                  className={cn(
                    "rounded-full px-3 py-1 text-xs font-medium transition-colors",
                    filter === f.id ? "bg-accent/10 text-accent" : "text-muted-foreground hover:bg-muted",
                  )}
                >
                  {f.label} <span className="opacity-70">{count}</span>
                </button>
              )
            })}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left text-xs text-slate-700">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/60 text-[11px] font-bold uppercase tracking-wider text-slate-500">
                  <th className="py-3 pl-6 pr-4" scope="col">Folder</th>
                  <th className="px-4 py-3" scope="col">Case</th>
                  <th className="px-4 py-3" scope="col">Documents</th>
                  <th className="px-4 py-3" scope="col">Status</th>
                  <th className="px-4 py-3 pr-6" scope="col">Risk</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((c) => (
                  <CaseRow key={c.index} item={c} />
                ))}
                {visible.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-6 py-8 text-center text-muted-foreground">
                      No cases in this view.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </main>
    </div>
  )
}

function CaseRow({ item }: { item: BulkUploadCase }) {
  const rejected = item.files.filter((f) => f.status === "rejected")
  const accepted = item.files.filter((f) => f.status === "accepted").length
  return (
    <>
      <tr className={cn("border-b border-slate-100 align-top", rejected.length === 0 && item.status !== "failed" && "last:border-0")}>
        <td className="py-3 pl-6 pr-4 font-medium text-foreground">
          <span dir="auto">{item.folder}</span>
        </td>
        <td className="px-4 py-3">
          {item.case_id ? (
            <Link to={`/cases/${item.case_id}`} className="font-mono text-accent hover:underline">
              {item.case_number}
            </Link>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </td>
        <td className="px-4 py-3">
          {item.case_id ? (
            <>
              {item.documents_finished}/{item.documents_total} processed
              {rejected.length > 0 && <span className="text-destructive"> · {rejected.length} rejected</span>}
            </>
          ) : item.status === "pending" || item.files.every((f) => f.status === "skipped") ? (
            `${item.files.length} file${item.files.length === 1 ? "" : "s"}`
          ) : (
            `${accepted} of ${item.files.length} accepted`
          )}
        </td>
        <td className="px-4 py-3">
          <Badge variant={LIVE_VARIANTS[item.live_status]} dot>
            {LIVE_LABELS[item.live_status]}
          </Badge>
        </td>
        <td className="px-4 py-3 pr-6">{item.flag ? <CaseFlagBadge flag={item.flag} /> : null}</td>
      </tr>
      {(item.error_message || rejected.length > 0) && (
        <tr className="border-b border-slate-100 bg-slate-50/40">
          <td colSpan={5} className="py-2 pl-6 pr-6">
            {item.error_message && <p className="mb-1 text-destructive">{item.error_message}</p>}
            {rejected.length > 0 && (
              <ul className="flex flex-col gap-1">
                {rejected.map((f, i) => (
                  <li key={`${f.name}-${i}`} className="flex items-start gap-2">
                    <FileXIcon className="mt-0.5 size-3.5 shrink-0 text-destructive" />
                    <span>
                      <span className="font-medium" dir="auto">{f.name}</span>
                      <span className="text-muted-foreground"> — {f.message}</span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </td>
        </tr>
      )}
    </>
  )
}
