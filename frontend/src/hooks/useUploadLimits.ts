import { useQuery } from "@tanstack/react-query"

import { apiFetch } from "@/api/client"
import { useAuth } from "@/hooks/useAuth"

export interface UploadLimits {
  max_file_size_mb: number
  max_zip_size_mb: number
  max_file_size_bytes: number
  max_zip_size_bytes: number
}

/**
 * The signed-in user's company upload limits (per company, set by a platform
 * admin). Re-read whenever an upload screen mounts, so a limit changed
 * mid-session shows up without signing in again. Null while loading, and for
 * platform admins, who have no company.
 */
export function useUploadLimits(): UploadLimits | null {
  const { token, user } = useAuth()
  const { data } = useQuery({
    queryKey: ["upload-limits", token],
    queryFn: () => apiFetch<UploadLimits>("/auth/me/upload-limits", { token }),
    enabled: Boolean(token) && Boolean(user) && !user?.is_platform_admin,
    staleTime: 0,
    refetchOnMount: "always",
  })
  return data ?? null
}
