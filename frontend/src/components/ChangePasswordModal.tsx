import * as React from "react"
import { useMutation } from "@tanstack/react-query"
import { CheckCircle2Icon, EyeIcon, EyeOffIcon, Loader2Icon } from "lucide-react"

import { ApiError } from "@/api/client"
import { changePassword } from "@/api/auth"
import { Modal } from "@/components/settings/Modal"
import { FieldError, invalidFieldClass, visibleError } from "@/components/ui/field-error"
import { useAuth } from "@/hooks/useAuth"

const MIN_PASSWORD = 8
const MAX_PASSWORD = 128
const inputClass =
  "w-full rounded-lg border border-slate-200 bg-white px-3 py-2 pr-9 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"

// Self-service password change for every signed-in user: the current
// password is required (POST /auth/me/password). There is no "forgot
// password" here — a platform admin resets a forgotten one.
export function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const { token } = useAuth()
  const [current, setCurrent] = React.useState("")
  const [next, setNext] = React.useState("")
  const [confirm, setConfirm] = React.useState("")
  const [show, setShow] = React.useState(false)
  const [touched, setTouched] = React.useState<Record<string, boolean>>({})
  const [submitted, setSubmitted] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const touch = (field: string) => () => setTouched((t) => ({ ...t, [field]: true }))

  const currentError = current ? null : "Enter your current password."
  const nextError =
    next.length < MIN_PASSWORD
      ? `Use at least ${MIN_PASSWORD} characters.`
      : next.length > MAX_PASSWORD
        ? `Use at most ${MAX_PASSWORD} characters.`
        : next === current
          ? "The new password must differ from the current one."
          : null
  const confirmError = confirm === next ? null : "The two new passwords do not match."
  const errors = { "current-password": currentError, "new-password": nextError, "confirm-password": confirmError }
  const shown = (field: keyof typeof errors) => visibleError(errors, field, touched, submitted)

  const change = useMutation({
    mutationFn: () => changePassword({ current_password: current, new_password: next }, token as string),
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not change the password."),
  })

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    setSubmitted(true)
    setError(null)
    if (currentError || nextError || confirmError) return
    change.mutate()
  }

  if (change.isSuccess) {
    return (
      <Modal title="Password changed" onClose={onClose}>
        <div className="space-y-4">
          <p className="flex items-center gap-2 text-sm text-slate-700">
            <CheckCircle2Icon className="size-5 text-emerald-600" />
            Your password has been changed. Use the new one next time you sign in.
          </p>
          <div className="flex justify-end">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg bg-[#0b1930] px-4 py-2 text-sm font-semibold text-white hover:bg-[#14233e]"
            >
              Done
            </button>
          </div>
        </div>
      </Modal>
    )
  }

  const field = (
    id: string,
    label: string,
    value: string,
    setValue: (v: string) => void,
    message: string | null,
    autoComplete: string,
  ) => (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-xs font-semibold text-slate-700">
        {label}
      </label>
      <div className="relative">
        <input
          id={id}
          type={show ? "text" : "password"}
          value={value}
          autoComplete={autoComplete}
          onChange={(e) => setValue(e.target.value)}
          onBlur={touch(id)}
          aria-invalid={Boolean(message)}
          aria-describedby={`${id}-error`}
          className={`${inputClass} ${message ? invalidFieldClass : ""}`}
        />
      </div>
      <FieldError id={`${id}-error`} message={message} />
    </div>
  )

  return (
    <Modal
      title="Change password"
      description="Enter your current password, then the new one twice. Forgot your password? Ask your administrator to reset it."
      onClose={onClose}
      busy={change.isPending}
    >
      <form onSubmit={submit} className="space-y-4" noValidate>
        {field("current-password", "Current password", current, setCurrent, shown("current-password"), "current-password")}
        {field("new-password", "New password", next, setNext, shown("new-password"), "new-password")}
        {field("confirm-password", "Confirm new password", confirm, setConfirm, shown("confirm-password"), "new-password")}
        <button
          type="button"
          onClick={() => setShow((v) => !v)}
          className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-800"
        >
          {show ? <EyeOffIcon className="size-3.5" /> : <EyeIcon className="size-3.5" />}
          {show ? "Hide passwords" : "Show passwords"}
        </button>
        {error && (
          <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={change.isPending}
            className="rounded-lg border border-slate-200 px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={change.isPending}
            className="inline-flex items-center gap-2 rounded-lg bg-[#0b1930] px-4 py-2 text-sm font-semibold text-white hover:bg-[#14233e] disabled:opacity-60"
          >
            {change.isPending && <Loader2Icon className="size-4 animate-spin" />}
            Change password
          </button>
        </div>
      </form>
    </Modal>
  )
}
