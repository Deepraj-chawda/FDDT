import * as React from "react"
import { ArrowUpRightIcon, ChevronDownIcon } from "lucide-react"
import { Link } from "react-router-dom"

import { Badge } from "@/components/ui/badge"
import type { OverlayBox, OverlayColor } from "@/components/case/PdfOverlayViewer"
import { cn } from "@/lib/utils"
import {
  DOCUMENT_CHECK_TYPE_LABELS,
  SIGNATURE_MATCH_RESULT_LABELS,
  type BoundingBox,
  type CheckSummary,
  type CheckSummaryItem,
  type CrossDocumentFinding,
  type DocumentCheck,
  type FieldRegion,
  type SignatureMatch,
} from "@/types/case"

/**
 * Renders every backend/app/models/document_check.py row for the active
 * document generically — field_validation, issuer_verification, and the
 * forensic checks (metadata_forensics, error_level_analysis,
 * copy_move_detection) all share the same top-level {result, details}
 * shape (see the individual services under app/services/forensics/), but
 * `details` itself comes in three real shapes depending on check_type:
 *  - a list of Finding dicts (the forensic checks) — FindingsList below
 *  - a dict of named sub-checks {status, reason} (field_validation) — SubCheckList
 *  - a small fixed-key dict (issuer_verification) — KeyValueList fallback
 * Nothing here is synthesized: a check that hasn't run yet, or has no
 * findings, says so rather than showing fabricated content.
 */

interface Finding {
  finding: string
  severity: "info" | "low" | "medium" | "high" | string
  description: string
  page?: number
  bounding_box?: BoundingBox
  data?: Record<string, unknown>
}

type SubCheck = { status: string; reason: string; regions?: FieldRegion[] }

const SEVERITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2, info: 3 }

const SEVERITY_DOT_CLASSES: Record<string, string> = {
  high: "bg-destructive",
  medium: "bg-warning",
  low: "bg-info",
  info: "bg-muted-foreground",
}

const RESULT_LABELS: Record<string, string> = {
  pass: "Pass",
  flag: "Flagged",
  not_applicable: "Not applicable",
  not_checked: "Not checked",
  limited: "Limited",
}

const SUB_CHECK_STATUS_DOT: Record<string, string> = {
  pass: "bg-success",
  flag: "bg-destructive",
  skipped: "bg-muted-foreground",
  not_checked: "bg-muted-foreground",
}

// Overlay color per check_type, matching PdfOverlayViewer's own legend
// (ela.py = red/destructive, copy_move.py = orange/warning,
// visual_inconsistency_service.py = dashed/"ai" — a vision-model
// judgment, not pixel-level analysis, drawn distinctly on purpose).
// getCheckOverlays below only pushes a box when a finding actually has
// one (backend/app/services/visual_inconsistency_service.py omits
// bounding_box entirely when the model couldn't localize a finding),
// so a non-localizable finding shows only its text description in the
// Checks panel below, with no overlay drawn.
const OVERLAY_COLOR_BY_CHECK_TYPE: Partial<Record<DocumentCheck["check_type"], OverlayColor>> = {
  error_level_analysis: "destructive",
  copy_move_detection: "warning",
  visual_inconsistency_review: "ai",
  font_consistency: "font",
  ghost_content: "ghost",
}

// Ghost-content findings that are only notes (the pages searched, a trace
// set aside as show-through or as a signature/stamp's ink) are listed but
// not drawn: only deleted / shortened content gets a box on the page.
const GHOST_DRAWN_FINDINGS = new Set(["ghost_deleted_block", "ghost_replaced_line"])

function isFindingArray(details: unknown): details is Finding[] {
  return Array.isArray(details)
}

// backend/app/services/forensics/duplicate_check.py's near_duplicate_page
// finding — deliberately cross-case (the whole point is catching the same
// document resubmitted into a DIFFERENT case), so its `data` carries the
// matched document's own case id/number for a direct link out, rather
// than leaving the reviewer to go search for it by name.
interface DuplicateMatchData {
  matched_case_id: string
  matched_case_number: string
  matched_document_filename: string
}

function getDuplicateMatchData(finding: Finding): DuplicateMatchData | null {
  if (finding.finding !== "near_duplicate_page" || !finding.data) return null
  const { matched_case_id, matched_case_number, matched_document_filename } = finding.data
  if (
    typeof matched_case_id === "string" &&
    typeof matched_case_number === "string" &&
    typeof matched_document_filename === "string"
  ) {
    return { matched_case_id, matched_case_number, matched_document_filename }
  }
  return null
}

