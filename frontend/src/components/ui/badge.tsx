import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

// Extends shadcn/ui's usual default/secondary/destructive/outline set with
// success/warning — every status, flag, and check-result pill across the
// case queue and case detail pages (CASE_STATUS_LABELS, CaseFlagType,
// check "pass"/"flag" results, cross-document severity) maps to one of
// these instead of each call site inventing its own bg-*/15 text-* pair.
const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors [&_svg]:pointer-events-none [&_svg]:size-3",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary text-primary-foreground",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        // Brand-tinted, not semantic — for fields like case status/type
        // where the point is "this is a category", not "this is good or
        // bad" (that's what success/warning/destructive are for). Uses
        // --accent (blue), not --primary (navy) — every tinted pill in
        // stitch_docauth_document_review_platform/ (active nav, "Review"
        // link) is blue; navy only ever appears as a solid CTA fill,
        // never as a tint.
        brand: "border-accent/20 bg-accent/10 text-accent",
        // Tint + a matching-hue border (not `border-transparent`) — the
        // reference's own flag/status pills (e.g. `bg-rose-50 text-
        // rose-700 border-rose-200/90`) all pair a soft fill with a
        // slightly stronger border of the same color, not a flat tint.
        destructive: "border-destructive/25 bg-destructive/15 text-destructive",
        success: "border-success/25 bg-success/15 text-success",
        warning: "border-warning/30 bg-warning/20 text-foreground",
        info: "border-info/25 bg-info/15 text-info",
        outline: "border-border text-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Badge({
  className,
  variant,
  dot = false,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean; dot?: boolean }) {
  const Comp = asChild ? Slot : "span"

  return (
    <Comp data-slot="badge" className={cn(badgeVariants({ variant, className }))} {...props}>
      {dot && <span className="size-1.5 shrink-0 rounded-full bg-current" />}
      {props.children}
    </Comp>
  )
}

export { Badge, badgeVariants }
