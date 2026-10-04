import type { VariantProps } from "class-variance-authority"
import { FlameIcon } from "lucide-react"

import { Badge, type badgeVariants } from "@/components/ui/badge"
import type { CaseFlag, CaseFlagType, CaseTier, RiskTier } from "@/types/case"

// Shared between CaseQueuePage (table cells) and CaseDetailPage (header
// bar) so the two pages never drift into rendering the same case's risk
// tier or flag with different colors.

// The flag IS the case's risk tier (low = green, medium = amber, high = red —
// same convention as RiskBadge below); "pending" = not scored yet, so it gets
// a neutral badge rather than implying the case is fine.
const FLAG_BADGE_VARIANTS: Record<CaseFlagType, VariantProps<typeof badgeVariants>["variant"]> = {
  low: "success",
  medium: "warning",
  high: "destructive",
  pending: "secondary",
}

export function CaseFlagBadge({ flag }: { flag: CaseFlag }) {
  return (
    <Badge variant={FLAG_BADGE_VARIANTS[flag.flag]} dot>
      {flag.label}
      {flag.score !== null && <span className="ml-1 font-mono text-[10px] opacity-70">{flag.score}</span>}
    </Badge>
  )
}

// Escalation moves the case to the L2 reviewer tier; it is not a status (see
// backend/app/models/case.py CaseTier). Nothing is shown for an L1 case.
export function TierBadge({ tier }: { tier: CaseTier }) {
  if (tier !== "l2") return null
  return (
    <Badge variant="destructive">
      <FlameIcon className="size-3" />
      Escalated · L2
    </Badge>
  )
}

const RISK_BADGE_VARIANTS: Record<RiskTier, VariantProps<typeof badgeVariants>["variant"]> = {
  low: "success",
  medium: "warning",
  high: "destructive",
}

const RISK_LABELS: Record<RiskTier, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
}

// `compact` drops the trailing "risk" for table cells where a "Risk"
// column header already gives that context; the detail-page header bar
// (where the badge stands alone next to status/type) uses the full label.
// Left-border stripe color for the case header bar — same tier→color
// mapping as RiskBadge, just as a border utility instead of a badge, and
// with a neutral fallback for a case that hasn't been scored yet.
const RISK_STRIPE_CLASSES: Record<RiskTier, string> = {
  low: "border-l-4 border-l-success",
  medium: "border-l-4 border-l-warning",
  high: "border-l-4 border-l-destructive",
}

export function riskStripeClass(tier: RiskTier | null): string {
  return tier === null ? "border-l-4 border-l-border" : RISK_STRIPE_CLASSES[tier]
}

export function RiskBadge({
  tier,
  compact = false,
  className,
}: {
  tier: RiskTier | null
  compact?: boolean
  className?: string
}) {
  if (tier === null) {
    return <span className="text-xs text-muted-foreground">Not yet scored</span>
  }
  return (
    <Badge variant={RISK_BADGE_VARIANTS[tier]} className={className}>
      {RISK_LABELS[tier]}
      {!compact && " risk"}
    </Badge>
  )
}
