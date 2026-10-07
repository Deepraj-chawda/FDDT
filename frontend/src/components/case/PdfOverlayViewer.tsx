import * as React from "react"
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react"
import { Document, Page } from "react-pdf"

import { Button } from "@/components/ui/button"
import { PDF_DOCUMENT_OPTIONS } from "@/lib/pdfjs"
import { cn } from "@/lib/utils"
import type { BoundingBox } from "@/types/case"

// Fixed per check_type (backend/app/services/forensics/ela.py = red,
// copy_move.py = orange) rather than derived from anything per-finding,
// so the legend below and every box drawn for a given check always
// agree, and a reviewer only ever has to learn the mapping once.
//
// "ai" (backend/app/services/visual_inconsistency_service.py) is drawn
// distinctly (dashed, third color, own legend/overlay label) rather
// than reusing destructive/warning's solid-box treatment — this box
// comes from a vision-language model's visual judgment, not
// pixel-level analysis, and shouldn't visually read as equally precise
// as the other two.
//
// "field" is a third, separate category: a solid PURPLE box for
// rule-based field exceptions (a failed field-validation sub-check, or a
// cross-document mismatch — backend/app/services/field_exception_service.py).
// Those are deterministic comparisons of extracted values, so they are
// neither pixel forensics (red/orange) nor model judgment (dashed).
//
// "font" is a solid FUCHSIA box around text whose font family differs from
// the text around it, read from the PDF's text layer
// (backend/app/services/forensics/font_consistency.py) — exact, not judged.
//
// "ghost" is a solid TEAL box around the faint trace of text erased from a
// scan converted to editable text, with no live text on top (deleted) or
// running on past it (shortened) — backend/app/services/forensics/
// ghost_content.py.
export type OverlayColor = "destructive" | "warning" | "ai" | "field" | "font" | "ghost"

export interface OverlayBox {
  box: BoundingBox
  color: OverlayColor
  label: string
}

const OVERLAY_COLOR_CLASSES: Record<OverlayColor, string> = {
  destructive: "border-destructive bg-destructive/20",
  warning: "border-warning bg-warning/25",
  ai: "border-dashed border-info bg-info/10",
  field: "border-violet-600 bg-violet-600/15",
  font: "border-fuchsia-600 bg-fuchsia-600/15",
  ghost: "border-teal-600 bg-teal-600/20",
}

const OVERLAY_COLOR_LEGEND: Record<OverlayColor, string> = {
  destructive: "ELA tampering",
  warning: "Copy-move",
  ai: "AI-described area (approximate)",
  field: "Field exception",
  font: "Font mismatch",
  ghost: "Deleted / replaced content",
}

// react-pdf/pdf.js can throw synchronously during render on a load
// failure (confirmed in testing: an expired/CORS-blocked blob URL threw
// straight through onLoadError and unmounted the *entire* case detail
// page, not just this panel — no error boundary existed anywhere above
// it). This is the page's only class component for exactly that reason:
// error boundaries have no hook equivalent.
class PdfViewerErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean }
> {
  state = { hasError: false }
  static getDerivedStateFromError() {
    return { hasError: true }
  }
  componentDidCatch(error: unknown) {
    console.error("PdfOverlayViewer crashed:", error)
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="rounded-lg border bg-background p-3 text-sm text-destructive">
          Couldn't load this file for preview.
        </div>
      )
    }
    return this.props.children
  }
}

// Renders one page of the case document with the currently-expanded
// tampering checks' bounding boxes drawn on top — SPECIFICATION.md's ask: no
// annotated/burned-in image is ever generated or stored, this is drawn
// live over the real PDF render, and disappears the moment `overlays`
// goes back to empty (the check gets collapsed).
export function PdfOverlayViewer(props: { fileUrl: string; overlays: OverlayBox[] }) {
  return (
    <PdfViewerErrorBoundary>
      {/* react-pdf v11 suspends while it loads a file (React `use()`). Without
          a boundary here the nearest one is the app's route-level fallback,
          which blanked the whole case page and remounted it — refetching the
          case, getting a new signed URL, loading again: a reload loop. */}
      <React.Suspense
        fallback={<div className="p-8 text-center text-sm text-muted-foreground">Loading document…</div>}
      >
        <PdfOverlayViewerInner {...props} />
      </React.Suspense>
    </PdfViewerErrorBoundary>
  )
}

