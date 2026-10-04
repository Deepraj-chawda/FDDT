import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CheckIcon, CopyIcon, EyeIcon, EyeOffIcon, KeyRoundIcon, Loader2Icon, PlusIcon, SearchIcon, SparklesIcon } from "lucide-react"

import { ApiError } from "@/api/client"
import { listUsers, resetUserPassword, updateUser } from "@/api/settings"
import { AddUserModal } from "@/components/settings/AddUserModal"
import { Modal } from "@/components/settings/Modal"
import { SettingsShell, StatusLine } from "@/components/settings/SettingsShell"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import { FieldError, invalidFieldClass } from "@/components/ui/field-error"
import { ROLE_LABELS, ROLE_OPTIONS, type UserRole } from "@/types/auth"
import type { AdminUser } from "@/types/settings"
import { APP_NAME } from "@/lib/appInfo"


const ROLE_STYLES: Record<UserRole, string> = {
  platform_admin: "bg-purple-50 text-purple-700 border-purple-200",
  reviewer_l2: "bg-indigo-50 text-indigo-700 border-indigo-200",
  reviewer_l1: "bg-blue-50 text-blue-700 border-blue-200",
  user: "bg-slate-50 text-slate-600 border-slate-200",
}

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "Something went wrong. Please try again."
}

