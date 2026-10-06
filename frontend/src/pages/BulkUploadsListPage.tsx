import { useQuery } from "@tanstack/react-query"
import { FileArchiveIcon } from "lucide-react"
import { Link, useNavigate } from "react-router-dom"

import { listBulkUploads } from "@/api/bulkUploads"
import { Button } from "@/components/ui/button"
import { Nav } from "@/design-system/Nav"
import { PageHeader } from "@/design-system/PageHeader"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import { formatFileSize } from "@/lib/uploadLimits"
import { BULK_STATUS_LABELS } from "@/types/bulkUpload"
import { CASE_TYPE_LABELS } from "@/types/case"

// The largest page the list endpoint allows (backend/app/api/bulk_uploads.py).
const HISTORY_LIMIT = 100

// Every zip uploaded (newest first): a `user` sees their own, reviewers their
// company's. Each row opens that upload's summary page.
export function BulkUploadsListPage() {
  const { token } = useAuth()
  const { platformCompanyParam, isPlatformAdmin } = useActingCompany()
  const navigate = useNavigate()

  const { data: uploads = [], isLoading } = useQuery({
    queryKey: ["bulk-uploads", platformCompanyParam, HISTORY_LIMIT],
    queryFn: () => listBulkUploads(token!, platformCompanyParam, HISTORY_LIMIT),
    enabled: Boolean(token) && (!isPlatformAdmin || Boolean(platformCompanyParam)),
  })

  return (
    <div className="min-h-svh bg-background">
      <Nav active="cases" />
      <main className="mx-auto flex max-w-[1440px] flex-col gap-5 px-3.5 py-5 sm:gap-6 sm:px-6 sm:py-7">
        <PageHeader
          eyebrow="Secure intake"
          title="All bulk uploads"
          description="Every zip of cases uploaded, newest first. Open one to see its cases and their progress."
          actions={
            <Button variant="outline" onClick={() => navigate("/cases/bulk")}>
              New bulk upload
            </Button>
          }
        />
        <div className="rounded-2xl border border-border bg-card p-6 shadow-card">
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : uploads.length === 0 ? (
            <p className="text-sm text-muted-foreground">No bulk uploads yet.</p>
          ) : (
            <ul className="flex flex-col divide-y">
              {uploads.map((b) => (
                <li key={b.id}>
                  <Link
                    to={`/bulk-uploads/${b.id}`}
                    className="flex items-start gap-3 rounded-md px-2 py-3 hover:bg-muted/40"
                  >
                    <FileArchiveIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{b.original_filename}</span>
                      <span className="block text-xs text-muted-foreground">
                        {new Date(b.created_at).toLocaleString()} · {BULK_STATUS_LABELS[b.status]} ·{" "}
                        {b.cases_created} created
                        {b.cases_failed > 0 && `, ${b.cases_failed} failed`} · {CASE_TYPE_LABELS[b.case_type]} ·{" "}
                        {formatFileSize(b.zip_size_bytes)}
                        {b.uploaded_by && ` · ${b.uploaded_by.full_name || b.uploaded_by.email}`}
                      </span>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      </main>
    </div>
  )
}
