// Mirrors backend/app/models/case.py (CaseType, CaseStatus, RiskTier) and
// backend/app/schemas/case.py / document.py. Keep in sync by hand — see
// SPECIFICATION.md section 2 (Forms: React Hook Form + Zod, "schema validation
// mirrored from backend Pydantic models").

  export const CASE_TYPES = [
    "school_document",
    "vendor_invoice",
    "commercial_invoice",
    "procurement_documentation",
    "quotation",
    "travel_reimbursement",
    "other",
] as const

export type CaseType = (typeof CASE_TYPES)[number]

export const CASE_TYPE_LABELS: Record<CaseType, string> = {
  school_document: "School / educational document",
  vendor_invoice: "Vendor invoice",
  commercial_invoice: "Commercial invoice",
  procurement_documentation: "Procurement documentation",
  quotation: "Quotation",
  travel_reimbursement: "Travel / accommodation reimbursement",
  other: "Other",
}

export const CASE_STATUSES = [
  "submitted",
  "under_automated_review",
  "pending_manual_review",
  "auto_approved",
  "escalated",
  "under_investigation",
  "approved",
  "rejected",
  "closed",
] as const

// Mirrors backend/app/services/classification_service.py's
// DOCUMENT_TYPE_LABELS — the per-DOCUMENT classification list. Distinct
// from CaseType above: a case is filed as one type (e.g. "vendor
// invoice"), but in practice its documents split into the claim itself
// (an invoice/quotation/etc) plus one or more documents proving it (a
// payment slip, bank confirmation, or confirmation email) — see
// getDocumentRole below.
export const DOCUMENT_TYPE_LABELS: Record<string, string> = {
  school_document: "School / educational document",
  vendor_invoice: "Vendor invoice",
  commercial_invoice: "Commercial invoice",
  procurement_documentation: "Procurement documentation",
  quotation: "Quotation",
  travel_invoice: "Travel / accommodation invoice",
  payment_evidence: "Payment evidence",
  other: "Other",
}

export type DocumentRole = "claim" | "evidence" | "other" | "pending"

// Which side of the claim/evidence split a document_type falls on. Users
// upload every supporting file for a case in one go and only pick the
// case's own type once (see frontend/src/pages/NewCasePage.tsx) — they
// don't tag each file — so this is how the case detail page identifies,
// per document, "this is the invoice" vs. "this is its evidence" instead
// of making a reviewer read each document_type value themselves.
export function getDocumentRole(documentType: string | null): DocumentRole {
  if (documentType === null) return "pending"
  if (documentType === "payment_evidence") return "evidence"
  if (documentType === "other") return "other"
  return "claim"
}

export type CaseStatus = (typeof CASE_STATUSES)[number]

export const CASE_STATUS_LABELS: Record<CaseStatus, string> = {
  submitted: "Submitted",
  under_automated_review: "Under automated review",
  pending_manual_review: "Pending manual review",
  auto_approved: "Auto-approved",
  escalated: "Escalated",
  under_investigation: "Under investigation",
  approved: "Approved",
  rejected: "Rejected",
  closed: "Closed",
}

export type RiskTier = "low" | "medium" | "high"

export interface Case {
  id: string
  case_number: string
  case_type: CaseType
  status: CaseStatus
  risk_tier: RiskTier | null
  submitted_by_user_id: string
  created_at: string
}

export interface CaseDocument {
  id: string
  case_id: string
  original_filename: string
  content_type: string | null
  file_size_bytes: number | null
  file_hash: string
  file_url: string
  uploaded_at: string
}

export interface UserSummary {
  id: string
  email: string
  full_name: string | null
}

// Mirrors backend/app/services/case_flag_service.py's CaseFlag: the case's
// REAL risk tier from its latest risk assessment (low/medium/high), or
// "pending" until its automated checks have finished and it has been scored.
export type CaseFlagType = "low" | "medium" | "high" | "pending"

export interface CaseFlag {
  flag: CaseFlagType
  label: string
  // The highest-weight reason, plus a "(+N more signals)" count.
  description: string
  // 0-100; null while pending.
  score: number | null
}

// Which reviewer tier owns the case — NOT a status (backend/app/models/case.py
// CaseTier). Escalation moves a case from l1 to l2, one-directionally.
export type CaseTier = "l1" | "l2"

export interface CaseListItem {
  id: string
  case_number: string
  case_type: CaseType
  status: CaseStatus
  assigned_tier: CaseTier
  risk_tier: RiskTier | null
  submitted_by: UserSummary
  created_at: string
  document_count: number
  flag: CaseFlag
  // Whether the current user may approve/reject/escalate given the tier
  // (false for a Reviewer L1 on an L2 case — read-only). Server-computed.
  can_act: boolean
}

