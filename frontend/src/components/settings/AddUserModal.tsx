import * as React from "react"
import { useMutation } from "@tanstack/react-query"
import { Loader2Icon } from "lucide-react"

import { ApiError } from "@/api/client"
import { createUser } from "@/api/settings"
import { Modal } from "@/components/settings/Modal"
import { StatusLine } from "@/components/settings/SettingsShell"
import { FieldError, invalidFieldClass, visibleError } from "@/components/ui/field-error"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import { ROLE_OPTIONS, type UserRole } from "@/types/auth"
import type { AdminUser } from "@/types/settings"


const MIN_PASSWORD = 8
const inputClass =
  "w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"

// A readable random password (no look-alike characters), drawn from the
// browser's crypto RNG rather than Math.random.
function generatePassword(length = 14): string {
  const alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
  const bytes = crypto.getRandomValues(new Uint32Array(length))
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("")
}

export function AddUserModal({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (user: AdminUser) => void
}) {
  const { token } = useAuth()
  const { companies, companyId: actingCompany } = useActingCompany()
  const [companyId, setCompanyId] = React.useState<string>(actingCompany ?? companies[0]?.id ?? "")
  const [email, setEmail] = React.useState("")
  const [fullName, setFullName] = React.useState("")
  const [role, setRole] = React.useState<UserRole>("user")
  const [password, setPassword] = React.useState("")
  const [showPassword, setShowPassword] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  // Field messages show once a field has been left (blurred) or Add user was clicked.
  const [touched, setTouched] = React.useState<Record<string, boolean>>({})
  const [submitted, setSubmitted] = React.useState(false)
  const touch = (field: string) => () => setTouched((t) => ({ ...t, [field]: true }))

  const create = useMutation({
    mutationFn: () =>
      createUser(
        {
          email: email.trim(),
          full_name: fullName.trim() || null,
          role,
          password,
          company_id: role === "platform_admin" ? null : companyId || null,
        },
        token as string
      ),
    onSuccess: onCreated,
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : "Couldn't create the account. Please try again."),
  })

  const emailOk = /^\S+@\S+\.\S+$/.test(email.trim())
  const passwordOk = password.length >= MIN_PASSWORD
  const companyOk = role === "platform_admin" || Boolean(companyId)
  const fieldErrors = {
    email: !email.trim() ? "Enter the user's email address." : emailOk ? null : "Enter a valid email address (e.g. name@company.com).",
    password: !password
      ? "Enter a password, or click Generate."
      : passwordOk
        ? null
        : `The password must be at least ${MIN_PASSWORD} characters (currently ${password.length}).`,
    company: companyOk ? null : "Choose the company this user belongs to.",
  }
  const shown = (field: keyof typeof fieldErrors) => visibleError(fieldErrors, field, touched, submitted)

  return (
    <Modal
      title="Add user"
      description="Creates an account in a company (or on the platform team) that can sign in straight away. There is no self-service sign-up."
      onClose={onClose}
      busy={create.isPending}
    >
      <form
        className="flex flex-col gap-4"
        noValidate
        onSubmit={(e) => {
          e.preventDefault()
          setError(null)
          setSubmitted(true)
          if (fieldErrors.email || fieldErrors.password || fieldErrors.company) return
          create.mutate()
        }}
      >
        <div>
          <label htmlFor="new-user-email" className="mb-1 block text-xs font-semibold text-slate-700">
            Email <span className="text-rose-600">*</span>
          </label>
          <input
            id="new-user-email"
            type="email"
            autoComplete="off"
            className={shown("email") ? `${inputClass} ${invalidFieldClass}` : inputClass}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onBlur={touch("email")}
            aria-invalid={Boolean(shown("email"))}
            aria-describedby="new-user-email-error"
            autoFocus
          />
          <FieldError id="new-user-email-error" message={shown("email")} />
        </div>
        <div>
          <label htmlFor="new-user-name" className="mb-1 block text-xs font-semibold text-slate-700">
            Full name
          </label>
          <input
            id="new-user-name"
            autoComplete="off"
            className={inputClass}
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="new-user-role" className="mb-1 block text-xs font-semibold text-slate-700">
            Role
          </label>
          <select id="new-user-role" className={inputClass} value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
            {ROLE_OPTIONS.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
          <p className="mt-1 text-[11px] text-slate-400">{ROLE_OPTIONS.find((r) => r.value === role)?.help}</p>
        </div>
        {role !== "platform_admin" && (
          <div>
            <label htmlFor="new-user-company" className="mb-1 block text-xs font-semibold text-slate-700">
              Company <span className="text-rose-600">*</span>
            </label>
            <select
              id="new-user-company"
              className={shown("company") ? `${inputClass} ${invalidFieldClass}` : inputClass}
              value={companyId}
              onChange={(e) => setCompanyId(e.target.value)}
              onBlur={touch("company")}
              aria-invalid={Boolean(shown("company"))}
              aria-describedby="new-user-company-error"
            >
              {companies.length === 0 && <option value="">Create a company first (Platform › Companies)</option>}
              {companies.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                  {c.is_active ? "" : " (suspended)"}
                </option>
              ))}
            </select>
            <FieldError id="new-user-company-error" message={shown("company")} />
            <p className="mt-1 text-[11px] text-slate-400">They will only ever see this company's data.</p>
          </div>
        )}
        <div>
          <div className="mb-1 flex items-center justify-between">
            <label htmlFor="new-user-password" className="block text-xs font-semibold text-slate-700">
              Password <span className="text-rose-600">*</span>
            </label>
            <div className="flex items-center gap-3 text-[11px] font-semibold">
              <button
                type="button"
                className="text-blue-600 hover:underline"
                onClick={() => {
                  setPassword(generatePassword())
                  setShowPassword(true)
                }}
              >
                Generate
              </button>
              <button type="button" className="text-slate-500 hover:underline" onClick={() => setShowPassword((v) => !v)}>
                {showPassword ? "Hide" : "Show"}
              </button>
            </div>
          </div>
          <input
            id="new-user-password"
            type={showPassword ? "text" : "password"}
            autoComplete="new-password"
            className={shown("password") ? `${inputClass} ${invalidFieldClass}` : inputClass}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onBlur={touch("password")}
            aria-invalid={Boolean(shown("password"))}
            aria-describedby="new-user-password-error"
          />
          <FieldError id="new-user-password-error" message={shown("password")} />
          <p className="mt-1 text-[11px] text-slate-400">
            At least {MIN_PASSWORD} characters. Share it with them securely — it is stored hashed and can't be shown again.
          </p>
        </div>
        <StatusLine error={error} />
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} disabled={create.isPending} className="rounded-lg px-4 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-100">
            Cancel
          </button>
          <button
            type="submit"
            disabled={create.isPending}
            className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-blue-700 disabled:opacity-60"
          >
            {create.isPending && <Loader2Icon className="size-3.5 animate-spin" />}
            Add user
          </button>
        </div>
      </form>
    </Modal>
  )
}
