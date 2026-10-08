/**
 * Reviewer decision panel for the case-detail page: approve / reject /
 * escalate, plus the case's decision history.
 *
 * Who sees what:
 *  - Reviewer L1 / L2 / admin: the three actions — except a Reviewer L1 on a
 *    case escalated to L2 (`can_act` false), who gets a read-only view.
 *  - user (submitter): read-only history (their case's approve/reject notes
 *    — e.g. why it was rejected). The backend never sends them escalation
 *    notes, and 403s the action endpoints regardless of this UI.
 *
 * Every rule is enforced server-side (backend/app/api/case_actions.py); the
 * disabled states and dialogs here just make it obvious to the reviewer:
 *  - Approve is disabled until the automated checks have finished and the
 *    case has a risk assessment (with a list of what's still running).
 *  - Approving a LOW-risk case is one click. Anything above low opens a
 *    confirmation that shows the tier/score and requires a written
 *    justification, which is stored with the decision.
 *  - Reject and Escalate each require a reason. Escalate hands the case to
 *    the L2 tier (one-way — L2 resolves it with approve/reject); it never
 *    changes status.
 */
import * as React from "react"
import { createPortal } from "react-dom"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2Icon, FlameIcon, Loader2Icon, XCircleIcon } from "lucide-react"

import { approveCase, escalateCase, rejectCase } from "@/api/cases"
import { ApiError } from "@/api/client"
import { Button } from "@/components/ui/button"
import { FieldError, invalidFieldClass } from "@/components/ui/field-error"
import { cn } from "@/lib/utils"
import { hasRank, type UserRole } from "@/types/auth"
import type { CaseAction, CaseDetail } from "@/types/case"

// Keep in step with backend/app/api/case_actions.py.
const MIN_JUSTIFICATION_CHARS = 10

type DialogKind = "approve" | "reject" | "escalate" | null

const ACTION_LABELS: Record<string, string> = {
  approve: "Approved",
  reject: "Rejected",
  escalate: "Escalated",
}

const ACTION_TONE: Record<string, string> = {
  approve: "bg-emerald-50 border-emerald-200 text-emerald-800",
  reject: "bg-rose-50 border-rose-200 text-rose-800",
  escalate: "bg-amber-50 border-amber-200 text-amber-800",
}

function formatWhen(iso: string): string {
  const d = new Date(iso)
  return `${d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })} · ${d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}`
}

function DecisionHistory({ actions }: { actions: CaseAction[] }) {
  if (actions.length === 0) return null
  return (
    <div className="mt-4 space-y-2 border-t border-slate-100 pt-3">
      <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Decision history</p>
      {[...actions].reverse().map((a) => (
        <div key={a.id} className={cn("rounded-lg border px-3 py-2 text-xs", ACTION_TONE[a.action_type] ?? "bg-slate-50 border-slate-200 text-slate-700")}>
          <div className="flex items-center justify-between gap-2">
            <span className="font-bold">{ACTION_LABELS[a.action_type] ?? a.action_type}</span>
            <span className="text-[10px] opacity-70">{formatWhen(a.created_at)}</span>
          </div>
          <div className="mt-0.5 text-[11px] opacity-80">
            by {a.actor_name ?? "Unknown"}
            {a.actor_role && <span className="opacity-80"> · {a.actor_role}</span>}
          </div>
          {a.notes && <p className="mt-1.5 whitespace-pre-wrap text-[11px] leading-snug">{a.notes}</p>}
        </div>
      ))}
    </div>
  )
}

function ActionDialog({
  title,
  description,
  confirmLabel,
  confirmClassName,
  fieldLabel,
  fieldPlaceholder,
  fieldHint,
  required,
  minLength = 1,
  isPending,
  error,
  onConfirm,
  onClose,
  children,
}: {
  title: string
  description: React.ReactNode
  confirmLabel: string
  confirmClassName?: string
  fieldLabel: string
  fieldPlaceholder: string
  fieldHint?: string
  required: boolean
  minLength?: number
  isPending: boolean
  error: string | null
  onConfirm: (text: string) => void
  onClose: () => void
  children?: React.ReactNode
}) {
  const [text, setText] = React.useState("")
  const trimmed = text.trim()
  const valid = required ? trimmed.length >= minLength : true
  const [attempted, setAttempted] = React.useState(false)
  const fieldError =
    attempted && !valid
      ? trimmed.length === 0
        ? `${fieldLabel} is required.`
        : `${fieldLabel} must be at least ${minLength} characters (currently ${trimmed.length}).`
      : null

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !isPending) onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose, isPending])

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-2 sm:p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !isPending) onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="decision-dialog-title"
        className="flex max-h-[92vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-border bg-background shadow-2xl"
      >
        <div className="border-b border-border px-4 sm:px-5 py-3 sm:py-4">
          <h2 id="decision-dialog-title" className="text-base font-bold text-foreground">
            {title}
          </h2>
          <div className="mt-1 text-xs text-muted-foreground">{description}</div>
        </div>
        <div className="flex flex-col gap-3 overflow-y-auto p-4 sm:p-5">
          {children}
          <label htmlFor="decision-note" className="text-sm font-medium text-foreground">
            {fieldLabel} {required && <span className="text-destructive">*</span>}
          </label>
          <textarea
            id="decision-note"
            rows={4}
            autoFocus
            value={text}
            disabled={isPending}
            onChange={(e) => setText(e.target.value)}
            placeholder={fieldPlaceholder}
            aria-invalid={Boolean(fieldError)}
            aria-describedby="decision-note-error"
            className={cn(
              "w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:ring-2 focus:ring-primary",
              fieldError && invalidFieldClass,
            )}
          />
          <FieldError id="decision-note-error" message={fieldError} className="mt-0" />
          {fieldHint && (
            <p className="text-[11px] text-muted-foreground">
              {fieldHint}
              {required && minLength > 1 && (
                <span className={cn("ml-1 tabular-nums", valid ? "text-success" : "")}>
                  ({trimmed.length}/{minLength} characters minimum)
                </span>
              )}
            </p>
          )}
          {error && (
            <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </p>
          )}
          <div className="flex flex-wrap justify-end gap-2 pt-1">
            <Button type="button" variant="ghost" onClick={onClose} disabled={isPending}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={isPending}
              onClick={() => {
                setAttempted(true)
                if (valid) onConfirm(trimmed)
              }}
              className={confirmClassName}
            >
              {isPending && <Loader2Icon className="size-4 animate-spin" />}
              {confirmLabel}
            </Button>
          </div>
        </div>
      </div>
    </div>,
    document.body
  )
}

