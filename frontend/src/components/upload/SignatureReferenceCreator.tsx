/**
 * Draw-a-box UI for creating a signature reference from an uploaded document.
 *
 * Opens as a modal when the reviewer clicks "Set as reference signature"
 * on an uploaded document in the post-upload panel. Allows the reviewer to:
 *   1. Navigate the document's pages (react-pdf)
 *   2. Drag to draw a normalized bounding box over the signature/stamp region
 *   3. Type the person's name (required, no auto-extraction)
 *   4. Save — always stored in the signature library (is_library=true) and,
 *      for now, compared against the other documents in the current case only
 *
 * Advisory language throughout — never "verify", "confirm", or "authenticate".
 * See SPECIFICATION.md §2.3/§4.
 */
import * as React from "react"
import { CheckCircle2Icon, ChevronLeftIcon, ChevronRightIcon } from "lucide-react"
import { Document, Page } from "react-pdf"

import { createSignatureReference, getDocumentFileUrl } from "@/api/cases"
import { ApiError } from "@/api/client"
import { Button } from "@/components/ui/button"
import { PDF_DOCUMENT_OPTIONS } from "@/lib/pdfjs"
import { cn } from "@/lib/utils"
import type { BoundingBox, SignatureReference } from "@/types/case"

const MAX_PAGE_WIDTH = 640
// Smaller than this is treated as a stray click, not a deliberate box.
const MIN_BOX_WIDTH = 0.01
const MIN_BOX_HEIGHT = 0.005

interface Point {
  x: number
  y: number
}

function boxBetween(a: Point, b: Point, page: number): BoundingBox {
  return {
    page,
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    width: Math.abs(b.x - a.x),
    height: Math.abs(b.y - a.y),
  }
}

// react-pdf/pdf.js can throw synchronously during render on some load
// failures; without a boundary that unmounts the entire page (this modal is
// mounted at the page root). Error boundaries have no hook equivalent.
class ViewerErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean }
> {
  state = { hasError: false }
  static getDerivedStateFromError() {
    return { hasError: true }
  }
  componentDidCatch(error: unknown) {
    console.error("SignatureReferenceCreator viewer crashed:", error)
  }
  render() {
    if (this.state.hasError) {
      return <ViewerError message="Couldn't display this file." />
    }
    return this.props.children
  }
}

function ViewerError({ message }: { message: string }) {
  return (
    <div role="alert" className="p-8 text-center text-sm text-destructive">
      {message} Only PDF documents can be used to set a reference signature.
    </div>
  )
}

interface SignatureReferenceCreatorProps {
  caseId: string
  documentId: string
  documentFilename: string
  token: string
  onCreated: (ref: SignatureReference) => void
  onClose: () => void
}