// Shared with the center-column PdfOverlayViewer (frontend/src/pages/
// CaseDetailPage.tsx) so the real ELA/copy-move bounding boxes drawn over
// the PDF and the findings listed here always agree — both read the same
// document_checks rows, nothing is computed twice differently.
//
// Field exceptions (solid purple, the "field" color) come from two places:
// a flagged field-validation sub-check carries the regions of the fields it
// flagged, and a medium/high cross-document finding carries one region per
// involved document — only the ones on `documentId` are drawn here, so each
// document shows its own side of the mismatch. Live overlays only: nothing is
// ever burned into the stored file (only the exported PDF report does that).
export function getCheckOverlays(
  checks: DocumentCheck[],
  crossDocumentFindings: CrossDocumentFinding[] = [],
  documentId?: string,
): OverlayBox[] {
  const boxes: OverlayBox[] = []
  for (const check of checks) {
    if (check.check_type === "field_validation" && check.status === "completed") {
      const details = check.result?.details
      if (details && typeof details === "object" && !Array.isArray(details)) {
        for (const sub of Object.values(details as Record<string, SubCheck>)) {
          if (sub?.status !== "flag") continue
          for (const region of sub.regions ?? []) {
            boxes.push({ box: region.bounding_box, color: "field", label: region.caption })
          }
        }
      }
    }
  }
  if (documentId) {
    for (const finding of crossDocumentFindings) {
      if (finding.severity !== "medium" && finding.severity !== "high") continue
      for (const region of finding.regions ?? []) {
        if (region.document_id === documentId) {
          boxes.push({ box: region.bounding_box, color: "field", label: region.caption })
        }
      }
    }
  }
  for (const check of checks) {
    const color = OVERLAY_COLOR_BY_CHECK_TYPE[check.check_type]
    if (!color || check.status !== "completed") continue
    const details = check.result?.details
    if (!isFindingArray(details)) continue
    for (const finding of details) {
      if (check.check_type === "ghost_content" && !GHOST_DRAWN_FINDINGS.has(finding.finding)) continue
      if (finding.bounding_box) {
        boxes.push({ box: finding.bounding_box, color, label: finding.description })
      }
    }
  }
  return boxes
}

function isSubCheckDict(details: unknown): details is Record<string, SubCheck> {
  if (!details || typeof details !== "object" || Array.isArray(details)) return false
  const values = Object.values(details as Record<string, unknown>)
  return values.length > 0 && values.every((v) => v && typeof v === "object" && "status" in v && "reason" in v)
}

function FindingsList({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) {
    return <p className="text-[11px] text-muted-foreground">No findings recorded for this check.</p>
  }
  const sorted = [...findings].sort(
    (a, b) => (SEVERITY_ORDER[a.severity] ?? 4) - (SEVERITY_ORDER[b.severity] ?? 4)
  )
  return (
    <ul className="space-y-2">
      {sorted.map((f, i) => {
        const duplicateMatch = getDuplicateMatchData(f)
        return (
          <li key={i} className="flex items-start gap-2">
            <span
              className={cn(
                "mt-1.5 size-2 shrink-0 rounded-full",
                SEVERITY_DOT_CLASSES[f.severity] ?? "bg-muted-foreground"
              )}
            />
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold text-foreground capitalize break-words [overflow-wrap:anywhere]">{f.finding.replace(/_/g, " ")}</p>
              <p className="text-[11px] leading-snug text-muted-foreground break-words [overflow-wrap:anywhere]">{f.description}</p>
              <GhostCrop finding={f} />
              {duplicateMatch && (
                <Link
                  to={`/cases/${duplicateMatch.matched_case_id}`}
                  className="mt-1 inline-flex items-center gap-1 text-[11px] font-semibold text-blue-600 hover:underline"
                >
                  View matched case {duplicateMatch.matched_case_number}
                  <ArrowUpRightIcon className="size-3" />
                </Link>
              )}
            </div>
          </li>
        )
      })}
    </ul>
  )
}

