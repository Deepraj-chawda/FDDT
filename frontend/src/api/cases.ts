import { API_BASE_URL, apiFetch, ApiError, BACKGROUND_REFRESH_HEADERS, parseErrorDetail } from "@/api/client"
import { withSession } from "@/lib/session"
import type {
  AuditLogEntry,
  Case,
  CaseDecisionResponse,
  CaseDetail,
  CaseDocument,
  CaseListFilters,
  CaseListItem,
  CaseListPage,
  CaseListSummary,
  CasePageQuery,
  CaseReport,
  CaseType,
  SignatureMatch,
  SignatureReference,
  SignatureReferenceCreatePayload,
} from "@/types/case"

export function createCase(caseType: CaseType, token: string): Promise<Case> {
  return apiFetch<Case>("/cases", {
    method: "POST",
    token,
    body: JSON.stringify({ case_type: caseType }),
  })
}

export function listCases(
  token: string,
  filters: CaseListFilters = {},
  // Platform admins only: the company whose queue to read (required for them).
  companyId: string | null = null
): Promise<CaseListItem[]> {
  const params = new URLSearchParams()
  if (filters.status) params.set("status", filters.status)
  if (filters.case_type) params.set("case_type", filters.case_type)
  if (companyId) params.set("company_id", companyId)
  const query = params.toString()
  return apiFetch<CaseListItem[]>(`/cases${query ? `?${query}` : ""}`, { token })
}

// One page of cases, filtered and searched on the server.
export function listCasesPage(
  token: string,
  query: CasePageQuery,
  // Platform admins only: the company whose cases to read (required for them).
  companyId: string | null = null
): Promise<CaseListPage> {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== "" && value !== false) params.set(key, String(value))
  }
  if (companyId) params.set("company_id", companyId)
  return apiFetch<CaseListPage>(`/cases/page?${params}`, { token })
}

// Every case matching `query`, page by page (for a CSV export).
export async function listAllCases(
  token: string,
  query: Omit<CasePageQuery, "limit" | "offset">,
  companyId: string | null = null
): Promise<CaseListItem[]> {
  const items: CaseListItem[] = []
  for (;;) {
    const page = await listCasesPage(token, { ...query, limit: 200, offset: items.length }, companyId)
    items.push(...page.items)
    if (page.items.length === 0 || items.length >= page.total) return items
  }
}

export function getCaseSummary(
  token: string,
  { mine = false }: { mine?: boolean } = {},
  companyId: string | null = null
): Promise<CaseListSummary> {
  const params = new URLSearchParams()
  if (mine) params.set("mine", "true")
  if (companyId) params.set("company_id", companyId)
  const query = params.toString()
  return apiFetch<CaseListSummary>(`/cases/summary${query ? `?${query}` : ""}`, { token })
}

/** `background`: an automatic refresh of a case already on screen (polling). */
export function getCase(caseId: string, token: string, background = false): Promise<CaseDetail> {
  return apiFetch<CaseDetail>(`/cases/${caseId}`, {
    token,
    headers: background ? BACKGROUND_REFRESH_HEADERS : undefined,
  })
}

// Platform admins only. Soft delete: the case disappears for its company
// (kept, restorable); recorded in the platform audit log only.
export function deleteCase(caseId: string, reason: string | null, token: string): Promise<void> {
  return apiFetch<void>(`/cases/${caseId}`, {
    method: "DELETE",
    token,
    body: JSON.stringify({ reason }),
  })
}

// The case's creator only, and only while it has no documents (409 otherwise):
// withdraws a new case whose every upload was rejected, so it doesn't linger
// as an empty case in the queue and counts.
export function discardEmptyCase(caseId: string, token: string): Promise<void> {
  return apiFetch<void>(`/cases/${caseId}/discard`, { method: "POST", token })
}

export function restoreCase(caseId: string, token: string): Promise<void> {
  return apiFetch<void>(`/cases/${caseId}/restore`, { method: "POST", token })
}

export function getCaseAuditLog(caseId: string, token: string): Promise<AuditLogEntry[]> {
  return apiFetch<AuditLogEntry[]>(`/cases/${caseId}/audit-log`, { token })
}

/**
 * Uploads one file to a case, reporting upload progress. Plain `fetch`
 * has no reliable cross-browser upload-progress event, so this uses
 * XMLHttpRequest directly instead of the apiFetch() wrapper.
 */
