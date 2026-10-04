import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2Icon, PlusIcon } from "lucide-react"

import { ApiError } from "@/api/client"
import { createCompany, listCompanies, updateCompany, type Company } from "@/api/platform"
import { PlatformShell } from "@/components/platform/PlatformShell"
import { Modal } from "@/components/settings/Modal"
import { StatusLine } from "@/components/settings/SettingsShell"
import { useAuth } from "@/hooks/useAuth"

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "Something went wrong. Please try again."
}

export function PlatformCompaniesPage() {
  const { token } = useAuth()
  const queryClient = useQueryClient()
  const [adding, setAdding] = React.useState(false)
  const [name, setName] = React.useState("")
  const [notice, setNotice] = React.useState<string | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [confirmSuspend, setConfirmSuspend] = React.useState<Company | null>(null)

  const { data: companies = [], isLoading, isError } = useQuery({
    queryKey: ["companies", token],
    queryFn: () => listCompanies(token as string),
    enabled: Boolean(token),
  })

  const create = useMutation({
    mutationFn: () => createCompany(name.trim(), token as string),
    onSuccess: (company) => {
      setAdding(false)
      setName("")
      setError(null)
      setNotice(`Created ${company.name} with the default risk-rule set. Add its users under Users.`)
      void queryClient.invalidateQueries({ queryKey: ["companies"] })
    },
    onError: (err) => setError(errorText(err)),
  })

  const toggle = useMutation({
    mutationFn: (c: Company) => updateCompany(c.id, { is_active: !c.is_active }, token as string),
    onSuccess: (c) => {
      setConfirmSuspend(null)
      setError(null)
      setNotice(c.is_active ? `Reactivated ${c.name}.` : `Suspended ${c.name}: its users are signed out and can't log in.`)
      void queryClient.invalidateQueries({ queryKey: ["companies"] })
    },
    onError: (err) => {
      setConfirmSuspend(null)
      setError(errorText(err))
    },
  })

  return (
    <PlatformShell
      active="companies"
      title="Companies"
      description="Each client company is a fully isolated tenant: its users see only its own cases, issuer registry and risk rules. A new company starts with the default risk-rule set, which its Reviewer L2s can then tune."
      actions={
        <button
          type="button"
          onClick={() => { setNotice(null); setError(null); setAdding(true) }}
          className="inline-flex items-center gap-2 bg-purple-600 hover:bg-purple-700 text-white text-xs font-semibold px-4 py-2.5 rounded-lg shadow-sm"
        >
          <PlusIcon className="w-3.5 h-3.5" /> New company
        </button>
      }
    >
      <div className="mb-3"><StatusLine ok={notice} error={error} /></div>
      <div className="overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-2xs">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs min-w-[550px]">
            <thead>
              <tr className="border-b border-slate-200/70 bg-slate-50/50 text-[11px] font-bold uppercase tracking-wider text-slate-400">
                <th className="px-5 py-3.5">Company</th>
                <th className="px-4 py-3.5">Users</th>
                <th className="px-4 py-3.5">Status</th>
                <th className="px-4 py-3.5">Created</th>
                <th className="px-5 py-3.5 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {isLoading ? (
                <tr><td colSpan={5} className="py-12 text-center text-sm text-slate-400">Loading companies…</td></tr>
              ) : isError ? (
                <tr><td colSpan={5} className="py-12 text-center text-sm text-rose-600">Couldn't load companies.</td></tr>
              ) : companies.length === 0 ? (
                <tr><td colSpan={5} className="py-12 text-center text-sm text-slate-400">No companies yet.</td></tr>
              ) : (
                companies.map((c) => (
                  <tr key={c.id} className={c.is_active ? "" : "bg-slate-50/60"}>
                    <td className="px-5 py-4 font-bold text-slate-900">{c.name}</td>
                    <td className="px-4 py-4 text-slate-600">{c.user_count}</td>
                    <td className="px-4 py-4">
                      {c.is_active ? (
                        <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-emerald-600"><span className="size-1.5 rounded-full bg-emerald-500" /> Active</span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-slate-400"><span className="size-1.5 rounded-full bg-slate-300" /> Suspended</span>
                      )}
                    </td>
                    <td className="px-4 py-4 text-slate-500">
                      {new Date(c.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
                    </td>
                    <td className="px-5 py-4 text-right">
                      {c.is_active ? (
                        <button type="button" onClick={() => setConfirmSuspend(c)} className="rounded-lg px-3 py-1.5 text-xs font-semibold text-rose-600 hover:bg-rose-50">
                          Suspend
                        </button>
                      ) : (
                        <button type="button" onClick={() => toggle.mutate(c)} className="rounded-lg px-3 py-1.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-50">
                          Reactivate
                        </button>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {adding && (
        <Modal title="New company" description="Creates an isolated tenant seeded with the default risk rules." onClose={() => setAdding(false)} busy={create.isPending}>
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              if (name.trim().length < 2) return setError("Enter the company's name.")
              create.mutate()
            }}
          >
            <div>
              <label htmlFor="company-name" className="mb-1 block text-xs font-semibold text-slate-700">Company name</label>
              <input
                id="company-name"
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-purple-500 focus:outline-none focus:ring-2 focus:ring-purple-500/20"
              />
            </div>
            <StatusLine error={error} />
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setAdding(false)} className="rounded-lg px-4 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-100">Cancel</button>
              <button type="submit" disabled={create.isPending} className="inline-flex items-center gap-2 rounded-lg bg-purple-600 px-4 py-2 text-xs font-semibold text-white hover:bg-purple-700 disabled:opacity-60">
                {create.isPending && <Loader2Icon className="size-3.5 animate-spin" />} Create company
              </button>
            </div>
          </form>
        </Modal>
      )}

      {confirmSuspend && (
        <Modal
          title={`Suspend ${confirmSuspend.name}?`}
          description="All of its users are signed out and can't log in until it is reactivated. Its data is kept."
          onClose={() => setConfirmSuspend(null)}
          busy={toggle.isPending}
        >
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setConfirmSuspend(null)} className="rounded-lg px-4 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-100">Cancel</button>
            <button type="button" onClick={() => toggle.mutate(confirmSuspend)} className="rounded-lg bg-rose-600 px-4 py-2 text-xs font-semibold text-white hover:bg-rose-700">
              Suspend company
            </button>
          </div>
        </Modal>
      )}
    </PlatformShell>
  )
}