export type DocumentProcessingStatus = "pending" | "processing" | "complete" | "failed"

// One plain-string extracted field, per SPECIFICATION.md section 3.6's
// confidence/uncertain flag requirement (backend/app/services/
// llm_service.py's FieldValue). Used for issuer/reference_number, and
// for every additional_fields entry (those are never normalized — see
// AmountFieldValue/DateFieldValue below for the core fields that are).
export interface ExtractedFieldValue {
  value: string | null
  confidence: number
  uncertain: boolean
}

export interface ExtractedAdditionalField extends ExtractedFieldValue {
  field_name: string
}

// A core field normalized at extraction time (backend/app/services/
// llm_service.py — Arabic-Indic numerals, mixed date formats, etc. are
// resolved once, there, rather than left for downstream code to
// re-parse). `raw_text` is the original as seen, kept for display only —
// never re-derive anything from it on this side either.
export interface DateFieldValue {
  value: string | null // ISO 8601 (YYYY-MM-DD), or null
  raw_text: string | null
  confidence: number
  uncertain: boolean
}

export interface NumericFieldValue {
  value: number | null
  raw_text: string | null
  confidence: number
  uncertain: boolean
}

export interface AmountFieldValue extends NumericFieldValue {
  currency: string | null // ISO 4217 3-letter code, e.g. "AED", "SAR", "USD"
}

export interface CoreFields {
  issuer: ExtractedFieldValue
  reference_number: ExtractedFieldValue
  date: DateFieldValue
  // The grand total actually due/paid (tax-inclusive, when tax applies).
  amount: AmountFieldValue
  subtotal: AmountFieldValue
  tax_amount: AmountFieldValue
  tax_rate: NumericFieldValue
}

// Shape written by the OCR + classification + extraction Celery task
// (backend/app/tasks/document_processing.py) into
// documents.extracted_fields. Only present once processing_status is
// "complete".
export interface ExtractedFields {
  document_type_confidence: number
  core_fields: CoreFields
  additional_fields: ExtractedAdditionalField[]
}

// One document_checks row (backend/app/models/document_check.py). `result`
// is whatever shape that check_type produces — see the individual check
// services (e.g. backend/app/services/field_validation_service.py) — so
// this stays loosely typed and the UI renders it generically rather than
// assuming a fixed shape per check_type.
export type DocumentCheckType =
  | "ocr_layout"
  | "classification"
  | "field_extraction"
  | "field_validation"
  | "issuer_verification"
  | "signature_stamp_detection"
  | "signature_stamp_verification"
  | "metadata_forensics"
  | "error_level_analysis"
  | "copy_move_detection"
  | "duplicate_detection"
  | "ai_content_detection"
  | "visual_inconsistency_review"
  | "font_consistency"
  | "ghost_content"
  // Not a real backend/app/models/document_check.py row — synthesized in
  // frontend/src/components/case/DocumentChecksPanel.tsx from the case's
  // cross_document_findings (case-level, not per-document) so it reads
  // alongside the real per-document checks as one more entry rather than
  // a separately-shaped section.
  | "cross_document_consistency"

export const DOCUMENT_CHECK_TYPE_LABELS: Record<DocumentCheckType, string> = {
  ocr_layout: "OCR / layout extraction",
  classification: "Document classification",
  field_extraction: "Field extraction",
  field_validation: "Date / amount / reference validation",
  issuer_verification: "Issuer verification",
  signature_stamp_detection: "Signature / stamp detection",
  // Synthesized check entry for in-case signature comparison results —
  // not a real document_checks row; built in DocumentChecksPanel from
  // the signature_matches API response.
  signature_stamp_verification: "Signature comparison (advisory)",
  metadata_forensics: "Metadata forensics",
  error_level_analysis: "Error level analysis",
  copy_move_detection: "Copy-move forgery detection",
  duplicate_detection: "Duplicate detection",
  ai_content_detection: "AI-generated content detection",
  // Covers SPECIFICATION.md's "visual inconsistency review" AND "AI-generated
  // content detection" questions in one check — see backend/app/
  // services/visual_inconsistency_service.py's module docstring for
  // why (Azure AI Content Safety has no AI-generated-content detection
  // capability, so that question is folded into this same vision-model
  // call instead of being its own check/vendor). "(experimental)" in
  // the label itself, not just buried in the findings text, per
  // SPECIFICATION.md section 4's "do not overstate ... AI-generated-document-
  // detection accuracy" rule — a reviewer should see the caveat before
  // they even expand the check.
  visual_inconsistency_review: "Visual inconsistency review (experimental)",
  font_consistency: "Font consistency",
  ghost_content: "Deleted / replaced content (ghost text)",
  cross_document_consistency: "Cross-document consistency",
}