// The ghost-content check's enhanced crop of a faint trace (the scan,
// contrast-stretched): the trace is too faint to see on the page itself.
function GhostCrop({ finding }: { finding: Finding }) {
  const crop = finding.data?.crop_png_base64
  if (typeof crop !== "string" || !crop) return null
  const hint = typeof finding.data?.hint === "string" ? finding.data.hint : ""
  return (
    <figure className="mt-1.5">
      <img
        src={`data:image/png;base64,${crop}`}
        alt="Enhanced trace of the erased text"
        className="max-h-40 max-w-full rounded border border-teal-600/60 bg-white"
      />
      <figcaption className="mt-0.5 text-[10px] text-muted-foreground">
        Enhanced scan background{hint ? ` — OCR best guess: “${hint}”` : ""}
      </figcaption>
    </figure>
  )
}

// The short form of a check (backend/app/services/check_summaries.py): one
// line per problem with the values, the check's full explanation behind
// "Why?", then short notes. The raw findings stay under "Technical details".
function SummaryItem({ item }: { item: CheckSummaryItem }) {
  const [open, setOpen] = React.useState(false)
  const detailId = React.useId()
  const hint = item.hint
  return (
    <li className="flex items-start gap-2">
      <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", SEVERITY_DOT_CLASSES[item.severity] ?? "bg-muted-foreground")} />
      <div className="min-w-0 flex-1">
        <p className="text-xs leading-snug text-foreground break-words [overflow-wrap:anywhere]">
          <span className="font-semibold">{item.title}</span>
          <span className="text-muted-foreground"> — {item.text}</span>
          {item.detail && (
            <button
              type="button"
              aria-expanded={open}
              aria-controls={detailId}
              onClick={() => setOpen((v) => !v)}
              className="ml-1.5 inline-block font-semibold text-blue-600 hover:underline"
            >
              {open ? "Hide" : "Why?"}
            </button>
          )}
        </p>
        {open && <p id={detailId} className="mt-1 whitespace-pre-line text-[11px] leading-snug text-muted-foreground break-words [overflow-wrap:anywhere]">{item.detail}</p>}
        {item.image_png_base64 && (
          <figure className="mt-1.5">
            <img
              src={`data:image/png;base64,${item.image_png_base64}`}
              alt="Enhanced trace of the erased text"
              className="max-h-40 max-w-full rounded border border-teal-600/60 bg-white"
            />
            <figcaption className="mt-0.5 text-[10px] text-muted-foreground">
              Scan background, contrast-stretched
              {hint && (hint.heading || hint.legible_words?.length)
                ? ` — the vision model read${hint.heading ? ` the heading “${hint.heading}”` : ""}${
                    hint.legible_words?.length ? ` the words ${hint.legible_words.join(", ")}` : ""
                  } (a guess, not evidence)`
                : ""}
            </figcaption>
          </figure>
        )}
      </div>
    </li>
  )
}

