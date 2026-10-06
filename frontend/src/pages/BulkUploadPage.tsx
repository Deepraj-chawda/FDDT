import * as React from "react"
import { useQuery } from "@tanstack/react-query"
import { AlertTriangleIcon, FileArchiveIcon, FolderIcon, UploadCloudIcon, XIcon } from "lucide-react"
import { Link, useNavigate } from "react-router-dom"

import { listBulkUploads, uploadBulkZip } from "@/api/bulkUploads"
import { ApiError } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Nav } from "@/design-system/Nav"
import { PageHeader } from "@/design-system/PageHeader"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import {
  BULK_CASE_WARNING_THRESHOLD,
  clientZipProblem,
  estimateZipCaseCount,
  formatFileSize,
} from "@/lib/uploadLimits"
import { useUploadLimits } from "@/hooks/useUploadLimits"
import { cn } from "@/lib/utils"
import { BULK_STATUS_LABELS } from "@/types/bulkUpload"
import { CASE_TYPE_LABELS, CASE_TYPES, type CaseType } from "@/types/case"

export function BulkUploadPage() {
  const { token, user } = useAuth()
  const { platformCompanyParam, isPlatformAdmin } = useActingCompany()
  const navigate = useNavigate()
  // This company's limits (set per company by a platform admin).
  const limits = useUploadLimits()
  const inputRef = React.useRef<HTMLInputElement>(null)

  const [caseType, setCaseType] = React.useState<CaseType>("vendor_invoice")
  const [file, setFile] = React.useState<File | null>(null)
  const [estimate, setEstimate] = React.useState<number | null>(null)
  const [problem, setProblem] = React.useState<string | null>(null)
  const [progress, setProgress] = React.useState<number | null>(null)
  const [isDragActive, setIsDragActive] = React.useState(false)

  const { data: recent = [] } = useQuery({
    queryKey: ["bulk-uploads", platformCompanyParam],
    queryFn: () => listBulkUploads(token!, platformCompanyParam),
    enabled: Boolean(token) && (!isPlatformAdmin || Boolean(platformCompanyParam)),
  })

  const choose = async (picked: File | undefined) => {
    if (!picked) return
    setProblem(null)
    setEstimate(null)
    const issue = clientZipProblem(picked, limits?.max_zip_size_bytes ?? null)
    if (issue) {
      setFile(null)
      setProblem(issue)
      return
    }
    setFile(picked)
    setEstimate(await estimateZipCaseCount(picked))
  }

  const submit = async () => {
    if (!token || !file) return
    setProblem(null)
    setProgress(0)
    try {
      const created = await uploadBulkZip(file, caseType, token, setProgress)
      navigate(`/bulk-uploads/${created.id}`)
    } catch (err) {
      setProblem(err instanceof ApiError ? err.message : "Upload failed")
      setProgress(null)
    }
  }

  const uploading = progress !== null
  const canUpload = Boolean(user) && !isPlatformAdmin

  return (
    <div className="min-h-svh bg-background">
      <Nav active="cases" />
      <main className="mx-auto flex max-w-[1440px] flex-col gap-5 px-3.5 py-5 sm:gap-6 sm:px-6 sm:py-7">
        <PageHeader
          eyebrow="Secure intake"
          title="Bulk upload"
          description="Submit many cases at once: one zip file, one folder per case."
          actions={
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => navigate("/bulk-uploads")}>
                All bulk uploads
              </Button>
              <Button variant="outline" onClick={() => navigate("/cases/new")}>
                Submit a single case
              </Button>
            </div>
          }
        />
        <div className="grid grid-cols-1 gap-5 sm:gap-6 lg:grid-cols-[1fr_420px]">
          <Card>
            <CardHeader>
              <CardTitle className="text-xl">Upload a zip of cases</CardTitle>
              <CardDescription>
                Each top-level folder in the zip becomes its own case, and every document directly
                inside that folder is attached to it. Cases are created and processed one by one, so
                results start appearing within seconds.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-5">
              {!canUpload ? (
                <p className="text-sm text-muted-foreground">
                  Platform admins have read-only access and can't submit cases.
                </p>
              ) : (
                <>
                  <div className="flex flex-col gap-2">
                    <Label htmlFor="bulkCaseType">Case type (applies to every case in the zip)</Label>
                    <Select value={caseType} onValueChange={(v) => setCaseType(v as CaseType)} disabled={uploading}>
                      <SelectTrigger id="bulkCaseType">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {CASE_TYPES.map((type) => (
                          <SelectItem key={type} value={type}>
                            {CASE_TYPE_LABELS[type]}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  <div className="flex flex-col gap-2">
                    <Label>Zip file</Label>
                    {file ? (
                      <div className="flex items-center gap-3 rounded-lg border bg-muted/20 px-4 py-3">
                        <FileArchiveIcon className="size-5 shrink-0 text-accent" />
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium">{file.name}</p>
                          <p className="text-xs text-muted-foreground">
                            {formatFileSize(file.size)}
                            {estimate !== null && ` · about ${estimate} case folder${estimate === 1 ? "" : "s"}`}
                          </p>
                          {uploading && <Progress value={progress ?? 0} className="mt-2" />}
                        </div>
                        {!uploading && (
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            aria-label="Remove zip"
                            onClick={() => {
                              setFile(null)
                              setEstimate(null)
                            }}
                          >
                            <XIcon className="size-4" />
                          </Button>
                        )}
                      </div>
                    ) : (
                      <div
                        role="button"
                        tabIndex={0}
                        onClick={() => inputRef.current?.click()}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault()
                            inputRef.current?.click()
                          }
                        }}
                        onDragOver={(e) => {
                          e.preventDefault()
                          setIsDragActive(true)
                        }}
                        onDragLeave={() => setIsDragActive(false)}
                        onDrop={(e) => {
                          e.preventDefault()
                          setIsDragActive(false)
                          void choose(e.dataTransfer.files[0])
                        }}
                        className={cn(
                          "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors hover:bg-muted/40",
                          isDragActive ? "border-accent bg-accent/5" : "border-border bg-muted/20",
                        )}
                      >
                        <UploadCloudIcon className="size-8 text-muted-foreground" />
                        <p className="text-sm font-medium">Drag and drop a .zip here, or click to browse</p>
                        {limits && (
                          <p className="text-xs text-muted-foreground">
                            Max zip size: {formatFileSize(limits.max_zip_size_bytes)} · max file size:{" "}
                            {formatFileSize(limits.max_file_size_bytes)} per PDF
                          </p>
                        )}
                      </div>
                    )}
                    <input
                      ref={inputRef}
                      type="file"
                      accept=".zip,application/zip"
                      className="hidden"
                      onChange={(e) => {
                        void choose(e.target.files?.[0])
                        e.target.value = ""
                      }}
                    />
                    {estimate !== null && estimate > BULK_CASE_WARNING_THRESHOLD && (
                      <p className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-xs">
                        <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                        This zip holds about {estimate} cases (more than {BULK_CASE_WARNING_THRESHOLD}). It
                        will be accepted, but results for the later cases will take longer to appear.
                      </p>
                    )}
                    {problem && (
                      <p role="alert" className="text-sm text-destructive">
                        {problem}
                      </p>
                    )}
                  </div>

                  <Button onClick={submit} disabled={!file || uploading} className="w-full rounded-xl">
                    {uploading ? `Uploading… ${progress}%` : "Upload and create cases"}
                  </Button>
                </>
              )}
            </CardContent>
          </Card>

          <div className="flex flex-col gap-4">
            <div className="rounded-2xl border border-border bg-card p-6 shadow-card">
              <h3 className="mb-3 text-base font-bold text-foreground">How to structure the zip</h3>
              <div className="rounded-lg bg-muted/40 p-3 font-mono text-xs leading-6 text-foreground">
                <div className="flex items-center gap-1.5"><FileArchiveIcon className="size-3.5" /> claims.zip</div>
                <div className="flex items-center gap-1.5 pl-4"><FolderIcon className="size-3.5" /> case-001/ invoice.pdf, receipt.pdf</div>
                <div className="flex items-center gap-1.5 pl-4"><FolderIcon className="size-3.5" /> case-002/ quotation.pdf</div>
                <div className="flex items-center gap-1.5 pl-4"><FolderIcon className="size-3.5" /> case-003/ …</div>
              </div>
              <ul className="mt-3 list-disc space-y-1 pl-4 text-xs text-muted-foreground">
                <li>Put each case's documents directly in its folder. A case folder that contains a subfolder is rejected.</li>
                <li>The folder name becomes the case's reference label; it doesn't need to be unique.</li>
                <li>A bad file is rejected on its own; the rest of its case still goes ahead.</li>
                <li>Arabic and other non-English file and folder names are supported.</li>
              </ul>
            </div>

            {recent.length > 0 && (
              <div className="rounded-2xl border border-border bg-card p-6 shadow-card">
                <div className="mb-3 flex items-center justify-between gap-2">
                  <h3 className="text-base font-bold text-foreground">Recent bulk uploads</h3>
                  <Link to="/bulk-uploads" className="text-sm font-semibold text-blue-600 hover:underline">
                    View all
                  </Link>
                </div>
                <ul className="flex flex-col divide-y">
                  {recent.slice(0, 8).map((b) => (
                    <li key={b.id} className="py-2">
                      <Link to={`/bulk-uploads/${b.id}`} className="block hover:underline">
                        <span className="block truncate text-sm font-medium">{b.original_filename}</span>
                      </Link>
                      <span className="text-xs text-muted-foreground">
                        {new Date(b.created_at).toLocaleString()} · {BULK_STATUS_LABELS[b.status]} ·{" "}
                        {b.cases_created} created
                        {b.cases_failed > 0 && `, ${b.cases_failed} failed`}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  )
}
