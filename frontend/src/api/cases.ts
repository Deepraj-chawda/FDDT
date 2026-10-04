import { API_BASE_URL, apiFetch, ApiError, parseErrorDetail } from "@/api/client"
import type {
  AuditLogEntry,
  Case,
  CaseDecisionResponse,
  CaseDetail,
  CaseDocument,
  CaseListFilters,
  CaseListItem,
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

export function getCase(caseId: string, token: string): Promise<CaseDetail> {
  return apiFetch<CaseDetail>(`/cases/${caseId}`, { token })
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
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open("POST", `${API_BASE_URL}/cases/${caseId}/documents`)
    xhr.setRequestHeader("Authorization", `Bearer ${token}`)

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
  })
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
export function getSignatureMatches(caseId: string, token: string): Promise<SignatureMatch[]> {
  return apiFetch<SignatureMatch[]>(`/cases/${caseId}/signature-matches`, { token })
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
