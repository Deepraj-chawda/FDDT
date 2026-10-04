import * as React from "react"

// Small accessible modal used by the Settings screens (issuer form, rule
// history, confirmations). Closes on Escape / backdrop click unless `busy`.
export function Modal({
  title,
  description,
  children,
  onClose,
  busy = false,
  wide = false,
}: {
  title: string
  description?: React.ReactNode
  children: React.ReactNode
  onClose: () => void
  busy?: boolean
  wide?: boolean
}) {
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose, busy])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-2 sm:p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`flex max-h-[92vh] w-full flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl ${wide ? "max-w-3xl" : "max-w-lg"}`}
      >
        <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-4 sm:px-5 py-3 sm:py-4">
          <div>
            <h2 className="text-base font-bold text-slate-900">{title}</h2>
            {description && <div className="mt-1 text-xs text-slate-500">{description}</div>}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label="Close"
            className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
          >
            ✕
          </button>
        </div>
        <div className="overflow-y-auto p-4 sm:p-5">{children}</div>
      </div>
    </div>
  )
}
