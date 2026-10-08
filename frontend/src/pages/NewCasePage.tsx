import * as React from "react"
import { zodResolver } from "@hookform/resolvers/zod"
import { CheckCircle2Icon, PinIcon } from "lucide-react"
import { Controller, useForm } from "react-hook-form"
import { useNavigate } from "react-router-dom"
import { z } from "zod"

import { createCase, uploadDocument } from "@/api/cases"
import { ApiError } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Nav } from "@/design-system/Nav"
import { PageHeader } from "@/design-system/PageHeader"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { FileDropzone, type FileWithProgress } from "@/components/upload/FileDropzone"
import { SignatureReferenceCreator } from "@/components/upload/SignatureReferenceCreator"
import { useAuth } from "@/hooks/useAuth"
import { useUploadLimits } from "@/hooks/useUploadLimits"
import { clientUploadProblem } from "@/lib/uploadLimits"
import { CASE_TYPE_LABELS, CASE_TYPES, type Case } from "@/types/case"
import type { CaseDocument, SignatureReference } from "@/types/case"

// The draw-a-box viewer (react-pdf) and the backend crop both work on PDFs
// only, same scope as every other forensics check in this project.
function isPdfDocument(doc: CaseDocument): boolean {
  return doc.content_type === "application/pdf" || doc.original_filename.toLowerCase().endsWith(".pdf")
}

const caseTypeSchema = z.object({
  caseType: z.enum(CASE_TYPES, { message: "Select a case type" }),
})

type CaseTypeFormValues = z.infer<typeof caseTypeSchema>

