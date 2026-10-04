import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2Icon, PlusIcon, SearchIcon } from "lucide-react"

import { ApiError } from "@/api/client"
import { createIssuer, listIssuers, updateIssuer } from "@/api/settings"
import { Modal } from "@/components/settings/Modal"
import { SettingsShell, StatusLine } from "@/components/settings/SettingsShell"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import { FieldError, invalidFieldClass } from "@/components/ui/field-error"
import { ISSUER_TYPE_LABELS, type Issuer, type IssuerType } from "@/types/settings"

const TYPE_STYLES: Record<IssuerType, string> = {
  vendor: "bg-blue-50 text-blue-700 border-blue-200",
  school: "bg-amber-50 text-amber-700 border-amber-200",
  government: "bg-emerald-50 text-emerald-700 border-emerald-200",
  other: "bg-slate-50 text-slate-600 border-slate-200",
}

interface FormState {
  name: string
  name_arabic: string
  tax_id: string
  type: IssuerType
}

const EMPTY_FORM: FormState = { name: "", name_arabic: "", tax_id: "", type: "vendor" }

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "Something went wrong. Please try again."
}

function IssuerForm({
  issuer,
  onClose,
  onSaved,
}: {
  issuer: Issuer | null
  onClose: () => void
  onSaved: (message: string) => void
}) {
  const { token } = useAuth()
  const { platformCompanyParam: cid } = useActingCompany()
  const [form, setForm] = React.useState<FormState>(
    issuer
      ? { name: issuer.name, name_arabic: issuer.name_arabic ?? "", tax_id: issuer.tax_id ?? "", type: issuer.type }
      : EMPTY_FORM,
  )
  const [error, setError] = React.useState<string | null>(null)

  const save = useMutation({
    mutationFn: () => {
      const payload = {
        name: form.name.trim(),
        name_arabic: form.name_arabic.trim() || null,
        tax_id: form.tax_id.trim() || null,
        type: form.type,
      }
      return issuer ? updateIssuer(issuer.id, payload, token!, cid) : createIssuer(payload, token!, cid)
    },
    onSuccess: (saved) => onSaved(issuer ? `Saved changes to ${saved.name}.` : `Added ${saved.name} to the registry.`),
    onError: (err) => setError(errorText(err)),
  })

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((f) => ({ ...f, [key]: value }))
  const [nameTouched, setNameTouched] = React.useState(false)
  const nameError = nameTouched && !form.name.trim() ? "Enter the issuer's name in English." : null
  const inputClass =
    "w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"

  return (
    <Modal
      title={issuer ? "Edit issuer" : "Add issuer"}
      description="Issuers are matched (fuzzily, in English or Arabic) against the issuer extracted from each document."
      onClose={onClose}
      busy={save.isPending}
    >
      <form
        className="flex flex-col gap-4"
        noValidate
        onSubmit={(e) => {
          e.preventDefault()
          setError(null)
          setNameTouched(true)
          if (!form.name.trim()) return
          save.mutate()
        }}
      >
        <div>
          <label htmlFor="issuer-name" className="mb-1 block text-xs font-semibold text-slate-700">
            Name (English) <span className="text-rose-600">*</span>
          </label>
          <input
            id="issuer-name"
            className={nameError ? `${inputClass} ${invalidFieldClass}` : inputClass}
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            onBlur={() => setNameTouched(true)}
            aria-invalid={Boolean(nameError)}
            aria-describedby="issuer-name-error"
            autoFocus
          />
          <FieldError id="issuer-name-error" message={nameError} />
        </div>
        <div>
          <label htmlFor="issuer-name-ar" className="mb-1 block text-xs font-semibold text-slate-700">
            Name (Arabic)
          </label>
          <input
            id="issuer-name-ar"
            className={inputClass}
            dir="rtl"
            lang="ar"
            value={form.name_arabic}
            onChange={(e) => set("name_arabic", e.target.value)}
            placeholder="الاسم بالعربية (اختياري)"
          />
          <p className="mt-1 text-[11px] text-slate-400">
            Optional. Lets an Arabic-script issuer on a document match this entry.
          </p>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label htmlFor="issuer-tax" className="mb-1 block text-xs font-semibold text-slate-700">
              Tax ID
            </label>
            <input id="issuer-tax" className={inputClass} value={form.tax_id} onChange={(e) => set("tax_id", e.target.value)} />
          </div>
          <div>
            <label htmlFor="issuer-type" className="mb-1 block text-xs font-semibold text-slate-700">
              Type
            </label>
            <select id="issuer-type" className={inputClass} value={form.type} onChange={(e) => set("type", e.target.value as IssuerType)}>
              {(Object.keys(ISSUER_TYPE_LABELS) as IssuerType[]).map((t) => (
                <option key={t} value={t}>
                  {ISSUER_TYPE_LABELS[t]}
                </option>
              ))}
            </select>
          </div>
        </div>
        <StatusLine error={error} />
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} disabled={save.isPending} className="rounded-lg px-4 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-100">
            Cancel
          </button>
          <button
            type="submit"
            disabled={save.isPending}
            className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-blue-700 disabled:opacity-60"
          >
            {save.isPending && <Loader2Icon className="size-3.5 animate-spin" />}
            {issuer ? "Save changes" : "Add issuer"}
          </button>
        </div>
      </form>
    </Modal>
  )
}

