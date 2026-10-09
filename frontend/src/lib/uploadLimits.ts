// Client-side mirror of the server's upload checks (backend/app/services/
// upload_validation.py). The size limits are PER COMPANY: a platform admin
// sets them, and they come from GET /auth/me/upload-limits (useUploadLimits).
// No size is hardcoded here. The client checks are a UX nicety so an
// oversized or empty file fails before it is sent; the server enforces every
// rule regardless.

export const ACCEPTED_UPLOAD_TYPES =
  ".pdf,application/pdf"

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** The reason a file can't be uploaded, judged before sending it, or null.
 * `maxBytes` is the company's per-file limit (null while it loads: the size
 * is then left to the server). */
export function clientUploadProblem(file: File, maxBytes: number | null): string | null {
  // Only PDFs are accepted. The server decides from the file's header bytes;
  // here, a file that is neither named .pdf nor typed as a PDF is refused early.
  if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
    return `${file.name}: only PDF files are accepted — please save or scan the document as a PDF.`
  }
  if (file.size === 0) return `${file.name}: the file is empty (0 bytes).`
  if (maxBytes !== null && file.size > maxBytes) {
    return `${file.name}: the file is ${formatFileSize(file.size)}; the maximum allowed size is ${formatFileSize(maxBytes)}.`
  }
  return null
}

// ---- Bulk upload (a zip of cases). The zip limit is per company too;
// the warning threshold mirrors BULK_UPLOAD_CASE_WARNING_THRESHOLD. ----

/** Above this many cases the UI warns (soft — the zip is still accepted). */
export const BULK_CASE_WARNING_THRESHOLD = 100

export function clientZipProblem(file: File, maxBytes: number | null): string | null {
  const looksLikeZip =
    file.name.toLowerCase().endsWith(".zip") ||
    ["application/zip", "application/x-zip-compressed"].includes(file.type)
  if (!looksLikeZip) return `${file.name}: bulk upload takes a single .zip file.`
  if (file.size === 0) return `${file.name}: the zip file is empty (0 bytes).`
  if (maxBytes !== null && file.size > maxBytes) {
    return `${file.name}: the zip file is ${formatFileSize(file.size)}; the maximum allowed size is ${formatFileSize(maxBytes)}.`
  }
  return null
}

/**
 * How many case folders the zip holds, from its central directory alone
 * (the last few KB–MB of the file; nothing is decompressed), so the soft
 * ">100 cases" warning can show before uploading. Same folder rules as the
 * server (backend/app/services/bulk_upload_service.py): OS clutter ignored,
 * one wrapper folder around everything stripped. Returns null if the zip
 * can't be read this way (e.g. ZIP64) — the server's count is authoritative.
 */
export async function estimateZipCaseCount(file: File): Promise<number | null> {
  try {
    const tailSize = Math.min(file.size, 65_557)
    const tail = new DataView(await file.slice(file.size - tailSize).arrayBuffer())
    let eocd = -1
    for (let i = tail.byteLength - 22; i >= 0; i--) {
      if (tail.getUint32(i, true) === 0x06054b50) {
        eocd = i
        break
      }
    }
    if (eocd < 0) return null
    const cdSize = tail.getUint32(eocd + 12, true)
    const cdOffset = tail.getUint32(eocd + 16, true)
    if (cdSize === 0xffffffff || cdOffset === 0xffffffff) return null // ZIP64
    const cd = new DataView(await file.slice(cdOffset, cdOffset + cdSize).arrayBuffer())
    const utf8 = new TextDecoder("utf-8", { fatal: true })
    const latin = new TextDecoder("latin1")

    const paths: { parts: string[]; isDir: boolean }[] = []
    let p = 0
    while (p + 46 <= cd.byteLength && cd.getUint32(p, true) === 0x02014b50) {
      const nameLen = cd.getUint16(p + 28, true)
      const extraLen = cd.getUint16(p + 30, true)
      const commentLen = cd.getUint16(p + 32, true)
      const raw = new Uint8Array(cd.buffer, cd.byteOffset + p + 46, nameLen)
      let name: string
      try {
        name = utf8.decode(raw)
      } catch {
        name = latin.decode(raw)
      }
      const parts = name.replace(/\\/g, "/").split("/").filter((s) => s !== "" && s !== ".")
      const junk = parts.some(
        (s) => s === "__MACOSX" || s.startsWith(".") || ["thumbs.db", "desktop.ini"].includes(s.toLowerCase()),
      )
      if (parts.length > 0 && !junk && !parts.includes("..")) {
        paths.push({ parts, isDir: name.endsWith("/") })
      }
      p += 46 + nameLen + extraLen + commentLen
    }

    let entries = paths
    const files = entries.filter((e) => !e.isDir)
    const tops = new Set(files.map((e) => e.parts[0]))
    if (files.length > 0 && tops.size === 1 && files.every((e) => e.parts.length >= 3)) {
      const wrapper = [...tops][0]
      entries = entries
        .filter((e) => e.parts[0] === wrapper && e.parts.length > 1)
        .map((e) => ({ ...e, parts: e.parts.slice(1) }))
    }
    const folders = new Set(
      entries.filter((e) => e.parts.length >= 2 || e.isDir).map((e) => e.parts[0]),
    )
    return folders.size
  } catch {
    return null
  }
}
