import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FileCheckIcon, HistoryIcon, Loader2Icon, PlusIcon, SearchIcon, ShieldAlertIcon, SlidersIcon, ZapIcon } from "lucide-react"

import { ApiError } from "@/api/client"
import { getRiskRuleHistory, getRiskThresholds, listRiskRules, updateRiskRule, updateRiskThresholds } from "@/api/settings"
import { AddRuleModal } from "@/components/settings/AddRuleModal"
import { Modal } from "@/components/settings/Modal"
import { SettingsShell, StatusLine } from "@/components/settings/SettingsShell"
import { useActingCompany } from "@/hooks/useActingCompany"
import { useAuth } from "@/hooks/useAuth"
import { FieldError, invalidFieldClass } from "@/components/ui/field-error"
import type { RiskRule, RuleSeverity } from "@/types/settings"

const CATEGORY_LABELS: Record<string, string> = {
  forensics: "Forensics",
  consistency: "Consistency",
  verification: "Verification",
  duplication: "Duplication",
}
const CATEGORY_ORDER = ["forensics", "consistency", "verification", "duplication"]

const SEVERITY_STYLES: Record<string, string> = {
  high: "bg-rose-50 text-rose-700 border-rose-200",
  medium: "bg-amber-50 text-amber-700 border-amber-200",
  low: "bg-blue-50 text-blue-700 border-blue-200",
}

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "Something went wrong. Please try again."
}

function formatWhen(iso: string): string {
  const d = new Date(iso)
  return `${d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })} ${d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}`
}

// ---------------------------------------------------------------- thresholds

function ThresholdsCard() {
  const { token } = useAuth()
  const { platformCompanyParam: cid, companyId } = useActingCompany()
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ["riskThresholds", token, companyId],
    queryFn: () => getRiskThresholds(token as string, cid),
    enabled: Boolean(token && companyId),
  })
  const [medium, setMedium] = React.useState("")
  const [high, setHigh] = React.useState("")
  const [cap, setCap] = React.useState("")
  const [error, setError] = React.useState<string | null>(null)
  const [ok, setOk] = React.useState<string | null>(null)

  React.useEffect(() => {
    if (data) {
      setMedium(String(data.medium_threshold))
      setHigh(String(data.high_threshold))
      setCap(String(data.metadata_score_cap))
    }
  }, [data])

  const m = Number(medium)
  const h = Number(high)
  const c = Number(cap)
  const isRangeOverlap = Number.isFinite(m) && Number.isFinite(h) && m >= h
  const isCapInvalid = !Number.isInteger(c) || c < 1 || c > 100
  const isValuesInvalid = !Number.isInteger(m) || !Number.isInteger(h) || m < 1 || h > 100
  const valid = !isRangeOverlap && !isCapInvalid && !isValuesInvalid
  const dirty =
    data !== undefined && (m !== data.medium_threshold || h !== data.high_threshold || c !== data.metadata_score_cap)

  const validationErrorMessage = isRangeOverlap
    ? `Medium threshold (${m}) must be strictly lower than High threshold (${h}).`
    : isCapInvalid
      ? "Metadata cap must be an integer between 1 and 100."
      : isValuesInvalid
        ? "Thresholds must be whole numbers from 1 to 100."
        : null

  const save = useMutation({
    mutationFn: () => updateRiskThresholds({ medium_threshold: m, high_threshold: h, metadata_score_cap: c }, token as string, cid),
    onSuccess: () => {
      setError(null)
      setOk("Thresholds saved. They apply to cases scored from now on; already-scored cases keep the thresholds they were scored with.")
      void queryClient.invalidateQueries({ queryKey: ["riskThresholds"] })
    },
    onError: (err) => {
      setOk(null)
      setError(errorText(err))
    },
  })

  const inputClass = "w-20 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-center font-mono text-sm font-bold text-slate-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"

  return (
    <section aria-label="Risk tier thresholds" className="mb-6 rounded-2xl border border-slate-200/80 bg-white p-5 shadow-2xs">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="max-w-xl">
          <h2 className="text-sm font-bold text-slate-900">Risk tier thresholds</h2>
          <p className="mt-1 text-xs text-slate-500">
            A case's score is the sum of its triggered rule weights, capped at 100. These two cutoffs turn that score into a tier.
            The metadata rules together add at most the metadata cap: one edit leaves several metadata traces (modified after
            created, the edit history, a date after the file's creation), which shouldn't max the score on their own.
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-3 sm:gap-4">
          <div className="text-center">
            <div className="mb-1 flex items-center justify-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-emerald-600">
              <span className="size-2 rounded-full bg-emerald-500" /> Low
            </div>
            <div className="rounded-lg bg-slate-50 px-3 py-1.5 font-mono text-sm text-slate-500">0 – {Number.isFinite(m) && m > 0 ? m - 1 : "—"}</div>
          </div>
          <div className="text-center">
            <label htmlFor="medium-threshold" className="mb-1 flex items-center justify-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-amber-600">
              <span className="size-2 rounded-full bg-amber-500" /> Medium from
            </label>
            <input id="medium-threshold" type="number" min={1} max={Number.isFinite(h) && h > 1 ? h - 1 : 99} value={medium} onChange={(e) => setMedium(e.target.value)} aria-invalid={!valid} className={valid ? inputClass : `${inputClass} ${invalidFieldClass}`} />
          </div>
          <div className="text-center">
            <label htmlFor="high-threshold" className="mb-1 flex items-center justify-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-rose-600">
              <span className="size-2 rounded-full bg-rose-500" /> High from
            </label>
            <input id="high-threshold" type="number" min={Number.isFinite(m) && m < 100 ? m + 1 : 2} max={100} value={high} onChange={(e) => setHigh(e.target.value)} aria-invalid={!valid} className={valid ? inputClass : `${inputClass} ${invalidFieldClass}`} />
          </div>
          <div className="text-center">
            <label htmlFor="metadata-cap" className="mb-1 flex items-center justify-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-slate-500">
              Metadata cap
            </label>
            <input id="metadata-cap" type="number" min={1} max={100} value={cap} onChange={(e) => setCap(e.target.value)} aria-invalid={!valid} className={valid ? inputClass : `${inputClass} ${invalidFieldClass}`} />
          </div>
          <button
            type="button"
            disabled={!dirty || !valid || save.isPending}
            onClick={() => save.mutate()}
            className="w-full sm:w-auto inline-flex items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-blue-700 disabled:opacity-50"
          >
            {save.isPending && <Loader2Icon className="size-3.5 animate-spin" />}
            Save thresholds
          </button>
        </div>
      </div>
      {!valid && data && validationErrorMessage && <FieldError message={validationErrorMessage} className="mt-3 text-xs" />}
      {data?.updated_by_name && (
        <p className="mt-3 text-[11px] text-slate-400">
          Last changed by {data.updated_by_name} on {formatWhen(data.updated_at)}.
        </p>
      )}
      <div className="mt-3 space-y-2">
        <StatusLine error={error} ok={ok} />
      </div>
    </section>
  )
}

