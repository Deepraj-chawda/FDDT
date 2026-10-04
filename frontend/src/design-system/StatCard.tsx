import type * as React from "react"
import { TrendingDownIcon, TrendingUpIcon } from "lucide-react"

import { cn } from "@/lib/utils"

/**
 * The 4-tile metric row from docauth_case_queue ("Active cases",
 * "High-risk flagged", "Avg. resolution time", "Auto-cleared rate") —
 * label + icon chip up top, a big number, a small trend/status note
 * below. `tone` recolors the icon chip, the number, and (for trend
 * notes) the arrow/text together, matching how that reference varies
 * "High-risk flagged" (red) and "Auto-cleared rate" (neutral number,
 * green trend note) from the plain slate default.
 */
export interface StatCardProps {
  label: string
  value: React.ReactNode
  icon: React.ComponentType<{ className?: string }>
  tone?: "default" | "destructive" | "success"
  /** Rendered below the value, e.g. "+4 awaiting intake" or
   * "-3m vs last week". `trend` draws a small up/down arrow before it —
   * omit when the note isn't actually a trend (e.g. "Requires senior
   * review" has a dot, not an arrow, in the reference). */
  note?: React.ReactNode
  trend?: "up" | "down"
  noteTone?: "muted" | "destructive" | "success"
}

const ICON_CHIP_CLASSES: Record<NonNullable<StatCardProps["tone"]>, string> = {
  default: "bg-accent/10 border-accent/20 text-accent",
  destructive: "bg-destructive/10 border-destructive/20 text-destructive",
  success: "bg-success/10 border-success/20 text-success",
}

const VALUE_CLASSES: Record<NonNullable<StatCardProps["tone"]>, string> = {
  default: "text-foreground",
  destructive: "text-destructive",
  success: "text-foreground",
}

const NOTE_CLASSES: Record<NonNullable<StatCardProps["noteTone"]>, string> = {
  muted: "text-muted-foreground",
  destructive: "text-destructive",
  success: "text-success",
}

export function StatCard({
  label,
  value,
  icon: Icon,
  tone = "default",
  note,
  trend,
  noteTone = "muted",
}: StatCardProps) {
  return (
    <div className="flex flex-col justify-between rounded-2xl border border-border bg-card p-5 shadow-card">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{label}</span>
        <span className={cn("flex size-8 items-center justify-center rounded-lg border", ICON_CHIP_CLASSES[tone])}>
          <Icon className="size-4" />
        </span>
      </div>
      <div className="mt-3">
        <div className={cn("text-3xl font-extrabold tracking-tight", VALUE_CLASSES[tone])}>{value}</div>
        {note && (
          <div className={cn("mt-1 flex items-center gap-1.5 text-xs font-medium", NOTE_CLASSES[noteTone])}>
            {trend === "up" && <TrendingUpIcon className="size-3.5" />}
            {trend === "down" && <TrendingDownIcon className="size-3.5" />}
            {note}
          </div>
        )}
      </div>
    </div>
  )
}