function initialsFor(u: AdminUser): string {
  const name = u.full_name || u.email
  return (
    name
      .split(/[\s@.]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((p) => p[0])
      .join("")
      .toUpperCase() || "?"
  )
}

function ResetPasswordModal({
  user,
  onClose,
  onSuccess,
}: {
  user: AdminUser
  onClose: () => void
  onSuccess: (message: string) => void
}) {
  const { token } = useAuth()
  const [password, setPassword] = React.useState("")
  const [showPassword, setShowPassword] = React.useState(false)
  const [copied, setCopied] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  const reset = useMutation({
    mutationFn: () => resetUserPassword(user.id, password, token as string),
    onSuccess: () => {
      onSuccess(`Password for ${user.email} has been successfully reset.`)
    },
    onError: (err) => {
      setError(errorText(err))
    },
  })

  const generateRandomPassword = () => {
    const chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%^&*"
    const bytes = crypto.getRandomValues(new Uint32Array(14))
    const generated = Array.from(bytes, (b) => chars[b % chars.length]).join("")
    setPassword(generated)
    setShowPassword(true)
    setCopied(false)
  }

  const copyToClipboard = async () => {
    if (!password) return
    try {
      await navigator.clipboard.writeText(password)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // ignore clipboard write failure
    }
  }

  const isValid = password.length >= 8
  const [attempted, setAttempted] = React.useState(false)
  const passwordError = !password
    ? "Enter a new password, or generate one."
    : isValid
      ? null
      : `The password must be at least 8 characters (currently ${password.length}).`
  const shownPasswordError = attempted ? passwordError : null

  return (
    <Modal
      title={`Reset password for ${user.full_name || user.email}`}
      description="Set a new password for this user account. The user will be required to sign in with this new password."
      onClose={onClose}
      busy={reset.isPending}
    >
      <form
        className="flex flex-col gap-4"
        noValidate
        onSubmit={(e) => {
          e.preventDefault()
          setError(null)
          setAttempted(true)
          if (passwordError) return
          reset.mutate()
        }}
      >
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label htmlFor="reset-password-input" className="block text-xs font-semibold text-slate-700">
              New password <span className="text-rose-600">*</span>
            </label>
            <button
              type="button"
              onClick={generateRandomPassword}
              className="inline-flex items-center gap-1 text-[11px] font-semibold text-blue-600 hover:text-blue-700 transition cursor-pointer"
            >
              <SparklesIcon className="size-3" />
              Generate secure password
            </button>
          </div>
          <div className="relative">
            <input
              id="reset-password-input"
              type={showPassword ? "text" : "password"}
              autoComplete="new-password"
              autoFocus
              value={password}
              onChange={(e) => {
                setPassword(e.target.value)
                setCopied(false)
              }}
              onBlur={() => password && setAttempted(true)}
              aria-invalid={Boolean(shownPasswordError)}
              aria-describedby="reset-password-error"
              placeholder="Enter at least 8 characters..."
              className={`w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 pr-20 text-xs sm:text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 font-mono ${shownPasswordError ? invalidFieldClass : ""}`}
            />
            <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-1 text-slate-400">
              {password && (
                <button
                  type="button"
                  onClick={copyToClipboard}
                  title="Copy password"
                  className="p-1 hover:text-slate-700 transition rounded cursor-pointer"
                >
                  {copied ? <CheckIcon className="size-4 text-emerald-600" /> : <CopyIcon className="size-4" />}
                </button>
              )}
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                title={showPassword ? "Hide password" : "Show password"}
                className="p-1 hover:text-slate-700 transition rounded cursor-pointer"
              >
                {showPassword ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}
              </button>
            </div>
          </div>
          <FieldError id="reset-password-error" message={shownPasswordError} />
          <p className="mt-1.5 text-[11px] text-slate-400">
            Must be at least 8 characters long. The password is hashed using bcrypt before saving.
          </p>
        </div>

        <StatusLine error={error} />

        <div className="flex justify-end gap-2 pt-2 border-t border-slate-100">
          <button
            type="button"
            onClick={onClose}
            disabled={reset.isPending}
            className="rounded-lg px-3.5 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-100 transition cursor-pointer"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={reset.isPending}
            className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-blue-700 disabled:opacity-50 transition cursor-pointer"
          >
            {reset.isPending && <Loader2Icon className="size-3.5 animate-spin" />}
            Reset password
          </button>
        </div>
      </form>
    </Modal>
  )
}

export function SettingsUsersPage() {
  const { token, user: me } = useAuth()
  const { companies } = useActingCompany()
  const queryClient = useQueryClient()
  const isPlatformAdmin = me?.role === "platform_admin"
  const [search, setSearch] = React.useState("")
  const [roleFilter, setRoleFilter] = React.useState<UserRole | "">("")
  // "" = everyone, "platform" = platform admins, otherwise a company id.
  const [companyFilter, setCompanyFilter] = React.useState<string>("")
  const [notice, setNotice] = React.useState<string | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [confirmDeactivate, setConfirmDeactivate] = React.useState<AdminUser | null>(null)
  const [resetPasswordUser, setResetPasswordUser] = React.useState<AdminUser | null>(null)
  const [adding, setAdding] = React.useState(false)

  const { data: users = [], isLoading, isError } = useQuery({
    queryKey: ["adminUsers", token],
    queryFn: () => listUsers(token as string),
    enabled: Boolean(token),
  })

  const update = useMutation({
    mutationFn: ({ id, ...payload }: { id: string; role?: UserRole; is_active?: boolean; company_id?: string | null }) =>
      updateUser(id, payload, token as string),
    onSuccess: (saved, vars) => {
      setError(null)
      setConfirmDeactivate(null)
      setNotice(
        vars.company_id !== undefined
          ? `${saved.email} moved to ${saved.company_name ?? "the platform team"}. They must sign in again.`
          : vars.role !== undefined
          ? `${saved.email} is now ${ROLE_LABELS[saved.role]}. It takes effect on their next request.`
          : saved.is_active
            ? `Reactivated ${saved.email}.`
            : `Deactivated ${saved.email}. They are signed out and can't log in.`,
      )
      void queryClient.invalidateQueries({ queryKey: ["adminUsers"] })
    },
    onError: (err) => {
      setNotice(null)
      setConfirmDeactivate(null)
      setError(errorText(err))
    },
  })

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase()
    return users.filter((u) => {
      if (roleFilter && u.role !== roleFilter) return false
      if (companyFilter === "platform" && u.company_id) return false
      if (companyFilter && companyFilter !== "platform" && u.company_id !== companyFilter) return false
      return !q || u.email.toLowerCase().includes(q) || (u.full_name ?? "").toLowerCase().includes(q)
    })
  }, [users, search, roleFilter, companyFilter])

  const counts = {
    total: users.length,
    active: users.filter((u) => u.is_active).length,
    admins: users.filter((u) => u.role === "platform_admin" && u.is_active).length,
    reviewers: users.filter((u) => (u.role === "reviewer_l1" || u.role === "reviewer_l2") && u.is_active).length,
  }

  return (
    <SettingsShell
      active="users"
      actions={
        <button
          type="button"
          onClick={() => { setNotice(null); setError(null); setAdding(true) }}
          className="inline-flex items-center space-x-2 bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold px-4 py-2.5 rounded-lg shadow-sm shadow-blue-500/25 transition-all"
        >
          <PlusIcon className="w-3.5 h-3.5" />
          <span>Add user</span>
        </button>
      }
      description={`Every account on ${APP_NAME}, across all companies. Only the platform team creates accounts and assigns each to a company and role — companies cannot manage users and there is no self-service sign-up. Role and status changes apply immediately.`}
    >
      <div className="mb-6 grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        {[
          { label: "Accounts", value: counts.total },
          { label: "Active", value: counts.active },
          { label: "Reviewers", value: counts.reviewers },
          { label: "Platform admins", value: counts.admins },
        ].map((c) => (
          <div key={c.label} className="rounded-2xl border border-slate-200/80 bg-white p-4 sm:p-5 shadow-2xs">
            <div className="text-[10px] sm:text-[11px] font-bold uppercase tracking-wider text-slate-400">{c.label}</div>
            <div className="mt-1 sm:mt-2 text-2xl sm:text-3xl font-extrabold text-slate-900">{c.value}</div>
          </div>
        ))}
      </div>

      <div className="mb-4 flex flex-col gap-3 rounded-2xl border border-slate-200/80 bg-white p-3.5 sm:p-4 shadow-2xs md:flex-row md:items-center md:justify-between">
        <div className="relative w-full md:w-80">
          <SearchIcon className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search users"
            placeholder="Search name or email…"
            className="w-full rounded-xl border border-slate-200 bg-slate-50/50 py-2 pl-9 pr-4 text-xs text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
          />
        </div>
        <div className="flex flex-wrap items-center gap-2.5">
          <select
            value={roleFilter}
            onChange={(e) => setRoleFilter(e.target.value as UserRole | "")}
            aria-label="Filter by role"
            className="flex-1 sm:flex-none rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700"
          >
            <option value="">Role: all</option>
            {ROLE_OPTIONS.map((r) => (
              <option key={r.value} value={r.value}>{r.label}</option>
            ))}
          </select>
          <select
            value={companyFilter}
            onChange={(e) => setCompanyFilter(e.target.value)}
            aria-label="Filter by company"
            className="flex-1 sm:flex-none rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700"
          >
            <option value="">Company: all</option>
            <option value="platform">Platform team (no company)</option>
            {companies.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="mb-3 space-y-2">
        <StatusLine ok={notice} error={error} />
      </div>

      <div className="overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-2xs">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-xs min-w-[780px]">
            <thead>
              <tr className="border-b border-slate-200/70 bg-slate-50/50 text-[11px] font-bold uppercase tracking-wider text-slate-400">
                <th className="px-5 py-3.5">User</th>
                <th className="px-4 py-3.5">Company</th>
                <th className="px-4 py-3.5">Role</th>
                <th className="px-4 py-3.5">Status</th>
                <th className="px-4 py-3.5">Created</th>
                <th className="px-4 py-3.5 text-center">Password</th>
                <th className="px-5 py-3.5 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {isLoading ? (
                <tr><td colSpan={7} className="py-12 text-center text-sm text-slate-400">Loading users…</td></tr>
              ) : isError ? (
                <tr><td colSpan={7} className="py-12 text-center text-sm text-rose-600">Couldn't load users.</td></tr>
              ) : filtered.length === 0 ? (
                <tr><td colSpan={7} className="py-12 text-center text-sm text-slate-400">No users match the current filters.</td></tr>
              ) : (
                filtered.map((u) => {
                  const isMe = me?.id === u.id
                  const busy = update.isPending && update.variables?.id === u.id
                  return (
                    <tr key={u.id} className={u.is_active ? "hover:bg-blue-50/30" : "bg-slate-50/60"}>
                      <td className="px-5 py-4">
                        <div className="flex items-center gap-3">
                          <div className={`flex size-8 items-center justify-center rounded-full text-[11px] font-bold ${u.is_active ? "bg-blue-100 text-blue-700" : "bg-slate-200 text-slate-500"}`}>
                            {initialsFor(u)}
                          </div>
                          <div className="min-w-0">
                            <div className={`font-bold ${u.is_active ? "text-slate-900" : "text-slate-500"}`}>
                              {u.full_name || u.email}
                              {isMe && <span className="ml-2 rounded bg-slate-100 px-1.5 py-px text-[9px] font-bold uppercase text-slate-500">You</span>}
                            </div>
                            {u.full_name && <div className="text-[11px] text-slate-400">{u.email}</div>}
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-4">
                        {u.role === "platform_admin" ? (
                          <span className="text-[11px] font-semibold text-purple-700">Platform team</span>
                        ) : (
                          <select
                            value={u.company_id ?? ""}
                            disabled={busy}
                            aria-label={`Company for ${u.email}`}
                            onChange={(e) => {
                              setNotice(null)
                              update.mutate({ id: u.id, company_id: e.target.value })
                            }}
                            className="max-w-[180px] rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] font-semibold text-slate-700"
                          >
                            {companies.map((c) => (
                              <option key={c.id} value={c.id}>{c.name}</option>
                            ))}
                          </select>
                        )}
                      </td>
                      <td className="px-4 py-4">
                        <select
                          value={u.role}
                          disabled={isMe || busy}
                          aria-label={`Role for ${u.email}`}
                          title={isMe ? "You can't change your own role" : ROLE_OPTIONS.find((r) => r.value === u.role)?.help}
                          onChange={(e) => {
                            setNotice(null)
                            const role = e.target.value as UserRole
                            // A platform admin becoming a company user needs a company.
                            const needsCompany = u.role === "platform_admin" && role !== "platform_admin"
                            const company = companyFilter && companyFilter !== "platform" ? companyFilter : companies[0]?.id
                            update.mutate(needsCompany ? { id: u.id, role, company_id: company ?? null } : { id: u.id, role })
                          }}
                          className={`rounded-full border px-2.5 py-1 text-[11px] font-bold disabled:opacity-70 ${ROLE_STYLES[u.role]}`}
                        >
                          {ROLE_OPTIONS.map((r) => (
                            <option key={r.value} value={r.value}>{r.label}</option>
                          ))}
                        </select>
                      </td>
                      <td className="px-4 py-4">
                        {u.is_active ? (
                          <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-emerald-600"><span className="size-1.5 rounded-full bg-emerald-500" /> Active</span>
                        ) : (
                          <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-slate-400"><span className="size-1.5 rounded-full bg-slate-300" /> Deactivated</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-4 text-slate-500">
                        {new Date(u.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
                      </td>
                      <td className="whitespace-nowrap px-4 py-4 text-center">
                        {isPlatformAdmin ? (
                          <button
                            type="button"
                            onClick={() => {
                              setError(null)
                              setNotice(null)
                              setResetPasswordUser(u)
                            }}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50 hover:border-slate-300 transition shadow-2xs cursor-pointer"
                            title={`Reset password for ${u.email}`}
                          >
                            <KeyRoundIcon className="size-3.5 text-blue-600" />
                            <span>Reset password</span>
                          </button>
                        ) : (
                          <span className="text-[11px] text-slate-300">—</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-5 py-4 text-right">
                        {isMe ? (
                          <span className="text-[11px] text-slate-300">—</span>
                        ) : u.is_active ? (
                          <button type="button" disabled={busy} onClick={() => setConfirmDeactivate(u)} className="rounded-lg px-3 py-1.5 text-xs font-semibold text-rose-600 hover:bg-rose-50 disabled:opacity-50">
                            Deactivate
                          </button>
                        ) : (
                          <button type="button" disabled={busy} onClick={() => update.mutate({ id: u.id, is_active: true })} className="rounded-lg px-3 py-1.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-50 disabled:opacity-50">
                            Reactivate
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {adding && (
        <AddUserModal
          onClose={() => setAdding(false)}
          onCreated={(created) => {
            setAdding(false)
            setError(null)
            setNotice(`Added ${created.email} as ${ROLE_LABELS[created.role]}. They can sign in now.`)
            void queryClient.invalidateQueries({ queryKey: ["adminUsers"] })
          }}
        />
      )}

      {confirmDeactivate && (
        <Modal
          title={`Deactivate ${confirmDeactivate.full_name || confirmDeactivate.email}?`}
          description="They will be signed out and unable to log in until reactivated. Their past cases and audit history are kept."
          onClose={() => setConfirmDeactivate(null)}
          busy={update.isPending}
        >
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setConfirmDeactivate(null)} disabled={update.isPending} className="rounded-lg px-4 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-100">
              Cancel
            </button>
            <button
              type="button"
              disabled={update.isPending}
              onClick={() => update.mutate({ id: confirmDeactivate.id, is_active: false })}
              className="inline-flex items-center gap-2 rounded-lg bg-rose-600 px-4 py-2 text-xs font-semibold text-white hover:bg-rose-700 disabled:opacity-60"
            >
              {update.isPending && <Loader2Icon className="size-3.5 animate-spin" />}
              Deactivate account
            </button>
          </div>
        </Modal>
      )}

      {resetPasswordUser && (
        <ResetPasswordModal
          user={resetPasswordUser}
          onClose={() => setResetPasswordUser(null)}
          onSuccess={(msg) => {
            setResetPasswordUser(null)
            setError(null)
            setNotice(msg)
          }}
        />
      )}
    </SettingsShell>
  )
}
