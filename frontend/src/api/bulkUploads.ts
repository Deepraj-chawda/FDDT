import { API_BASE_URL, apiFetch, ApiError, parseErrorDetail } from "@/api/client"
import type { BulkUploadDetail, BulkUploadSummary } from "@/types/bulkUpload"
import type { CaseType } from "@/types/case"

/**
 * Sends the zip itself as the request body (not multipart), so the server can
 * refuse an over-limit zip from its Content-Length before reading it.
 * XMLHttpRequest for upload progress, like uploadDocument().
 */
export function uploadBulkZip(
  file: File,
  caseType: CaseType,
  token: string,
  onProgress?: (percent: number) => void,
): Promise<BulkUploadDetail> {
  return new Promise((resolve, reject) => {
    const params = new URLSearchParams({ case_type: caseType, filename: file.name })
    const xhr = new XMLHttpRequest()
    xhr.open("POST", `${API_BASE_URL}/bulk-uploads?${params}`)
    xhr.setRequestHeader("Authorization", `Bearer ${token}`)
    xhr.setRequestHeader("Content-Type", "application/zip")

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
        resolve(body as BulkUploadDetail)
      } else {
        const { message, code } = parseErrorDetail(body)
        reject(new ApiError(xhr.status, message ?? (xhr.statusText || "Upload failed"), code))
      }
    }
    xhr.onerror = () => reject(new ApiError(0, "Network error during upload"))
    xhr.send(file)
  })
}

export function getBulkUpload(
  id: string,
  token: string,
  companyId: string | null = null,
): Promise<BulkUploadDetail> {
  const query = companyId ? `?company_id=${encodeURIComponent(companyId)}` : ""
  return apiFetch<BulkUploadDetail>(`/bulk-uploads/${id}${query}`, { token })
}

export function listBulkUploads(
  token: string,
  companyId: string | null = null,
  limit: number | null = null,
): Promise<BulkUploadSummary[]> {
  const params = new URLSearchParams()
  if (companyId) params.set("company_id", companyId)
  if (limit) params.set("limit", String(limit))
  const query = params.size ? `?${params}` : ""
  return apiFetch<BulkUploadSummary[]>(`/bulk-uploads${query}`, { token })
}