function SummaryView({ summary }: { summary: CheckSummary }) {
  return (
    <div className="space-y-2">
      {summary.items.length > 0 && (
        <ul className="space-y-2">
          {summary.items.map((item, i) => (
            <SummaryItem key={i} item={item} />
          ))}
        </ul>
      )}
      {summary.notes.length > 0 && (
        <ul className="space-y-0.5">
          {summary.notes.map((note, i) => (
            <li key={i} className="text-[11px] leading-snug text-muted-foreground">· {note}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function SubCheckList({ details }: { details: Record<string, SubCheck> }) {
  return (
    <ul className="space-y-2">
      {Object.entries(details).map(([name, sub]) => (
        <li key={name} className="flex items-start gap-2">
          <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", SUB_CHECK_STATUS_DOT[sub.status] ?? "bg-muted-foreground")} />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-semibold text-foreground capitalize break-words [overflow-wrap:anywhere]">{name.replace(/_/g, " ")}</p>
            <p className="text-[11px] leading-snug text-muted-foreground break-words [overflow-wrap:anywhere]">{sub.reason}</p>
          </div>
        </li>
      ))}
    </ul>
  )
}

function KeyValueList({ details }: { details: Record<string, unknown> }) {
  return (
    <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5">
      {Object.entries(details).map(([key, value]) => (
        <div key={key} className="min-w-0">
          <dt className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">
            {key.replace(/_/g, " ")}
          </dt>
          <dd className="truncate text-xs font-medium text-foreground">
            {value === null || value === undefined || value === "" ? "—" : String(value)}
          </dd>
        </div>
      ))}
    </dl>
  )
}

// backend/app/services/signature_detection_service.py's result shape:
// details = { signature_expected, detected: [{kind, description, confidence,
// bounding_box}], bounding_box }. Presence/placement only — worded that way
// on purpose (SPECIFICATION.md §2.3: never imply verification).
interface SignatureDetectionDetails {
  signature_expected?: boolean
  detected?: { kind: string; description: string; text?: string; confidence: string; bounding_box: BoundingBox }[]
}

// Field validation's stamp-vs-issuer sub-check is stored with field validation
// (that is what the risk rule reads) but shown here, beside the stamp it is about.
const STAMP_ISSUER_SUB_CHECK = "stamp_issuer_consistency"

function isSignatureDetectionDetails(details: unknown): details is SignatureDetectionDetails {
  return !!details && typeof details === "object" && !Array.isArray(details) && "detected" in details
}

function StampIssuerRow({ sub }: { sub: SubCheck }) {
  return (
    <li className="flex items-start gap-2">
      <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", SUB_CHECK_STATUS_DOT[sub.status] ?? "bg-muted-foreground")} />
      <div className="min-w-0 flex-1">
        <p className="text-xs font-semibold text-foreground break-words [overflow-wrap:anywhere]">Stamp names the issuer</p>
        <p className="text-[11px] leading-snug text-muted-foreground break-words [overflow-wrap:anywhere]">{sub.reason}</p>
      </div>
    </li>
  )
}

function SignatureDetectionSummary({ details, stampIssuer }: { details: SignatureDetectionDetails; stampIssuer?: SubCheck }) {
  const detected = details.detected ?? []
  if (detected.length === 0) {
    return (
      <p className="text-[11px] leading-snug text-muted-foreground break-words [overflow-wrap:anywhere]">
        {details.signature_expected
          ? "No signature or stamp was located, though this kind of document would normally carry one. Worth a manual look."
          : "No signature or stamp was located; none is normally expected on this kind of document."}
      </p>
    )
  }
  return (
    <ul className="space-y-2">
      {detected.map((d, i) => (
        <li key={i} className="flex items-start gap-2">
          <span className="mt-1.5 size-2 shrink-0 rounded-full bg-muted-foreground" />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-semibold text-foreground capitalize break-words [overflow-wrap:anywhere]">
              {d.kind} located · page {d.bounding_box.page}
            </p>
            <p className="text-[11px] leading-snug text-muted-foreground break-words [overflow-wrap:anywhere]">
              {d.description} ({d.confidence} confidence). Presence and placement only — not a
              check of who signed it or whether it is genuine.
            </p>
            {d.kind === "stamp" && d.text && (
              <p className="mt-0.5 text-[11px] leading-snug text-foreground break-words [overflow-wrap:anywhere]">Stamp reads: “{d.text}”</p>
            )}
          </div>
        </li>
      ))}
      {stampIssuer && <StampIssuerRow sub={stampIssuer} />}
    </ul>
  )
}

// Why a check is "not checked" (issuer registry) or "limited" (pixel checks on
// vector text drawn over a scan) — shown on hover over the result badge.
function resultTooltip(result: string, details: unknown): string | undefined {
  if (result === "not_checked") return String((details as Record<string, unknown> | undefined)?.reason ?? "")
  if (result === "limited" && Array.isArray(details)) {
    return (details as Finding[]).find((d) => d?.finding === "pixel_analysis_limited")?.description
  }
  return undefined
}

function CheckCard({ check, stampIssuer }: { check: DocumentCheck; stampIssuer?: SubCheck }) {
  const label = DOCUMENT_CHECK_TYPE_LABELS[check.check_type] ?? check.check_type
  const result = check.result?.result
  const details = check.result?.details
  const needsAttention = check.status === "failed" || result === "flag"
  // Flagged/failed checks open by default so the reviewer sees what's
  // wrong immediately; clean passes start collapsed since there's
  // nothing to act on — the chevron still lets either be toggled.
  const [expanded, setExpanded] = React.useState(needsAttention)
  const [technical, setTechnical] = React.useState(false)
  const summary = check.summary
  const raw =
    isFindingArray(details) ? (
      <FindingsList findings={details} />
    ) : check.check_type === "signature_stamp_detection" && isSignatureDetectionDetails(details) ? (
      <SignatureDetectionSummary details={details} stampIssuer={stampIssuer} />
    ) : isSubCheckDict(details) ? (
      <SubCheckList details={details} />
    ) : details && typeof details === "object" ? (
      <KeyValueList details={details as Record<string, unknown>} />
    ) : (
      <p className="text-[11px] text-muted-foreground">No further detail recorded.</p>
    )

  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center justify-between gap-2 text-left"
        aria-expanded={expanded}
      >
        <span className="flex items-center gap-1.5 text-xs font-bold text-foreground">
          <ChevronDownIcon className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", expanded && "rotate-180")} />
          {label}
        </span>
        {check.status === "completed" && typeof result === "string" ? (
          <Badge
            variant={result === "pass" ? "success" : result === "flag" ? "destructive" : result === "limited" ? "warning" : "outline"}
            title={resultTooltip(result, details)}
          >
            {RESULT_LABELS[result] ?? result}
          </Badge>
        ) : (
          <Badge variant={check.status === "failed" ? "destructive" : "secondary"}>
            {check.status === "pending" ? "Pending" : check.status === "running" ? "Running" : "Failed"}
          </Badge>
        )}
      </button>
      {summary?.headline && (
        <p className={cn("mt-1 pl-5 text-[11px] leading-snug", needsAttention ? "text-foreground" : "text-muted-foreground")}>
          {summary.headline}
        </p>
      )}

      {expanded && (
        <>
          {check.status === "failed" && check.error_message && (
            <p className="mt-2 text-[11px] text-destructive">{check.error_message}</p>
          )}
          {(check.status === "pending" || check.status === "running") && (
            <p className="mt-2 text-[11px] text-muted-foreground">This check hasn't finished running yet.</p>
          )}
          {check.status === "completed" && (
            <div className="mt-2.5">
              {summary ? (
                <>
                  <SummaryView summary={summary} />
                  {check.check_type === "signature_stamp_detection" && isSignatureDetectionDetails(details) ? (
                    <div className="mt-2.5">{raw}</div>
                  ) : (
                    <>
                      <button
                        type="button"
                        onClick={() => setTechnical((v) => !v)}
                        className="mt-2 inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground hover:text-foreground"
                      >
                        <ChevronDownIcon className={cn("size-3 transition-transform", technical && "rotate-180")} />
                        Technical details
                      </button>
                      {technical && <div className="mt-2 border-t border-border pt-2">{raw}</div>}
                    </>
                  )}
                </>
              ) : (
                raw
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}

// cross_document_findings is case-level (backend/app/models/cross_
// document_finding.py), not a document_checks row, but a cross-document
// comparison is conceptually still a check that ran involving this
// document — synthesized here into the same {result, details} shape so
// it renders as one more CheckCard instead of a separately-shaped
// section. `findings` is expected pre-filtered to the ones whose
// document_ids include the active document.
function buildCrossDocumentCheck(findings: CrossDocumentFinding[], hasEnoughDocuments: boolean): DocumentCheck {
  const result: "pass" | "flag" | "not_applicable" = !hasEnoughDocuments
    ? "not_applicable"
    : findings.some((f) => f.severity === "medium" || f.severity === "high")
    ? "flag"
    : "pass"
  return {
    id: "cross-document-consistency",
    check_type: "cross_document_consistency",
    status: "completed",
    result: {
      result,
      details: findings.map((f) => ({
        finding: f.finding_type || "cross_document_discrepancy",
        severity: f.severity,
        description: f.description,
      })),
    },
    error_message: null,
    created_at: new Date(0).toISOString(),
  }
}

// Badge color per signature match result — advisory, not authoritative.
const SIGNATURE_RESULT_BADGE: Record<string, string> = {
  consistent: "bg-success/10 text-success border-success/30",
  possibly_consistent: "bg-warning/10 text-warning border-warning/30",
  inconsistent: "bg-destructive/10 text-destructive border-destructive/30",
  cannot_determine: "bg-muted text-muted-foreground border-border",
  identical_reuse: "bg-warning/10 text-warning border-warning/30",
  reused_different_signer: "bg-destructive/10 text-destructive border-destructive/30",
}

function SignatureMatchesCard({ matches }: { matches: SignatureMatch[] }) {
  const [open, setOpen] = React.useState(true)

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card shadow-card-sm">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-4 py-3 hover:bg-accent/30"
        aria-expanded={open}
      >
        <div className="flex items-center gap-2.5">
          <span
            className="flex size-2 shrink-0 rounded-full bg-muted-foreground"
            aria-hidden="true"
          />
          <span className="text-sm font-semibold text-foreground">
            {DOCUMENT_CHECK_TYPE_LABELS.signature_stamp_verification}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="bg-muted/40 text-muted-foreground text-[10px]">
            {matches.length} result{matches.length === 1 ? "" : "s"}
          </Badge>
          <ChevronDownIcon
            className={cn("size-3.5 text-muted-foreground transition-transform", open && "rotate-180")}
          />
        </div>
      </button>

      {open && (
        <div className="border-t border-border px-4 py-3">
          <p className="mb-3 text-[11px] text-muted-foreground">
            <strong>For reviewer use only.</strong> This is a qualitative visual comparison —
            not an automated verification or identity confirmation. Natural variation in
            handwritten signatures is expected.
          </p>
          <div className="flex flex-col gap-2.5">
            {matches.map((match) => {
              const badgeClass = SIGNATURE_RESULT_BADGE[match.result] ?? SIGNATURE_RESULT_BADGE.cannot_determine
              return (
                <div
                  key={match.id}
                  className="rounded-md border border-border bg-background p-3"
                >
                  <div className="mb-1.5 flex items-start justify-between gap-2">
                    <span className="text-[12px] font-medium text-foreground">
                      Compared to reference: <em>{match.reference_person_name}</em>
                    </span>
                    <Badge
                      variant="outline"
                      className={cn("shrink-0 text-[10px]", badgeClass)}
                    >
                      {SIGNATURE_MATCH_RESULT_LABELS[match.result] ?? match.result_label}
                    </Badge>
                  </div>
                  {match.reasoning && (
                    <p className="text-[11px] text-muted-foreground leading-relaxed">
                      {match.reasoning}
                    </p>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

// The stamp sub-checks of field validation (does the stamp name the issuer,
// is it a real stamp or typed into the file, is it unsigned) are shown with
// signature/stamp detection: taken out of field validation (re-deriving that
// card's badge from its remaining sub-checks), and the signature/stamp card
// shows Flagged when one of them flags. The stamp-vs-issuer one is returned
// for the card's own row.
const STAMP_SUB_CHECKS = [STAMP_ISSUER_SUB_CHECK, "stamp_authenticity", "synthetic_stamp_unsigned"]

function moveStampIssuerCheck(checks: DocumentCheck[]): { checks: DocumentCheck[]; stampIssuer?: SubCheck } {
  const validation = checks.find((c) => c.check_type === "field_validation")
  const details = validation?.result?.details
  if (!validation || !isSubCheckDict(details) || !STAMP_SUB_CHECKS.some((name) => name in details)) return { checks }
  const rest = Object.fromEntries(Object.entries(details).filter(([name]) => !STAMP_SUB_CHECKS.includes(name)))
  const moved = STAMP_SUB_CHECKS.map((name) => details[name]).filter(Boolean)
  const stampIssuer = details[STAMP_ISSUER_SUB_CHECK]
  const anyFlag = Object.values(rest).some((sub) => sub.status === "flag")
  const stampFlag = moved.some((sub) => sub.status === "flag")
  return {
    stampIssuer,
    checks: checks.map((c) => {
      if (c === validation) {
        return { ...c, result: { ...c.result, result: anyFlag ? "flag" : "pass", details: rest } }
      }
      if (c.check_type === "signature_stamp_detection" && stampFlag && c.result?.result === "pass") {
        return { ...c, result: { ...c.result, result: "flag" } }
      }
      return c
    }),
  }
}

export function DocumentChecksPanel({
  checks,
  crossDocumentFindings,
  hasEnoughDocumentsForCrossCheck,
  signatureMatches = [],
}: {
  checks: DocumentCheck[]
  crossDocumentFindings: CrossDocumentFinding[]
  hasEnoughDocumentsForCrossCheck: boolean
  signatureMatches?: SignatureMatch[]
}) {
  const allChecks = [buildCrossDocumentCheck(crossDocumentFindings, hasEnoughDocumentsForCrossCheck), ...checks]
  const { checks: shown, stampIssuer } = moveStampIssuerCheck(allChecks)

  return (
    <div className="space-y-3">
      {shown.map((check) => (
        <CheckCard
          key={check.id}
          check={check}
          stampIssuer={check.check_type === "signature_stamp_detection" ? stampIssuer : undefined}
        />
      ))}
      {signatureMatches.length > 0 && (
        <SignatureMatchesCard matches={signatureMatches} />
      )}
    </div>
  )
}
