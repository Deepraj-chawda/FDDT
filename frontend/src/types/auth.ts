// Mirrors backend/app/schemas/auth.py and backend/app/models/user.py
// (UserRole). Keep these in sync with the backend schemas by hand for
// now — see SPECIFICATION.md section 2 (Forms: React Hook Form + Zod, "schema
// validation mirrored from backend Pydantic models").

// Company roles (user < reviewer_l1 < reviewer_l2) belong to exactly one
// company. platform_admin belongs to none: it creates companies and users,
// sees billing/usage and the queue monitor, and has read-only (audited)
// support access to any company's cases. It is NOT part of the rank ladder.
export type CompanyRole = "user" | "reviewer_l1" | "reviewer_l2"
export type UserRole = CompanyRole | "platform_admin"

// Mirrors ROLE_RANK in backend/app/models/user.py. UI gating asks "at least
// rank N" (hasRank) for company roles; platform admins are checked with
// isPlatformAdmin — the backend enforces the same.
const ROLE_RANK: Record<CompanyRole, number> = { user: 0, reviewer_l1: 1, reviewer_l2: 2 }

export const ROLE_LABELS: Record<UserRole, string> = {
  user: "User",
  reviewer_l1: "Reviewer L1",
  reviewer_l2: "Reviewer L2",
  platform_admin: "Platform Admin",
}

// Role picker options (Settings > Users, Add user), lowest rank first.
export const ROLE_OPTIONS: { value: UserRole; label: string; help: string }[] = [
  { value: "user", label: ROLE_LABELS.user, help: "Submits documents and tracks their own cases" },
  { value: "reviewer_l1", label: ROLE_LABELS.reviewer_l1, help: "Reviews the company's queue; approves, rejects, escalates to L2" },
  {
    value: "reviewer_l2",
    label: ROLE_LABELS.reviewer_l2,
    help: "Everything a Reviewer L1 can, plus escalated cases and the company's Issuer Registry and Risk Rules",
  },
  {
    value: "platform_admin",
    label: ROLE_LABELS.platform_admin,
    help: "Platform team only — no company. Companies, users, billing; read-only support access",
  },
]

export function isPlatformAdmin(role: UserRole | undefined): boolean {
  return role === "platform_admin"
}

/** True if `role` is a company role ranked at least `minimum`. Always false
 * for platform admins, who have no company rank. */
export function hasRank(role: UserRole | undefined, minimum: CompanyRole): boolean {
  if (role === undefined || role === "platform_admin") return false
  return ROLE_RANK[role] >= ROLE_RANK[minimum]
}

export interface TokenResponse {
  access_token: string
  token_type: string
}

export interface CurrentUser {
  id: string
  email: string
  full_name: string | null
  role: UserRole
  role_label: string
  is_active: boolean
  is_platform_admin: boolean
  company_id: string | null
  company_name: string | null
}
