import type * as React from "react"

/**
 * "Eyebrow + bold title + description" typography pattern — extracted
 * from docauth_case_queue ("CASE INVESTIGATIONS" / "Active Case Queue" /
 * "Prioritized triage for...") and docauth_case_detail ("CASE
 * INVESTIGATION" / "CASE-20419" / the case's document-count summary
 * line). Same three-line shape both places, just the title's font-mono
 * on the case-detail one where the title is a case number — pass
 * `titleMono` for that.
 *
 * `actions` is a right-aligned slot for whatever accompanies the header
 * (case_queue's metrics pill + Export CSV button; case_detail's Export
 * evidence + Escalate buttons) — kept generic rather than baking in
 * either page's specific buttons.
 */
export function PageHeader({
  eyebrow,
  title,
  description,
  titleMono = false,
  actions,
}: {
  eyebrow: string
  title: React.ReactNode
  description?: React.ReactNode
  titleMono?: boolean
  actions?: React.ReactNode
}) {
  return (
    <div className="flex flex-col justify-between gap-4 lg:flex-row lg:items-end">
      <div>
        <div className="mb-1 text-[11px] font-bold uppercase tracking-widest text-accent">{eyebrow}</div>
        <h1
          className={
            titleMono
              ? "font-mono text-2xl font-extrabold tracking-tight text-foreground sm:text-3xl"
              : "text-2xl font-extrabold tracking-tight text-foreground sm:text-3xl"
          }
        >
          {title}
        </h1>
        {description && <p className="mt-1 max-w-2xl text-xs text-muted-foreground sm:text-sm">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2.5">{actions}</div>}
    </div>
  )
}
