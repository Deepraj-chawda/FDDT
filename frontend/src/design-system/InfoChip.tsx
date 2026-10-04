import type * as React from "react"

/**
 * The small bordered "File Integrity / Classification / Extraction"
 * tiles from docauth_case_detail's bottom metadata strip — tiny
 * uppercase caption on top, the actual value below (with an optional
 * small leading icon, used there for the SHA-256-verified checkmark).
 * Meant to sit 3-across in a grid, per that reference.
 */
export function InfoChip({
  caption,
  value,
  icon: Icon,
}: {
  caption: string
  value: React.ReactNode
  icon?: React.ComponentType<{ className?: string }>
}) {
  return (
    <div className="rounded-lg border border-border bg-secondary/40 p-2.5">
      <span className="mb-0.5 block text-[10px] font-bold uppercase tracking-wide text-muted-foreground">
        {caption}
      </span>
      <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
        {Icon && <Icon className="size-3.5 shrink-0 text-success" />}
        <span className="truncate">{value}</span>
      </div>
    </div>
  )
}
