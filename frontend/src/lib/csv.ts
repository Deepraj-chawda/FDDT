// CSV export, built in the browser from data the page already has.
//
// - RFC 4180 quoting (commas, quotes, line breaks inside a value).
// - Spreadsheet-formula injection guard: a value starting with = + - @ (or a
//   tab / carriage return) is prefixed with ' so Excel/Sheets show it as text
//   instead of running it — case data comes from uploaded documents.
// - UTF-8 with a byte-order mark, so Excel opens Arabic names correctly.

type Cell = string | number | boolean | null | undefined

function escapeCell(value: Cell): string {
  if (value === null || value === undefined) return ""
  let text = String(value)
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

export function toCsv(header: string[], rows: Cell[][]): string {
  return [header, ...rows].map((row) => row.map(escapeCell).join(",")).join("\r\n")
}

export function downloadCsv(filename: string, csv: string): void {
  downloadBlob(filename, new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8" }))
}

/** Save a file the page already holds (e.g. a CSV the server built). */
export function downloadBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
