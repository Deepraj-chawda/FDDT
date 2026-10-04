import { cn } from "@/lib/utils"

/**
 * One entry in docauth_case_detail's "Audit trail" strip — a colored
 * left border, a mono timestamp, a bold event title, and a muted
 * description. The reference lays 3 of these out as columns
 * (`grid-cols-3`, most-recent first, only that one highlighted in
 * accent blue); `current` reproduces that highlight for whichever
 * entry a grid of these puts first, rather than hardcoding "the first
 * child is special" into layout CSS.
 */
export function AuditRow({
  timestamp,
  title,
  description,
  current = false,
}: {
  timestamp: string
  title: string
  description: string
  current?: boolean
}) {
  return (
    <div className={cn("border-l-2 pl-3", current ? "border-accent" : "border-border")}>
      <span className={cn("font-mono text-[11px] font-bold", current ? "text-accent" : "text-muted-foreground")}>
        {timestamp}
      </span>
      <h4 className="mt-0.5 text-xs font-bold text-foreground">{title}</h4>
      <p className="mt-0.5 text-[11px] text-muted-foreground">{description}</p>
    </div>
  )
}