// The page render must never be wider than its own card — a fixed pixel
// width (the old behavior) overflowed the center column at some of this
// page's real 3-column widths, spilling the rendered PDF into the risk
// panel next to it. Measured from the card's own content box instead,
// capped at a sensible max so it doesn't blow up full-bleed on a wide
// viewport.
const MAX_PAGE_WIDTH = 560

function PdfOverlayViewerInner({
  fileUrl,
  overlays,
}: {
  fileUrl: string
  overlays: OverlayBox[]
}) {
  const containerRef = React.useRef<HTMLDivElement>(null)
  const [containerWidth, setContainerWidth] = React.useState(MAX_PAGE_WIDTH)
  const [numPages, setNumPages] = React.useState(0)
  const [pageNumber, setPageNumber] = React.useState(1)
  const [pageSize, setPageSize] = React.useState<{ width: number; height: number } | null>(null)
  const [loadError, setLoadError] = React.useState(false)

  React.useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width
      if (width) setContainerWidth(Math.min(MAX_PAGE_WIDTH, Math.floor(width)))
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // Jump to whatever page the newest set of overlays actually lands on
  // — a reviewer expanding a flagged check shouldn't have to go
  // hunting through a multi-page document to find what it's pointing at.
  React.useEffect(() => {
    if (overlays.length > 0) {
      setPageNumber(overlays[0].box.page)
    }
  }, [overlays])

  const pageOverlays = overlays.filter((o) => o.box.page === pageNumber)
  const legendColors = Array.from(new Set(overlays.map((o) => o.color)))

  return (
    <div ref={containerRef} className="flex flex-col gap-2 rounded-lg border bg-background p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="size-7 shrink-0"
            disabled={pageNumber <= 1}
            onClick={() => setPageNumber((p) => Math.max(1, p - 1))}
          >
            <ChevronLeftIcon className="size-3.5" />
          </Button>
          <span className="text-xs tabular-nums text-muted-foreground whitespace-nowrap">
            Page {pageNumber} of {numPages || "…"}
          </span>
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="size-7 shrink-0"
            disabled={numPages === 0 || pageNumber >= numPages}
            onClick={() => setPageNumber((p) => Math.min(numPages, p + 1))}
          >
            <ChevronRightIcon className="size-3.5" />
          </Button>
        </div>
        {legendColors.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 sm:gap-3 text-[11px] text-muted-foreground">
            {legendColors.map((color) => (
              <span key={color} className="flex items-center gap-1.5 shrink-0">
                <span className={cn("size-2.5 rounded-sm border-2", OVERLAY_COLOR_CLASSES[color])} />
                {OVERLAY_COLOR_LEGEND[color]}
              </span>
            ))}
          </div>
        )}
      </div>

      {loadError ? (
        <div className="p-8 text-center text-sm text-destructive">
          Couldn't load this file for preview.
        </div>
      ) : (
        <div
          className="relative mx-auto overflow-hidden rounded-md border bg-muted/20 max-w-full"
          style={pageSize ? { width: Math.min(containerWidth, pageSize.width), height: pageSize.height } : undefined}
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
              onRenderSuccess={(page) => setPageSize({ width: page.width, height: page.height })}
            />
          </Document>
          {pageOverlays.map((overlay, i) => (
            <div
              key={i}
              title={overlay.label}
              className={cn(
                "pointer-events-none absolute rounded-sm border-2",
                OVERLAY_COLOR_CLASSES[overlay.color]
              )}
              style={{
                left: `${overlay.box.x * 100}%`,
                top: `${overlay.box.y * 100}%`,
                width: `${overlay.box.width * 100}%`,
                height: `${overlay.box.height * 100}%`,
              }}
            />
          ))}
        </div>
      )}
    </div>
  )
}