// ------------------------------------------------------------------- history

function HistoryModal({ rule, onClose }: { rule: RiskRule; onClose: () => void }) {
  const { token } = useAuth()
  const { platformCompanyParam: cid, companyId } = useActingCompany()
  const { data = [], isLoading } = useQuery({
    queryKey: ["riskRuleHistory", rule.rule_id, token, companyId],
    queryFn: () => getRiskRuleHistory(rule.rule_id, token as string, cid),
    enabled: Boolean(token),
  })
  return (
    <Modal
      wide
      title={`Change history — ${rule.rule_id}`}
      description="Every edit creates a new version. Cases already scored keep the version that scored them."
      onClose={onClose}
    >
      {isLoading ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : (
        <table className="w-full border-collapse text-left text-xs">
          <thead>
            <tr className="border-b border-slate-200 text-[10px] font-bold uppercase tracking-wider text-slate-400">
              <th className="py-2 pr-3">Version</th>
              <th className="py-2 pr-3">Weight</th>
              <th className="py-2 pr-3">Severity</th>
              <th className="py-2 pr-3">Active</th>
              <th className="py-2 pr-3">Changed by</th>
              <th className="py-2 pr-3">When</th>
              <th className="py-2">Note</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.map((v, i) => (
              <tr key={v.id} className={i === 0 ? "bg-blue-50/40" : ""}>
                <td className="py-2.5 pr-3 font-mono font-bold">
                  v{v.version}
                  {i === 0 && <span className="ml-1.5 rounded bg-blue-100 px-1 text-[9px] font-bold uppercase text-blue-700">current</span>}
                </td>
                <td className="py-2.5 pr-3 font-mono">{v.weight > 0 ? `+${v.weight}` : v.weight}</td>
                <td className="py-2.5 pr-3 capitalize">{v.severity}</td>
                <td className="py-2.5 pr-3">{v.is_active ? "Yes" : "No"}</td>
                <td className="py-2.5 pr-3">{v.updated_by_name ?? <span className="text-slate-400">Initial seed</span>}</td>
                <td className="whitespace-nowrap py-2.5 pr-3 text-slate-500">{formatWhen(v.effective_from)}</td>
                <td className="py-2.5 text-slate-600">{v.change_note ?? <span className="text-slate-300">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Modal>
  )
}

// ----------------------------------------------------------------- rule row

function RuleRow({ rule, onHistory }: { rule: RiskRule; onHistory: (rule: RiskRule) => void }) {
  const { token } = useAuth()
  const { platformCompanyParam: cid } = useActingCompany()
  const queryClient = useQueryClient()
  const [weight, setWeight] = React.useState(String(rule.weight))
  const [severity, setSeverity] = React.useState<string>(rule.severity)
  const [active, setActive] = React.useState(rule.is_active)
  const [note, setNote] = React.useState("")
  const [error, setError] = React.useState<string | null>(null)
  const [savedVersion, setSavedVersion] = React.useState<number | null>(null)

  // A new version arrived from the server (after a save): reset the draft to it.
  React.useEffect(() => {
    setWeight(String(rule.weight))
    setSeverity(rule.severity)
    setActive(rule.is_active)
    setNote("")
  }, [rule.id, rule.weight, rule.severity, rule.is_active])

  const w = Number(weight)
  const weightValid = weight.trim() !== "" && Number.isFinite(w) && w >= -100 && w <= 100
  const dirty = w !== rule.weight || severity !== rule.severity || active !== rule.is_active

  const save = useMutation({
    mutationFn: () =>
      updateRiskRule(
        rule.rule_id,
        {
          ...(w !== rule.weight ? { weight: w } : {}),
          ...(severity !== rule.severity ? { severity: severity as RuleSeverity } : {}),
          ...(active !== rule.is_active ? { is_active: active } : {}),
          change_note: note.trim() || null,
        },
        token as string,
        cid,
      ),
    onSuccess: (saved) => {
      setError(null)
      setSavedVersion(saved.version)
      void queryClient.invalidateQueries({ queryKey: ["riskRules"] })
      void queryClient.invalidateQueries({ queryKey: ["riskRuleHistory", rule.rule_id] })
    },
    onError: (err) => {
      setSavedVersion(null)
      setError(errorText(err))
    },
  })

  return (
    <tr className={`align-top transition-colors ${dirty ? "bg-amber-50/40" : "hover:bg-blue-50/30"} ${rule.is_active ? "" : "opacity-60"}`}>
      <td className="max-w-md px-5 py-4">
        <div className="font-mono text-[11px] font-bold text-slate-800">{rule.rule_id}</div>
        <div className="mt-1 text-[11px] leading-relaxed text-slate-500">{rule.reason_template}</div>
        <div className="mt-1.5 text-[10px] text-slate-400">
          Check: <span className="font-mono">{rule.check_type}</span> · v{rule.version}
          {rule.updated_by_name && <> · changed by {rule.updated_by_name}</>}
        </div>
        {dirty && (
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Reason for this change (optional)"
            aria-label={`Change note for ${rule.rule_id}`}
            maxLength={1024}
            className="mt-2 w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
          />
        )}
        {error && <p role="alert" className="mt-2 text-[11px] text-rose-600">{error}</p>}
        {savedVersion !== null && !dirty && <p role="status" className="mt-2 text-[11px] text-emerald-600">Saved as version {savedVersion}.</p>}
      </td>
      <td className="px-4 py-4">
        <input
          type="number"
          step="0.5"
          min={-100}
          max={100}
          value={weight}
          onChange={(e) => {
            setSavedVersion(null)
            setWeight(e.target.value)
          }}
          aria-label={`Weight for ${rule.rule_id}`}
          className={`w-20 rounded-lg border bg-white px-2.5 py-1.5 text-center font-mono text-sm font-bold focus:outline-none focus:ring-2 focus:ring-blue-500/20 ${weightValid ? "border-slate-200 text-slate-900 focus:border-blue-500" : "border-rose-400 text-rose-700"}`}
        />
        {!weightValid && <FieldError message="Enter a number from -100 to 100." className="max-w-[9rem]" />}
      </td>
      <td className="px-4 py-4">
        <select
          value={severity}
          onChange={(e) => {
            setSavedVersion(null)
            setSeverity(e.target.value)
          }}
          aria-label={`Severity for ${rule.rule_id}`}
          className={`rounded-full border px-2 py-1 text-[11px] font-bold capitalize ${SEVERITY_STYLES[severity] ?? "border-slate-200"}`}
        >
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
      </td>
      <td className="px-4 py-4 text-center">
        <label className="inline-flex cursor-pointer items-center gap-2 text-[11px] font-semibold text-slate-600">
          <input
            type="checkbox"
            role="switch"
            checked={active}
            onChange={(e) => {
              setSavedVersion(null)
              setActive(e.target.checked)
            }}
            aria-label={`Active: ${rule.rule_id}`}
            className="size-4 cursor-pointer accent-blue-600"
          />
          {active ? "Active" : "Off"}
        </label>
      </td>
      <td className="whitespace-nowrap px-5 py-4 text-right">
        {dirty ? (
          <div className="flex justify-end gap-1.5">
            <button
              type="button"
              disabled={save.isPending}
              onClick={() => {
                setWeight(String(rule.weight))
                setSeverity(rule.severity)
                setActive(rule.is_active)
                setNote("")
                setError(null)
              }}
              className="rounded-lg px-2.5 py-1.5 text-xs font-semibold text-slate-500 hover:bg-slate-100"
            >
              Reset
            </button>
            <button
              type="button"
              disabled={!weightValid || save.isPending}
              onClick={() => save.mutate()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {save.isPending && <Loader2Icon className="size-3 animate-spin" />}
              Save
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => onHistory(rule)}
            className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-semibold text-slate-500 hover:bg-slate-100 hover:text-slate-800"
            title="View every version of this rule"
          >
            <HistoryIcon className="size-3.5" />
            History
          </button>
        )}
      </td>
    </tr>
  )
}

// --------------------------------------------------------------------- page

export function SettingsRiskRulesPage() {
  const { token } = useAuth()
  const { platformCompanyParam: cid, companyId } = useActingCompany()
  const queryClient = useQueryClient()
  const [adding, setAdding] = React.useState(false)
  const [notice, setNotice] = React.useState<string | null>(null)
  const [search, setSearch] = React.useState("")
  const [categoryFilter, setCategoryFilter] = React.useState("")
  const [historyRule, setHistoryRule] = React.useState<RiskRule | null>(null)

  const { data: rules = [], isLoading, isError } = useQuery({
    queryKey: ["riskRules", token, companyId],
    queryFn: () => listRiskRules(token as string, cid),
    enabled: Boolean(token && companyId),
  })

  const grouped = React.useMemo(() => {
    const q = search.trim().toLowerCase()
    const visible = rules.filter((r) => {
      if (categoryFilter && r.category !== categoryFilter) return false
      return !q || r.rule_id.toLowerCase().includes(q) || r.reason_template.toLowerCase().includes(q) || r.check_type.toLowerCase().includes(q)
    })
    const byCategory = new Map<string, RiskRule[]>()
    for (const r of visible) byCategory.set(r.category, [...(byCategory.get(r.category) ?? []), r])
    return [...byCategory.entries()].sort((a, b) => {
      const ai = CATEGORY_ORDER.indexOf(a[0])
      const bi = CATEGORY_ORDER.indexOf(b[0])
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi)
    })
  }, [rules, search, categoryFilter])

  const activeCount = rules.filter((r) => r.is_active).length
  const tunedCount = rules.filter((r) => r.version > 1).length
  const maxAchievable = rules.filter((r) => r.is_active && r.weight > 0).reduce((sum, r) => sum + r.weight, 0)

  return (
    <SettingsShell
      active="risk-rules"
      actions={
        <button
          type="button"
          onClick={() => { setNotice(null); setAdding(true) }}
          className="inline-flex items-center space-x-2 bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold px-4 py-2.5 rounded-lg shadow-sm shadow-blue-500/25 transition-all"
        >
          <PlusIcon className="w-3.5 h-3.5" />
          <span>Add risk rule</span>
        </button>
      }
      description="Every flaggable condition the checks can raise is a rule. Adjust weight, severity or on/off here — no redeploy. Each save creates a new version; cases already scored are never re-scored."
    >
      <ThresholdsCard />

      {notice && (
        <div className="mb-4">
          <StatusLine ok={notice} />
        </div>
      )}

      <div className="mb-6 grid grid-cols-1 gap-3 sm:gap-4 sm:grid-cols-3">
        <div className="flex items-start justify-between rounded-2xl border border-slate-200/80 bg-white p-4 sm:p-5 shadow-2xs">
          <div>
            <div className="text-[10px] sm:text-[11px] font-bold uppercase tracking-wider text-slate-400">Active rules</div>
            <div className="mt-2 text-2xl sm:text-3xl font-extrabold text-slate-900">{activeCount}</div>
            <div className="mt-1 text-xs text-slate-500">of {rules.length} total</div>
          </div>
          <div className="flex size-9 sm:size-10 items-center justify-center rounded-xl bg-blue-50 text-blue-600 shrink-0"><FileCheckIcon className="size-4 sm:size-5" /></div>
        </div>
        <div className="flex items-start justify-between rounded-2xl border border-slate-200/80 bg-white p-4 sm:p-5 shadow-2xs">
          <div>
            <div className="text-[10px] sm:text-[11px] font-bold uppercase tracking-wider text-slate-400">Tuned since seed</div>
            <div className="mt-2 text-2xl sm:text-3xl font-extrabold text-slate-900">{tunedCount}</div>
            <div className="mt-1 text-xs text-slate-500">rules with more than one version</div>
          </div>
          <div className="flex size-9 sm:size-10 items-center justify-center rounded-xl bg-indigo-50 text-indigo-600 shrink-0"><SlidersIcon className="size-4 sm:size-5" /></div>
        </div>
        <div className="flex items-start justify-between rounded-2xl border border-slate-200/80 bg-white p-4 sm:p-5 shadow-2xs">
          <div>
            <div className="text-[10px] sm:text-[11px] font-bold uppercase tracking-wider text-slate-400">Combined weight</div>
            <div className="mt-2 text-2xl sm:text-3xl font-extrabold text-slate-900">{maxAchievable}</div>
            <div className="mt-1 text-xs text-slate-500">if every active rule fired (score caps at 100)</div>
          </div>
          <div className="flex size-9 sm:size-10 items-center justify-center rounded-xl bg-amber-50 text-amber-600 shrink-0"><ZapIcon className="size-4 sm:size-5" /></div>
        </div>
      </div>

      <div className="mb-4 flex flex-col items-stretch sm:items-center justify-between gap-3 rounded-2xl border border-slate-200/80 bg-white p-3.5 sm:p-4 shadow-2xs md:flex-row">
        <div className="relative w-full md:w-80">
          <SearchIcon className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search risk rules"
            placeholder="Search rule ID, wording or check…"
            className="w-full rounded-xl border border-slate-200 bg-slate-50/50 py-2 pl-9 pr-4 text-xs text-slate-800 placeholder-slate-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
          />
        </div>
        <div className="flex items-center gap-2.5">
          <select
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value)}
            aria-label="Filter by category"
            className="flex-1 sm:flex-none rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700"
          >
            <option value="">Category: all</option>
            {CATEGORY_ORDER.map((c) => (
              <option key={c} value={c}>{CATEGORY_LABELS[c]}</option>
            ))}
          </select>
          <button type="button" onClick={() => { setSearch(""); setCategoryFilter("") }} className="px-2 py-1.5 text-xs font-semibold text-slate-500 hover:text-slate-800">
            Reset
          </button>
        </div>
      </div>

      {isLoading ? (
        <p className="py-12 text-center text-sm text-slate-400">Loading risk rules…</p>
      ) : isError ? (
        <p className="py-12 text-center text-sm text-rose-600">Couldn't load the risk rules.</p>
      ) : grouped.length === 0 ? (
        <p className="py-12 text-center text-sm text-slate-400">No rules match the current filters.</p>
      ) : (
        <div className="space-y-6">
          {grouped.map(([category, list]) => (
            <section key={category} aria-label={`${CATEGORY_LABELS[category] ?? category} rules`} className="overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-2xs">
              <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50/60 px-4 sm:px-5 py-3">
                <h2 className="flex items-center gap-2 text-sm font-bold text-slate-800">
                  <ShieldAlertIcon className="size-4 text-slate-400" />
                  {CATEGORY_LABELS[category] ?? category}
                </h2>
                <span className="text-xs text-slate-400">
                  {list.filter((r) => r.is_active).length} active · {list.length} rule{list.length === 1 ? "" : "s"}
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-left text-xs min-w-[620px]">
                  <thead>
                    <tr className="border-b border-slate-100 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                      <th className="px-5 py-2.5">Rule &amp; reason shown to reviewers</th>
                      <th className="px-4 py-2.5">Weight</th>
                      <th className="px-4 py-2.5">Severity</th>
                      <th className="px-4 py-2.5 text-center">Status</th>
                      <th className="px-5 py-2.5 text-right"> </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {list.map((rule) => (
                      <RuleRow key={rule.rule_id} rule={rule} onHistory={setHistoryRule} />
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ))}
        </div>
      )}

      {adding && (
        <AddRuleModal
          onClose={() => setAdding(false)}
          onCreated={(created) => {
            setAdding(false)
            setNotice(`Added ${created.rule_id} (version 1). It applies to cases scored from now on; cases already scored are unchanged.`)
            void queryClient.invalidateQueries({ queryKey: ["riskRules"] })
          }}
        />
      )}

      {historyRule && <HistoryModal rule={historyRule} onClose={() => setHistoryRule(null)} />}
    </SettingsShell>
  )
}
