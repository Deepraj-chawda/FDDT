// Display helpers for audit_log rows, shared by the system Audit History page
// and the per-case activity timeline so an event reads the same in both.
// Every event type below is one the backend really writes (record_event call
// sites in app/api/*, app/tasks/*, app/services/workflow_service.py); this only
// controls wording and colour — nothing is synthesized.

export type AuditTone = "rose" | "emerald" | "amber" | "blue" | "slate"

export const AUDIT_EVENT_LABELS: Record<string, string> = {
  // case lifecycle
  case_created: "Case submitted",
  document_uploaded: "Document uploaded",
  case_status_changed: "Status changed",
  case_approved: "Case approved",
  case_rejected: "Case rejected",
  case_escalated: "Case escalated to L2",
  // automated pipeline
  document_processing_completed: "OCR / extraction complete",
  document_processing_failed: "OCR / extraction failed",
  document_checks_completed: "Document checks complete",
  metadata_forensics_completed: "Metadata forensics complete",
  metadata_forensics_failed: "Metadata forensics failed",
  tampering_checks_completed: "Tampering checks complete",
  tampering_checks_failed: "Tampering checks failed",
  duplicate_check_completed: "Duplicate check complete",
  duplicate_check_failed: "Duplicate check failed",
  visual_inconsistency_review_completed: "Visual inconsistency review complete",
  visual_inconsistency_review_failed: "Visual inconsistency review failed",
  font_consistency_completed: "Font consistency check complete",
  analysis_integrity_completed: "Analysis integrity check complete",
  analysis_integrity_failed: "Analysis integrity check failed",
  font_consistency_failed: "Font consistency check failed",
  spelling_check_completed: "Spelling check complete",
  spelling_check_failed: "Spelling check failed",
  cross_document_check_completed: "Cross-document check complete",
  signature_stamp_detection_completed: "Signature / stamp detection complete",
  signature_stamp_detection_failed: "Signature / stamp detection failed",
  signature_reference_created: "Reference signature saved",
  signature_comparison_completed: "Signature comparison complete",
  signature_comparison_skipped: "Signature comparison skipped",
  risk_assessment_completed: "Risk assessment scored",
  // admin changes
  risk_rule_updated: "Risk rule changed",
  risk_thresholds_updated: "Risk thresholds changed",
  issuer_created: "Issuer added",
  issuer_updated: "Issuer edited",
  issuer_deactivated: "Issuer deactivated",
  issuer_reactivated: "Issuer reactivated",
  user_updated: "User changed",
  user_password_reset: "Password reset",
  user_password_changed: "Password changed",
  risk_rule_created: "Risk rule added",
  bulk_upload_received: "Bulk upload received",
  bulk_upload_ingested: "Bulk upload ingested",
  case_report_generated: "Case report generated",
  case_discarded: "Empty case discarded",
  // sign-in & sessions
  user_signed_in: "Signed in",
  user_signed_out_everywhere: "Signed out everywhere",
  refresh_token_reuse_detected: "Session token reuse blocked",
  // platform-only (platform admins see these; companies never do)
  platform_admin_access: "Platform admin access",
  case_deleted: "Case deleted (admin)",
  case_restored: "Case restored (admin)",
  user_created: "User created",
  company_created: "Company created",
  company_updated: "Company changed",
  risk_rule_template_created: "Rule template added",
  risk_rule_template_updated: "Rule template changed",
  usage_stats_reconciled: "Usage counters reconciled",
}

export function auditLabel(eventType: string): string {
  const label = AUDIT_EVENT_LABELS[eventType]
  if (label) return label
  // An event with no label yet still reads like its neighbours: "Some event done".
  const words = eventType.replace(/_/g, " ")
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export function auditTone(eventType: string): AuditTone {
  if (
    eventType.endsWith("_failed") ||
    eventType === "case_rejected" ||
    eventType === "case_escalated" ||
    eventType === "issuer_deactivated" ||
    eventType === "case_deleted" ||
    eventType === "refresh_token_reuse_detected"
  ) {
    return "rose"
  }
  if (eventType === "case_approved" || eventType === "issuer_reactivated" || eventType === "case_restored") return "emerald"
  if (
    eventType === "risk_rule_updated" ||
    eventType === "risk_thresholds_updated" ||
    eventType === "user_updated" ||
    eventType === "user_password_reset" ||
    eventType === "user_password_changed" ||
    eventType === "issuer_updated" ||
    eventType === "issuer_created" ||
    eventType === "risk_assessment_completed" ||
    eventType === "risk_rule_created" ||
    eventType === "user_created" ||
    eventType === "company_created" ||
    eventType === "company_updated" ||
    eventType === "risk_rule_template_created" ||
    eventType === "risk_rule_template_updated" ||
    eventType === "platform_admin_access" ||
    eventType === "user_signed_out_everywhere"
  ) {
    return "amber"
  }
  if (eventType === "case_created" || eventType === "document_uploaded" || eventType === "case_status_changed") return "blue"
  return "slate"
}

type Data = Record<string, unknown> | null | undefined

const str = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null)
const num = (v: unknown): number | null => (typeof v === "number" ? v : null)

