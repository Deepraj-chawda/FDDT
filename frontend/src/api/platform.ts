import { apiFetch } from "@/api/client"
import type { RiskRuleCreate, RuleSeverity } from "@/types/settings"

// Platform-admin-only endpoints (backend/app/api/platform.py). Every one is a
// 403 for company roles.

export interface Company {
  id: string
  name: string
  is_active: boolean
  created_at: string
  user_count: number
  /** Per-company upload limits (MB), a platform-admin capacity lever. */
  max_file_size_mb: number
  max_zip_size_mb: number
}

export interface CompanyUpdatePayload {
  name?: string
  is_active?: boolean
  max_file_size_mb?: number
  max_zip_size_mb?: number
}

export const listCompanies = (token: string) => apiFetch<Company[]>("/platform/companies", { token })

export const createCompany = (name: string, token: string) =>
  apiFetch<Company>("/platform/companies", { method: "POST", token, body: JSON.stringify({ name }) })

export const updateCompany = (id: string, payload: CompanyUpdatePayload, token: string) =>
  apiFetch<Company>(`/platform/companies/${id}`, { method: "PATCH", token, body: JSON.stringify(payload) })

export type UsagePeriod = "this_month" | "last_month" | "last_30_days" | "this_year" | "all_time" | "custom"

export interface CompanyUsageRow {
  company_id: string
  company_name: string
  is_active: boolean
  cases_created: number
  documents_uploaded: number
  files_stored: number
  storage_bytes: number
  total_storage_bytes: number
  total_files_stored: number
}

export interface UsageResponse {
  period: UsagePeriod
  start: string | null
  end: string | null
  companies: CompanyUsageRow[]
  totals: Omit<CompanyUsageRow, "company_id" | "company_name" | "is_active">
}

export function getUsage(token: string, period: UsagePeriod, start?: string, end?: string) {
  const params = new URLSearchParams({ period })
  if (period === "custom") {
    if (start) params.set("start", start)
    if (end) params.set("end", end)
  }
  return apiFetch<UsageResponse>(`/platform/usage?${params.toString()}`, { token })
}

export interface ReconcileResponse {
  rows_checked: number
  rows_corrected: number
  drift: { company_id: string; usage_date: string; counters: Record<string, { was: number; now: number }> }[]
}

export const reconcileUsage = (token: string) =>
  apiFetch<ReconcileResponse>("/platform/usage/reconcile", { method: "POST", token })

export interface LimiterStats {
  available: boolean
  units_in_current_window?: number
  calls_total?: number
  throttled_calls_total?: number
  throttled_seconds_total?: number
  http_429_total?: number
  cooldown_remaining_seconds?: number
}

export interface QueueStatus {
  queue: string
  service: string
  rate_limit: string
  configured_workers: number | string
  waiting: number
  running: number
  oldest_waiting_seconds: number | null
  started_in_window: number
  avg_wait_seconds: number | null
  p95_wait_seconds: number | null
  max_wait_seconds: number | null
  avg_runtime_seconds: number | null
  completed_in_window: number
  rate_limiter?: LimiterStats
  token_rate_limiter?: LimiterStats
  workers?: string[] | null
  outstanding_by_company?: { company_id: string; company_name: string | null; outstanding: number }[]
}

export interface QueueSnapshot {
  available: boolean
  error?: string
  window_seconds?: number
  queues: QueueStatus[]
}

export const getQueues = (token: string, windowMinutes = 15) =>
  apiFetch<QueueSnapshot>(`/platform/queues?window_minutes=${windowMinutes}`, { token })

/** Append `company_id` for platform-admin requests (company users are always
 * confined to their own company server-side and never send it). */
export function withCompany(path: string, companyId: string | null | undefined): string {
  if (!companyId) return path
  return `${path}${path.includes("?") ? "&" : "?"}company_id=${encodeURIComponent(companyId)}`
}

// ---- Risk-rule templates (default rule set for NEW companies) -------------
export interface RuleTemplate {
  id: string
  rule_id: string
  category: string
  check_type: string
  condition: Record<string, unknown>
  weight: number
  severity: RuleSeverity
  reason_template: string
  is_active: boolean
  updated_at: string
}

export const listRuleTemplates = (token: string) => apiFetch<RuleTemplate[]>("/platform/risk-rule-templates", { token })

export const createRuleTemplate = (payload: RiskRuleCreate, token: string) =>
  apiFetch<RuleTemplate>("/platform/risk-rule-templates", { method: "POST", token, body: JSON.stringify(payload) })

export const updateRuleTemplate = (
  ruleId: string,
  payload: { weight?: number; severity?: RuleSeverity; reason_template?: string; is_active?: boolean },
  token: string
) =>
  apiFetch<RuleTemplate>(`/platform/risk-rule-templates/${encodeURIComponent(ruleId)}`, {
    method: "PATCH",
    token,
    body: JSON.stringify(payload),
  })
