// Platform admins only: delete (soft) or restore a case. A deleted case
// disappears for its company everywhere and nothing tells them it existed;
// it is kept and can be restored here. Only the platform audit log records it
// (backend/app/api/cases.py).
import * as React from "react"
import { createPortal } from "react-dom"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Loader2Icon, RotateCcwIcon, Trash2Icon } from "lucide-react"

import { deleteCase, restoreCase } from "@/api/cases"
import { ApiError } from "@/api/client"
import { Button } from "@/components/ui/button"
import type { CaseDetail } from "@/types/case"

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "Something went wrong. Please try again."
}

export function CaseDeleteControl({ caseDetail, token }: { caseDetail: CaseDetail; token: string }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = React.useState(false)
  const [reason, setReason] = React.useState("")
  const deleted = Boolean(caseDetail.deleted_at)

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["case", caseDetail.id] }).then(
    () => queryClient.invalidateQueries({ queryKey: ["cases"] }),
  )
  const remove = useMutation({
    mutationFn: () => deleteCase(caseDetail.id, reason.trim() || null, token),
    onSuccess: () => {
      setOpen(false)
      setReason("")
      return refresh()
    },
  })
  const restore = useMutation({ mutationFn: () => restoreCase(caseDetail.id, token), onSuccess: refresh })

  if (deleted) {
    return (
      <div className="flex flex-col items-end gap-1">
        <Button type="button" variant="outline" size="sm" disabled={restore.isPending} onClick={() => restore.mutate()}>
          {restore.isPending ? <Loader2Icon className="size-4 animate-spin" /> : <RotateCcwIcon className="size-4" />}
          Restore case
        </Button>
        {restore.isError && <p role="alert" className="text-[11px] text-destructive">{errorMessage(restore.error)}</p>}
      </div>
    )
  }

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="border-rose-200 text-rose-700 hover:bg-rose-50 hover:text-rose-800"
        onClick={() => setOpen(true)}
      >
        <Trash2Icon className="size-4" />
        Delete case
      </Button>
      {open &&
        createPortal(
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-2 sm:p-4"
            onClick={(e) => {
              if (e.target === e.currentTarget && !remove.isPending) setOpen(false)
            }}
          >
            <div
              role="dialog"
              aria-modal="true"
              aria-labelledby="delete-case-title"
              className="flex w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-border bg-background shadow-2xl"
            >
              <div className="border-b border-border px-4 sm:px-5 py-3 sm:py-4">
                <h2 id="delete-case-title" className="text-base font-bold text-foreground">
                  Delete {caseDetail.case_number}?
                </h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  The case disappears for the company everywhere (queues, dashboards, bulk uploads, audit
                  history, duplicate checks) and they are not told. Its documents and results are kept, and you
                  can restore it from this page.
                </p>
              </div>
              <div className="flex flex-col gap-3 p-4 sm:p-5">
                <label htmlFor="delete-case-reason" className="text-sm font-medium text-foreground">
                  Reason <span className="font-normal text-muted-foreground">(optional, platform audit log only)</span>
                </label>
                <textarea
                  id="delete-case-reason"
                  rows={3}
                  autoFocus
                  maxLength={1000}
                  value={reason}
                  disabled={remove.isPending}
                  onChange={(e) => setReason(e.target.value)}
                  className="w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:ring-2 focus:ring-primary"
                />
                {remove.isError && (
                  <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
                    {errorMessage(remove.error)}
                  </p>
                )}
                <div className="flex flex-wrap justify-end gap-2 pt-1">
                  <Button type="button" variant="ghost" onClick={() => setOpen(false)} disabled={remove.isPending}>
                    Cancel
                  </Button>
                  <Button
                    type="button"
                    disabled={remove.isPending}
                    onClick={() => remove.mutate()}
                    className="bg-rose-600 text-white hover:bg-rose-700"
                  >
                    {remove.isPending && <Loader2Icon className="size-4 animate-spin" />}
                    Delete case
                  </Button>
                </div>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}
