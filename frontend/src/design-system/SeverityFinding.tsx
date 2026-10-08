import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * One row of docauth_case_detail's "Explainable findings" list: a
 * colored dot, a bold title, a muted description, and a signed point
 * delta on the right (+31, +22, -8 in the reference — that last one a
 * trust factor that *lowers* risk, hence "positive" tone reading green
 * with a minus sign, not red).
 *
 * Worth calling out: this is a near-exact visual match for SPECIFICATION.md
 * section 3.3's risk_scoring_service — one triggered risk_rule's
 * `reason_template` + `weight`, rendered explainably. Once that engine
 * exists, `risk_scores.triggered_reasons` (jsonb) is the real data
 * source for a list of these; `pointDelta` is that rule's weight,
 * `tone` is derived from its `severity`.
 *
 * `pointDelta` is optional: risk_scoring_service isn't built yet (no
 * `risk_rules`/weights exist), so today's callers pass real
 * document-check findings with no numeric weight to show — omit it
 * rather than fabricate a score, and the delta column just doesn't
 * render for that row.
 */
export interface SeverityFindingProps {
  title: string
  description: string
  /** The full explanation, behind a "Why?" toggle (the description is the short line). */
  detail?: string
  /** Signed — pass the literal number with its sign (31, -8), not an
   * absolute value; the "+" for positives is added automatically.
   * Omit when no real weight exists yet. */
  pointDelta?: number
  tone: "negative" | "warning" | "positive"
}

const DOT_CLASSES: Record<SeverityFindingProps["tone"], string> = {
  negative: "bg-destructive",
  warning: "bg-warning",
  positive: "bg-success",
}

const DELTA_CLASSES: Record<SeverityFindingProps["tone"], string> = {
  negative: "text-foreground",
  warning: "text-foreground",
  positive: "text-success",
}

export function SeverityFinding({ title, description, detail, pointDelta, tone }: SeverityFindingProps) {
  const [open, setOpen] = React.useState(false)
  const detailId = React.useId()
  return (
    <div className="flex items-start justify-between gap-3 min-w-0 w-full">
      <div className="flex items-start gap-2.5 min-w-0 flex-1">
        <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", DOT_CLASSES[tone])} />
        <div className="min-w-0 flex-1">
          <h4 className="text-xs font-bold text-foreground break-words [overflow-wrap:anywhere]">{title}</h4>
          <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground break-words [overflow-wrap:anywhere]">
            {description}
            {detail && (
              <button
                type="button"
                aria-expanded={open}
                aria-controls={detailId}
                onClick={() => setOpen((v) => !v)}
                className="ml-1.5 inline-block font-semibold text-blue-600 hover:underline"
              >
                {open ? "Hide" : "Why?"}
              </button>
            )}
          </p>
          {open && detail && (
            <p id={detailId} className="mt-1 text-[11px] leading-snug text-muted-foreground/90 break-words [overflow-wrap:anywhere]">
              {detail}
            </p>
          )}
        </div>
      </div>
      {pointDelta !== undefined && (
        <span className={cn("ml-2 shrink-0 font-mono text-xs font-bold", DELTA_CLASSES[tone])}>
          {pointDelta > 0 ? `+${pointDelta}` : pointDelta}
        </span>
      )}
    </div>
  )
}
