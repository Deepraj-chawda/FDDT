// Every field an audit row recorded, for platform admins: nothing summarised
// away (who, from where, which company, reasons, before → after values).
import * as React from "react"
import { ChevronDownIcon } from "lucide-react"

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—"
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return JSON.stringify(value, null, 2)
}

export function AuditDetails({ data }: { data: Record<string, unknown> | null | undefined }) {
  const [open, setOpen] = React.useState(false)
  const entries = Object.entries(data ?? {})
  if (entries.length === 0) return null
  return (
    <div className="mt-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="inline-flex items-center gap-1 text-[11px] font-semibold text-blue-600 hover:text-blue-700"
      >
        <ChevronDownIcon className={`size-3 transition ${open ? "rotate-180" : ""}`} />
        {open ? "Hide details" : "All details"}
      </button>
      {open && (
        <dl className="mt-1.5 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 rounded-lg border border-slate-200 bg-slate-50 p-2.5 text-[11px]">
          {entries.map(([key, value]) => (
            <React.Fragment key={key}>
              <dt className="font-mono text-slate-500">{key}</dt>
              <dd className="min-w-0 whitespace-pre-wrap break-words font-mono text-slate-800">{formatValue(value)}</dd>
            </React.Fragment>
          ))}
        </dl>
      )}
    </div>
  )
}
