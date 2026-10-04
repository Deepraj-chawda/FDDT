import { apiFetch } from "@/api/client"
import { withCompany } from "@/api/platform"
import type { UserRole } from "@/types/auth"
import type {
  AdminUser,
  AdminUserCreate,
  AuditPage,
  Issuer,
  IssuerPayload,
  RiskRule,
  RiskRuleCreate,
  RiskRuleOptions,
  RiskRuleUpdate,
  RiskThresholds,
  RiskThresholdsResponse,
} from "@/types/settings"

// Issuer registry and risk rules are PER COMPANY: a Reviewer L2 always works on
// their own company; a platform admin passes `companyId` (and the backend
// audits the access). Users are platform-admin-only. The backend enforces all
// of it; the UI only hides what a role can't use.

// ---- Issuer registry -------------------------------------------------------
export const listIssuers = (token: string, companyId?: string | null) =>
  apiFetch<Issuer[]>(withCompany("/settings/issuers", companyId), { token })

export const createIssuer = (payload: IssuerPayload, token: string, companyId?: string | null) =>
  apiFetch<Issuer>(withCompany("/settings/issuers", companyId), {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  })

export const updateIssuer = (
  id: string,
  payload: Partial<IssuerPayload> & { is_active?: boolean },
  token: string,
  companyId?: string | null
) =>
  apiFetch<Issuer>(withCompany(`/settings/issuers/${id}`, companyId), {
    method: "PATCH",
    token,
    body: JSON.stringify(payload),
  })

// ---- Risk rules ------------------------------------------------------------
export const listRiskRules = (token: string, companyId?: string | null) =>
  apiFetch<RiskRule[]>(withCompany("/settings/risk-rules", companyId), { token })

export const getRiskRuleOptions = (token: string) =>
  apiFetch<RiskRuleOptions>("/settings/risk-rule-options", { token })

export const createRiskRule = (payload: RiskRuleCreate, token: string, companyId?: string | null) =>
  apiFetch<RiskRule>(withCompany("/settings/risk-rules", companyId), {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  })

export const getRiskRuleHistory = (ruleId: string, token: string, companyId?: string | null) =>
  apiFetch<RiskRule[]>(withCompany(`/settings/risk-rules/${encodeURIComponent(ruleId)}/history`, companyId), {
    token,
  })

export const updateRiskRule = (ruleId: string, payload: RiskRuleUpdate, token: string, companyId?: string | null) =>
  apiFetch<RiskRule>(withCompany(`/settings/risk-rules/${encodeURIComponent(ruleId)}`, companyId), {
    method: "PATCH",
    token,
    body: JSON.stringify(payload),
  })

export const getRiskThresholds = (token: string, companyId?: string | null) =>
  apiFetch<RiskThresholdsResponse>(withCompany("/settings/risk-thresholds", companyId), { token })

export const updateRiskThresholds = (payload: RiskThresholds, token: string, companyId?: string | null) =>
  apiFetch<RiskThresholdsResponse>(withCompany("/settings/risk-thresholds", companyId), {
    method: "PUT",
    token,
    body: JSON.stringify(payload),
  })

// ---- Users (platform admin only) ------------------------------------------
export const listUsers = (token: string) => apiFetch<AdminUser[]>("/settings/users", { token })

export const createUser = (payload: AdminUserCreate, token: string) =>
  apiFetch<AdminUser>("/settings/users", { method: "POST", token, body: JSON.stringify(payload) })

export const updateUser = (
  id: string,
  payload: { role?: UserRole; is_active?: boolean; company_id?: string | null },
  token: string
) => apiFetch<AdminUser>(`/settings/users/${id}`, { method: "PATCH", token, body: JSON.stringify(payload) })

export const resetUserPassword = (
  id: string,
  password: string,
  token: string
) => apiFetch<AdminUser>(`/settings/users/${id}/reset-password`, {
  method: "POST",
  token,
  body: JSON.stringify({ password }),
})

// ---- System audit history (reviewer/admin) ---------------------------------
export interface AuditQuery {
  event_type?: string
  q?: string
  limit?: number
  offset?: number
  /** Platform admins: one company's log; omitted = the platform-level log. */
  company_id?: string | null
}

export function listAuditEvents(token: string, query: AuditQuery = {}) {
  const params = new URLSearchParams()
  if (query.company_id) params.set("company_id", query.company_id)
  if (query.event_type) params.set("event_type", query.event_type)
  if (query.q?.trim()) params.set("q", query.q.trim())
  params.set("limit", String(query.limit ?? 100))
  if (query.offset) params.set("offset", String(query.offset))
  return apiFetch<AuditPage>(`/audit-log?${params.toString()}`, { token })
}

export const listAuditEventTypes = (token: string, companyId?: string | null) =>
  apiFetch<string[]>(withCompany("/audit-log/event-types", companyId), { token })
