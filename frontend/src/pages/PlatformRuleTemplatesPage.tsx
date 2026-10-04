import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2Icon, PlusIcon, SearchIcon } from "lucide-react"

import { ApiError } from "@/api/client"
import { createRuleTemplate, listRuleTemplates, updateRuleTemplate, type RuleTemplate } from "@/api/platform"
import { PlatformShell } from "@/components/platform/PlatformShell"
import { AddRuleModal } from "@/components/settings/AddRuleModal"
import { StatusLine } from "@/components/settings/SettingsShell"
import { useAuth } from "@/hooks/useAuth"
import { FieldError, invalidFieldClass } from "@/components/ui/field-error"
import type { RuleSeverity } from "@/types/settings"

const SEVERITIES: RuleSeverity[] = ["low", "medium", "high"]

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "Something went wrong. Please try again."
}

function TemplateRow({ t, onSaved }: { t: RuleTemplate; onSaved: (msg: string) => void }) {
  const { token } = useAuth()
  const [weight, setWeight] = React.useState(String(t.weight))
  const [severity, setSeverity] = React.useState<RuleSeverity>(t.severity)
  const [active, setActive] = React.useState(t.is_active)
  const [reason, setReason] = React.useState(t.reason_template)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    setWeight(String(t.weight))
    setSeverity(t.severity)
    setActive(t.is_active)
    setReason(t.reason_template)
  }, [t.updated_at, t.weight, t.severity, t.is_active, t.reason_template])

  const w = Number(weight)
  const weightOk = weight.trim() !== "" && Number.isFinite(w) && w >= -100 && w <= 100
  const reasonOk = reason.trim().length >= 3
  const valid = weightOk && reasonOk
  const dirty = w !== t.weight || severity !== t.severity || active !== t.is_active || reason.trim() !== t.reason_template

  const save = useMutation({
    mutationFn: () =>
      updateRuleTemplate(
        t.rule_id,
        {
          ...(w !== t.weight ? { weight: w } : {}),
          ...(severity !== t.severity ? { severity } : {}),
          ...(active !== t.is_active ? { is_active: active } : {}),
          ...(reason.trim() !== t.reason_template ? { reason_template: reason.trim() } : {}),
        },
        token as string
      ),
    onSuccess: (saved) => {
      setError(null)
      onSaved(`Saved ${saved.rule_id}. Companies created from now on start with it; existing companies are unchanged.`)
    },
    onError: (err) => setError(errorText(err)),
  })

  return (
    <tr className={t.is_active ? "" : "bg-slate-50/60"}>
      <td className="px-4 py-3 align-top">
        <div className="font-mono text-[11px] font-bold text-slate-800">{t.rule_id}</div>
        <div className="text-[10px] text-slate-400">{t.check_type.replace(/_/g, " ")}</div>
      </td>
      <td className="px-3 py-3 align-top">
        <textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          rows={2}
          className={`w-full min-w-[260px] rounded-md border border-slate-200 px-2 py-1 text-[11px] text-slate-700 ${reasonOk ? "" : invalidFieldClass}`}
          aria-label={`Reason text for ${t.rule_id}`}
          aria-invalid={!reasonOk}
        />
        {!reasonOk && <FieldError message="Write the reason reviewers will read (at least 3 characters)." />}
        {error && <p className="mt-1 text-[11px] text-rose-600">{error}</p>}
      </td>
      <td className="px-3 py-3 align-top">
        <input
          value={weight}
          onChange={(e) => setWeight(e.target.value)}
          className={`w-16 rounded-md border border-slate-200 px-2 py-1 text-xs tabular-nums ${weightOk ? "" : invalidFieldClass}`}
          aria-label={`Weight for ${t.rule_id}`}
          aria-invalid={!weightOk}
        />
        {!weightOk && <FieldError message="-100 to 100" />}
      </td>
      <td className="px-3 py-3 align-top">
        <select
          value={severity}
          onChange={(e) => setSeverity(e.target.value as RuleSeverity)}
          className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs"
          aria-label={`Severity for ${t.rule_id}`}
        >
          {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </td>
      <td className="px-3 py-3 align-top">
        <label className="flex items-center gap-1.5 text-xs text-slate-600">
          <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
          Active
        </label>
      </td>
      <td className="px-4 py-3 text-right align-top">
        <button
          type="button"
          disabled={!dirty || !valid || save.isPending}
          onClick={() => save.mutate()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-purple-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
        >
          {save.isPending && <Loader2Icon className="size-3 animate-spin" />}
          Save
        </button>
      </td>
    </tr>
  )
}

export function PlatformRuleTemplatesPage() {
  const { token } = useAuth()
  const queryClient = useQueryClient()
  const [search, setSearch] = React.useState("")
  const [adding, setAdding] = React.useState(false)
  const [notice, setNotice] = React.useState<string | null>(null)

  const { data: templates = [], isLoading, isError } = useQuery({
    queryKey: ["ruleTemplates", token],
    queryFn: () => listRuleTemplates(token as string),
    enabled: Boolean(token),
  })

  const grouped = React.useMemo(() => {
    const q = search.trim().toLowerCase()
    const out: Record<string, RuleTemplate[]> = {}
    for (const t of templates) {
      if (q && !t.rule_id.toLowerCase().includes(q) && !t.reason_template.toLowerCase().includes(q)) continue
      ;(out[t.category] ??= []).push(t)
    }
    return out
  }, [templates, search])

  const refresh = (msg: string) => {
    setNotice(msg)
    void queryClient.invalidateQueries({ queryKey: ["ruleTemplates"] })
  }

  return (
    <PlatformShell
      active="templates"
      title="Risk-rule templates"
      description="The default rule set every NEW company starts with. When a company is created, each active template rule is copied into that company's own Risk Rules, which its Reviewer L2s then tune. Editing a template never changes companies that already exist."
      actions={
        <button
          type="button"
          onClick={() => { setNotice(null); setAdding(true) }}
          className="inline-flex items-center gap-2 bg-purple-600 hover:bg-purple-700 text-white text-xs font-semibold px-4 py-2.5 rounded-lg shadow-sm"
        >
          <PlusIcon className="w-3.5 h-3.5" /> Add template rule
        </button>
      }
    >
      <div className="mb-4 flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 rounded-2xl border border-slate-200/80 bg-white p-3.5 sm:p-4 shadow-2xs">
        <div className="relative w-full md:w-80">
          <SearchIcon className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search rule id or reason…"
            aria-label="Search template rules"
            className="w-full rounded-xl border border-slate-200 bg-slate-50/50 py-2 pl-9 pr-4 text-xs"
          />
        </div>
        <span className="text-[11px] text-slate-400 sm:ml-auto">
          {templates.length} rules · {templates.filter((t) => t.is_active).length} active
        </span>
      </div>
      <div className="mb-3"><StatusLine ok={notice} error={isError ? "Couldn't load the templates." : null} /></div>

      {isLoading ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : (
        Object.entries(grouped).map(([category, rows]) => (
          <div key={category} className="mb-5 overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-2xs">
            <div className="border-b border-slate-100 bg-slate-50/60 px-4 py-2.5 text-[11px] font-bold uppercase tracking-wider text-slate-500">
              {category}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs min-w-[620px]">
                <thead>
                  <tr className="text-[10px] font-bold uppercase tracking-wider text-slate-400">
                    <th className="px-4 py-2">Rule</th>
                    <th className="px-3 py-2">Reason shown to reviewers</th>
                    <th className="px-3 py-2">Weight</th>
                    <th className="px-3 py-2">Severity</th>
                    <th className="px-3 py-2">Status</th>
                    <th className="px-4 py-2" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {rows.map((t) => <TemplateRow key={t.id} t={t} onSaved={refresh} />)}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}

      {adding && (
        <AddRuleModal
          onClose={() => setAdding(false)}
          submit={(payload) => createRuleTemplate(payload, token as string)}
          onCreated={(created) => {
            setAdding(false)
            refresh(`Added ${created.rule_id} to the template. New companies will get it.`)
          }}
        />
      )}
    </PlatformShell>
  )
}