export function SettingsIssuerRegistryPage() {
  const { token } = useAuth()
  const { platformCompanyParam: cid, companyId } = useActingCompany()
  const queryClient = useQueryClient()
  const [search, setSearch] = React.useState("")
  const [typeFilter, setTypeFilter] = React.useState<IssuerType | "">("")
  const [showInactive, setShowInactive] = React.useState(true)
  const [editing, setEditing] = React.useState<Issuer | "new" | null>(null)
  const [notice, setNotice] = React.useState<string | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  const { data: issuers = [], isLoading, isError } = useQuery({
    queryKey: ["issuers", token, companyId],
    queryFn: () => listIssuers(token as string, cid),
    enabled: Boolean(token && companyId),
  })

  const toggleActive = useMutation({
    mutationFn: (issuer: Issuer) => updateIssuer(issuer.id, { is_active: !issuer.is_active }, token as string, cid),
    onSuccess: (saved) => {
      setError(null)
      setNotice(saved.is_active ? `Reactivated ${saved.name}.` : `Deactivated ${saved.name}. Historical cases are unaffected.`)
      void queryClient.invalidateQueries({ queryKey: ["issuers"] })
    },
    onError: (err) => setError(errorText(err)),
  })

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase()
    return issuers.filter((i) => {
      if (!showInactive && !i.is_active) return false
      if (typeFilter && i.type !== typeFilter) return false
      if (!q) return true
      return [i.name, i.name_arabic ?? "", i.tax_id ?? ""].some((v) => v.toLowerCase().includes(q))
    })
  }, [issuers, search, typeFilter, showInactive])

  const activeCount = issuers.filter((i) => i.is_active).length

  return (
    <SettingsShell
      active="issuers"
      description="The registry of known vendors, schools and other issuers that issuer verification matches each document against. Deactivated issuers stop verifying but are never deleted."
      actions={
        <button
          type="button"
          onClick={() => {
            setNotice(null)
            setEditing("new")
          }}
          className="inline-flex items-center space-x-2 bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold px-4 py-2.5 rounded-lg shadow-sm shadow-blue-500/25 transition-all"
        >
          <PlusIcon className="w-3.5 h-3.5" />
          <span>Add issuer</span>
        </button>
      }
    >
      <div className="mb-4 flex flex-col gap-3 rounded-2xl border border-slate-200/80 bg-white p-3.5 sm:p-4 shadow-2xs md:flex-row md:items-center md:justify-between">
        <div className="relative w-full md:w-80">
          <SearchIcon className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search issuers"
            placeholder="Search name, Arabic name or tax ID…"
            className="w-full rounded-xl border border-slate-200 bg-slate-50/50 py-2 pl-9 pr-4 text-xs text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
          />
        </div>
        <div className="flex flex-wrap items-center gap-2.5 sm:gap-3">
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value as IssuerType | "")}
            aria-label="Filter by type"
            className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700"
          >
            <option value="">Type: all</option>
            {(Object.keys(ISSUER_TYPE_LABELS) as IssuerType[]).map((t) => (
              <option key={t} value={t}>
                {ISSUER_TYPE_LABELS[t]}
              </option>
            ))}
          </select>
          <label className="flex cursor-pointer items-center gap-2 text-xs font-medium text-slate-600">
            <input type="checkbox" checked={showInactive} onChange={(e) => setShowInactive(e.target.checked)} className="size-3.5" />
            Show deactivated
          </label>
          <span className="text-xs text-slate-400">
            {activeCount} active of {issuers.length}
          </span>
        </div>
      </div>

      <div className="mb-3 space-y-2">
        <StatusLine ok={notice} error={error} />
      </div>

      <div className="overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-2xs">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-xs min-w-[650px]">
            <thead>
              <tr className="border-b border-slate-200/70 bg-slate-50/50 text-[11px] font-bold uppercase tracking-wider text-slate-400">
                <th className="px-5 py-3.5">Issuer</th>
                <th className="px-4 py-3.5">Arabic name</th>
                <th className="px-4 py-3.5">Tax ID</th>
                <th className="px-4 py-3.5">Type</th>
                <th className="px-4 py-3.5">Status</th>
                <th className="px-5 py-3.5 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {isLoading ? (
                <tr>
                  <td colSpan={6} className="py-12 text-center text-sm text-slate-400">
                    Loading issuers…
                  </td>
                </tr>
              ) : isError ? (
                <tr>
                  <td colSpan={6} className="py-12 text-center text-sm text-rose-600">
                    Couldn't load the issuer registry.
                  </td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-12 text-center text-sm text-slate-400">
                    {issuers.length === 0 ? "No issuers yet — add the first one." : "No issuers match the current filters."}
                  </td>
                </tr>
              ) : (
                filtered.map((issuer) => (
                  <tr key={issuer.id} className={issuer.is_active ? "hover:bg-blue-50/30" : "bg-slate-50/60 text-slate-400"}>
                    <td className="px-5 py-4 font-bold text-slate-900">
                      <span className={issuer.is_active ? "" : "text-slate-500"}>{issuer.name}</span>
                    </td>
                    <td className="px-4 py-4 text-slate-700" dir="rtl" lang="ar">
                      {issuer.name_arabic ?? <span className="text-slate-300">—</span>}
                    </td>
                    <td className="px-4 py-4 font-mono text-slate-600">{issuer.tax_id ?? <span className="text-slate-300">—</span>}</td>
                    <td className="px-4 py-4">
                      <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-bold ${TYPE_STYLES[issuer.type]}`}>
                        {ISSUER_TYPE_LABELS[issuer.type]}
                      </span>
                    </td>
                    <td className="px-4 py-4">
                      {issuer.is_active ? (
                        <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-emerald-600">
                          <span className="size-1.5 rounded-full bg-emerald-500" /> Active
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-slate-400">
                          <span className="size-1.5 rounded-full bg-slate-300" /> Deactivated
                        </span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-5 py-4 text-right">
                      <button
                        type="button"
                        onClick={() => {
                          setNotice(null)
                          setEditing(issuer)
                        }}
                        className="rounded-lg px-3 py-1.5 text-xs font-semibold text-blue-600 hover:bg-blue-50"
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        disabled={toggleActive.isPending}
                        onClick={() => toggleActive.mutate(issuer)}
                        className={
                          issuer.is_active
                            ? "rounded-lg px-3 py-1.5 text-xs font-semibold text-rose-600 hover:bg-rose-50 disabled:opacity-50"
                            : "rounded-lg px-3 py-1.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-50 disabled:opacity-50"
                        }
                      >
                        {issuer.is_active ? "Deactivate" : "Reactivate"}
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {editing !== null && (
        <IssuerForm
          issuer={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(message) => {
            setEditing(null)
            setError(null)
            setNotice(message)
            void queryClient.invalidateQueries({ queryKey: ["issuers"] })
          }}
        />
      )}
    </SettingsShell>
  )
}
