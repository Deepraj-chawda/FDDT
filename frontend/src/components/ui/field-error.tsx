import { AlertCircleIcon } from "lucide-react"

import { cn } from "@/lib/utils"

/**
 * A field's validation message, in red directly under the field. It is always
 * visible, never a hover tooltip. Give the input `aria-invalid` and
 * `aria-describedby={id}` and add `invalidFieldClass` to its className.
 *
 * Forms using it keep their submit button ENABLED: clicking it with a problem
 * shows the message on the field instead of silently doing nothing. Forms also
 * set `noValidate`, so the browser's own hover bubbles never replace these
 * messages.
 */
export function FieldError({ id, message, className }: { id?: string; message?: string | null; className?: string }) {
  if (!message) return null
  return (
    <p id={id} role="alert" className={cn("mt-1 flex items-start gap-1 text-[11px] font-medium text-rose-600", className)}>
      <AlertCircleIcon className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      <span>{message}</span>
    </p>
  )
}

/** Red border + tint for an invalid input (appended after its normal classes). */
export const invalidFieldClass =
  "border-rose-500 bg-rose-50/40 focus:border-rose-500 focus:ring-rose-500/20"

/** Field ids → message, shown once the field is touched (blurred) or the form submitted. */
export function visibleError(
  errors: Record<string, string | null | undefined>,
  field: string,
  touched: Record<string, boolean>,
  submitted: boolean,
): string | null {
  return submitted || touched[field] ? errors[field] ?? null : null
}