export function NewCasePage() {
  const { token, user } = useAuth()
  const navigate = useNavigate()
  const isPlatformAdmin = Boolean(user?.is_platform_admin)
  const canUpload = Boolean(user) && !isPlatformAdmin
  // This company's per-file limit (set by a platform admin).
  const maxFileBytes = useUploadLimits()?.max_file_size_bytes ?? null

  const [fileEntries, setFileEntries] = React.useState<FileWithProgress[]>([])
  const [filesError, setFilesError] = React.useState<string | null>(null)
  const [formError, setFormError] = React.useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = React.useState(false)
  const [createdCase, setCreatedCase] = React.useState<Case | null>(null)

  // Uploaded document records (returned by the upload endpoint) — used to
  // populate the "Set as reference signature" list after all uploads complete.
  const [uploadedDocs, setUploadedDocs] = React.useState<CaseDocument[]>([])

  // Which document the reviewer is currently creating a signature reference for.
  const [activeRefDoc, setActiveRefDoc] = React.useState<CaseDocument | null>(null)
  // References created this session (in order), shown per document.
  const [createdRefs, setCreatedRefs] = React.useState<SignatureReference[]>([])

  const {
    control,
    handleSubmit,
    formState: { errors },
  } = useForm<CaseTypeFormValues>({
    resolver: zodResolver(caseTypeSchema),
    defaultValues: {
      caseType: "vendor_invoice",
    },
  })

  const addFiles = (newFiles: File[]) => {
    // Empty or over-the-limit files are refused here, before anything is
    // sent; the server applies the same checks (plus type/corruption ones).
    const problems = newFiles
      .map((file) => clientUploadProblem(file, maxFileBytes))
      .filter((p): p is string => p !== null)
    const accepted = newFiles.filter((file) => clientUploadProblem(file, maxFileBytes) === null)
    setFilesError(problems.length > 0 ? problems.join(" ") : null)
    setFileEntries((prev) => [...prev, ...accepted.map((file) => ({ file }))])
  }

  const removeFile = (index: number) => {
    setFileEntries((prev) => {
      const updated = prev.filter((_, i) => i !== index)
      const hasErrors = updated.some((f) => f.error)
      if (!hasErrors) {
        setFilesError(null)
        setFormError(null)
        if (uploadedDocs.length === 0) {
          setCreatedCase(null)
        }
      }
      return updated
    })
  }

  const setEntryProgress = (index: number, progress: number) => {
    setFileEntries((prev) =>
      prev.map((entry, i) =>
        i === index ? { ...entry, progress, error: undefined } : entry
      )
    )
  }

  const setEntryError = (index: number, error: string) => {
    setFileEntries((prev) =>
      prev.map((entry, i) =>
        i === index ? { ...entry, error, progress: undefined } : entry
      )
    )
  }

  const uploadAll = async (caseId: string) => {
    if (!token) return
    const results = await Promise.all(
      fileEntries.map(async (entry, index) => {
        if (entry.progress === 100) return null // already uploaded (a retry)
        try {
          setEntryProgress(index, 0)
          const doc = await uploadDocument(caseId, entry.file, token, (percent) =>
            setEntryProgress(index, percent)
          )
          return doc
        } catch (err) {
          setEntryError(
            index,
            err instanceof ApiError ? err.message : "Upload failed"
          )
          return null
        }
      })
    )
    // Accumulate successfully-uploaded docs (deduplicating by id on retry).
    const newDocs = results.filter((d): d is CaseDocument => d !== null)
    setUploadedDocs((prev) => {
      const existingIds = new Set(prev.map((d) => d.id))
      return [...prev, ...newDocs.filter((d) => !existingIds.has(d.id))]
    })
  }

  const onSubmit = async (values: CaseTypeFormValues) => {
    if (!token || isPlatformAdmin) return
    if (fileEntries.length === 0) {
      setFilesError("Attach at least one supporting document")
      return
    }
    setFilesError(null)
    setFormError(null)
    setIsSubmitting(true)

    try {
      let activeCase = createdCase
      if (!activeCase) {
        activeCase = await createCase(values.caseType, token)
        setCreatedCase(activeCase)
      }
      await uploadAll(activeCase.id)
    } catch (err) {
      setFormError(
        err instanceof ApiError ? err.message : "Something went wrong. Please try again."
      )
    } finally {
      setIsSubmitting(false)
    }
  }

  const uploadedCount = fileEntries.filter((f) => f.progress === 100).length
  const failedCount = fileEntries.filter((f) => f.error).length
  const pendingCount = fileEntries.filter((f) => f.progress !== 100 && !f.error).length
  const allDone =
    createdCase !== null && fileEntries.length > 0 && uploadedCount === fileEntries.length

  // Setting a reference signature is optional — the case can be opened
  // without one; comparison simply doesn't run for that case.
  const hasPdf = uploadedDocs.some(isPdfDocument)

  const startNewCase = () => {
    setCreatedCase(null)
    setFileEntries([])
    setFilesError(null)
    setFormError(null)
    setUploadedDocs([])
    setActiveRefDoc(null)
    setCreatedRefs([])
  }

  return (
    <div className="min-h-svh bg-background">
      <Nav active="cases" />
      <main className="mx-auto flex max-w-[1440px] flex-col gap-5 sm:gap-6 px-3.5 sm:px-6 py-5 sm:py-7">
        <PageHeader
          eyebrow="Secure intake"
          title="Analyze new documents"
          description="Upload related evidence together for cross-document verification."
          actions={
            <Button variant="outline" onClick={() => navigate("/cases/bulk")}>
              Bulk upload (zip of cases)
            </Button>
          }
        />
        <div className="grid grid-cols-1 gap-5 sm:gap-6 lg:grid-cols-[1fr_420px]">
          {/* Left column — upload card */}
          <Card>
            <CardHeader>
              <CardTitle className="text-xl">Submit new case</CardTitle>
              <CardDescription>
                Select a case type and attach every supporting document — they
                will all be linked to the same case for cross-document checks.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {!canUpload ? (
                <p className="text-sm text-muted-foreground">
                  Platform admins have read-only access and can't submit cases.
                </p>
              ) : allDone ? (
                <div className="flex flex-col gap-4">
                  <div className="rounded-md border border-success/30 bg-success/10 px-4 py-3 text-sm">
                    <p className="font-medium text-foreground">Case submitted successfully.</p>
                    <p className="text-muted-foreground">
                      Case ID: <span className="font-mono">{createdCase.case_number}</span>
                      {" · "}
                      {uploadedCount} document{uploadedCount === 1 ? "" : "s"} uploaded.
                    </p>
                  </div>

                  {/* Signature reference creation — optional, per-document */}
                  {uploadedDocs.length > 0 && (
                    <div className="rounded-lg border border-border bg-muted/20 p-4">
                      <div className="mb-3 flex items-center gap-2">
                        <PinIcon className="size-4 text-primary" />
                        <span className="text-sm font-medium text-foreground">
                          Set reference signature{" "}
                          <span className="text-muted-foreground">(optional)</span>
                        </span>
                      </div>
                      <p className="mb-3 text-[11px] text-muted-foreground">
                        {hasPdf
                          ? "Optionally set a reference signature on a PDF below: draw a box around its signature or stamp and type the signer's name. You can skip this step. "
                          : "None of these documents is a PDF, so a reference signature can't be drawn. "}
                        The saved signature is kept in the signature library and, for now, is
                        compared only against signatures detected on the other documents in this
                        case. This comparison is advisory — for reviewer use only.
                      </p>
                      <div className="flex flex-col gap-2">
                        {uploadedDocs.map((doc) => {
                          const docRefs = createdRefs.filter((r) => r.source_document_id === doc.id)
                          const canDraw = isPdfDocument(doc)
                          return (
                            <div
                              key={doc.id}
                              className="rounded-md border bg-background px-3 py-2"
                            >
                              <div className="flex items-center justify-between gap-3">
                                <span className="truncate text-[13px] text-foreground">
                                  {doc.original_filename}
                                </span>
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="outline"
                                  className="h-7 shrink-0 text-[11px]"
                                  disabled={!canDraw}
                                  title={
                                    canDraw
                                      ? undefined
                                      : "Reference signatures can only be drawn on PDF documents"
                                  }
                                  onClick={() => setActiveRefDoc(doc)}
                                >
                                  {docRefs.length > 0
                                    ? "Add another reference"
                                    : "Set as reference signature"}
                                </Button>
                              </div>
                              {docRefs.map((r) => (
                                <div
                                  key={r.id}
                                  className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]"
                                >
                                  <span className="flex items-center gap-1 text-success">
                                    <CheckCircle2Icon className="size-3.5" />
                                    Reference saved: <strong>{r.person_name}</strong>
                                  </span>
                                  <span className="rounded border border-border px-1.5 py-px text-muted-foreground">
                                    Saved to signature library
                                  </span>
                                  {r.signature_image_url ? (
                                    <span className="text-muted-foreground">
                                      Comparing against other documents in this case — results
                                      appear in the case's Checks panel.
                                    </span>
                                  ) : (
                                    <span className="text-warning">
                                      The selected region couldn't be extracted from this file, so
                                      no comparison will run for it.
                                    </span>
                                  )}
                                </div>
                              ))}
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  )}

                  <div className="flex flex-wrap gap-2">
                    <Button
                      onClick={() => navigate(`/cases/${createdCase.id}`)}
                    >
                      View Case Details →
                    </Button>
                    <Button onClick={startNewCase} variant="outline">
                      Submit another case
                    </Button>
                    <Button
                      onClick={() => navigate("/")}
                      variant="secondary"
                    >
                      Back to case queue
                    </Button>
                  </div>
                </div>
              ) : (
                <form className="flex flex-col gap-5" onSubmit={handleSubmit(onSubmit)} noValidate>
                  <div className="flex flex-col gap-2">
                    <Label htmlFor="caseType">Case type</Label>
                    <Controller
                      control={control}
                      name="caseType"
                      render={({ field }) => (
                        <Select
                          value={field.value}
                          onValueChange={field.onChange}
                          disabled={Boolean(createdCase && uploadedDocs.length > 0)}
                        >
                          <SelectTrigger id="caseType" aria-invalid={Boolean(errors.caseType)}>
                            <SelectValue placeholder="Select a case type" />
                          </SelectTrigger>
                          <SelectContent>
                            {CASE_TYPES.map((type) => (
                              <SelectItem key={type} value={type}>
                                {CASE_TYPE_LABELS[type]}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    />
                    {errors.caseType && (
                      <p className="text-sm text-destructive">{errors.caseType.message}</p>
                    )}
                  </div>

                  <div className="flex flex-col gap-2">
                    <Label>Supporting documents</Label>
                    <FileDropzone
                      files={fileEntries}
                      onFilesAdded={addFiles}
                      onFileRemoved={removeFile}
                      disabled={isSubmitting}
                      maxFileBytes={maxFileBytes}
                    />
                    {filesError && <p className="text-sm text-destructive">{filesError}</p>}
                    {failedCount > 0 && !isSubmitting && (
                      <p className="text-sm text-destructive">
                        {failedCount} file{failedCount === 1 ? "" : "s"} failed to upload. Fix
                        the issue and submit again to retry just those.
                      </p>
                    )}
                  </div>

                  {formError && (
                    <p role="alert" className="text-sm text-destructive">
                      {formError}
                    </p>
                  )}

                  <div className="flex gap-2">
                    <Button type="submit" disabled={isSubmitting} className="w-full rounded-xl bg-primary text-primary-foreground">
                      {isSubmitting
                        ? "Submitting…"
                        : failedCount > 0
                          ? "Retry failed uploads"
                          : createdCase && pendingCount > 0
                            ? "Upload remaining documents"
                            : "Start integrity analysis"}
                    </Button>
                    {failedCount > 0 && !isSubmitting && (
                      <Button
                        type="button"
                        variant="outline"
                        onClick={startNewCase}
                        className="rounded-xl px-4 shrink-0"
                      >
                        Reset form
                      </Button>
                    )}
                  </div>
                </form>
              )}
            </CardContent>
          </Card>

          {/* Right column — analysis pipeline + integrity info */}
          <div className="flex flex-col gap-4">
            <div className="rounded-2xl border border-border bg-card p-6 shadow-card">
              <h3 className="mb-4 text-base font-bold text-foreground">Analysis pipeline</h3>
              <div className="flex flex-col gap-3">
                {[
                  { step: 1, label: "Secure original & verify hash", done: true },
                  { step: 2, label: "Extract layout and fields", done: true },
                  { step: 3, label: "Validate issuer and consistency" },
                  { step: 4, label: "Run forensic checks" },
                  { step: 5, label: "Calculate explainable risk" },
                ].map(({ step, label, done }) => (
                  <div key={step} className="flex items-center gap-3">
                    {done ? (
                      <span className="flex size-6 items-center justify-center rounded-full bg-success text-white">
                        <svg className="size-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                      </span>
                    ) : (
                      <span className="flex size-6 items-center justify-center rounded-full bg-accent text-[11px] font-bold text-white">
                        {step}
                      </span>
                    )}
                    <span className="text-sm text-foreground">{label}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="rounded-2xl border border-border bg-status-info-bg p-6 shadow-card">
              <div className="mb-1 text-[11px] font-bold uppercase tracking-widest text-accent">Evidence Integrity</div>
              <h3 className="text-base font-bold text-foreground">Originals remain unchanged</h3>
              <p className="mt-1 text-xs text-muted-foreground">
                Every uploaded file is preserved with a verifiable integrity hash and complete audit history.
              </p>
            </div>
          </div>
        </div>
      </main>

      {/* Signature reference creator modal — mounted at page root to avoid
          layout clipping from Card overflow:hidden context */}
      {activeRefDoc && createdCase && token && (
        <SignatureReferenceCreator
          caseId={createdCase.id}
          documentId={activeRefDoc.id}
          documentFilename={activeRefDoc.original_filename}
          token={token}
          onCreated={(ref) => {
            setCreatedRefs((prev) => [...prev, ref])
            setActiveRefDoc(null)
          }}
          onClose={() => setActiveRefDoc(null)}
        />
      )}
    </div>
  )
}
