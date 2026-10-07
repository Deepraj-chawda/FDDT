// Mirrors backend/app/schemas/settings.py (admin Settings API + system audit).
import type { UserRole } from "@/types/auth"

export type IssuerType = "vendor" | "school" | "government" | "other"

export const ISSUER_TYPE_LABELS: Record<IssuerType, string> = {
  vendor: "Vendor",
  school: "School / education",
  government: "Government",
  other: "Other",
}

export interface Issuer {
  id: string
  name: string
  name_arabic: string | null
  tax_id: string | null
  type: IssuerType
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface IssuerPayload {
  name: string
  name_arabic: string | null
  tax_id: string | null
  type: IssuerType
}

export type RuleSeverity = "low" | "medium" | "high"

// One immutable version of a rule. Editing creates version + 1.
export interface RiskRule {
  id: string
  rule_id: string
  category: string
  check_type: string
  weight: number
  severity: RuleSeverity | string
  reason_template: string
  is_active: boolean
  version: number
  effective_from: string
  updated_by_name: string | null
  change_note: string | null
}

// A brand-new rule (POST /settings/risk-rules). The admin picks a rule type
// and its parameters; the backend builds the engine condition from them.
export type RuleMatchKind = "finding" | "check_result" | "sub_check" | "cross_document" | "signature_match"

export interface RiskRuleCreate {
  rule_id: string
  category: string
  match: RuleMatchKind
  check_type?: string | null
  finding?: string | null
  severity_in?: RuleSeverity[] | null
  sub_check?: string | null
  field_name?: string | null
  signature_result?: string | null
  weight: number
  severity: RuleSeverity
  reason_template: string
  is_active: boolean
  change_note?: string | null
}

interface Option {
  value: string
  label: string
}

// GET /settings/risk-rule-options — everything the Add-rule form offers.
export interface RiskRuleOptions {
  categories: Option[]
  match_kinds: (Option & { help: string })[]
  finding_checks: (Option & { findings: string[] })[]
  check_result_checks: Option[]
  sub_checks: Option[]
  cross_fields: Option[]
  signature_results: Option[]
  placeholders: string[]
}

export interface AdminUserCreate {
  email: string
  full_name?: string | null
  role: UserRole
  password: string
  company_id?: string | null
}

export interface RiskRuleUpdate {
  weight?: number
  severity?: RuleSeverity
  is_active?: boolean
  change_note?: string | null
}

export interface RiskThresholds {
  medium_threshold: number
  high_threshold: number
  // Most points the metadata rules (metadata.*) add together; 100 = no cap.
  metadata_score_cap?: number
}

export interface RiskThresholdsResponse extends RiskThresholds {
  metadata_score_cap: number
  updated_at: string
  updated_by_name: string | null
}

export interface AdminUser {
  id: string
  email: string
  full_name: string | null
  role: UserRole
  is_active: boolean
  created_at: string
  company_id: string | null
  company_name: string | null
}

export interface AuditEvent {
  id: string
  created_at: string
  event_type: string
  actor_name: string | null
  actor_email: string | null
  case_id: string | null
  case_number: string | null
  document_id: string | null
  document_filename: string | null
  event_data: Record<string, unknown> | null
}

export interface AuditPage {
  items: AuditEvent[]
  total: number
}