export function CaseDecisionPanel({
  caseDetail,
  role,
  token,
}: {
  caseDetail: CaseDetail
  role: UserRole | undefined
  token: string
}) {
  const queryClient = useQueryClient()
  const [dialog, setDialog] = React.useState<DialogKind>(null)
  const [error, setError] = React.useState<string | null>(null)

  const { assessment, pipeline } = caseDetail
  // Company reviewers only — a platform admin's access is read-only.
  const isReviewer = hasRank(role, "reviewer_l1")
  const decided = caseDetail.status === "approved" || caseDetail.status === "rejected" || caseDetail.status === "closed"
  const escalated = caseDetail.assigned_tier === "l2"
  const tier = assessment?.tier
  const canApprove = pipeline.complete && assessment !== null

  const onSuccess = () => {
    setDialog(null)
    setError(null)
    void queryClient.invalidateQueries({ queryKey: ["case", caseDetail.id] })
    void queryClient.invalidateQueries({ queryKey: ["caseAuditLog", caseDetail.id] })
    void queryClient.invalidateQueries({ queryKey: ["cases"] })
  }
  const onError = (err: unknown) =>
    setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.")

  const approve = useMutation({
    mutationFn: (note: string | null) => approveCase(caseDetail.id, note, token),
    onSuccess,
    onError,
  })
  const reject = useMutation({
    mutationFn: (reason: string) => rejectCase(caseDetail.id, reason, token),
    onSuccess,
    onError,
  })
  const escalate = useMutation({
    mutationFn: (reason: string) => escalateCase(caseDetail.id, reason, token),
    onSuccess,
    onError,
  })

  const openDialog = (kind: DialogKind) => {
    setError(null)
    setDialog(kind)
  }

  const onApproveClick = () => {
    // Low risk: no friction. Anything higher: confirm + justify.
    if (tier === "low") {
      setError(null)
      approve.mutate(null)
    } else {
      openDialog("approve")
    }
  }

  // ---- submitter / read-only view --------------------------------------
  if (!isReviewer) {
    return (
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <h3 className="text-xs font-bold uppercase tracking-wide text-slate-800">Review status</h3>
        <p className="mt-2 text-xs text-slate-500">
          {caseDetail.status === "approved"
            ? "This case has been approved."
            : caseDetail.status === "rejected"
              ? "This case was rejected. The reviewer's reason is below."
              : "Your case is with the review team. You'll see the decision here."}
        </p>
        <DecisionHistory actions={caseDetail.actions} />
      </div>
    )
  }

  // ---- Reviewer L1 on an L2-escalated case: read-only ---------------------
  if (!caseDetail.can_act) {
    return (
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-bold uppercase tracking-wide text-slate-800">Reviewer decision</h3>
          <span className="inline-flex items-center gap-1 rounded-full border border-rose-200 bg-rose-100 px-2 py-0.5 text-[10px] font-bold uppercase text-rose-700">
            <FlameIcon className="size-3" />
            Escalated · L2
          </span>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          {decided ? (
            <>
              This case is <strong>{caseDetail.status}</strong>.
            </>
          ) : (
            "This case has been escalated to a Reviewer L2. You can view it, but only a Reviewer L2 can approve or reject it."
          )}
        </p>
        <DecisionHistory actions={caseDetail.actions} />
      </div>
    )
  }

  // ---- reviewer view ---------------------------------------------------
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-bold uppercase tracking-wide text-slate-800">Reviewer decision</h3>
        {escalated && !decided && (
          <span className="inline-flex items-center gap-1 rounded-full border border-rose-200 bg-rose-100 px-2 py-0.5 text-[10px] font-bold uppercase text-rose-700">
            <FlameIcon className="size-3" />
            Escalated · L2
          </span>
        )}
      </div>

      {decided ? (
        <p className="mt-2 text-xs text-slate-500">
          This case is <strong>{caseDetail.status}</strong>; no further action is needed.
        </p>
      ) : (
        <>
          {!canApprove && (
            <div
              role="status"
              className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-snug text-amber-800"
            >
              <strong>Approve is unavailable while automated checks are still running.</strong>
              {pipeline.pending.length > 0 && (
                <ul className="mt-1 list-disc pl-4">
                  {pipeline.pending.slice(0, 4).map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                  {pipeline.pending.length > 4 && <li>+{pipeline.pending.length - 4} more</li>}
                </ul>
              )}
            </div>
          )}

          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3 lg:grid-cols-1 xl:grid-cols-3">
            <Button
              type="button"
              disabled={!canApprove || approve.isPending}
              onClick={onApproveClick}
              className="gap-1.5 bg-emerald-600 text-white hover:bg-emerald-700"
              title={canApprove ? undefined : "Wait for the automated checks to finish"}
            >
              {approve.isPending ? <Loader2Icon className="size-4 animate-spin" /> : <CheckCircle2Icon className="size-4" />}
              Approve
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => openDialog("reject")}
              className="gap-1.5 border-rose-200 text-rose-700 hover:bg-rose-50"
            >
              <XCircleIcon className="size-4" />
              Reject
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={escalated}
              onClick={() => openDialog("escalate")}
              className="gap-1.5"
              title={escalated ? "Already escalated to L2" : "Hand this case to a Reviewer L2"}
            >
              <FlameIcon className="size-4" />
              {escalated ? "Escalated" : "Escalate"}
            </Button>
          </div>
          <p className="mt-2 text-[11px] text-slate-400">
            {tier && tier !== "low"
              ? `This case is ${tier} risk — approving it requires a written justification.`
              : escalated
                ? "Escalated to L2 — resolve it with Approve or Reject."
                : "Escalating hands the case to a Reviewer L2; Reviewer L1s keep read-only access."}
          </p>
          {error && dialog === null && (
            <p role="alert" className="mt-2 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </p>
          )}
        </>
      )}

      <DecisionHistory actions={caseDetail.actions} />

      {dialog === "approve" && assessment && (
        <ActionDialog
          title={`Approve this ${assessment.tier}-risk case?`}
          description={
            <>
              Risk score <strong>{assessment.score}/100</strong> ({assessment.tier}). Approving is allowed — you may
              have verified something the system couldn't — but your justification is recorded with the decision.
            </>
          }
          confirmLabel="Approve with justification"
          confirmClassName="bg-emerald-600 text-white hover:bg-emerald-700"
          fieldLabel="Justification"
          fieldPlaceholder="e.g. Confirmed the total directly with the vendor by phone; the flagged edit is their template update."
          fieldHint="Stored with the decision and shown in the audit history."
          required
          minLength={MIN_JUSTIFICATION_CHARS}
          isPending={approve.isPending}
          error={error}
          onConfirm={(text) => approve.mutate(text)}
          onClose={() => setDialog(null)}
        >
          <ul className="max-h-32 space-y-1 overflow-y-auto rounded-md border border-border bg-muted/30 p-2 text-[11px] text-muted-foreground">
            {assessment.triggered_reasons.slice(0, 5).map((r, i) => (
              <li key={`${r.rule_id}-${i}`}>
                <span className="font-mono font-bold text-foreground">+{r.weight}</span>{" "}
                {r.title ? (
                  <>
                    <span className="font-semibold text-foreground">{r.title}</span> — {r.short}
                  </>
                ) : (
                  r.reason
                )}
              </li>
            ))}
            {assessment.triggered_reasons.length > 5 && <li>+{assessment.triggered_reasons.length - 5} more signals</li>}
          </ul>
        </ActionDialog>
      )}

      {dialog === "reject" && (
        <ActionDialog
          title="Reject this case"
          description="The reason is required. It is stored, shown in the audit history, and visible to the submitter."
          confirmLabel="Reject case"
          confirmClassName="bg-rose-600 text-white hover:bg-rose-700"
          fieldLabel="Reason for rejection"
          fieldPlaceholder="What is wrong with this submission?"
          required
          isPending={reject.isPending}
          error={error}
          onConfirm={(text) => reject.mutate(text)}
          onClose={() => setDialog(null)}
        />
      )}

      {dialog === "escalate" && (
        <ActionDialog
          title="Escalate this case"
          description="Hands the case to the Reviewer L2 tier, which approves or rejects it. Reviewer L1s can still view it but can no longer act on it, and it can't be sent back to L1. Its status doesn't change."
          confirmLabel="Escalate to L2"
          fieldLabel="Reason for escalation"
          fieldPlaceholder="Why does this need an L2 reviewer?"
          fieldHint="Stored with the escalation and shown in the audit history."
          required
          isPending={escalate.isPending}
          error={error}
          onConfirm={(text) => escalate.mutate(text)}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  )
}