export function SignatureReferenceCreator({
  caseId,
  documentId,
  documentFilename,
  token,
  onCreated,
  onClose,
}: SignatureReferenceCreatorProps) {
  const outerRef = React.useRef<HTMLDivElement>(null)
  // The wrapper sized exactly to the rendered PDF page. Pointer coordinates
  // and the overlay box are BOTH relative to this element, so they always
  // agree regardless of how wide the surrounding modal is.
  const pageWrapRef = React.useRef<HTMLDivElement>(null)

  const [fileUrl, setFileUrl] = React.useState<string | null>(null)
  const [fileUrlError, setFileUrlError] = React.useState<string | null>(null)
  const [loadError, setLoadError] = React.useState(false)

  const [numPages, setNumPages] = React.useState(0)
  const [pageNumber, setPageNumber] = React.useState(1)
  const [pageSize, setPageSize] = React.useState<{ width: number; height: number } | null>(null)
  const [containerWidth, setContainerWidth] = React.useState(MAX_PAGE_WIDTH)

  // Drawing state
  const [dragStart, setDragStart] = React.useState<Point | null>(null)
  const [drawnBox, setDrawnBox] = React.useState<BoundingBox | null>(null)
  const isDrawing = dragStart !== null

  // Form state
  const [personName, setPersonName] = React.useState("")
  const [nameError, setNameError] = React.useState<string | null>(null)
  const [boxError, setBoxError] = React.useState<string | null>(null)

  // Submission state
  const [isSubmitting, setIsSubmitting] = React.useState(false)
  const [submitError, setSubmitError] = React.useState<string | null>(null)

  // Mint a fresh signed URL on open — the one handed back at upload time is
  // short-lived, and the private container rejects the bare blob URL.
  React.useEffect(() => {
    let cancelled = false
    getDocumentFileUrl(caseId, documentId, token)
      .then((res) => {
        if (!cancelled) setFileUrl(res.file_url)
      })
      .catch((err) => {
        if (!cancelled) {
          setFileUrlError(
            err instanceof ApiError ? err.message : "Couldn't get a link to this document.",
          )
        }
      })
    return () => {
      cancelled = true
    }
  }, [caseId, documentId, token])

  React.useEffect(() => {
    const el = outerRef.current
    if (!el) return
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width
      if (w) setContainerWidth(Math.min(MAX_PAGE_WIDTH, Math.floor(w)))
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !isSubmitting) onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose, isSubmitting])

  // --- Normalized coordinate helper ---
  function toNormalized(e: React.PointerEvent): Point | null {
    const rect = pageWrapRef.current?.getBoundingClientRect()
    if (!rect || rect.width === 0 || rect.height === 0) return null
    return {
      x: Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)),
      y: Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height)),
    }
  }

  // --- Pointer draw events (pointer capture keeps the drag alive even if
  //     the cursor leaves the page, and covers touch/pen as well as mouse) ---
  function handlePointerDown(e: React.PointerEvent<HTMLDivElement>) {
    if (e.button !== 0 || isSubmitting) return
    const start = toNormalized(e)
    if (!start) return
    e.preventDefault()
    try {
      e.currentTarget.setPointerCapture(e.pointerId)
    } catch {
      // Capture is a nicety (keeps the drag alive off-page); drawing still
      // works without it, e.g. for synthetic pointer ids.
    }
    setDragStart(start)
    setDrawnBox(null)
    setBoxError(null)
  }

  function handlePointerMove(e: React.PointerEvent<HTMLDivElement>) {
    if (!dragStart) return
    const here = toNormalized(e)
    if (here) setDrawnBox(boxBetween(dragStart, here, pageNumber))
  }

  function handlePointerUp(e: React.PointerEvent<HTMLDivElement>) {
    if (!dragStart) return
    const here = toNormalized(e)
    const box = here ? boxBetween(dragStart, here, pageNumber) : null
    setDragStart(null)
    if (!box || box.width < MIN_BOX_WIDTH || box.height < MIN_BOX_HEIGHT) {
      setDrawnBox(null) // too small — treat as a mis-click
      return
    }
    setDrawnBox(box)
  }

  function handlePointerCancel() {
    setDragStart(null)
    setDrawnBox(null)
  }

  function changePage(next: number) {
    setPageNumber(next)
    setDrawnBox(null)
    setDragStart(null)
  }

  // --- Submission ---
  async function handleSubmit() {
    let hasError = false
    if (!personName.trim()) {
      setNameError("Person name is required")
      hasError = true
    } else {
      setNameError(null)
    }
    if (!drawnBox) {
      setBoxError("Draw a box over the signature or stamp region")
      hasError = true
    } else {
      setBoxError(null)
    }
    if (hasError || !drawnBox) return

    setIsSubmitting(true)
    setSubmitError(null)

    try {
      const ref = await createSignatureReference(
        caseId,
        documentId,
        {
          person_name: personName.trim(),
          bounding_box: drawnBox,
          // Every reference is kept in the signature library; for now it is
          // only compared against other documents in THIS case.
          is_library: true,
        },
        token,
      )
      onCreated(ref)
    } catch (err) {
      setSubmitError(
        err instanceof ApiError ? err.message : "Something went wrong. Please try again.",
      )
      setIsSubmitting(false)
    }
  }

  const viewerReady = fileUrl !== null && !loadError

  return (
    // Full-screen modal backdrop
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !isSubmitting) onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="sig-ref-title"
        className="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-border bg-background shadow-2xl"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h2 id="sig-ref-title" className="text-base font-bold text-foreground">
              Set reference signature
            </h2>
            <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
              {documentFilename} — drag to draw a box over the signature or stamp
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isSubmitting}
            className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {/* Advisory notice */}
        <div className="border-b border-border bg-amber-50 px-5 py-2.5 text-[11px] text-amber-800">
          <strong>For reviewer use only.</strong> This visual comparison is advisory — it supports
          human review and is not an automated verification or identity confirmation.
        </div>

        <div className="flex flex-1 flex-col gap-4 overflow-y-auto p-5">
          {/* Page navigation */}
          <div className="flex flex-wrap items-center gap-3">
            <Button
              type="button"
              variant="outline"
              size="icon"
              className="size-7"
              aria-label="Previous page"
              disabled={pageNumber <= 1}
              onClick={() => changePage(Math.max(1, pageNumber - 1))}
            >
              <ChevronLeftIcon className="size-3.5" />
            </Button>
            <span className="text-xs tabular-nums text-muted-foreground">
              Page {pageNumber} of {numPages || "…"}
            </span>
            <Button
              type="button"
              variant="outline"
              size="icon"
              className="size-7"
              aria-label="Next page"
              disabled={numPages === 0 || pageNumber >= numPages}
              onClick={() => changePage(Math.min(numPages, pageNumber + 1))}
            >
              <ChevronRightIcon className="size-3.5" />
            </Button>
            <span className="ml-2 text-[11px] text-muted-foreground">
              {drawnBox && !isDrawing
                ? "Box drawn ✓ — drag again to redraw"
                : "Click and drag on the document below to mark the signature region"}
            </span>
            {drawnBox && !isDrawing && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-[11px]"
                onClick={() => setDrawnBox(null)}
              >
                Clear box
              </Button>
            )}
          </div>

          {/* PDF + draw overlay */}
          {/* shrink-0: overflow-hidden lets a flex child shrink below its content
              height, which clipped the page to a strip on short viewports instead
              of letting the modal body scroll. */}
          <div ref={outerRef} className="shrink-0 overflow-hidden rounded-lg border bg-muted/20">
            {fileUrlError ? (
              <ViewerError message={fileUrlError} />
            ) : loadError ? (
              <ViewerError message="Couldn't load this file for preview." />
            ) : !fileUrl ? (
              <div className="p-8 text-sm text-muted-foreground">Loading document…</div>
            ) : (
              <ViewerErrorBoundary>
                <div
                  ref={pageWrapRef}
                  data-testid="signature-draw-surface"
                  className="relative mx-auto select-none"
                  style={{
                    cursor: "crosshair",
                    touchAction: "none",
                    ...(pageSize ? { width: pageSize.width, height: pageSize.height } : {}),
                  }}
                  onPointerDown={handlePointerDown}
                  onPointerMove={handlePointerMove}
                  onPointerUp={handlePointerUp}
                  onPointerCancel={handlePointerCancel}
                >
                  {/* react-pdf v11 suspends while it loads (React `use()`); without
                      a boundary here the nearest one is the app's route fallback,
                      which would blank the whole page. */}
                  <React.Suspense
                    fallback={<div className="p-8 text-sm text-muted-foreground">Loading document…</div>}
                  >
                    <Document
                      file={fileUrl}
                      options={PDF_DOCUMENT_OPTIONS}
                      onLoadSuccess={({ numPages: n }) => setNumPages(n)}
                      onLoadError={() => setLoadError(true)}
                      loading={<div className="p-8 text-sm text-muted-foreground">Loading document…</div>}
                    >
                      <Page
                        pageNumber={pageNumber}
                        width={containerWidth}
                        renderTextLayer={false}
                        renderAnnotationLayer={false}
                        onRenderSuccess={(page) =>
                          setPageSize({ width: page.width, height: page.height })
                        }
                      />
                    </Document>
                  </React.Suspense>

                  {/* Live draw box */}
                  {drawnBox && drawnBox.page === pageNumber && (
                    <div
                      data-testid="signature-draw-box"
                      className="pointer-events-none absolute rounded-sm border-2 border-primary bg-primary/20"
                      style={{
                        left: `${drawnBox.x * 100}%`,
                        top: `${drawnBox.y * 100}%`,
                        width: `${drawnBox.width * 100}%`,
                        height: `${drawnBox.height * 100}%`,
                      }}
                    />
                  )}
                </div>
              </ViewerErrorBoundary>
            )}
          </div>

          {boxError && <p className="text-sm text-destructive">{boxError}</p>}

          {/* Person name field */}
          <div className="flex flex-col gap-1.5">
            <label htmlFor="person-name" className="text-sm font-medium text-foreground">
              Signer / stamp owner name <span className="text-destructive">*</span>
            </label>
            <input
              id="person-name"
              type="text"
              className={cn(
                "w-full rounded-md border bg-background px-3 py-2 text-sm text-foreground outline-none",
                "placeholder:text-muted-foreground focus:ring-2 focus:ring-primary",
                nameError ? "border-destructive" : "border-border",
              )}
              placeholder="Type the name of the person or organization (no auto-extraction)"
              value={personName}
              onChange={(e) => {
                setPersonName(e.target.value)
                if (e.target.value.trim()) setNameError(null)
              }}
              disabled={isSubmitting}
              autoComplete="off"
            />
            {nameError && <p className="text-xs text-destructive">{nameError}</p>}
            <p className="text-[11px] text-muted-foreground">
              Required — type it manually. This name labels the comparison results shown to reviewers.
            </p>
          </div>

          {submitError && (
            <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {submitError}
            </p>
          )}

          {/* Action buttons */}
          <div className="flex flex-wrap gap-2 pt-1">
            <Button
              type="button"
              disabled={isSubmitting || !viewerReady}
              onClick={handleSubmit}
              className="gap-1.5"
            >
              <CheckCircle2Icon className="size-4" />
              {isSubmitting ? "Saving…" : "Save reference signature"}
            </Button>
            <Button type="button" variant="ghost" onClick={onClose} disabled={isSubmitting}>
              Cancel
            </Button>
          </div>
          <p className="text-[11px] text-muted-foreground">
            Every saved reference is kept in the signature library for future use. For now it is
            only compared against signatures detected on the other documents in this case — no
            comparison runs against other cases.
          </p>
        </div>
      </div>
    </div>
  )
}