function changeSummary(changes: unknown): string {
  if (!changes || typeof changes !== "object") return ""
  return Object.entries(changes as Record<string, { from?: unknown; to?: unknown }>)
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${String(v?.from ?? "—")} → ${String(v?.to ?? "—")}`)
    .join("; ")
}

/** A one-line human description of an event, or null if there's nothing
 * worth adding beyond its label. Includes decision reasons/justifications.
 * Platform-only rows (a platform admin's actions) also name the company and
 * the admin, which only platform admins ever receive. */
export function describeAuditEvent(eventType: string, data: Data): string | null {
  const text = describe(eventType, data)
  if (
    !data ||
    data.platform_admin !== true ||
    eventType === "platform_admin_access" ||
    eventType === "case_deleted" ||
    eventType === "case_restored"
  )
    return text
  const who = [str(data.company_name), str(data.admin_email) && `by ${str(data.admin_email)}`].filter(Boolean).join(" · ")
  return [text, who].filter(Boolean).join(" — ") || null
}

function describe(eventType: string, data: Data): string | null {
  if (!data) return null
  switch (eventType) {
    case "platform_admin_access":
      return str(data.summary) ?? `${str(data.admin_email) ?? "Platform admin"} → ${str(data.company_name) ?? "company"} / ${str(data.resource) ?? ""}`
    case "case_deleted":
      return `${str(data.case_number) ?? "Case"} deleted by admin${str(data.reason) ? `. Reason: ${str(data.reason)}` : ""}`
    case "case_restored":
      return `${str(data.case_number) ?? "Case"} restored by admin`
    case "user_created":
      return `${str(data.email) ?? "user"} as ${str(data.role)?.replace(/_/g, " ") ?? "?"}`
    case "user_signed_in": {
      const where = [str(data.ip_address), str(data.user_agent)].filter(Boolean).join(" · ")
      return `${str(data.email) ?? "User"}${where ? ` from ${where}` : ""}`
    }
    case "user_signed_out_everywhere":
      return `${str(data.email) ?? "User"} ended every session`
    case "refresh_token_reuse_detected":
      return `${str(data.email) ?? "User"}: a used session token came back${str(data.ip_address) ? ` from ${str(data.ip_address)}` : ""} — that sign-in was ended`
    case "company_created":
      return str(data.name)
    case "company_updated":
      return `${str(data.name) ?? "company"} — ${changeSummary(data.changes)}`
    case "risk_rule_created":
    case "risk_rule_template_created":
      return `${str(data.rule_id) ?? "rule"} (weight ${num(data.weight) ?? "?"}, ${str(data.severity) ?? "?"})`
    case "risk_rule_template_updated":
      return `${str(data.rule_id) ?? "rule"} — ${changeSummary(data.changes)}`
    case "usage_stats_reconciled":
      return `${num(data.rows_corrected) ?? 0} of ${num(data.rows_checked) ?? 0} counters corrected (${str(data.trigger) ?? "?"})`
    case "case_approved": {
      const tier = str(data.risk_tier)
      const score = num(data.risk_score)
      const note = str(data.note)
      const head = tier ? `Approved at ${tier} risk${score !== null ? ` (score ${score})` : ""}` : "Approved"
      return note ? `${head}. Justification: ${note}` : head
    }
    case "case_rejected":
      return `Reason: ${str(data.reason) ?? "—"}`
    case "case_escalated":
      return str(data.reason) ? `Escalated to Reviewer L2. Note: ${str(data.reason)}` : "Escalated to Reviewer L2"
    case "case_status_changed":
      return `${str(data.from_status) ?? "?"} → ${str(data.to_status) ?? "?"}${str(data.reason) ? ` (${str(data.reason)})` : ""}`
    case "risk_assessment_completed": {
      const rules = Array.isArray(data.rules_fired) ? data.rules_fired.length : 0
      return `Score ${num(data.score) ?? "?"}/100 (${str(data.tier) ?? "?"}) from ${rules} triggered rule${rules === 1 ? "" : "s"}`
    }
    case "risk_rule_updated": {
      const note = str(data.note)
      return `${str(data.rule_id) ?? "rule"} v${num(data.from_version) ?? "?"} → v${num(data.to_version) ?? "?"} — ${changeSummary(data.changes)}${note ? `. Note: ${note}` : ""}`
    }
    case "risk_thresholds_updated": {
      const f = (data.from ?? {}) as Record<string, unknown>
      const t = (data.to ?? {}) as Record<string, unknown>
      return `Medium ${String(f.medium)} → ${String(t.medium)}; high ${String(f.high)} → ${String(t.high)}`
    }
    case "user_updated":
      return `${str(data.email) ?? "user"} — ${changeSummary(data.changes)}`
    case "user_password_reset":
      return `Password reset for ${str(data.email) ?? "user"}`
    case "user_password_changed":
      return `${str(data.email) ?? "User"} changed their own password`
    case "issuer_created":
    case "issuer_updated":
    case "issuer_deactivated":
    case "issuer_reactivated":
      return `${str(data.name) ?? "issuer"}${data.changes ? ` — ${changeSummary(data.changes)}` : ""}`
    case "document_uploaded":
      return str(data.original_filename)
    case "cross_document_check_completed": {
      const n = num(data.finding_count)
      return n === null ? null : `${n} finding${n === 1 ? "" : "s"}`
    }
    case "signature_reference_created":
      return `Reference for ${str(data.person_name) ?? "signer"}`
    case "signature_comparison_completed": {
      const n = num(data.comparisons_stored)
      return n === null ? null : `${n} comparison${n === 1 ? "" : "s"} stored`
    }
    default:
      return str(data.summary) ?? str(data.original_filename) ?? str(data.error)
  }
}