export function uploadDocument(
  caseId: string,
  file: File,
  token: string,
  onProgress?: (percent: number) => void
): Promise<CaseDocument> {
  // `token` only says the caller is signed in; the live access token is sent.
  void token
  return withSession((bearer) => new Promise<CaseDocument>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open("POST", `${API_BASE_URL}/cases/${caseId}/documents`)
    xhr.setRequestHeader("Authorization", `Bearer ${bearer}`)

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    }

    xhr.onload = () => {
      let body: unknown
      try {
        body = JSON.parse(xhr.responseText)
      } catch {
        body = null
      }

      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100)
        resolve(body as CaseDocument)
      } else {
        // Upload rejections carry `detail: {code, message}` with a specific,
        // user-facing reason (too large, empty, wrong type, corrupted,
        // password-protected) — show that, not a generic failure.
        const { message, code } = parseErrorDetail(body)
        reject(new ApiError(xhr.status, message ?? (xhr.statusText || "Upload failed"), code))
      }
    }

    xhr.onerror = () => reject(new ApiError(0, "Network error during upload"))

    const formData = new FormData()
    formData.append("file", file)
    xhr.send(formData)
   }))
}

/**
 * Create a signature reference from a reviewer-drawn bounding box.
 * `is_library=false` → in-case reference only.
 * `is_library=true`  → also saved to the permanent library (data collection
 *                       only — no cross-case comparison logic runs yet).
 */
export function createSignatureReference(
  caseId: string,
  documentId: string,
  payload: SignatureReferenceCreatePayload,
  token: string,
): Promise<SignatureReference> {
  return apiFetch<SignatureReference>(
    `/cases/${caseId}/documents/${documentId}/signature-references`,
    {
      method: "POST",
      token,
      body: JSON.stringify(payload),
    },
  )
}

/**
 * A fresh short-lived signed URL for a document's original file. The URL
 * returned at upload time expires after a few minutes, so anything that
 * renders the file later (the reference-signature modal) mints its own.
 */
export function getDocumentFileUrl(
  caseId: string,
  documentId: string,
  token: string,
): Promise<{ file_url: string }> {
  return apiFetch<{ file_url: string }>(
    `/cases/${caseId}/documents/${documentId}/file-url`,
    { token },
  )
}

/** All in-case signature comparison results for a case (comparison_scope=in_case). */
export function getSignatureMatches(caseId: string, token: string, background = false): Promise<SignatureMatch[]> {
  return apiFetch<SignatureMatch[]>(`/cases/${caseId}/signature-matches`, {
    token,
    headers: background ? BACKGROUND_REFRESH_HEADERS : undefined,
  })
}

// ---- Reviewer workflow (reviewers/admin only, and a Reviewer L1 can't act on
// an L2-escalated case; every rule is enforced by the backend — the UI's
// disabled buttons and dialogs are conveniences). ----

export function approveCase(
  caseId: string,
  note: string | null,
  token: string,
): Promise<CaseDecisionResponse> {
  return apiFetch<CaseDecisionResponse>(`/cases/${caseId}/approve`, {
    method: "POST",
    token,
    body: JSON.stringify({ note }),
  })
}

export function rejectCase(
  caseId: string,
  reason: string,
  token: string,
): Promise<CaseDecisionResponse> {
  return apiFetch<CaseDecisionResponse>(`/cases/${caseId}/reject`, {
    method: "POST",
    token,
    body: JSON.stringify({ reason }),
  })
}

export function escalateCase(
  caseId: string,
  reason: string,
  token: string,
): Promise<CaseDecisionResponse> {
  return apiFetch<CaseDecisionResponse>(`/cases/${caseId}/escalate`, {
    method: "POST",
    token,
    body: JSON.stringify({ reason }),
  })
}

// ---- Per-case PDF report (reviewer/admin only). Generation is synchronous
// (a few seconds); every call adds a new report to the case's history. ----

export function generateCaseReport(caseId: string, token: string): Promise<CaseReport> {
  return apiFetch<CaseReport>(`/cases/${caseId}/reports`, { method: "POST", token })
}

export function listCaseReports(caseId: string, token: string): Promise<CaseReport[]> {
  return apiFetch<CaseReport[]>(`/cases/${caseId}/reports`, { token })
}