// Normalized (0-1 fraction of page width/height) bounding box, per
// backend/app/services/forensics/ela.py and copy_move.py — deliberately
// NOT pixel coordinates, so the frontend overlay (components/case/
// PdfOverlayViewer.tsx) scales it correctly at any render zoom/
// resolution without needing to match the backend's render DPI.
export interface BoundingBox {
  page: number // 1-based
  x: number
  y: number
  width: number
  height: number
}

// A highlight region for a rule-based field exception (backend/app/services/
// field_regions.py). The box is where the field's value was located on the
// page — approximate, and absent when the field couldn't be located.
export interface FieldRegion {
  field: string
  label: string
  value: string
  caption: string
  bounding_box: BoundingBox
}

export interface DocumentCheckSubResult {
  status: "pass" | "flag" | "skipped" | string
  reason: string
  regions?: FieldRegion[]
}

export interface DocumentCheck {
  id: string
  check_type: DocumentCheckType
  status: "pending" | "running" | "completed" | "failed"
  // Every check_type shares {result: "pass"|"flag"|"not_applicable"|"not_checked"|"limited",
  // details: ...}, but `details` itself varies: a list of Finding dicts
  // for the forensic checks (metadata_forensics/error_level_analysis/
  // copy_move_detection), a dict of named {status, reason} sub-checks for
  // field_validation, or a small fixed-key dict for issuer_verification.
  // See frontend/src/components/case/DocumentChecksPanel.tsx, which
  // renders all three generically rather than assuming one shape.
  result: Record<string, unknown> | null
  error_message: string | null
  // Short, readable form of `result` (backend/app/services/check_summaries.py):
  // what the card shows first. Absent on the synthesized cross-document entry.
  summary?: CheckSummary | null
  created_at: string
}

export interface CaseDetailDocument {
  id: string
  original_filename: string
  document_type: string | null
  processing_status: DocumentProcessingStatus
  extracted_fields: ExtractedFields | null
  processing_error: string | null
  content_type: string | null
  file_size_bytes: number | null
  file_hash: string
  // Short-lived signed URL — fetch a fresh case detail if it expires.
  file_url: string
  uploaded_at: string
  checks: DocumentCheck[]
}

// One cross_document_findings row (backend/app/models/cross_document_
// finding.py) — case-level, not per-document (SPECIFICATION.md section 3.1).
export interface CrossDocumentFinding {
  id: string
  field_name: string
  finding_type: string
  severity: "info" | "low" | "medium" | "high"
  description: string
  document_ids: string[] | null
  created_at: string
  // One region per involved document whose field location is known, each
  // captioned with what the OTHER document showed (see backend/app/services/
  // field_exception_service.py). Drawn on that document's page.
  regions: CrossDocumentRegion[]
}

export interface CrossDocumentRegion extends FieldRegion {
  document_id: string
  document_filename: string
  other: { document_id: string; document_filename: string; value: string }[]
}

// One medium/high-severity finding from a flagged metadata_forensics/
// error_level_analysis/copy_move_detection check (backend/app/services/
// case_flag_service.py's get_forensic_findings) — the per-document
// counterpart to CrossDocumentFinding above, both rendered together in
// the case detail "Explainable findings" panel. Temporary stand-in for
// the real risk-scoring engine, same as CaseFlag itself.
export interface ForensicFinding {
  document_id: string
  document_filename: string
  check_type: string
  check_type_label: string
  finding: string
  severity: "info" | "low" | "medium" | "high"
  description: string
}

// One fired risk rule, frozen at scoring time (backend/app/schemas/case.py
// RiskReasonSchema): rendered text + the weight/version that produced it.
export interface CheckSummaryItem {
  severity: "info" | "low" | "medium" | "high" | string
  title: string
  // One line with the actual values ("1,450.00 · 16,450.00 … in a second copy of …").
  text: string
  // The check's full explanation, behind "Why?".
  detail: string
  page: number | null
  findings: string[]
  image_png_base64?: string | null
  hint?: { kind?: string; heading?: string; legible_words?: string[]; confidence?: string; answers_agree?: boolean } | null
}

export interface CheckSummary {
  headline: string
  items: CheckSummaryItem[]
  notes: string[]
}

export interface RiskReason {
  rule_id: string
  rule_version: number
  category: string
  check_type: string
  severity: "low" | "medium" | "high" | string
  weight: number
  reason: string
  document_id: string | null
  document_filename: string | null
  // Short form: a few-word title and one line with the values.
  title?: string
  short?: string
}

export interface RiskAssessment {
  id: string
  score: number
  raw_score: number
  tier: RiskTier
  computed_at: string
  triggered_reasons: RiskReason[]
  thresholds: { medium?: number; high?: number }
  // Rule families whose points were capped together when scored.
  group_caps?: Record<string, { cap: number; points: number }>
}

// Whether every automated check has finished; `pending` says what hasn't.
export interface PipelineStatus {
  complete: boolean
  pending: string[]
}

// A reviewer decision/note (approve / reject / escalate) on a case.
export interface CaseAction {
  id: string
  action_type: "approve" | "reject" | "escalate" | string
  actor_name: string | null
  // Role label when they acted, e.g. "Reviewer L1".
  actor_role: string | null
  notes: string | null
  created_at: string
}

// One generated per-case PDF report (backend/app/api/case_reports.py). A
// separate, point-in-time artifact — findings burned into rendered page
// copies inside the report; the live overlays and the original file are
// untouched. `download_url` is a short-lived signed URL.
export interface CaseReport {
  id: string
  case_id: string
  generated_by_name: string | null
  generated_at: string
  file_size_bytes: number
  page_count: number
  report_sha256: string
  risk_tier: RiskTier | null
  risk_score: number | null
  download_url: string
}

export interface CaseDecisionResponse {
  case_id: string
  status: CaseStatus
  assigned_tier: CaseTier
  action: CaseAction
}

export interface CaseDetail extends CaseListItem {
  documents: CaseDetailDocument[]
  forensic_findings: ForensicFinding[]
  cross_document_findings: CrossDocumentFinding[]
  assessment: RiskAssessment | null
  pipeline: PipelineStatus
  actions: CaseAction[]
}

export interface CaseListFilters {
  status?: CaseStatus
  case_type?: CaseType
}

// One audit_log row scoped to a case (backend/app/api/cases.py's
// GET /cases/{id}/audit-log) — powers the case detail page's activity
// timeline. Every event_type below is a real one written by app/services/
// audit_service.py's call sites; this label map only controls display text.
export type AuditEventType =
  | "case_created"
  | "document_uploaded"
  | "document_processing_completed"
  | "document_processing_failed"
  | "document_checks_completed"
  | "metadata_forensics_completed"
  | "metadata_forensics_failed"
  | "tampering_checks_completed"
  | "tampering_checks_failed"
  | "visual_inconsistency_review_completed"
  | "visual_inconsistency_review_failed"
  | "font_consistency_completed"
  | "font_consistency_failed"
  | "cross_document_check_completed"

// Event labels live in lib/audit.ts (shared with the system Audit History page).
export { AUDIT_EVENT_LABELS } from "@/lib/audit"

export interface AuditLogEntry {
  id: string
  event_type: string
  actor_name: string | null
  document_id: string | null
  event_data: Record<string, unknown> | null
  created_at: string
}

// Mirrors backend/app/models/signature_reference.py.
// `is_library=false` = in-case reference; `true` = library (data-collected only).
export interface SignatureReference {
  id: string
  person_name: string
  signature_image_url: string | null
  bounding_box: BoundingBox | null
  source_document_id: string
  source_case_id: string
  created_by: string
  is_library: boolean
  created_at: string
}

// Result of a signature comparison — advisory language. The first four come
// from the vision-model comparison; the last two from a classical pixel
// comparison ("identical" = the same image file, not a fresh signing).
// Mirrors backend/app/models/signature_match.py SignatureMatchResult enum.
// "verified" / "confirmed" / "authenticated" intentionally absent.
export type SignatureMatchResult =
  | "consistent"
  | "possibly_consistent"
  | "inconsistent"
  | "cannot_determine"
  | "identical_reuse"
  | "reused_different_signer"

// Human-readable labels — advisory framing per SPECIFICATION.md §2.3/§4.
export const SIGNATURE_MATCH_RESULT_LABELS: Record<SignatureMatchResult, string> = {
  consistent: "Visually consistent with reference on file",
  possibly_consistent: "Possibly consistent with reference on file",
  inconsistent: "Visual appearance inconsistent with reference",
  cannot_determine: "Cannot determine — insufficient image quality",
  identical_reuse: "Pixel-identical to reference — same image appears to be reused",
  reused_different_signer: "Pixel-identical to reference, but printed signer differs",
}

// Mirrors backend/app/schemas/signature.py SignatureMatchResponse.
export interface SignatureMatch {
  id: string
  document_id: string
  case_id: string
  signature_reference_id: string
  reference_person_name: string
  comparison_scope: "in_case" | "library"
  result: SignatureMatchResult
  result_label: string
  reasoning: string | null
  compared_at: string
}

// Request body for POST /cases/{id}/documents/{docId}/signature-references
export interface SignatureReferenceCreatePayload {
  person_name: string
  bounding_box: {
    page: number
    x: number
    y: number
    width: number
    height: number
  }
  is_library: boolean
}
